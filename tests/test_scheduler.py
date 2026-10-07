"""Scheduler tests: politeness as executable assertions.

These are the tests that matter most in the project. Everything else can be
wrong and merely waste time; if the scheduler is wrong, Sam's interactive
session gets stomped and the whole premise of Cooker is broken.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from cooker import db, detect
from cooker.config import Config
from cooker.scheduler import Scheduler, backoff_seconds


def cfg(**sched) -> Config:
    base = {
        "detect": {"max_stage_seconds": 180, "interval_seconds": 0.01},
        "scheduler": {
            "max_concurrency": 1,
            "min_concurrency": 0,
            "tick_seconds": 0.01,
            "backoff_base_seconds": 30,
            "backoff_max_seconds": 3600,
            "max_attempts": 3,
            "latency_p95_ceiling_s": 4.0,
            "dry_run_stage_seconds": 0.05,
        },
    }
    base["scheduler"].update(sched)
    return Config(base, root=Path("."))


class SimClock:
    """Injected time, so backoff windows are asserted instead of waited out."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Clock:
    """Controllable time, because backoff is the thing under test and we are not
    sitting here for real seconds."""

    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeDetector:
    """Stands in for the real detector so state transitions are scripted rather
    than waited for."""

    def __init__(self, state: str = detect.IDLE, ready: bool = True) -> None:
        self.state = state
        self.reason = "test"
        self.ready = ready
        self.signals = detect.Signals(cost_ms={})
        self.tick_count = 0

    async def tick(self) -> detect.BusState:
        self.tick_count += 1
        return self.snapshot()

    def snapshot(self) -> detect.BusState:
        return detect.BusState(
            state=self.state, reason=self.reason,
            blockers=() if self.ready else ("blocked: scripted",),
            ready=self.ready, ready_in_s=0.0, ts=time.time(),
        )

    def go(self, state: str, ready: bool = False) -> None:
        self.state, self.ready = state, ready


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def seed(conn: sqlite3.Connection, n: int = 1, generator: str = "research") -> list[db.Task]:
    chain = db.new_id()
    return [
        db.create_task(conn, chain_id=chain, kind=f"stage{i}", generator=generator,
                       title=f"task {i}")
        for i in range(n)
    ]


# --- arithmetic ---


def test_backoff_grows_and_caps() -> None:
    got = [backoff_seconds(i, 30, 3600) for i in range(8)]
    assert got[:4] == [30, 60, 120, 240]
    assert got[-1] == 3600, "must cap: 8 doublings is already half a day"


# --- dry-run honesty ---


async def test_dry_run_never_writes_to_tasks(conn) -> None:
    """The invariant of M2: the queue is observed, not touched. No fabricated
    SUCCEEDED rows, no inflated attempt counts, no phantom artifacts."""
    seed(conn, 3)
    det = FakeDetector()
    sched = Scheduler(cfg(), conn, det, dry_run=True)
    for _ in range(12):
        await sched.tick()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.12)
    for row in conn.execute("SELECT status, attempts, finished_at FROM tasks"):
        assert row["status"] == db.QUEUED, row
        assert row["attempts"] == 0, row
        assert row["finished_at"] is None, row


async def test_dry_run_still_uses_the_real_queue(conn) -> None:
    """No queue, no cooking: it must point at real rows rather than invent work
    to look busy."""
    det = FakeDetector()
    sched = Scheduler(cfg(), conn, det, dry_run=True)
    await sched.tick()
    assert sched.slots == {}, "empty queue must produce no slots"
    seed(conn, 1, generator="brainstorm")
    await sched.tick()
    assert len(sched.slots) == 1
    slot = next(iter(sched.sim.values()))
    assert slot.generator == "brainstorm"
    assert slot.dry_run


# --- politeness ---


async def test_not_ready_means_no_new_stages(conn) -> None:
    seed(conn, 4)
    det = FakeDetector(detect.ACTIVE_USER, ready=False)
    sched = Scheduler(cfg(), conn, det, dry_run=True)
    for _ in range(20):
        await sched.tick()
    assert sched.slots == {}, "started work while the user was active"


async def test_active_infer_cancels_everything_in_flight(conn) -> None:
    """The load-bearing test. An inference socket opens while we are mid-stage:
    the stage must die inside one tick."""
    seed(conn, 1)
    det = FakeDetector()
    sched = Scheduler(cfg(), conn, det, dry_run=True)
    await sched.tick()
    assert len(sched.slots) == 1

    det.go(detect.ACTIVE_INFER)
    d = await sched.tick()
    assert d.state == detect.ACTIVE_INFER
    assert d.preempted, "did not preempt"
    assert sched.slots == {}
    await asyncio.sleep(0.02)
    evts = [r for r in db.tail_events(conn) if r["type"].startswith("sched.")]
    assert any(e["type"] == "sched.preempt_all" for e in evts)
    assert any(e["type"] == "sched.preempt" for e in evts)


