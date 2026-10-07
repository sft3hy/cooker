"""Process assembly: the daemon that Cooker runs as.

One process, three cooperating parts. Kept in its own module so the web server
(M7) mounts the same objects instead of building a second set, and so `run` and
`watch` differ by a flag rather than by a fork of the wiring.

    detector  ──► bus state ──► scheduler ──► slots
       │                            │
       └──── events table ◄────────┘   (SSE in M7 tails this)

Nothing here talks to omlx. The stage runner is injected and in M2 it is None,
which makes the scheduler simulate rather than generate: this milestone is about
traffic control, not tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sqlite3
import time
from typing import Any

from cooker import chains, db
from cooker import llm as llm_mod
from cooker import opencode as opencode_mod
from cooker import runner as runner_mod
from cooker.config import Config
from cooker.detect import IDLE, Detector
from cooker.scheduler import Scheduler


class Kitchen:
    """The live state of a running Cooker: detector, scheduler, and the clock."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection | None = None,
                 *, dry_run: bool = True) -> None:
        self.cfg = cfg
        self.conn = conn if conn is not None else db.connect(cfg.db_path)
        self._owns_conn = conn is None
        self.clock = time.time
        # `own_busy` is a lazy lambda because the GPU client does not exist
        # yet in dry-run — and in dry-run it must keep not existing. The ledger
        # needs the number so our own streaming stage does not read as "someone
        # else generating" and preempt itself; in dry-run it stays zero, which
        # is the truth: dry-run has nothing in flight and never will.
        self.oc: opencode_mod.OpenCode | None = None
        self.detector = Detector(cfg, self.conn, clock=self.clock,
                                  own_busy=lambda: (
                                      self.llm.inflight if self.llm else 0) + (
                                      # A delegation burns the same GPU through
                                      # opencode's hands; the ledger cannot tell
                                      # whose request is whose, so we subtract the
                                      # one session we know is ours (§21). When
                                      # Sam prompts too, `active` outruns `own`
                                      # again and preemption behaves normally.
                                      1 if self.oc and self.oc.busy_session else 0))
        # The GPU client exists only when we are actually live. In dry-run there
        # is no LLM object at all, which is the strongest guarantee the code can
        # give: there is nothing here holding a connection to omlx to accidentally
        # use. M3 is the first milestone where that object is allowed to exist.
        self.llm: llm_mod.LLM | None = None
        self.runner: runner_mod.StageRunner | None = None
        if not dry_run:
            self.llm = llm_mod.LLM(cfg, emit=self._emit)
            if bool(cfg.get("opencode.enabled", True)):
                self.oc = opencode_mod.OpenCode(cfg, emit=self._emit)
            self.runner = runner_mod.StageRunner(cfg, self.conn, self.llm,
                                                 oc=self.oc)
        self.scheduler = Scheduler(cfg, self.conn, self.detector, dry_run=dry_run,
                                   clock=self.clock,
                                   stage_runner=self.runner if self.runner else None)
        self.stop = asyncio.Event()
        self.started_at = self.clock()
        self.states: dict[str, float] = {}
        self.prev_state = IDLE
        self._prev_ts: float | None = None
        self._prev_key: tuple[str, bool] | None = None
        self.timers: list[asyncio.Task] = []
        # Only to keep the dry-run's "I would have cooked this" log to once
        # per empty-queue spell instead of once per second.
        self._seed_noted = False

    def _emit(self, type_: str, message: str,
              data: dict[str, Any] | None = None) -> None:
        """The callback shape llm.py takes, bound to our connection."""
        db.emit(self.conn, type_, message=message, data=data or {})

    # --- the loop ---------------------------------------------------------

    async def run(self, *, seconds: float | None = None) -> dict[str, Any]:
        """Tick until stopped. `seconds` bounds it, which is what the tests and
        a quick manual check need; launchd leaves it unset and runs forever."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop)
        if seconds is not None:
            self.timers.append(asyncio.create_task(self._alarm(seconds)))

        # The wire reader is a resident subprocess and the detector is deaf
        # without it: it would see peers, be unable to tell a keepalive from a
        # generation, and (correctly) refuse to cook. So bring it up before the
        # first tick, and be explicit when it will not come up.
        starter = getattr(self.detector.sampler, "start", None)
        wire_up = bool(starter()) if callable(starter) else False
        db.emit(self.conn, "daemon.start",
                message=f"cooker starting ({'dry-run' if self.scheduler.dry_run else 'live'})",
                data={"dry_run": self.scheduler.dry_run,
                     "max_concurrency": self.scheduler.max_workers,
                     "wire_up": wire_up,
                     "bind": f"{self.cfg.get('daemon.bind_host')}:"
                            f"{self.cfg.get('daemon.port')}"})
        if not wire_up:
            print("!! no wire reader: peers will be visible but their traffic "
                  "will not, so the detector will decline to start work")
        try:
            while not self.stop.is_set():
                self._maybe_seed()
                decision = await self.scheduler.tick()
                self._account(decision)
                for line in self._render(decision):
                    print(line, flush=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(),
                                           timeout=self.scheduler.tick_seconds)
        finally:
            await self.shutdown()
        return self.summary()

    def _maybe_seed(self) -> None:
        """Refill the queue when it is empty — the reason an empty queue is a
        request for work rather than a sign of a dead daemon.

        Dry-run does not seed. It reports the topic it would have chosen and
        creates nothing, because "dry run" means the database is untouched, and a
        dry run that writes rows has quietly become a real run with a misleading
        name. The topic is still resolved and logged, so the pipeline's intent is
        visible and reviewable before the first live night.

        The live check runs every tick rather than once at startup: the queue
        empties during a run, not only at the beginning of one. `max_live_research`
        is what stops that from becoming a flood, so the cap is the throttle and
        the tick rate is not.
        """
        counts = db.queue_counts(self.conn)
        live = counts.get("QUEUED", 0) + counts.get("RUNNING", 0)
        if live:
            self._seed_noted = False
            return
        if self.scheduler.dry_run:
            if self._seed_noted:
                return
            self._seed_noted = True
            picked = chains.next_topic(self.cfg, self.conn)
            if picked is None:
                db.emit(self.conn, "dry.topics_exhausted",
                        message="dry-run: no topic outside the dedup window",
                        data={})
                return
            topic, _ = picked
            db.emit(self.conn, "dry.would_seed",
                    message=f"dry-run: would start research on {topic[:70]}",
                    data={"dry_run": True})
            return
        made = chains.seed_research_if_thirsty(self.cfg, self.conn)
        if made:
            db.emit(self.conn, "daemon.seeded",
                    message=f"queue was empty; started {len(made)} research chain(s)",
                    data={"chains": made, "counts": counts})

    async def _alarm(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.request_stop()

    def request_stop(self) -> None:
        self.stop.set()

    async def shutdown(self) -> None:
        """Cancel what is in flight. Without this a stage stays RUNNING in the
        database and the next process has to guess that it is actually dead."""
        for task in [*self.scheduler.slots.values(), *self.timers]:
            task.cancel()
        stopper = getattr(self.detector.sampler, "stop", None)
        if callable(stopper):
            stopper()
        if self.llm is not None:
            with contextlib.suppress(Exception):
                await self.llm.aclose()
        self.stop.set()
        db.emit(self.conn, "daemon.stop",
                message=f"stopping after {self.clock() - self.started_at:.0f}s",
                data=self.summary())
        if self._owns_conn:
            self.conn.close()

    # --- accounting ------------------------------------------------------

    def _account(self, decision: Any) -> None:
        """Charge wall time to the state that was actually in force. Timing the
        tick itself would report 'ACTIVE_INFER 0.2s' after five seconds of
        someone else generating — the wrong number, in the field that matters."""
        now = self.clock()
        if self._prev_ts is not None:
            self.states[self.prev_state] = self.states.get(self.prev_state, 0.0) + (
                now - self._prev_ts)
        self._prev_ts = now
        self.prev_state = decision.state

    def summary(self) -> dict[str, Any]:
        out = self.scheduler.summary()
        out["uptime_s"] = round(self.clock() - self.started_at, 1)
        out["seconds_in_state"] = {k: round(v, 1) for k, v in self.states.items()}
        return out

    # --- rendering -------------------------------------------------------

    def _render(self, decision: Any) -> list[str]:
        """A line when something changed, silence otherwise. Steady state is not
        news every second; the pot animation in M7 conveys that, and a terminal
        that repeats itself trains you to stop reading it."""
        out: list[str] = []
        key = (decision.state, decision.ready)
        if key != self._prev_key:
            self._prev_key = key
            stamp = time.strftime("%H:%M:%S", time.localtime(decision.ts))
            out.append(f"{stamp} {decision.state:<13}{self._why(decision)}")
        prefix = " " * 10
        for note in decision.notes:
            out.append(f"{prefix}note:  {note}")
        for name in decision.started:
            out.append(f"{prefix}start: {name}")
        for name in decision.finished:
            out.append(f"{prefix}plate: {name}")
        for name in decision.preempted:
            out.append(f"{prefix}STOP:  {name}")
        return out

    @staticmethod
    def _why(decision: Any, limit: int = 58) -> str:
        """One honest reason, truncated on a boundary. Cutting a pid in half
        ('opencode(2378') turns a clear answer into a typo."""
        if decision.ready:
            return "ready to cook"
        text = "; ".join(decision.blockers) or "not ready"
        if len(text) <= limit:
            return text
        cut = text.rfind(";", 0, limit)
        return (text[:cut] if cut > 24 else text[:limit]).rstrip("; ") + " …"


def build(cfg: Config, *, dry_run: bool = True) -> Kitchen:
    return Kitchen(cfg, dry_run=dry_run)
