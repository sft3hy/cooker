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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cooker import db
from cooker.config import Config
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
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.our_pid = our_pid if our_pid is not None else os.getpid()
        self.port = int(cfg.get("detect.omlx_port", 8000))
        self.names = tuple(cfg.get("detect.omlx_proc_names", ["omlx", "omlx-server"]))
        self.oc_port = int(cfg.get("detect.opencode_port", 4096))
        self.wal = Path(str(cfg.get("detect.opencode_wal", "~/.local/share/opencode/opencode.db-wal")))
        self.stats = Path(str(cfg.get("detect.llm_stats_file", "~/.omlx/stats.json")))
        self.fs_roots = [Path(str(p)) for p in cfg.get("detect.fs_roots", ["~/dev", "~/homelab"])]
        self.load_names = tuple(cfg.get("detect.load_proc_names", ["omlx", "opencode"]))
        self.floor_bps = float(cfg.get("detect.infer_min_bps", 256))
        # Resident, not per-tick: nettop counts cumulatively, so a fresh process
        # each second has no earlier sample to diff and would answer "0 bytes"
        # forever. One long-lived reader, and `sample` just reads its dict.
        self.wire = WireProbe(
            interval_s=float(cfg.get("detect.wire_interval_seconds", 1.0)),
            window_s=float(cfg.get("detect.wire_window_seconds", 3.0)),
            clock=clock,
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

    def start(self) -> bool:
        """Bring up the reader. The daemon calls this, not `sample`: a probe
        spawned per tick has no baseline to diff against its own last sample."""
        return self.wire.start()

    def stop(self) -> None:
        self.wire.stop()

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
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.sampler = sampler or LiveSampler(cfg, clock=clock)
        self.clock = clock
        self.tick_seconds = float(cfg.get("detect.interval_seconds", 1.0))
        self.user_idle = float(cfg.get("detect.user_idle_seconds", 120))
        self.wal_seconds = float(cfg.get("detect.opencode_wal_seconds", 30))
        self.fs_seconds = float(cfg.get("detect.fs_activity_seconds", 60))
        self.cooldown = float(cfg.get("detect.interactive_cooldown_seconds", 60))
        self.idle_confirm = float(cfg.get("detect.idle_confirm_seconds", 60))
        self.floor_bps = float(cfg.get("detect.infer_min_bps", 256))
        self.state = IDLE
        self.reason = "startup"
        self.signals = Signals()
        self.last_infer_end: float | None = None
        self.quiet_since: float | None = None
        self.last_event: dict[str, Any] = {}

    # --- classification ---

    def _user_evidence(self, sig: Signals) -> list[str]:
        ev: list[str] = []
        if sig.hid_idle_s is not None and sig.hid_idle_s < self.user_idle:
            ev.append(f"hid {sig.hid_idle_s:.0f}s")
        if sig.oc_wal_age_s is not None and sig.oc_wal_age_s < self.wal_seconds:
            ev.append(f"opencode.db-wal {sig.oc_wal_age_s:.0f}s")
        if sig.oc_clients:
            ev.append(f"opencode socket {', '.join(str(c) for c in sig.oc_clients)}")
        if sig.fs_age_s is not None and sig.fs_age_s < self.fs_seconds:
            ev.append(f"fs {sig.fs_age_s:.0f}s")
        return ev

    def _classify(self, sig: Signals) -> tuple[str, str]:
        """Strictest wins: inference > user > idle."""
        failed = set(sig.failed)
        if not sig.omlx_up and "sockets" not in failed:
            # Nothing listening: not busy, but there is also nothing to cook for.
            return IDLE, f"nothing listening on :{int(self.cfg.get('detect.omlx_port', 8000))}"
        if "sockets" in failed:
            # Blind is not the same as quiet. Stay stopped and say so.
            return ACTIVE_USER, "blind: lsof failed"
        clients = sig.socks.clients if sig.socks else ()
        servers = sig.socks.server_pids if sig.socks else ()
        if sig.wire.server_talking(servers, self.floor_bps):
            # The server itself is answering: the primary trigger, and it
            # deliberately does not ask who. On this box the peer named on the
            # socket is OrbStack rather than the real client, so blaming a process
            # by name would be a guess, while bytes leaving omlx is a fact.
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
        if ev:
            return ACTIVE_USER, " · ".join(ev)
        return IDLE, "quiet"

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
        now = self.clock()
        blockers: list[str] = []
        sig = self.signals

        if self.state != IDLE:
            blockers.append(self.reason)
        if "sockets" in set(sig.failed):
            blockers.append("detection blind (lsof)")
        if not sig.omlx_up:
            blockers.append("omlx offline")

        ready_in = 0.0
        if self.last_infer_end is not None:
            left = self.cooldown - (_age_then(self.last_infer_end, now) or 0.0)
            if left > 0:
                blockers.append(f"cooldown {left:.0f}s")
                ready_in = max(ready_in, left)
        if self.quiet_since is None:
            blockers.append(f"idle confirm {self.idle_confirm:.0f}s")
            ready_in = max(ready_in, self.idle_confirm)
        else:
            left = self.idle_confirm - (_age_then(self.quiet_since, now) or 0.0)
            if left > 0:
                blockers.append(f"idle confirm {left:.0f}s")
                ready_in = max(ready_in, left)

        return BusState(
            state=self.state,
            reason=self.reason,
            blockers=tuple(blockers),
            ready=self.state == IDLE and not blockers,
            ready_in_s=ready_in,
            ts=now,
            hid_idle_s=sig.hid_idle_s or 0.0,
            clients=tuple(str(c) for c in (sig.socks.clients if sig.socks else ())),
            wire_up=sig.wire_up,
            cooking=tuple(str(c) for c in
                          (sig.socks.generating(self.floor_bps) if sig.socks else ())),
        )

    def _event(self, type_: str, message: str, *, severity: str, data: dict) -> None:
        """Transitions go in the events table, because 'why did the kitchen stop'
        has to be answerable from the UI after the fact, not only live."""
        self.last_event = {"type": type_, "message": message}
        if self.conn is None:
            return
        db.emit(self.conn, type_, message=message, severity=severity, data=data)
