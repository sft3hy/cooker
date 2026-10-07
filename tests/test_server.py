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


def test_products_lists_the_shelves(client):
    """The pantry tab's endpoint: every artifact, joined to its stage kind,
    with byte sizes — a shelf that can say what is on it."""
    cl, c, conn = client
    task = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                          generator="research", title="synthesize: nas backups")
    out = c.outputs_dir / "article.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# jar of facts\n")
    runner.register_artifact(conn, task, str(out), "h", status=db.PUBLISHED)
    p = cl.get("/api/products").json()
    assert p["counts"] == {"published": 1, "candidate": 0}
    got = p["products"][0]
    assert got["kind"] == "synthesize"
    assert got["bytes"] == len("# jar of facts\n")
    assert got["status"] == db.PUBLISHED
    # and the id it hands out round-trips straight back through the reader
    assert cl.get(f"/api/artifacts/{got['id']}").text.startswith("# jar")


def test_stats_books_the_day(client):
    cl, _c, conn = client
    now = time.time()
    task = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                          generator="research", title="synthesize: chunked prefill")
    conn.execute(
        "UPDATE tasks SET status='SUCCEEDED', started_at=?, finished_at=?,"
        " duration_ms=5000, input_tokens=300, output_tokens=1200,"
        " ttft_ms=95, preemptions=1 WHERE id=?", (now - 5, now, task.id))
    conn.commit()
    s = cl.get("/api/stats").json()
    day = s["days"][-1]
    assert day["stages"] == 1
    assert day["output_tokens"] == 1200 and day["input_tokens"] == 300
    assert day["gpu_seconds"] == 5.0 and day["preemptions"] == 1
    gen = s["generators"][0]
    assert gen["generator"] == "research" and gen["stages"] == 1


def test_stats_published_counts_from_artifacts_not_the_cache(client):
    """The books are honest even when the rollup cache missed the publish."""
    cl, c, conn = client
    now = time.time()
    task = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                          generator="brainstorm", title="brainstorm: ideas")
    out = c.outputs_dir / "ideas.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("ideas\n")
    runner.register_artifact(conn, task, str(out), "h", status=db.PUBLISHED)
    conn.execute("UPDATE tasks SET started_at=?, finished_at=?"
                 " WHERE id=?", (now - 2, now, task.id))
    conn.commit()  # no daily_stats rollup written at all
    day = cl.get("/api/stats").json()["days"][-1]
    assert day["published"] == 1, "published is truth, not cache"


def test_digest_endpoint(client):
    cl, c, _conn = client
    assert cl.get("/api/digest").status_code == 404
    assert cl.get("/api/digest?date=nope").status_code == 400
    p = digest.digest_path(c, date.today())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# today's menu\n")
    assert cl.get("/api/digest").text.startswith("# today's menu")


def test_orders_counter_takes_research_and_deep_dives(client):
    cl, _c, conn = client
    r = cl.post("/api/orders", json={"topic": "why does zsh start slow",
                                       "kind": "research"})
    assert r.status_code == 200 and r.json()["ok"]
    r2 = cl.post("/api/orders", json={"topic": "deep-dive: nas snapshot "
                                                 "scheduling for dummies",
                                        "kind": "deep-dive"})
    assert r2.status_code == 200
    kinds = {o["kind"] for o in cl.get("/api/orders").json()["orders"]}
    assert kinds == {"research", "deep-dive"}
    head = conn.execute("SELECT kind, generator FROM tasks ORDER BY priority,"
                        " created_at LIMIT 4").fetchall()
    assert ("delegate", "deep-dive") in [(h["kind"], h["generator"])
                                           for h in head]


def test_orders_accept_lengthy_subjects_cap_only_at_the_knob(client):
    """the owner writes lengthy orders (§ orders amendment): 200 was a
    guess, the ceiling is now a registered knob, and only the knob refuses."""
    cl, _c, _conn = client
    segment = ("why do dotfiles deserve their own manager, considering bare "
               "git repos, stow symlink farms, chezmoi templating, and the "
               "Nix home-manager declarative approach — with a recommendation "
               "for a mixed macOS/Linux household and notes on migrating a "
               "ten-year-old .zshrc without breaking anything; ")
    long_topic = segment * 12  # ~1900 chars, spaces intact
    assert len(long_topic) > 800
    r = cl.post("/api/orders", json={"topic": long_topic,
                                       "kind": "deep-dive"})
    assert r.status_code == 200, "lengthy orders are the point"
    seen = cl.get("/api/orders").json()["orders"]
    assert any(o["topic"].startswith("why do dotfiles") and
               len(o["topic"]) > 800 for o in seen), \
        "the full topic comes back on the ticket, not a truncation"
    over = "x" * 21000
    blocked = cl.post("/api/orders", json={"topic": over,
                                            "kind": "research"})
    assert blocked.status_code == 400 and "8-20000" in blocked.json()["error"]


def test_orders_are_validated_rate_limited_and_pause_respecting(client):
    cl, _c, conn = client
    assert cl.post("/api/orders", json={"topic": "short"}).status_code == 400
    assert cl.post("/api/orders",
                   json={"topic": "long enough to be a real subject",
                        "kind": "podcasts"}).status_code == 400
    for i in range(3):
        assert cl.post("/api/orders",
                       json={"topic": f"a subject worth researching {i}",
                            "kind": "research"}).status_code == 200
    fourth = cl.post("/api/orders",
                     json={"topic": "a subject worth researching 99",
                          "kind": "research"})
    assert fourth.status_code == 429, "the counter caps pending orders loudly"
    db.set_meta(conn, "paused", "sam is presenting")
    blocked = cl.post("/api/orders",
                      json={"topic": "approved after the pause lifts here",
                           "kind": "deep-dive"})
    assert blocked.status_code == 409, "an order never unlocks a closed kitchen"
