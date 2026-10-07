"""The scheduler: the only thing in Cooker that may start or stop work.

Everything else — the detector, the CLI, the web UI — *asks* or *observes*. This
is the single place with hands on the throttle, which makes it the place where
"polite" is either real or a comment in a YAML file.

The rule set, in priority order:

    ACTIVE_INFER  cancel everything in flight, now. Cooldown starts.
    ACTIVE_USER   no new stages. In-flight stages finish (bounded).
    IDLE          ramp 1 -> max_concurrency, but only after idle_confirm and
                  the cooldown have both expired.

Why cancel on ACTIVE_INFER but not on ACTIVE_USER: `chunked_prefill: false` on
omlx means a prefill is atomic and exclusive, so an in-flight *prefill* is
already lost — cancelling saves the interactive session the rest of the decode.
A stage that is merely decoding is nearly done, and throwing it away buys
nothing and wastes the tokens already spent. Typing is not generating, so the
reaction is deliberately different.

Dry-run (the M2 default) never writes to `tasks`. It points at the real queue
through `peek_runnable` and holds simulated slots in memory, so the preemption
arithmetic, the backoff, and the whole cadence are exercised without a single
token or a fabricated result. M3 swaps the simulated slot for a claimed stage
streamed from `llm.py`; nothing else in here changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from cooker import db
from cooker import eval as eval_mod
from cooker.config import Config
from cooker.detect import ACTIVE_INFER, Detector


def backoff_seconds(preemptions: int, base: float, cap: float) -> float:
    """30s, 60s, 120s ... capped. Exponential, because a stage that has been
    preempted three times is asking for a window that is not coming back; the
    cap keeps it from disappearing for a day."""
    return min(base * (2 ** max(0, preemptions)), cap)


@dataclass
class Slot:
    """One burner. A stage we are running (dry-run: pretending to run)."""

    index: int
    task_id: str
    kind: str
    generator: str
    title: str
    started_at: float
    duration_s: float
    dry_run: bool
    # Set by the supervisor when it cancels us. Cancellation is not a safe place
    # to do bookkeeping — cancel a task before its first await and none of its
    # except blocks ever run — so the trigger pulls the record, and this flag
    # stops the corpse filing a duplicate.
    preempted: bool = False

    def remaining(self, now: float) -> float:
        return max(0.0, self.started_at + self.duration_s - now)

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        return {
            "slot": self.index,
            "task_id": self.task_id,
            "kind": self.kind,
            "generator": self.generator,
            "title": self.title,
            "elapsed_s": round(now - self.started_at, 1),
            "remaining_s": round(self.remaining(now), 1),
            "dry_run": self.dry_run,
        }


@dataclass
class Decision:
    """One tick, explained. The UI renders `blockers` verbatim, so that a cold
    kitchen is a kitchen with a reason rather than a kitchen that is broken."""

    ts: float
    state: str
    ready: bool
    reason: str
    blockers: tuple[str, ...]
    running: int
    wanted: int
    started: tuple[str, ...] = ()
    preempted: tuple[str, ...] = ()
    finished: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "state": self.state,
            "reason": self.reason,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "running": self.running,
            "wanted": self.wanted,
            "started": list(self.started),
            "preempted": list(self.preempted),
            "finished": list(self.finished),
            "notes": list(self.notes),
        }


class NoStageRunner(RuntimeError):
    """Raised when asked to run for real before a stage runner exists."""


@dataclass
class Scheduler:
    cfg: Config
    conn: sqlite3.Connection
    detector: Detector
    dry_run: bool = True
    clock: Callable[[], float] = time.time
    stage_runner: Callable[[db.Task], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        sched = self.cfg.get("scheduler", {}) or {}
        self.max_workers = int(sched.get("max_concurrency", 1))
        self.min_workers = int(sched.get("min_concurrency", 0))
        self.tick_seconds = float(sched.get("tick_seconds", 1.0))
        self.backoff_base = float(sched.get("backoff_base_seconds", 30))
        self.backoff_cap = float(sched.get("backoff_max_seconds", 3600))
        self.max_attempts = int(sched.get("max_attempts", 3))
        self.latency_ceiling = float(sched.get("latency_p95_ceiling_s", 4.0))
        self.max_stage_s = float(self.cfg.get("detect.max_stage_seconds", 180))
        # A simulated stage: long enough to be preempted mid-flight by a real
        # keystroke, short enough to watch the loop turn over.
        self.dry_stage_s = float(sched.get("dry_run_stage_seconds", 20))
        self.slots: dict[int, asyncio.Task] = {}
        self.sim: dict[int, Slot] = {}
        self.sim_reserved: dict[str, float] = {}
        self.sim_preemptions: dict[str, int] = {}
        self.just_finished: list[str] = []
        self.braked = False
        self.paused_note: str | None = None
        self.last_state: str = ""
        self.last_ready: bool | None = None
        self.decisions: list[Decision] = []

    # --- policy ---------------------------------------------------------

    def _paused(self) -> str | None:
        note = db.get_meta(self.conn, "paused")
        return None if note in (None, "", "0") else note

    def _p95_ttft(self) -> float | None:
        """Our own background p95, from the rows we wrote. None until we have
        at least five requests to rank — a p95 of one sample is theatre."""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n, MAX(ttft_ms) AS hi FROM tasks"
            f" WHERE ttft_ms IS NOT NULL AND status = '{db.SUCCEEDED}'"
        ).fetchone()
        if not row or row["n"] < 5 or row["hi"] is None:
            return None
        rows = self.conn.execute(
            f"SELECT ttft_ms FROM tasks WHERE ttft_ms IS NOT NULL"
            f" AND status = '{db.SUCCEEDED}' ORDER BY ttft_ms ASC"
        ).fetchall()
        idx = max(0, round(0.95 * len(rows)) - 1)
        return float(rows[idx]["ttft_ms"]) / 1000.0

    def desired_workers(self, state: str, ready: bool,
                        ramped: bool = True) -> tuple[int, list[str]]:
        notes: list[str] = []
        if state == ACTIVE_INFER:
            return 0, notes
        if not ready:
            return 0, notes
        cap = self.max_workers
        # One pot until the machine has been quiet long enough to be trusted with
        # two. `ready` means a bounded stage fits in the pause; `ramped` means the
        # pause looks durable. Concurrency is the expensive request: it does not
        # queue behind a generation, it shares the decode throughput with it, so the
        # gate that lets it in has to be the slow one.
        if not ramped:
            cap = min(cap, 1)
        p95 = self._p95_ttft()
        if p95 is not None and p95 > self.latency_ceiling:
            cap = max(self.min_workers, cap // 2)
            if not self.braked:
                self.braked = True
                notes.append(f"brake: our own p95 TTFT {p95:.2f}s > {self.latency_ceiling}s")
        elif self.braked and p95 is not None and p95 < self.latency_ceiling * 0.75:
            # Hysteresis on the brake too: releasing it at the same number that
            # applied it is how you get a machine that oscillates.
            self.braked = False
            notes.append(f"brake released: p95 {p95:.2f}s")
        # Ramp one slot per tick, never straight to max.
        want = min(cap, len(self.slots) + 1)
        return want, notes

    # --- the loop ---------------------------------------------------------

    async def tick(self) -> Decision:
        now = self.clock()
        state = await self.detector.tick()
        self.paused_note = self._paused()

        blockers = list(state.blockers)
        if self.paused_note:
            blockers.append(f"paused: {self.paused_note}")
        ready = state.ready and not self.paused_note

        started: list[str] = []
        preempted: list[str] = []
        # drained, not polled: a stage that plates between ticks must still
        # show up in the decision that follows it.
        finished = self.just_finished
        self.just_finished = []
        notes: list[str] = []

        if state.state == ACTIVE_INFER and self.slots:
            preempted = self._preempt_all(now, state.reason)

        want, want_notes = self.desired_workers(state.state, ready,
                                                 ramped=state.ramped and not self.paused_note)
        notes.extend(want_notes)

        if ready:
            started = await self._fill(want, now)
        elif self.slots and state.state != ACTIVE_INFER:
            notes.append(
                f"holding {len(self.slots)} in flight to finish"
                f" (<= {self.max_stage_s:.0f}s each)"
            )

        if ready != self.last_ready:
            self._event(
                "sched.ready" if ready else "sched.hold",
                "ready to cook" if ready else "; ".join(blockers) or "not ready",
                severity="info",
                data={"blockers": blockers},
            )
            self.last_ready = ready
        self.last_state = state.state

        d = Decision(
            ts=now,
            state=state.state,
            reason=state.reason,
            ready=ready,
            blockers=tuple(blockers),
            running=len(self.slots),
            wanted=want,
            started=tuple(started),
            preempted=tuple(preempted),
            finished=tuple(finished),
            notes=tuple(notes),
        )
        self.decisions.append(d)
        del self.decisions[:-120]
        return d

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # never let one bad tick kill the daemon
                self._event("sched.error", f"{type(exc).__name__}: {exc}",
                           severity="error", data={})
            # wait_for is the tick: it returns early the moment `stop` fires, so
            # Ctrl-C lands inside a second rather than at the end of the interval.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.tick_seconds)

    # --- slots ---------------------------------------------------------

    def _free_slot(self) -> int | None:
        for i in range(self.max_workers):
            if i not in self.slots:
                return i
        return None

    async def _fill(self, want: int, now: float) -> list[str]:
        started: list[str] = []
        while len(self.slots) < want:
            index = self._free_slot()
            if index is None:
                break
            if self.dry_run:
                task = self._pick(now)
                if task is None:
                    break
                slot = Slot(index, task.id, task.kind, task.generator, task.title,
                            now, self.dry_stage_s, True)
                self.sim[index] = slot
                # Reserve it, so the next tick points at different work instead of
                # narrating the same stage over and over.
                self.sim_reserved[task.id] = now + self.dry_stage_s + self.backoff_base
                self.slots[index] = asyncio.create_task(
                    self._sim_stage(index), name=f"dry-{index}-{task.kind}"
                )
            else:
                if self.stage_runner is None:
                    raise NoStageRunner("--live needs a stage runner; llm.py lands in M3")
                # The RLHF loop, applied at the only place it can bite. Weights,
                # EMAs and parks elsewhere are bookkeeping: a generator is stopped
                # by not appearing in the SELECT, so this list is the enforcement
                # point and every other mention of a parked generator is a report.
                claimed = db.claim_next(
                    self.conn,
                    generators=eval_mod.allowed_generators(self.cfg, self.conn))
                if claimed is None:
                    break
                slot = Slot(index, claimed.id, claimed.kind, claimed.generator,
                            claimed.title, now, self.max_stage_s, False)
                self.sim[index] = slot
                self.slots[index] = asyncio.create_task(
                    self._live_stage(index, claimed, slot),
                    name=f"live-{index}-{claimed.kind}",
                )
            started.append(f"slot{index}:{slot.generator}.{slot.kind}")
            self._event(
                "sched.start",
                f"slot {index} {'would run' if slot.dry_run else 'running'} "
                f"{slot.generator}.{slot.kind} — {slot.title}",
                severity="info",
                data=slot.as_dict(now),
            )
        return started

    def _pick(self, now: float) -> db.Task | None:
        """The stage we would take next, honouring simulated backoffs so the
        dry run does not point at the same row every tick.

        Parked generators are skipped here too. A dry run that narrates "would
        run brainstorm" for work the live path will never claim is a rehearsal
        that lies about the performance.
        """
        allowed = eval_mod.allowed_generators(self.cfg, self.conn)
        for task in db.peek_runnable(self.conn, limit=8):
            if allowed is not None and task.generator not in allowed:
                continue
            if task.id in self.sim_reserved and self.sim_reserved[task.id] > now:
                continue
            return task
        return None

    async def _sim_stage(self, index: int) -> None:
        slot = self.sim[index]
        try:
            await asyncio.sleep(slot.duration_s)
        except asyncio.CancelledError:
            # Nothing to record here: _preempt_all already retired the slot and
            # wrote the backoff, synchronously, at the moment it decided.
            raise
        self.slots.pop(index, None)
        self.sim.pop(index, None)
        self.sim_reserved.pop(slot.task_id, None)
        self.just_finished.append(f"slot{index}:{slot.kind}")
        self._event("sched.finish",
                    f"slot {index} would publish (dry-run): {slot.title}",
                    severity="info",
                    data={"slot": index, "task_id": slot.task_id,
                        "elapsed_s": round(self.clock() - slot.started_at, 1)})

    async def _live_stage(self, index: int, task: db.Task, slot: Slot) -> None:
        # The slot arrives as an argument rather than being looked up by index:
        # _preempt_all pops self.sim the instant it cancels, so a lookup from
        # here finds None, which reads as "nobody preempted me" and files the
        # requeue a second time with the wrong backoff.
        started = self.clock()
        try:
            await asyncio.wait_for(self.stage_runner(task), timeout=self.max_stage_s)
        except asyncio.CancelledError:
            # Two ways to be cancelled and they must not both file the same
            # report. _preempt_all already requeued this one — synchronously, at
            # the moment it decided — so writing here too would bump `preemptions`
            # twice and double the backoff the stage has to wait. Anything else
            # (Ctrl-C, launchd stopping us) is ours to record.
            if not slot.preempted:
                db.requeue(self.conn, task.id, reason="cancelled: daemon stopping",
                          backoff_seconds=self.backoff_base)
                self._event("sched.cancel",
                            f"slot {index} {task.generator}.{task.kind} back to queue",
                            severity="warn", data={"task_id": task.id})
            raise
        except TimeoutError:
            db.requeue(self.conn, task.id, reason="stage timeout",
                       backoff_seconds=self.backoff_base)
            self._event("sched.timeout", f"{task.kind} exceeded "
                        f"{self.max_stage_s:.0f}s", severity="warn",
                        data={"task_id": task.id})
        except Exception as exc:
            attempts = int(self.conn.execute(
                "SELECT attempts FROM tasks WHERE id=?", (task.id,)
            ).fetchone()["attempts"])
            if attempts >= self.max_attempts:
                db.park(self.conn, task.id, f"{type(exc).__name__}: {exc}")
            else:
                db.requeue(self.conn, task.id, reason=f"{type(exc).__name__}: {exc}",
                          backoff_seconds=self.backoff_base)
        else:
            self.just_finished.append(f"slot{index}:{task.kind}")
            self._event("sched.finish",
                        f"slot {index} {task.generator}.{task.kind} -> {db.SUCCEEDED}",
                        severity="info",
                        data={"slot": index, "task_id": task.id,
                            "elapsed_s": round(self.clock() - started, 1)})
        finally:
            self.slots.pop(index, None)
            self.sim.pop(index, None)

    def _preempt_all(self, now: float, reason: str) -> list[str]:
        """Drop the socket. Measured (DISCOVERY.md): a client disconnect halts
        generation on omlx, so this genuinely frees the box rather than merely
        stopping us from reading."""
        out: list[str] = []
        for index, task in list(self.slots.items()):
            slot = self.sim.pop(index, None)
            self.slots.pop(index, None)
            if slot is None:
                task.cancel()
                out.append(f"slot{index}")
                continue
            slot.preempted = True
            label = f"{slot.generator}.{slot.kind}"
            # One contract, dry and live: `preemptions` is the count *before* this
            # one, so the first cut waits base, the second twice that. Without
            # growth a hot stage knocks on the door every 30s forever.
            if slot.dry_run:
                hits = self.sim_preemptions.get(slot.task_id, 0)
                self.sim_preemptions[slot.task_id] = hits + 1
                seconds = backoff_seconds(hits, self.backoff_base, self.backoff_cap)
            else:
                row = self.conn.execute(
                    "SELECT preemptions FROM tasks WHERE id=?", (slot.task_id,)).fetchone()
                hits = int(row["preemptions"]) if row else 0
                seconds = backoff_seconds(hits, self.backoff_base, self.backoff_cap)
                db.requeue(self.conn, slot.task_id,
                          reason="preempted: interactive traffic",
                          backoff_seconds=seconds, preempted=True)
            # Reserved before anything can re-claim it, so the freed burner does
            # not immediately re-take the stage we just dropped.
            self.sim_reserved[slot.task_id] = now + seconds
            self._event(
                "sched.preempt",
                f"slot {index} cancelled {label} backoff {seconds:.0f}s",
                severity="warn",
                data={"slot": index, "task_id": slot.task_id, "backoff_s": seconds,
                    "preemptions": hits + 1,
                    "elapsed_s": round(now - slot.started_at, 1)},
            )
            task.cancel()
            out.append(f"slot{index}:{label}")
        if out:
            self._event("sched.preempt_all", f"{reason} — {len(out)} cancelled",
                        severity="warn", data={"cancelled": out, "reason": reason})
        return out

    # --- reporting -------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "dry_run": self.dry_run,
            "state": self.detector.state,
            "reason": self.detector.reason,
            "ready": self.detector.snapshot().ready,
            "max_workers": self.max_workers,
            "braked": self.braked,
            "paused": self.paused_note,
            "slots": [s.as_dict(now) for s in self.sim.values()],
            "queue": db.queue_counts(self.conn),
            "runnable_now": db.runnable_now(self.conn),
            "next_runnable_in_s": (
                round(e - now, 1) if (e := db.next_runnable_eta(self.conn)) else None
            ),
            "p95_ttft_s": (round(v, 2) if (v := self._p95_ttft()) else None),
            "cost_ms": self.detector.signals.cost_ms,
        }

    def _event(self, type_: str, message: str, *, severity: str, data: dict) -> None:
        db.emit(self.conn, type_, message=message, severity=severity, data=data)
