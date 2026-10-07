"""Detection: what is this machine doing, right now, and may we cook?

omlx has no queue API (`/metrics`, `/health`, `/admin/api/queue` all 404 — see
DISCOVERY.md), so the only honest way to know whether inference is happening is
to look at the sockets, the HID idle clock, and the files other tools write.
All of it is passive: we never ask the inference server anything, because
asking is itself load.

Measured cost per probe on this box (2026-10-06, 28-core Studio), which is why
the cadences are not all the same and why every one of them runs in a thread:

    lsof -nP -Fpcn -iTCP:8000      190ms wall / 182ms cpu   (one call, both
                                 states: LISTEN rows and ESTABLISHED rows together)
    lsof … -iTCP:4096 -sTCP:EST   201ms wall / 191ms cpu
    ioreg -rc IOHIDSystem           71ms wall /  66ms cpu     (4.7KB of ASCII)
    ps -Axo pid,%cpu,comm         101ms wall /  94ms cpu
    depth-2 stat over ~/dev         4ms wall, in-process

`lsof` spends its time in the kernel walking the proc table, so at 1 Hz it
costs roughly 18% of one core — nothing on a Studio whose defining property at
that moment is that it is idle, but not free, so we pay it once per tick for
both states and never on the event loop.

The trap this module exists to avoid: **a port is not a service**. `lsof :8000`
returns two LISTEN rows here — omlx on `*:8000` and a stray `http.server` on
`127.0.0.1:8000`, which wins because a specific bind beats a wildcard one. So
traffic to 127.0.0.1:8000 is *not* inference, and counting it as inference
would put the kitchen in a fake preemption. `classify()` drops those peers by
address, after naming the processes by argv.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess  # fixed argv only: no shell, no interpolation
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cooker import db
from cooker.config import Config
from cooker.ledger import DEAD_LEDGER, LedgerProbe, LedgerView
from cooker.wire import DEAD, Wire, WireProbe

IDLE = "IDLE"
ACTIVE_USER = "ACTIVE_USER"
ACTIVE_INFER = "ACTIVE_INFER"

# `ioreg -rc IOHIDSystem` prints `"HIDIdleTime" = 37462416` — nanoseconds since
# the last keyboard/mouse event, confirmed by sampling 3s apart. We take the MIN
# across instances: several HID objects exist, and the one that was touched most
# recently is the one that tells us the user is here.
_HID_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')

_LOOPBACK = ("127.0.0.1", "localhost", "::1", "::")


def _age_then(ts: float | None, now: float) -> float | None:
    """Seconds since `ts`, clamped at zero.

    A wall clock that stepped backwards (NTP, or a Mac waking) would otherwise
    make a cooldown last forever — `(cooldown_end - now)` going negative is the
    same class of bug that once printed `next_runnable_in_s: -1791353871.1`.
    A timestamp in the future is not "a long time from now", it is bad data.
    """
    if ts is None:
        return None
    return max(0.0, now - ts)


def _split(addr: str) -> tuple[str, int | None]:
    """`'100.122.197.81:8000'` -> `('100.122.197.81', 8000)`; `'*:8000'` ->
    `('*', 8000)`; a bare `'[::1]:8000'` keeps its brackets. Anything without a
    numeric port returns None, which callers treat as 'not our port'."""
    host, sep, port = addr.rpartition(":")
    if not sep:
        return addr, None
    try:
        return host, int(port)
    except ValueError:
        return host, None


# --- socket parsing -------------------------------------------------------


@dataclass(frozen=True)
class Conn:
    """One `n` row from `lsof -Fpcn`. LISTEN rows have remote == ''."""

    pid: int
    command: str
    local: str
    remote: str


@dataclass(frozen=True)
class Peer:
    """A process with an open connection to omlx.

    `rate_bps` is what makes this worth carrying. A socket is a relationship and
    lasts for hours; bytes are an event and happen when someone is generating.
    Bench #7 (DISCOVERY.md) put the two side by side: an established OpenCode or
    Traefik connection holds *exactly* zero bytes per second for as long as you
    watch it, while a real streamed reply moves kilobytes in one second. So
    `connected` is not `cooking`, and a detector that confuses them never cooks.
    """

    pid: int
    command: str
    rate_bps: float = 0.0

    def __str__(self) -> str:
        return f"{self.command}({self.pid})"


@dataclass(frozen=True)
class SockView:
    """What :port looked like on our last look: who serves it, who shadows it,
    and who is connected — with the throughput that says whether 'connected'
    currently means 'generating'."""

    server_pids: tuple[int, ...] = ()
    shadow_addrs: tuple[str, ...] = ()
    clients: tuple[Peer, ...] = ()
    listening: bool = False

    @property
    def omlx_up(self) -> bool:
        return bool(self.server_pids)

    def generating(self, floor_bps: float) -> tuple[Peer, ...]:
        """Peers moving bytes. Empty means the connections are keepalives."""
        return tuple(c for c in self.clients if c.rate_bps >= floor_bps)


def peer_summary(peers: Iterable[Peer], limit: int = 3) -> str:
    """Peers the way a HUD can actually show them: `opencode x2, OrbStack Helper`.

    Truncating `opencode(23788)` to `opencode(2378` is not brevity, it is a
    typo that sends you looking for a process that does not exist. Grouping by
    command name is shorter *and* keeps every name intact; the exact pids live in
    the events table, which is where you look when you mean to kill something.
    """
    counts: dict[str, int] = {}
    for peer in peers:
        counts[peer.command] = counts.get(peer.command, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    bits = [f"{name} x{n}" if n > 1 else name for name, n in ranked[:limit]]
    extra = len(ranked) - len(bits)
    if extra > 0:
        bits.append(f"+{extra} more")
    return ", ".join(bits) or "unknown"


def parse_conns(out: str) -> list[Conn]:
    """Parse `lsof -nP -Fpcn` field output. `f` (descriptor) lines are skipped;
    the command is remembered across a process block."""
    conns: list[Conn] = []
    pid, cmd = 0, ""
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid, cmd = int(val or 0), ""
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid:
            local, _, remote = val.partition("->")
            conns.append(Conn(pid, cmd, local.strip(), remote.strip()))
    return conns


def classify(
    conns: list[Conn],
    *,
    port: int,
    server_names: tuple[str, ...] = ("omlx", "omlx-server"),
    our_pid: int | None = None,
    argv_of: Callable[[int], str] = lambda _pid: "",
) -> SockView:
    """Split what we saw on :port into the server, its shadows, and its clients.

    Both the LISTEN rows and the ESTABLISHED rows come from the same lsof call,
    which is the whole point: one 190ms probe, not two.

    A LISTEN row by a process whose argv does not mention omlx is a **shadow**:
    it answers on an address where we would otherwise reach omlx, and anything
    connected to it is not inference. Naming is by argv (`argv_of`), because
    lsof's command column truncates both omlx and a stray http.server to
    'Python' — the bug this exact code path fixed in M1.
    """
    listeners = [c for c in conns if not c.remote and _split(c.local)[1] == port]
    server = tuple(
        sorted(
            {
                c.pid
                for c in listeners
                if any(n in (argv_of(c.pid) or "").lower() for n in server_names)
            }
        )
    )
    shadow = tuple(
        sorted(
            {
                _split(c.local)[0]
                for c in listeners
                if server and c.pid not in server
            }
        )
    )

    clients: list[Peer] = []
    seen: set[int] = set()
    for c in conns:
        if not c.remote or c.pid in seen or c.pid in server or c.pid == our_pid:
            continue
        rhost, rport = _split(c.remote)
        # The client side of an omlx connection dials :port. If it dialled an
        # address a shadow owns, the bytes never reached omlx.
        if rport != port or (rhost in shadow and rhost in _LOOPBACK):
            continue
        seen.add(c.pid)
        clients.append(Peer(c.pid, c.command or "?"))
    return SockView(server, shadow, tuple(clients), bool(listeners))


# --- signals ------------------------------------------------------------


@dataclass
class Signals:
    ts: float = 0.0
    socks: SockView | None = None
    hid_idle_s: float | None = None
    oc_wal_age_s: float | None = None
    oc_clients: tuple[Peer, ...] = ()
    fs_age_s: float | None = None
    side_age_s: float | None = None
    cpu: tuple[tuple[str, float], ...] = ()
    # The wire's own verdict. `wire.up` False means nettop is not running or
    # has gone stale, and then `clients` cannot be read as generating OR as idle:
    # the detector has to stop and say so instead of picking the hopeful one.
    wire: Wire = DEAD
    # The server's own account (§20). `ledger.up` False means we could not ask,
    # and then bytes are the judge again — the fallback, not the assumption.
    ledger: LedgerView = DEAD_LEDGER
    cost_ms: dict[str, float] = field(default_factory=dict)
    failed: tuple[str, ...] = ()

    @property
    def omlx_up(self) -> bool:
        return bool(self.socks and self.socks.omlx_up)

    @property
    def wire_up(self) -> bool:
        return self.wire.up


class Sampler(Protocol):
    async def sample(self, prev: Signals | None) -> Signals: ...


def _run(argv: list[str], timeout: float) -> str:
    """stdout, or raise.

    Raising matters: a swallowed failure returns '' which classifies as 'nothing
    listening' — the one wrong answer that lets a background prefill stomp an
    interactive session. Callers that want best-effort catch it themselves.
    (`lsof` exits 1 when nothing matches, so returncode is deliberately not
    checked: empty output is a real answer, a timeout is not.)
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{argv[0]} failed: {type(exc).__name__}") from exc


