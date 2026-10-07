"""Wire: bytes moved per process, the only honest proof that inference is running.

Why this module exists
--------------------
Cooker's politeness rests on knowing whether someone else is generating. The
obvious detector — `lsof -iTCP:8000 -sTCP:ESTABLISHED` — turned out to be wrong.
Measured on 2026-10-07, in a window where a tool was running and no tokens were
being produced (re-run it: `bench/bench7_generate_vs_keepalive.py`):

    ESTABLISHED sockets      2-6 connections, permanently. Traefik keeps an
                             upstream alive and the OpenCode client keeps a pool.
                             A socket is a relationship, not an event.
    omlx cumulative cputime  3.90 CPU-seconds inside a 4.05 second window in
                             which it served nothing. It busy-spins. And `ps -o
                             %cpu` is a decayed average on top of that, so it
                             answers a question from ten seconds ago.
    ~/.omlx/stats.json       dead live: three successful 200 responses moved
                             neither its counters nor its mtime.
    nettop bytes             flat zero with those sockets wide open. Then, on
                             one real streamed request, 204 bytes in and 6,683
                             bytes out within a single second.

So bytes are the signal. A peer that moves nothing is not cooking, and the
kitchen has to be allowed to light anyway — otherwise a permanently-open
keepalive means a permanently cold kitchen, which is a daemon with no purpose.

The asymmetry we accept
----------------------
nettop attributes bytes per *process* and offers no address column to filter on,
so a peer connected to :8000 that is also fetching a webpage reads as busy. That
is wrong in the safe direction: a false busy costs us cooking time, a false idle
costs Sam his interactive latency.

Dependency direction is deliberate: nothing here imports Cooker's types. The
probe reports `{pid: bytes}` and `detect` decides what a pid means, so `detect`
can depend on `wire` without a cycle.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SAMPLE_SECONDS = 1.0  # the `-s 1` inside each poll; the run costs ~1.25s


@dataclass(frozen=True)
class Wire:
    """Bytes each process moved across the probe's window, and whether the probe
    was actually watching when they were counted."""

    moved: dict[int, int]
    window_s: float
    ts: float
    up: bool
    samples: int = 0

    @property
    def blind(self) -> bool:
        """Not seeing the wire. `moved` being empty then means *unknown*, not
        *idle* — confusing those two is the whole reason this module exists."""
        return not self.up

    def rate(self, pid: int) -> float:
        """Bytes per second for one pid. Threshold on a rate rather than on a
        total, because a keepalive's zero and a small request's zero look the same
        until you put a time underneath them."""
        return self.moved.get(pid, 0) / self.window_s if self.window_s else 0.0

    def served_bps(self, server_pids: tuple[int, ...]) -> float:
        """Aggregate throughput of the inference server itself, for the HUD."""
        return sum(self.rate(p) for p in server_pids)

    def busy_pids(self, floor_bps: float) -> tuple[int, ...]:
        return tuple(sorted(p for p in self.moved if self.rate(p) >= floor_bps))

    def server_talking(self, server_pids: tuple[int, ...], floor_bps: float) -> bool:
        """Is omlx itself moving bytes? The cleanest proof there is.

        Nobody asked the server a question and got an answer without bytes
        leaving it, so `omlx bytes_out > 0` is equivalent to 'someone is being
        served'. It needs no attribution to a peer, which matters because on this
        box most traffic reaches omlx *through OrbStack*, so the peer name on the
        socket is the proxy's and not the caller's.
        """
        return any(self.rate(p) >= floor_bps for p in server_pids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "up": self.up,
            "cost_ms": self.cost_ms,
            "cpu_total_ms": round(self.cpu_total_ms, 1),
            "cycles": self.cycles,
            "window_s": round(self.window_s, 1),
            "samples": self.samples,
            "busy": {str(p): round(self.rate(p)) for p in self.moved
                    if self.rate(p) > 0},
        }


DEAD = Wire(moved={}, window_s=0.0, ts=0.0, up=False)


class WireProbe:
    """Per-process throughput from one continuous `nettop -P`.

    Polls rather than streams, measured on 2026-10-07: a resident `nettop -L 0`
    block-buffers its piped stdout and delivered its first line 8.22 seconds after
    start and its next 8 seconds later, which is an eight second blind spot on the
    one question this daemon exists to answer fast. A short `nettop -L 2 -s 1`
    returns in 1.25s, consistently, and prints two cumulative samples so the diff
    is still honest. Costs a process spawn per cycle, in a thread, which is the
    cheaper mistake by a wide margin.
    """

    def __init__(
        self,
        cfg: Any = None,
        *,
        clock: Callable[[], float] = time.time,
        interval_s: float | None = None,
        window_s: float | None = None,
    ) -> None:
        self.clock = clock
        get = getattr(cfg, "get", lambda _k, d=None: d)
        self.interval = max(0.2, float(interval_s if interval_s is not None
                                      else get("detect.wire_interval_seconds", 1.0)))
        self.window = float(window_s if window_s is not None
                            else get("detect.wire_window_seconds", 3.0))
        self.window = max(self.window, self.interval * 2)
        self.capacity = max(2, round(self.window / self.interval))
        # Poll period. The run itself costs ~1.25s of the cycle, so this is the
        # floor on how quickly a new generation is noticed.
        self.cycle = self.interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cols: dict[str, int] = {}
        self._prev: dict[int, tuple[int, int]] = {}
        self._ring: deque[dict[int, int]] = deque(maxlen=self.capacity)
        self._pending: dict[int, int] = {}
        self._snap = DEAD
        self.cost_ms = 0.0
        self.cpu_total_ms = 0.0
        self.wall_ms = 0.0
        self.cycles = 0
        self.up = False
        # nettop flushes a piped stdout in bursts rather than every interval, so
        # samples arrive in clumps up to several seconds apart. Judging staleness
        # against the window made the probe flicker blind every few seconds, and
        # each flicker is a tick in which the kitchen refuses to light for no
        # reason. This is the measured burst spacing plus slack, and it is a
        # documented compromise: we would rather be late to see traffic than
        # pretend we cannot see it.
        self.stale_after = max(self.window * 2.0, self.interval * 8.0)
        self.last_error: str | None = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Bring the reader thread up. Returns False, with `up` left False, when
        nettop cannot run at all, so the detector reports blindness rather than
        inventing quiet."""
        if self._thread is not None and self._thread.is_alive():
            return self.up
        if not shutil.which("nettop"):
            self.last_error = "nettop not on PATH"
            self.up = False
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._read, daemon=True,
                                         name="cooker-wire")
        self._thread.start()
        self.up = True
        return True

    def stop(self) -> None:
        """Tear the reader down. `up` goes False so a stopped probe cannot be
        misread as an idle machine by whatever asks next."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            with contextlib.suppress(Exception):
                thread.join(timeout=self.cycle + 2.0)
        self.up = False

    # --- the reader ------------------------------------------------------

    def _read(self) -> None:
        """One short `nettop` per cycle, forever, in this thread.

        Each run prints two cumulative samples one second apart, so the run
        answers on its own: the diff between its two samples is the bytes moved
        in that second. Nothing has to survive between cycles except the ring.
        """
        while not self._stop.is_set():
            t0 = self.clock()
            try:
                proc = subprocess.Popen(
                    ["nettop", "-n", "-x", "-P", "-L", "2", "-s",
                     f"{SAMPLE_SECONDS:g}"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
            except OSError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.up = False
                return
            try:
                assert proc.stdout is not None
                text = proc.stdout.read()
            except (OSError, ValueError) as exc:
                self.last_error = f"reading nettop: {type(exc).__name__}"
                text = ""
            # `wait4` rather than `wait`, because it hands back this child's
            # rusage and we want nettop's own CPU. Wall time is worthless here:
            # nettop sleeps for the sample interval inside every run, so a
            # 1,219ms wall measurement reads as "121% of a core" and is a lie.
            cpu_ms = 0.0
            try:
                _pid, _status, ru = os.wait4(proc.pid, 0)
                cpu_ms = (ru.ru_utime + ru.ru_stime) * 1000.0
            except (ChildProcessError, OSError):
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
            self._ingest(text)
            self.cost_ms = round(cpu_ms, 1)
            self.cpu_total_ms += cpu_ms
            # Cumulative as well as per-cycle: a single cycle's figure depends on
            # what else the box was doing at that instant, and quoting one of them
            # as *the* cost of the probe has already misled me once. Totals do not.
            self.wall_ms = round((self.clock() - t0) * 1000.0, 1)
            self.cycles += 1
            self._stop.wait(max(0.1, self.cycle - (self.clock() - t0)))

    def _ingest(self, text: str) -> None:
        """Turn nettop's CSV into per-pid byte deltas, sealing at each header.

        Column positions come from the header above every sample, never from a
        hard-coded index: `-J` is silently ignored on this build, and assuming an
        index once had me reading retransmit counts as throughput, which looked
        exactly like traffic that was not there.
        """
        for line in text.splitlines():
            if self._stop.is_set():
                return
            cells = line.rstrip("\r\n").split(",")
            if not cells or not cells[0]:
                continue
            if cells[0] == "time":
                # The header is the only honest sample boundary. Every data row
                # carries its own microsecond timestamp, so sealing on those made
                # the window three ROWS long instead of three seconds, and the
                # snapshot looked frozen between bursts while quietly discarding
                # almost everything it saw.
                self._seal()
                self._cols = {name: i for i, name in enumerate(cells) if name}
                continue
            cols = self._cols
            if "bytes_in" not in cols or "bytes_out" not in cols:
                continue
            need = max(cols["bytes_in"], cols["bytes_out"])
            if len(cells) <= need:
                continue
            pid_s = cells[1].rpartition(".")[2]
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            try:
                b_in, b_out = int(cells[cols["bytes_in"]]), int(cells[cols["bytes_out"]])
            except ValueError:
                continue
            with self._lock:
                prev = self._prev.get(pid)
                if prev is not None:
                    # Saturating: a counter that fell means the process restarted,
                    # and zero is the honest answer where a negative is a lie and a
                    # wraparound is a firework.
                    self._pending[pid] = self._pending.get(pid, 0) + (
                        max(0, b_in - prev[0]) + max(0, b_out - prev[1]))
                self._prev[pid] = (b_in, b_out)

    def _seal(self) -> None:
        """Close one sample into the ring and rebuild the window.

        The ring is the window, so it is deliberately *not* cleared here: clearing
        it would make the window one second long no matter what the config said,
        which is the kind of bug that shows up as 'the detector is twitchy' three
        weeks from now and never as a stack trace.
        """
        with self._lock:
            if not self._pending:
                return
            self._ring.append(self._pending)
            self._pending = {}
            totals: dict[int, int] = {}
            for row in self._ring:
                for pid, moved in row.items():
                    totals[pid] = totals.get(pid, 0) + moved
            self._snap = Wire(moved=totals, window_s=self.window, ts=self.clock(),
                             up=self.up, samples=len(self._ring))

    # --- query ---------------------------------------------------------

    def snapshot(self) -> Wire:
        """The current window. A stale read comes back `up=False`: the numbers
        may still be the last true ones, but nobody should start work on them."""
        with self._lock:
            snap = self._snap
        if not self.up:
            return Wire(moved=snap.moved, window_s=snap.window_s, ts=snap.ts,
                        up=False, samples=snap.samples)
        if snap.ts == 0.0 or self.clock() - snap.ts > self.stale_after:
            return Wire(moved=snap.moved, window_s=snap.window_s, ts=snap.ts,
                        up=False, samples=snap.samples)
        return snap

    def as_dict(self) -> dict[str, Any]:
        snap = self.snapshot()
        out = snap.as_dict()
        out["age_s"] = round(self.clock() - snap.ts, 1) if snap.ts else None
        out["error"] = self.last_error
        return out