async def test_preemption_backs_off_and_the_delay_grows(conn) -> None:
    """First loss waits `base`, second waits twice that, and the second one has
    to be reachable: a reservation that never expires is a silent park."""
    seed(conn, 1)
    tid = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    clk = Clock()
    det = FakeDetector()
    sched = Scheduler(cfg(backoff_base_seconds=30, dry_run_stage_seconds=600),
                      conn, det, dry_run=True, clock=clk)
    await sched.tick()
    assert sched.slots, "nothing started"

    det.go(detect.ACTIVE_INFER)
    await sched.tick()
    assert not sched.slots, "phantom burner still lit after preemption"

    def backoffs() -> list[float]:
        return [json.loads(r["data_json"])["backoff_s"]
                for r in db.tail_events(conn) if r["type"] == "sched.preempt"]

    assert backoffs() == [30], backoffs()
    assert sched.sim_reserved.get(tid, 0) > clk(), "stage was not reserved"

    det.go(detect.IDLE, ready=True)
    await sched.tick()
    assert not sched.slots, "re-took the stage while it was in backoff"

    clk.advance(31)
    await sched.tick()
    assert sched.slots, "backoff expired but the stage never came back"

    det.go(detect.ACTIVE_INFER)
    await sched.tick()
    assert backoffs()[-1] == 60, f"backoff did not double: {backoffs()}"


async def test_never_exceeds_max_concurrency(conn) -> None:
    seed(conn, 20)
    det = FakeDetector()
    sched = Scheduler(cfg(max_concurrency=2, dry_run_stage_seconds=60), conn, det)
    for _ in range(10):
        await sched.tick()
    assert len(sched.slots) <= 2


async def test_concurrency_ramps_one_at_a_time(conn) -> None:
    """Ramp, not flood: even at max 4 we open one burner per tick."""
    seed(conn, 20)
    det = FakeDetector()
    sched = Scheduler(cfg(max_concurrency=4, dry_run_stage_seconds=60), conn, det)
    counts = []
    for _ in range(6):
        await sched.tick()
        counts.append(len(sched.slots))
    assert counts == [1, 2, 3, 4, 4, 4]


async def test_manual_pause_beats_readiness(conn) -> None:
    """`cooker pause` from another shell must stop new work immediately, even
    though the detector says the box is free."""
    seed(conn, 5)
    det = FakeDetector()
    sched = Scheduler(cfg(), conn, det, dry_run=True)
    db.set_meta(conn, "paused", "sam is presenting")
    d = await sched.tick()
    assert not d.ready
    assert any("sam is presenting" in b for b in d.blockers)
    assert sched.slots == {}
    db.set_meta(conn, "paused", "")
    d = await sched.tick()
    assert d.ready, "resume did not take"
    assert len(sched.slots) == 1


async def test_latency_brake_caps_the_plateau_and_releases(conn) -> None:
    """Our own slowness is a signal too: if our background requests are slow we
    are the noisy neighbour, whatever the sockets say.

    Two things to be careful asserting: the cap shows at the *plateau* (the ramp
    starts at one burner either way), and the note is emitted *once*, on the tick
    the brake engages, so notes must be collected across ticks."""
    for i in range(6):
        db.create_task(conn, chain_id="history", kind="done", generator="research",
                       title=f"history {i}")
    seed(conn, 6, generator="brainstorm")
    history = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE chain_id='history' ORDER BY created_at")]
    for tid, ms in zip(history, [5000, 5300, 5600, 5900, 6200, 6500], strict=True):
        conn.execute("UPDATE tasks SET ttft_ms=?, status='SUCCEEDED' WHERE id=?",
                     (ms, tid))

    det = FakeDetector()
    sched = Scheduler(cfg(max_concurrency=4, dry_run_stage_seconds=600), conn, det)
    assert sched._p95_ttft() == pytest.approx(6.5), "fixture is not what we think"

    plateau: list[int] = []
    notes: list[str] = []
    for _ in range(8):
        notes.extend((await sched.tick()).notes)
        plateau.append(len(sched.slots))
    assert plateau == [1, 2, 2, 2, 2, 2, 2, 2], f"ramp/cap: {plateau}"
    assert sum("brake" in n for n in notes) == 1, f"note not once: {notes}"

    conn.execute("UPDATE tasks SET ttft_ms=500")
    for _ in range(8):
        await sched.tick()
    assert len(sched.slots) == 4, f"brake never released: {len(sched.slots)}"


async def test_p95_needs_five_samples_or_it_stays_quiet(conn) -> None:
    det = FakeDetector()
    sched = Scheduler(cfg(), conn, det)
    assert sched._p95_ttft() is None, "a p95 of one sample is theatre"


async def test_tick_survives_a_broken_detector(conn) -> None:
    """One bad tick must not kill the daemon — launchd would restart it, but the
    events table would show nothing about why."""
    seed(conn, 1)

    class Exploding(FakeDetector):
        async def tick(self) -> detect.BusState:
            raise RuntimeError("ioreg ate it")

    sched = Scheduler(cfg(), conn, Exploding(), dry_run=True)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert any(r["type"] == "sched.error" for r in db.tail_events(conn))


async def test_run_loop_stops_promptly(conn) -> None:
    det = FakeDetector()
    sched = Scheduler(cfg(tick_seconds=0.02), conn, det, dry_run=True)
    stop = asyncio.Event()
    t0 = time.perf_counter()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(0.08)
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)
    assert time.perf_counter() - t0 < 0.5
    assert det.tick_count >= 2


async def test_summary_is_the_payload_the_ui_needs(conn) -> None:
    seed(conn, 2)
    det = FakeDetector()
    sched = Scheduler(cfg(), conn, det, dry_run=True)
    await sched.tick()
    s = sched.summary()
    assert s["dry_run"] is True
    assert s["queue"][db.QUEUED] >= 1
    assert s["runnable_now"] >= 1
    assert isinstance(s["slots"], list)
    assert "p95_ttft_s" in s and "cost_ms" in s