# --- the live sampler ---------------------------------------------------


class LiveSampler:
    """Six signals, six cadences. Each one is cheap or infrequent; none of them
    run on the event loop, and a failure never blocks the others.

    A signal that fails returns the *previous* value rather than None, and is
    named in `failed`. That is deliberate: a flaky `lsof` must not silently read
    as "nobody is generating", which is the one mistake that lets a background
    prefill stomp an interactive session. The detector turns `failed` into a
    blocker (be polite while blind), and the value goes stale but never falsely
    clears.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        clock: Callable[[], float] = time.time,
        our_pid: int | None = None,
        own_busy: Callable[[], int] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.our_pid = our_pid if our_pid is not None else os.getpid()
        self.port = int(cfg.get("detect.omlx_port", 8000))
        self.names = tuple(cfg.get("detect.omlx_proc_names", ["omlx", "omlx-server"]))
        self.oc_port = int(cfg.get("detect.opencode_port", 4096))
        self.wal = Path(str(cfg.get("detect.opencode_wal",
                                     "~/.local/share/opencode/opencode.db-wal")))
        self.stats = Path(str(cfg.get("detect.llm_stats_file", "~/.omlx/stats.json")))
        self.fs_roots = [Path(str(p)) for p in cfg.get("detect.fs_roots", ["~/dev", "~/homelab"])]
        self.load_names = tuple(cfg.get("detect.load_proc_names", ["omlx", "opencode"]))
        self.floor_bps = float(cfg.get("detect.infer_min_bps", 256))
        # The ledger probe: one small GET per interval against the server's own
        # admin API (§20). `own_busy` is our in-flight count, subtracted so we
        # do not preempt ourselves reading our own completion.
        self.ledger = LedgerProbe(cfg, clock=clock, own_busy=own_busy)
        self.ledger_enabled = bool(cfg.get("detect.ledger.enabled", True))
        # Resident, not per-tick: nettop counts cumulatively, so a fresh process
        # each second has no earlier sample to diff and would answer "0 bytes"
        # forever. One long-lived reader, and `sample` just reads its dict.
        # infer_ports: bytes count only across the inference endpoint, so a
        # 70 MB/s model download over WAN cannot read as someone generating
        # (§19 — the rollup that day said 23 MB/s while the port served nothing).
        self.wire = WireProbe(
            interval_s=float(cfg.get("detect.wire_interval_seconds", 1.0)),
            window_s=float(cfg.get("detect.wire_window_seconds", 3.0)),
            clock=clock,
            infer_ports=(self.port,),
        )
        cad = cfg.get("detect.cadence_seconds", {}) or {}
        tick = float(cfg.get("detect.interval_seconds", 1.0))
        self.cadence: dict[str, float] = {
            "sockets": float(cad.get("sockets", tick)),
            "hid": float(cad.get("hid", 2.0)),
            "wal": float(cad.get("wal", 2.0)),
            "oc_socks": float(cad.get("oc_socks", 5.0)),
            "fs": float(cad.get("fs", 5.0)),
            "side": float(cad.get("side", 5.0)),
            "load": float(cad.get("load", 5.0)),
            # Reading the probe is free: the cost is paid once by the resident
            # nettop, not per read, so it rides the tick by default.
            "wire": float(cad.get("wire", tick)),
        }
        # The ledger poll is a real network call, so it rides its own measured
        # cadence, not the generic tick — and when it is switched off the lane
        # disappears rather than returning dead: no lane, no `ledger` key in
        # `vals`, and `detect` falls back to bytes as if it were blind, which
        # is the same safety posture and the one the config already describes.
        if self.ledger_enabled:
            self.cadence["ledger"] = float(
                cad.get("ledger", self.ledger.interval))
        self.lsof_timeout = float(cfg.get("detect.lsof_command_timeout_seconds", 2.0))
        self.hid_timeout = float(cfg.get("detect.hid_idle_command_timeout_seconds", 2.0))
        self._last_run: dict[str, float] = {}
        self._argv_cache: dict[int, tuple[float, str]] = {}
        self.vals: dict[str, Any] = {}
        self.cost_ms: dict[str, float] = {}
        self.prev = Signals()
        # name -> probe. The name is the cadence key, so adding a signal means
        # adding one entry here and one in config; nothing else changes.
        self.fns: dict[str, Callable[[], Any]] = {
            "sockets": self._sockets,
            "hid": self._hid,
            "wal": self._wal_age,
            "oc_socks": self._oc_socks,
            "fs": self._fs_age,
            "side": self._side_age,
            "load": self._load,
            # In-process dict read, so it can run every tick for free.
            "wire": self.wire.snapshot,
        }
        if self.ledger_enabled:
            self.fns["ledger"] = self._ledger_poll

    def start(self) -> bool:
        """Bring up the reader. The daemon calls this, not `sample`: a probe
        spawned per tick has no baseline to diff against its own last sample."""
        return self.wire.start()

    def stop(self) -> None:
        self.wire.stop()
        if self.ledger_enabled:
            self.ledger.aclose()

    def _due(self, name: str, now: float) -> bool:
        return now - self._last_run.get(name, float("-inf")) >= self.cadence[name]

    def _argv(self, pid: int) -> str:
        """argv, cached 30s. It changes only when a process restarts, and
        `ps -p` costs ~10ms, so re-reading it every second is waste we feel.
        Best-effort on purpose: one vanished pid must not blind the whole tick."""
        now = self.clock()
        hit = self._argv_cache.get(pid)
        if hit and now - hit[0] < 30.0:
            return hit[1]
        try:
            args = _run(["ps", "-p", str(pid), "-o", "args="], 2.0)
        except RuntimeError:
            args = hit[1] if hit else ""
        self._argv_cache[pid] = (now, args)
        return args

    # --- each signal, in its own thread ---

    def _sockets(self) -> SockView:
        out = _run(["lsof", "-nP", "-Fpcn", f"-iTCP:{self.port}"], self.lsof_timeout)
        return classify(
            parse_conns(out), port=self.port, server_names=self.names,
            our_pid=self.our_pid, argv_of=self._argv,
        )

    def _hid(self) -> float | None:
        out = _run(["ioreg", "-rc", "IOHIDSystem"], self.hid_timeout)
        vals = [int(m) for m in _HID_RE.findall(out)]
        return min(vals) / 1e9 if vals else None

    def _wal_age(self) -> float | None:
        try:
            return max(0.0, self.clock() - self.wal.stat().st_mtime)
        except OSError:
            return None

    def _oc_socks(self) -> tuple[Peer, ...]:
        out = _run(
            ["lsof", "-nP", "-Fpcn", f"-iTCP:{self.oc_port}", "-sTCP:ESTABLISHED"],
            self.lsof_timeout,
        )
        return tuple(
            c for c in classify(parse_conns(out), port=self.oc_port,
                                 server_names=(), our_pid=self.our_pid,
                                 argv_of=self._argv).clients
        )

    def _fs_age(self) -> float | None:
        """Newest mtime over the work trees, without walking them.

        Directory mtimes move when an entry is created or removed, so a depth-2
        directory scan plus each repo's `.git/index` catches edits where people
        actually work (a file saved in `proj/src/` moves `proj/src`) for 4ms and
        no subprocess. Deeper trees are a known blind spot; the HID and socket
        signals cover them, because someone editing deep is someone typing.
        """
        newest: float | None = None
        for root in self.fs_roots:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for child in entries:
                if not child.is_dir():
                    continue
                for cand in (child, child / ".git" / "index"):
                    try:
                        m = cand.stat().st_mtime
                        newest = m if newest is None or m > newest else newest
                    except OSError:
                        pass
                try:
                    for gc in child.iterdir():
                        if gc.is_dir():
                            try:
                                m = gc.stat().st_mtime
                                newest = m if newest is None or m > newest else newest
                            except OSError:
                                pass
                except OSError:
                    pass
        return _age_then(newest, self.clock())

    def _side_age(self) -> float | None:
        return _age_then(
            self.stats.stat().st_mtime if self.stats.exists() else None, self.clock()
        )

    def _load(self) -> tuple[tuple[str, float], ...]:
        out = _run(["ps", "-Axo", "pid,%cpu,comm"], 2.0)
        sums: dict[str, float] = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            try:
                cpu = float(parts[1])
            except ValueError:
                continue
            for name in self.load_names:
                if name in parts[2].lower():
                    sums[name] = sums.get(name, 0.0) + cpu
        return tuple(sorted(sums.items(), key=lambda kv: -kv[1]))

    def _ledger_poll(self) -> LedgerView:
        """Poll the server and hand back what it says, freshness-flagged.

        The poll never raises (see ledger.py); the freshness flag is applied on
        the way out so a stale last-good read cannot be mistaken for news just
        because it came back from the probe without an exception."""
        self.ledger.poll()
        return self.ledger.view()

    async def sample(self, prev: Signals | None = None) -> Signals:
        """Refresh whatever is due, concurrently, off the event loop.

        Signals that are not due keep their previous value — a stale HID reading
        is still the best reading we have of the last 2 seconds. Signals that
        *failed* are reported in `failed` so the detector can stop instead of
        guessing.
        """
        now = self.clock()
        due = [name for name in self.cadence if self._due(name, now)]

        async def timed(name: str, fn: Callable[[], Any]) -> Any:
            out = await asyncio.to_thread(fn)
            self.cost_ms[name] = round((self.clock() - now) * 1000, 1)
            return out

        jobs = {name: timed(name, self.fns[name]) for name in due}
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)

        failed: list[str] = []
        for name, res in zip(names, results, strict=True):
            self._last_run[name] = now
            if isinstance(res, BaseException):
                failed.append(name)
            else:
                self.vals[name] = res

        socks = self.vals.get("sockets")
        wire: Wire | None = self.vals.get("wire")
        if socks is not None and wire is not None:
            # Stamp throughput onto the peers lsof named. The two probes answer
            # different questions and are joined here, not inside classify(), so
            # that classify stays a pure function of what the sockets said.
            tagged = tuple(
                Peer(c.pid, c.command, wire.rate(c.pid) if wire.up else 0.0)
                for c in socks.clients
            )
            if tagged != socks.clients:
                socks = SockView(server_pids=socks.server_pids,
                                 shadow_addrs=socks.shadow_addrs,
                                 clients=tagged, listening=socks.listening)

        sig = Signals(
            ts=now,
            socks=socks,
            wire=wire if wire is not None else DEAD,
            # Read fresh every tick regardless of when the poll last ran: the
            # staleness flag is a function of the clock, and `view()` is free.
            ledger=self.ledger.view() if self.ledger_enabled else DEAD_LEDGER,
            hid_idle_s=self.vals.get("hid"),
            oc_wal_age_s=self.vals.get("wal"),
            oc_clients=self.vals.get("oc_socks", ()),
            fs_age_s=self.vals.get("fs"),
            side_age_s=self.vals.get("side"),
            cpu=self.vals.get("load", ()),
            cost_ms={k: round(v, 1) for k, v in self.cost_ms.items()},
            failed=tuple(failed),
        )
        self.prev = sig
        return sig


# --- the state machine --------------------------------------------------


@dataclass(frozen=True)
class BusState:
    state: str
    reason: str
    blockers: tuple[str, ...]
    ready: bool
    ready_in_s: float
    ts: float
    hid_idle_s: float | None = 0.0
    clients: tuple[str, ...] = ()
    # `clients` holds a socket; `cooking` moves bytes. The UI has to show the
    # difference, because "connected" and "generating" are different answers to
    # "why did the kitchen stop", and only one of them is a lie waiting to happen.
    wire_up: bool = False
    cooking: tuple[str, ...] = ()
    # `ready` means one bounded stage may start; `ramped` means the concurrency
    # ladder may climb past one. They were the same flag until the gap distribution
    # was measured (DISCOVERY §16), because a four-second pause is enough for a
    # `plan` stage and nowhere near enough for three workers, and a single boolean
    # has to answer both by being wrong about one of them.
    ramped: bool = False
    # How long the wire has been known quiet, for the UI and for stage sizing.
    quiet_s: float | None = None
    # Why concurrency is still one when `ready` is true. Kept separate from
    # `blockers` because a blocker that appears while the kitchen is already
    # cooking is a contradiction the UI cannot render honestly.
    ramp_note: str = ""
    # The median of recently completed quiet streaks, copied out of the detector at
    # each snapshot. Carried on the state rather than read off the detector by
    # callers because the UI should be able to explain "why only one pot" from the
    # payload it already has, and stage sizing needs the number at claim time, when
    # the detector object is not in scope in the runner.
    typical_gap_s: float | None = None
    # The server's own ledger, surfaced verbatim (§20): `ledger_up` False means
    # the detector fell back to bytes, and the UI has to be able to show that
    # difference — "the box is idle" and "we had to guess from bytes" are
    # different sentences.
    ledger_up: bool = False
    ledger_idle_s: float | None = None
    ledger_active: int = 0
    ledger_own: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "blockers": list(self.blockers),
            "ready": self.ready,
            "ready_in_s": round(self.ready_in_s, 1),
            "hid_idle_s": round(self.hid_idle_s, 1) if self.hid_idle_s else None,
            "clients": list(self.clients),
            "wire_up": self.wire_up,
            "cooking": list(self.cooking),
            "ramped": self.ramped,
            "quiet_s": round(self.quiet_s, 1) if self.quiet_s is not None else None,
            "ramp_note": self.ramp_note,
            "typical_gap_s": (round(self.typical_gap_s, 1)
                              if self.typical_gap_s is not None else None),
            "ledger_up": self.ledger_up,
            "ledger_idle_s": (round(self.ledger_idle_s, 1)
                              if self.ledger_idle_s is not None else None),
            "ledger_active": self.ledger_active,
            "ledger_own": self.ledger_own,
        }


class Detector:
    """Signals -> three states, with hysteresis, and a list of reasons.

    `blockers` is the interesting output. The UI is required to explain why it
    is standing still rather than just standing still, so every reason the
    scheduler must not start work is named, with the seconds left on it.
    """

    def __init__(
        self,
        cfg: Config,
        conn: Any = None,
        *,
        sampler: Sampler | None = None,
        clock: Callable[[], float] = time.time,
        own_busy: Callable[[], int] | None = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.sampler = sampler or LiveSampler(cfg, clock=clock, own_busy=own_busy)
        self.clock = clock
        self.tick_seconds = float(cfg.get("detect.interval_seconds", 1.0))
        self.user_idle = float(cfg.get("detect.user_idle_seconds", 120))
        self.wal_seconds = float(cfg.get("detect.opencode_wal_seconds", 30))
        self.fs_seconds = float(cfg.get("detect.fs_activity_seconds", 60))
        self.cooldown = float(cfg.get("detect.interactive_cooldown_seconds", 60))
        self.idle_confirm = float(cfg.get("detect.idle_confirm_seconds", 60))
        # How long the wire must be *known* quiet before one bounded stage may
        # start. 4s is measured, not chosen: the gap distribution during a live
        # agent session is 2-5s with a median of 2.2s (DISCOVERY §16), so 4s is
        # already past the typical next burst and selects for "this session has
        # actually paused". It also has to clear the meter's own latency - the
        # wire window is 3s and the poll 2s - so anything under ~4s is a number
        # the detector could not have observed.
        self.claim_quiet = float(cfg.get("detect.claim_quiet_seconds", 4.0))
        self.floor_bps = float(cfg.get("detect.infer_min_bps", 256))
        # Amended 2026-10-07 by the owner: "it should run when I'm using the
        # computer, just not when I'm hitting omlx via anything." Presence
        # evidence (HID, WAL, fs, :4096 sockets) still gets *read* — the UI
        # shows the kitchen lit while hands are on the keys, because that is
        # the truth of the new contract — but only omlx traffic and blindness
        # to omlx traffic block. Code default is the old conservative regime;
        # config.yaml carries the owner's amendment. The collision this now
        # tolerates is measured, not hoped: +95ms median TTFT, +350ms on a
        # prefill race (§16), inside the 750ms budget that was always the bar.
        self.presence_blocks = bool(cfg.get("detect.presence_blocks", True))
        # How long after a completion the ledger still calls the model "recently
        # active" when unexplained bytes are on the wire: a poll interval's
        # worth of benefit of the doubt for the request the counters missed
        # (bench15: a 0.26s completion is invisible to `active` but resets
        # `idle_seconds`). Monitors never reset that clock, so this window is
        # inference memory, not poll memory.
        self.infer_margin = float(cfg.get("detect.ledger.infer_margin_seconds", 5.0))
        self.state = IDLE
        self.reason = "startup"
        self.signals = Signals()
        self.last_infer_end: float | None = None
        self.quiet_since: float | None = None
        # Every completed quiet streak, newest last. This is the burst structure of
        # the machine: the thing `idle_confirm` was guessing at with one number.
        # Bounded because a daemon that remembers every pause of a five-year-old
        # session is a daemon whose status line is a histogram nobody reads.
        self.quiet_streaks: deque[float] = deque(maxlen=64)
        self.last_event: dict[str, Any] = {}

    @property
    def typical_gap_s(self) -> float | None:
        """Median length of completed quiet streaks.

        The honest ceiling on how long a stage may run is not a policy number, it is
        however long this machine tends to stay quiet - and that is a fact about a
        particular Tuesday afternoon, not a constant. Median rather than mean because
        one lucky two-minute pause overnight should not make the daytime
        optimistic. Lower middle on an even count, deliberately: err toward a stage
        that finishes early rather than one that gets preempted and pays for its
        prompt twice."""
        if not self.quiet_streaks:
            return None
        vals = sorted(self.quiet_streaks)
        return vals[(len(vals) - 1) // 2]

    # --- classification ---

    def _user_evidence(self, sig: Signals) -> list[str]:
        ev: list[str] = []
        if sig.hid_idle_s is not None and sig.hid_idle_s < self.user_idle:
            ev.append(f"hid {sig.hid_idle_s:.0f}s")
        if sig.oc_wal_age_s is not None and sig.oc_wal_age_s < self.wal_seconds:
            ev.append(f"opencode.db-wal {sig.oc_wal_age_s:.0f}s")
            # A connected :4096 client counts only while the WAL is also fresh.
            # Landmine #16 says sockets are not inference on :8000, and today it
            # learned the same about :4096: a resident subscriber — a dashboard
            # poller, a parked TUI, this daemon's own sibling — holds one open
            # for hours while nobody types, and unconditonal socket evidence
            # made ACTIVE_USER permanent no matter how quiet the box was. The
            # WAL is the event; the socket is only the relationship that lets
            # it happen.
            if sig.oc_clients:
                ev.append(f"opencode socket {', '.join(str(c) for c in sig.oc_clients)}")
        if sig.fs_age_s is not None and sig.fs_age_s < self.fs_seconds:
            ev.append(f"fs {sig.fs_age_s:.0f}s")
        return ev

    def _classify(self, sig: Signals) -> tuple[str, str]:
        """Strictest wins: inference > user > idle — where "user" since the
        owner's amendment (§20b) only means *blind*: not seeing the GPU is a
        reason to stop, but typing, compiling and browsing are not. Presence
        evidence survives as annotation on the quiet verdict.

        Two judges, in a deliberate order (§20). When the ledger is *up*, the
        server answers for itself: inference is happening iff it says somebody
        else is active or queued, or bytes moved while its own `idle_seconds`
        says the model was busy moments ago (the short request the counters
        miss). Bytes that the ledger does not explain as a generation are
        monitors — metronomes at any cadence — and monitors do not close the
        gate. When the ledger is *blind* the old judge runs, unchanged and
        safety-first: bytes are the trigger again, because unexplained bytes
        while deaf are exactly what we must not cook over.
        """
        failed = set(sig.failed)
        if not sig.omlx_up and "sockets" not in failed:
            # Nothing listening: not busy, but there is also nothing to cook for.
            return IDLE, f"nothing listening on :{int(self.cfg.get('detect.omlx_port', 8000))}"
        if "sockets" in failed:
            # Blind is not the same as quiet. Stay stopped and say so.
            return ACTIVE_USER, "blind: lsof failed"
        clients = sig.socks.clients if sig.socks else ()
        servers = sig.socks.server_pids if sig.socks else ()
        led = sig.ledger
        note = ""
        if led.up:
            if led.others_busy:
                return ACTIVE_INFER, (
                    f"omlx serving {led.active} request(s) (ledger"
                    + (f", {led.waiting} queued" if led.waiting else "") + ")")
            talking = sig.wire.up and sig.wire.server_talking(servers, self.floor_bps)
            if talking and led.idle_s is not None and led.idle_s < self.infer_margin:
                if led.own > 0:
                    # Our own stream explains the bytes and the server names
                    # nobody else: keep cooking. A sub-second interactive burst
                    # hiding in here is the measured +95ms we already budgeted
                    # (§16); the queue check and active-minus-own catch
                    # everything longer, one poll later.
                    note = f"cooking: ours, {sig.wire.served_bps(servers):,.0f} B/s"
                else:
                    # Bytes on the wire the ledger shows no *current* request for,
                    # inside the window in which a finished request would still have
                    # reset the server's idle clock: a short generation the live
                    # counters missed. Trust the reset; a monitor never moves it.
                    return ACTIVE_INFER, f"model active {led.idle_s:.1f}s ago (ledger)"
            if talking:
                # Bytes the ledger refuses to call a generation. Not busy — but
                # the reason has to say what those bytes were, or the next
                # reader of the log re-litigates §17 from scratch.
                note = (f"monitor traffic: {sig.wire.served_bps(servers):,.0f} B/s,"
                      " ledger idle")
            elif led.idle_s is not None:
                note = f"quiet (server idle {led.idle_s:.0f}s)"
        else:
            if sig.wire.server_talking(servers, self.floor_bps):
                # The server itself is answering: the fallback judge while the
                # ledger is blind, unchanged from §16 and safety-first: bytes
                # leaving omlx, unexplained, are a generation until proven not.
                # Measured: 0 B/s idle against 10,752 B in a 2s window generating.
                return ACTIVE_INFER, f"omlx serving {sig.wire.served_bps(servers):,.0f} B/s"
            if clients:
                # Someone is connected and moving nothing. Two readings, and they are
                # not the same: if we can see the wire it is a keepalive, so the
                # kitchen may light; if we cannot see the wire we simply do not know,
                # and not-knowing stops new work without killing work in flight.
                if not sig.wire.up:
                    return ACTIVE_USER, "blind: no throughput data, cannot tell 0 from unknown"
                return IDLE, f"keepalive: {peer_summary(clients)}"
        ev = self._user_evidence(sig)
        if ev and self.presence_blocks:
            return ACTIVE_USER, " · ".join(ev)
        if ev:
            # The owner's amendment (§20b): hands on the keyboard are not a
            # blocker, they are context. Say who is home without stopping the
            # burn — a UI that shows "quiet" while the human types would be a
            # cleaner lie, and the ledger already stopped us making that one.
            note = (note + " · " if note else "") + f"human present: {', '.join(ev[:2])}"
        return IDLE, note or "quiet"

    # --- the tick ---

    async def tick(self) -> BusState:
        now = self.clock()
        self.signals = await self.sampler.sample(self.signals)
        state, reason = self._classify(self.signals)

        # "quiet" is the only clock that matters for ramping up, so it resets on
        # any non-IDLE tick rather than enumerating the busy states.
        if state == IDLE:
            if self.quiet_since is None:
                self.quiet_since = now
        else:
            if self.quiet_since is not None:
                # The streak is retired here, at the moment it ends, because after
                # this tick we no longer know when it started.
                self.quiet_streaks.append(now - self.quiet_since)
            self.quiet_since = None

        if self.state == ACTIVE_INFER and state != ACTIVE_INFER:
            self.last_infer_end = now
        if state != self.state:
            self._event(
                "bus.state",
                f"{self.state} -> {state}: {reason}",
                severity="warn" if state == ACTIVE_INFER else "info",
                data={"from": self.state, "to": state, "reason": reason},
            )
        self.state, self.reason = state, reason
        return self.snapshot()

    def snapshot(self) -> BusState:
        """Two gates, because there are two questions.

        `ready` asks whether *one bounded stage* may go on the GPU. The measured
        answer is about four quiet seconds (DISCOVERY §16): the gap distribution
        during a live agent session is 2-5s, so four seconds is already past the
        typical next burst, and the meter could not have observed anything shorter
        anyway with a 3s window polled every 2s. `ramped` asks whether *more than
        one* pot may burn, and that one genuinely does want the minute, because
        concurrency shares decode throughput rather than merely queueing behind.

        These were one flag until today, which is how `cooker run --live` came to be
        blocked on 180 of 180 ticks while a third of that time was genuinely idle: a
        single boolean answering both questions is wrong about one of them by
        construction.
        """
        now = self.clock()
        sig = self.signals

        # Hard blockers first: facts that make any work wrong right now.
        blockers: list[str] = []
        if self.state != IDLE:
            blockers.append(self.reason)
        if "sockets" in set(sig.failed):
            blockers.append("detection blind (lsof)")
        if not sig.omlx_up:
            blockers.append("omlx offline")

        quiet_s = (now - self.quiet_since) if self.quiet_since is not None else None
        ready_in = 0.0
        if not blockers:
            if quiet_s is None:
                blockers.append(f"quiet {self.claim_quiet:.0f}s")
                ready_in = self.claim_quiet
            elif quiet_s < self.claim_quiet:
                ready_in = self.claim_quiet - quiet_s
                blockers.append(f"quiet {ready_in:.0f}s")
        elif quiet_s is not None:
            ready_in = max(0.0, self.claim_quiet - quiet_s)

        ready = self.state == IDLE and not blockers

        ramp: list[str] = []
        if self.last_infer_end is not None:
            left = self.cooldown - (_age_then(self.last_infer_end, now) or 0.0)
            if left > 0:
                ramp.append(f"cooldown {left:.0f}s")
        if quiet_s is None or quiet_s < self.idle_confirm:
            ramp.append(f"idle confirm {self.idle_confirm - (quiet_s or 0.0):.0f}s")
        ramped = ready and not ramp

        return BusState(
            state=self.state,
            reason=self.reason,
            blockers=tuple(blockers),
            ready=ready,
            ready_in_s=ready_in,
            ts=now,
            hid_idle_s=sig.hid_idle_s or 0.0,
            clients=tuple(str(c) for c in (sig.socks.clients if sig.socks else ())),
            wire_up=sig.wire_up,
            cooking=tuple(str(c) for c in
                          (sig.socks.generating(self.floor_bps) if sig.socks else ())),
            ramped=ramped,
            quiet_s=quiet_s,
            ramp_note="; ".join(ramp) if not ramped else "",
            typical_gap_s=self.typical_gap_s,
            ledger_up=sig.ledger.up,
            ledger_idle_s=sig.ledger.idle_s,
            ledger_active=sig.ledger.active,
            ledger_own=sig.ledger.own,
        )

    def _event(self, type_: str, message: str, *, severity: str, data: dict) -> None:
        """Transitions go in the events table, because 'why did the kitchen stop'
        has to be answerable from the UI after the fact, not only live."""
        self.last_event = {"type": type_, "message": message}
        if self.conn is None:
            return
        db.emit(self.conn, type_, message=message, severity=severity, data=data)
