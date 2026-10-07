"""M1 DoD: the queue is a real DAG, claiming is atomic, and nothing is lost."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from cooker import db


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "cooker.db")
    assert db.migrate(c) is True
    yield c
    c.close()


def test_schema_reapplies_idempotently(conn):
    assert db.migrate(conn) is False
    conn.execute("SELECT COUNT(*) FROM tasks").fetchone()


def test_claim_respects_dependency_gate(conn):
    """Fan out extract x3, fan in synthesize. Nothing downstream is claimable
    until every upstream stage has SUCCEEDED."""
    chain = db.new_id()
    plan = db.create_task(conn, chain_id=chain, kind="plan", generator="research",
                          title="plan the sweep")
    assert db.claim_next(conn).id == plan.id  # only the root is runnable

    extracts = [
        db.create_task(conn, chain_id=chain, kind="extract", generator="research",
                       title=f"extract source {i}", dependencies=[plan.id], priority=400)
        for i in range(3)
    ]
    synth = db.create_task(conn, chain_id=chain, kind="synthesize", generator="research",
                           title="synthesize", dependencies=[t.id for t in extracts])

    # plan still RUNNING? no — we claimed it but never finished it.
    assert db.claim_next(conn) is None, "extracts blocked on an unfinished plan"

    db.finish(conn, plan.id, db.SUCCEEDED)
    # now all three extracts are runnable, synth is not
    for _ in range(3):
        got = db.claim_next(conn)
        assert got is not None and got.kind == "extract"
        assert synth.id not in (got.id,)
    assert db.claim_next(conn) is None, "synthesize must not run before its deps"

    for t in extracts:
        assert db.claim_next(conn) is None
        db.finish(conn, t.id, db.SUCCEEDED)

    got = db.claim_next(conn)
    assert got is not None and got.id == synth.id


def test_concurrent_claims_never_hand_out_the_same_task(conn):
    """N threads race for M tasks: every claim is unique, none is double-served."""
    chain = db.new_id()
    ids = {
        db.create_task(conn, chain_id=chain, kind="extract", generator="research",
                       title=f"t{i}", priority=500).id
        for i in range(40)
    }

    claimed: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker():
        c = db.connect(conn_path)
        try:
            while True:
                t = db.claim_next(c)
                if t is None:
                    break
                with lock:
                    claimed.append(t.id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            c.close()

    # the fixture's connection holds no write lock between statements; share the
    # file path so threads contend for real
    conn_path = conn.execute("PRAGMA database_list").fetchone()[2]

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(claimed) == 40, f"expected 40 claims, got {len(claimed)}"
    assert len(set(claimed)) == 40, f"duplicate claims: {claimed}"
    assert set(claimed) == ids
    left = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='RUNNING'").fetchone()[0]
    assert left == 40


def test_preemption_requeues_with_backoff_and_survives_restart(conn, tmp_path):
    chain = db.new_id()
    t = db.create_task(conn, chain_id=chain, kind="synthesize", generator="research",
                       title="big synthesis")
    got = db.claim_next(conn)
    assert got and got.id == t.id and got.attempts == 1

    db.requeue(conn, t.id, reason="ACTIVE_INFER", backoff_seconds=0.4, preempted=True)
    assert db.claim_next(conn) is None, "must respect not_before"

    time.sleep(0.45)
    again = db.claim_next(conn)
    assert again and again.id == t.id and again.attempts == 2
    assert again.preemptions == 1

    # crash now: row is RUNNING. A fresh daemon must recover it to QUEUED.
    conn.close()
    c2 = db.connect(tmp_path / "cooker.db")
    assert db.recover_orphans(c2) == 1
    row = c2.execute("SELECT status, attempts FROM tasks WHERE id=?", (t.id,)).fetchone()
    assert row["status"] == db.QUEUED and row["attempts"] == 2
    c2.close()


def test_failure_parks_downstream(conn):
    chain = db.new_id()
    a = db.create_task(conn, chain_id=chain, kind="fetch", generator="research", title="fetch")
    b = db.create_task(conn, chain_id=chain, kind="extract", generator="research",
                       title="extract", dependencies=[a.id])
    db.finish(conn, a.id, db.FAILED, error="404")
    assert db.propagate_blocked(conn) == 1
    assert db.claim_next(conn) is None
    assert b.status == db.QUEUED
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (b.id,)).fetchone()
    assert row["status"] == db.PARKED


def test_missing_dependency_blocks_not_claims(conn):
    chain = db.new_id()
    db.create_task(conn, chain_id=chain, kind="synthesize", generator="research",
                   title="orphan", dependencies=["does-not-exist"])
    assert db.claim_next(conn) is None
    assert db.propagate_blocked(conn) == 1


def test_priority_ordering(conn):
    chain = db.new_id()
    low = db.create_task(conn, chain_id=chain, kind="x", generator="digest", title="low",
                         priority=900)
    high = db.create_task(conn, chain_id=chain, kind="x", generator="digest", title="high",
                          priority=100)
    assert db.claim_next(conn).id == high.id
    assert db.claim_next(conn).id == low.id


def test_generator_filter(conn):
    chain = db.new_id()
    db.create_task(conn, chain_id=chain, kind="x", generator="research", title="r")
    assert db.claim_next(conn, generators=["digest"]) is None
    assert db.claim_next(conn, generators=["research"]).generator == "research"


def test_events_are_replayable_by_cursor(conn):
    chain = db.new_id()
    t = db.create_task(conn, chain_id=chain, kind="plan", generator="research", title="hi")
    db.emit(conn, "state.change", message="IDLE", data={"workers": 1})
    rows = db.tail_events(conn, after=0)
    cursor = rows[-1]["id"]
    assert any(r["task_id"] == t.id for r in rows)

    db.emit(conn, "state.change", message="ACTIVE_INFER", severity="warn")
    new = db.tail_events(conn, after=cursor)
    assert len(new) == 1 and new[0]["message"] == "ACTIVE_INFER"


def test_queue_counts_and_eta(conn):
    chain = db.new_id()
    assert db.next_runnable_eta(conn) is None
    assert db.runnable_now(conn) == 0

    db.create_task(conn, chain_id=chain, kind="x", generator="research", title="a",
                   not_before=time.time() + 3600)
    counts = db.queue_counts(conn)
    assert counts[db.QUEUED] == 1
    eta = db.next_runnable_eta(conn)
    assert eta and eta > time.time() + 3500
    assert db.runnable_now(conn) == 0, "still in backoff"


def test_eta_ignores_dependency_blocked_stages(conn):
    """A stage waiting on a dependency has no timer. Reporting one is how the
    HUD starts lying about when work will resume."""
    chain = db.new_id()
    db.create_task(conn, chain_id=chain, kind="synthesize", generator="research",
                   title="blocked forever", dependencies=["missing"])
    assert db.runnable_now(conn) == 0
    assert db.next_runnable_eta(conn) is None, "dep-blocked is not a backoff"


def test_immediately_runnable_reports_zero_wait(conn):
    chain = db.new_id()
    db.create_task(conn, chain_id=chain, kind="x", generator="research", title="now")
    assert db.runnable_now(conn) == 1
    assert db.next_runnable_eta(conn) == 0.0
