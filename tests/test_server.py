"""Tests for the kitchen API (M7).

The rule the whole file obeys: the API is a projection of the database, so
every test seeds the database the server actually opens (`cfg.db_path`, not a
private in-memory one — the landmine test_cli.py documents) and asserts on what
comes back through HTTP. No live detector is required for any of it: that is
the point of a projection. Where honesty about *not* being live is the thing
under test — the `stale` flag — that too is asserted directly.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cooker import db, digest, runner, server
from cooker import eval as eval_mod
from cooker.config import Config


def cfg(tmp_path: Path, **over: object) -> Config:
    base: dict = {
        "daemon": {"data_dir": str(tmp_path), "bind_host": "127.0.0.1",
                   "port": 8256},
        "safety": {"outputs_dir": str(tmp_path / "outputs"), "max_disk_gb": 2.0},
        "quality": {"publish_threshold": 3.5, "dedup_window_days": 14,
                    "feedback_ema_floor": 2.5, "feedback_park_after": 5,
                    "park_parked_generators": True, "url_dedup_days": 7},
        "generators": {g: {"enabled": True, "weight": 1} for g in server.BURNERS},
        "topics": {"file": str(tmp_path / "topics.md"),
                   "fallback": ["a fallback subject"]},
        "detect": {"max_stage_seconds": 180},
        "scheduler": {"max_concurrency": 1},
        "ui": {"sfx_default": "off"},
    }
    for section, key in over.items():
        base.setdefault(section, {}).update(key)  # type: ignore[attr-defined]
    return Config(base, tmp_path)


def conn_at(c: Config) -> sqlite3.Connection:
    conn = db.connect(c.db_path)
    db.migrate(conn)
    return conn


@pytest.fixture
def client(tmp_path: Path):
    c = cfg(tmp_path)
    app = server.create_app(c)   # detached on purpose: the stale path matters
    with TestClient(app) as cl:
        yield cl, c, conn_at(c)


def test_health_and_root_without_build(client):
    cl, _, _ = client
    r = cl.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["dry_run"] is None  # detached: unknown, not False


def test_state_shape_is_the_cli_shape_plus_the_kitchen(client):
    cl, _, conn = client
    db.emit(conn, "bus.state", message="IDLE -> IDLE: quiet",
            data={"from": "IDLE", "to": "IDLE"})
    r = cl.get("/api/state")
    assert r.status_code == 200
    d = r.json()
    # the tested `cooker status --json` keys are embedded verbatim
    for k in ("counts", "backoff", "parked_stages", "today", "generators",
              "disk", "topics", "paused", "next_runnable_s"):
        assert k in d, k
    # and the kitchen extras
    assert len(d["pots"]) == len(server.BURNERS)
    assert d["gate"]["stale"] is True  # detached must say so, never guess
    assert d["workers"]["dry_run"] is None


def test_pot_states_come_from_rows_not_opinions(client):
    cl, c, conn = client
    # a published artifact today => plated
    task = db.create_task(conn, chain_id=db.new_id(),
                          kind="synthesize", generator="research",
                          title="why prefill blocks")
    out = c.outputs_dir / "research.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# plated\n")
    db.finish(conn, task.id, db.SUCCEEDED, result_path=str(out),
              result_hash="hash1")
    runner.register_artifact(conn, task, str(out), "hash1",
                             status=db.PUBLISHED)
    # a queued stage elsewhere => waiting
    db.create_task(conn, chain_id=db.new_id(), kind="generate",
                   generator="brainstorm", title="ideas for the stove")
    pots = {p["generator"]: p for p in cl.get("/api/state").json()["pots"]}
    assert pots["research"]["state"] == "plated"
    assert pots["research"]["today"]["plated"] == 1
    assert pots["brainstorm"]["state"] == "waiting"
    assert pots["deep-dive"]["state"] == "cold"


def test_taped_beats_everything(client):
    """A parked generator is taped off even if it has a published plate."""
    cl, _, conn = client
    eval_mod.park(conn, "research", reason="because")
    pots = {p["generator"]: p for p in cl.get("/api/state").json()["pots"]}
    assert pots["research"]["state"] == "taped"


def test_preempted_smoke_and_backoff(client):
    cl, _, conn = client
    now = time.time()
    t = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                       generator="research", title="boiling")
    conn.execute("UPDATE tasks SET status='QUEUED', not_before=? WHERE id=?",
                 (now + 47.0, t.id))
    conn.commit()
    db.emit(conn, "llm.abort", message="dropped for interactive",
            generator="research")
    pot = {p["generator"]: p
           for p in cl.get("/api/state").json()["pots"]}["research"]
    assert pot["state"] == "preempted"
    assert 40 < pot["backoff_s"] <= 47


def test_running_pot_carries_stage_and_progress(client):
    cl, _, conn = client
    db.create_task(conn, chain_id=db.new_id(), kind="plan",
                       generator="research", title="queries for prefill")
    db.claim_next(conn)  # QUEUED -> RUNNING, the real path
    pot = {p["generator"]: p
           for p in cl.get("/api/state").json()["pots"]}["research"]
    assert pot["state"] == "filling"        # plan is ingredients, not boil
    assert pot["running"]["kind"] == "plan"
    assert pot["progress"] == {"done": 0, "seen": 1}


def test_events_sse_replays_by_id(client):
    cl, _, conn = client
    first = db.emit(conn, "daemon.start", message="up")
    second = db.emit(conn, "runner.stage", message="plated something",
                     generator="research")
    body = cl.get(f"/api/events?after={first - 1}&max_frames=2").text
    assert f"id: {second}" in body
    assert "event: runner.stage" in body
    assert "plated something" in body
    assert "event: end" in body  # bounded: curl and tests can finish


def test_artifact_by_tail_ref_and_refusals(client):
    cl, c, conn = client
    task = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                          generator="research", title="prefill fairness")
    out = c.outputs_dir / "art.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# honest markdown\n")
    runner.register_artifact(conn, task, str(out), "h", status=db.PUBLISHED)
    full = str(task.id)
    tail = eval_mod.short_id(full)
    assert cl.get(f"/api/artifacts/{tail}").text.startswith("# honest")
    assert cl.get(f"/api/artifacts/{full}").status_code == 200
    assert cl.get("/api/artifacts/nothing9").status_code == 404
    # a row whose path wanders outside outputs_dir is refused, not read
    conn.execute("UPDATE artifacts SET path=? WHERE task_id=?",
                 (str(Path.home() / "secret.md"), task.id))
    conn.commit()
    assert cl.get(f"/api/artifacts/{tail}").status_code == 403


def test_metrics_and_digest(client):
    cl, c, _conn = client
    p = digest.digest_path(c, date.today())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("digest\n")
    m = cl.get("/api/metrics").json()
    assert m["today"]["stages"] == 0
    s = cl.get("/api/state").json()
    assert s["digest"]["written"] is True


def test_collect_pots_uses_the_burners_in_order(client):
    cl, _, _ = client
    pots = cl.get("/api/state").json()["pots"]
    assert [p["generator"] for p in pots] == list(server.BURNERS)
    assert [p["burner"] for p in pots] == list(range(6))
