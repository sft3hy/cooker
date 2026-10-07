"""Tests for M5: the rubric, the gate, the dedup, and the feedback loop.

The theme of this file is that a gate has to be able to fail. Every test here is
shaped as "here is the input that would make this a lie" — an unparsable judge, a
parked generator that keeps getting claimed, a database that never received the
column the digest reads — because a quality system that only ever reports success is
indistinguishable from one that is not checking.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cooker import db
from cooker import eval as ev
from cooker.config import Config


def cfg(tmp_path: Path, **over: object) -> Config:
    base: dict = {
        "quality": {"publish_threshold": 3.5, "dedup_window_days": 14,
                    "feedback_ema_floor": 2.5, "feedback_park_after": 5,
                    "park_parked_generators": True, "url_dedup_days": 7},
        "generators": {"research": {"enabled": True, "weight": 3},
                       "brainstorm": {"enabled": True, "weight": 2},
                       "digest": {"enabled": True, "weight": 1}},
        "daemon": {"data_dir": str(tmp_path)},
        "safety": {"outputs_dir": str(tmp_path / "outputs")},
    }
    for section, key in over.items():
        base.setdefault(section, {}).update(key)  # type: ignore[attr-defined]
    return Config(base, tmp_path)


def conn_fresh() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def artifact(conn: sqlite3.Connection, generator: str, *, score: float | None = None,
             task_id: str = "") -> str:
    """A published artifact row, so feedback has something to attach to."""
    aid = db.new_id()
    conn.execute(
        "INSERT INTO artifacts (id, task_id, generator, title, path, hash, score,"
        " scores_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, task_id or db.new_id(), generator, f"{generator} thing",
         "/tmp/x.md", "h" * 64, score, None, "PUBLISHED", db.now()))
    conn.commit()
    return aid


# --- the rubric ---------------------------------------------------------


def test_strict_json_is_the_trusted_shape() -> None:
    r = ev.parse_eval('{"usefulness":4,"accuracy":4,"novelty":3,"actionability":4,'
                      '"relevance":5,"rationale":"solid"}', threshold=3.5)
    assert r.parsed and r.overall == 4.0 and r.publishes
    assert r.rationale == "solid"


def test_a_fenced_block_is_recovered_not_refused() -> None:
    r = ev.parse_eval('Here is my assessment:\n```json\n{"usefulness":5,'
                      '"accuracy":5,"novelty":5,"actionability":5,"relevance":5}\n'
                      '```', threshold=3.5)
    assert r.parsed and r.overall == 5.0


def test_prose_axes_are_accepted_but_marked_as_a_fallback() -> None:
    r = ev.parse_eval("usefulness: 4, accuracy: 4, novelty: 2, actionability: 4,"
                      " relevance: 4", threshold=3.5)
    assert r.parsed and r.overall == 3.6
    assert "prose" in r.rationale


def test_a_missing_axis_is_a_reject_not_a_default() -> None:
    """No `accuracy` means accuracy was not judged. Assuming 3 there is the
    gate inventing a score it never got, and the artifact rides on it."""
    r = ev.parse_eval('{"usefulness":5,"accuracy":5,"novelty":5,"actionability":5}',
                      threshold=3.5)
    assert not r.parsed and r.verdict == "REJECT" and r.overall == 0.0


def test_garbage_rejects_and_keeps_the_raw_text() -> None:
    r = ev.parse_eval("I think it was pretty good!", threshold=3.5)
    assert not r.parsed and r.verdict == "REJECT"
    assert "pretty good" in r.raw


def test_scores_are_clamped_because_a_9_is_not_a_9() -> None:
    r = ev.parse_eval('{"usefulness":9,"accuracy":0,"novelty":3,"actionability":4,'
                      '"relevance":5}', threshold=3.5)
    assert r.scores == {"usefulness": 5, "accuracy": 1, "novelty": 3,
                        "actionability": 4, "relevance": 5}


def test_the_threshold_boundary_is_a_step_not_a_point() -> None:
    """Five integer axes means `overall` is always a multiple of 0.2, so a
    threshold of 3.5 never sits between two attainable scores: the effective gate
    is the smallest step at or above it, which is 3.6. Pinning the step keeps the
    number in config.yaml honest about what it actually rejects."""
    at_34 = ('{"usefulness":4,"accuracy":3,"novelty":3,"actionability":3,'
             '"relevance":4}')    # 4+3+3+3+4 = 17 → 3.4
    at_36 = ('{"usefulness":4,"accuracy":4,"novelty":4,"actionability":3,'
             '"relevance":3}')    # 4+4+4+3+3 = 18 → 3.6
    assert ev.parse_eval(at_34, threshold=3.4).publishes
    assert ev.parse_eval(at_34, threshold=3.4).overall == 3.4
    assert not ev.parse_eval(at_34, threshold=3.5).publishes
    assert ev.parse_eval(at_36, threshold=3.6).publishes
    assert ev.parse_eval(at_36, threshold=3.6).overall == 3.6
    assert not ev.parse_eval(at_36, threshold=3.7).publishes


def test_the_rationale_is_stored_not_re_derived(tmp_path: Path) -> None:
    conn = conn_fresh()
    t = db.create_task(conn, chain_id=db.new_id(), kind="evaluate",
                       generator="research", title="e")
    r = ev.parse_eval('{"usefulness":4,"accuracy":4,"novelty":4,"actionability":4,'
                      '"relevance":4,"rationale":"thin but correct"}', threshold=3.5)
    ev.record(conn, t.id, r)
    row = conn.execute("SELECT rationale, overall, verdict FROM evaluations"
                       " WHERE task_id=?", (t.id,)).fetchone()
    assert row["rationale"] == "thin but correct"
    assert row["overall"] == 4.0 and row["verdict"] == "PUBLISH"
    conn.close()


def test_migrate_adds_the_rationale_column_to_an_old_database(
        tmp_path: Path) -> None:
    """The database that has been running since M1 does not get new columns from
    a `CREATE TABLE IF NOT EXISTS`. Without this, `rationale` exists in every test
    and in a fresh checkout, and raises on the one machine that matters."""
    path = tmp_path / "cooker.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        scores_json TEXT NOT NULL, overall REAL NOT NULL, verdict TEXT NOT NULL,
        created_at REAL NOT NULL)""")
    old.execute("INSERT INTO evaluations (task_id, scores_json, overall, verdict,"
                " created_at) VALUES ('t','{}',4.0,'PUBLISH',1.0)")
    old.commit()
    old.close()

    c = cfg(tmp_path)
    assert c.db_path == path, "the test must reopen the same file the daemon uses"
    conn = db.connect(c.db_path)
    db.migrate(conn)
    assert "rationale" in {r["name"] for r in
                           conn.execute("PRAGMA table_info(evaluations)").fetchall()}
    # the row that predates the column survives with a NULL, not a rewrite
    row = conn.execute("SELECT overall, rationale FROM evaluations").fetchone()
    assert row["overall"] == 4.0 and row["rationale"] is None
    conn.close()


# --- dedup --------------------------------------------------------------


def test_the_hash_ignores_formatting_and_respects_the_question() -> None:
    h1 = ev.result_hash("why prefill?", "Prefill  is   atomic.\n- a\n")
    h2 = ev.result_hash("why prefill?", "prefill is atomic. - a")
    assert h1 == h2, "the same answer with different bullets is the same answer"
    h3 = ev.result_hash("why chunking?", "Prefill is atomic. - a")
    assert h1 != h3, "one paragraph answering two questions must stay two records"


def test_a_duplicate_is_found_before_it_is_published(tmp_path: Path) -> None:
    conn = conn_fresh()
    h = ev.result_hash("t", "body text")
    assert ev.find_duplicate(conn, h) is None
    ev.mark_result_seen(conn, h, "task-1")
    assert ev.find_duplicate(conn, h) == "task-1"
    assert ev.find_duplicate(conn, h, exclude_id="task-1") is None, \
        "an artifact must not be its own duplicate"


def test_a_recently_read_url_is_not_read_again(tmp_path: Path) -> None:
    conn = conn_fresh()
    url = "https://docs.example.com/prefill"
    assert not ev.url_already_seen(conn, url, days=7)
    ev.mark_result_seen(conn, "h" * 64, "task-1", url=url)
    assert ev.url_already_seen(conn, url, days=7)
    assert ev.url_already_seen(conn, "https://docs.example.com/prefill/", days=7), \
        "a trailing slash is not a different document"
    assert not ev.url_already_seen(conn, url, days=0), \
        "the window has to have an end"


# --- the feedback loop --------------------------------------------------


def test_one_grumpy_morning_does_not_switch_a_generator_off(
        tmp_path: Path) -> None:
    """Parking needs a sample. The first bad rating of a new generator is one
    person's Tuesday, not a trend, and a loop that starves itself on first contact
    teaches you nothing."""
    conn = conn_fresh()
    c = cfg(tmp_path)
    aid = artifact(conn, "brainstorm")
    ev.add_feedback(conn, aid, "bad")
    assert ev.parked_generators(c, conn) == {}, "parked after a single rating"
    assert "brainstorm" in ev.allowed_generators(c, conn)


def test_a_generator_that_stays_bad_gets_stopped(tmp_path: Path) -> None:
    conn = conn_fresh()
    c = cfg(tmp_path, quality={"feedback_park_after": 5})
    for _ in range(5):
        ev.add_feedback(conn, artifact(conn, "brainstorm"), "bad")
    parked = ev.parked_generators(c, conn)
    assert "brainstorm" in parked
    assert "brainstorm" not in (ev.allowed_generators(c, conn) or [])
    assert "research" in (ev.allowed_generators(c, conn) or [])


def test_the_recent_rating_carries_the_weight(tmp_path: Path) -> None:
    """Folded oldest-first with alpha 0.4, so one good rating after one bad lands
    on 0.4·5 + 0.6·1 = 2.6. The value is the proof of the ordering: folding the
    same two ratings newest-first gives 3.4 instead. 2.6 is still below the good
    rating, which is the point — one rating does not erase the previous one, it
    just moves the average toward the present."""
    conn = conn_fresh()
    early = artifact(conn, "digest")
    ev.add_feedback(conn, early, "bad")
    conn.execute("UPDATE feedback SET created_at = 1 WHERE artifact_id=?", (early,))
    later = artifact(conn, "digest")
    ev.add_feedback(conn, later, "good")
    conn.execute("UPDATE feedback SET created_at = 2 WHERE artifact_id=?", (later,))
    ema = ev.generator_ema(conn, "digest")
    assert ema == 2.6, f"expected oldest-first fold (2.6), got {ema}"
    assert ema > 1.0, "the improvement did not register at all"


def test_an_unjudged_generator_is_not_below_the_floor(tmp_path: Path) -> None:
    conn = conn_fresh()
    c = cfg(tmp_path, quality={"feedback_park_after": 0})
    assert ev.generator_ema(conn, "research") is None
    assert "research" in (ev.allowed_generators(c, conn) or [])


def test_rating_accepts_the_id_you_have(tmp_path: Path) -> None:
    """`cooker rate` takes whatever the digest printed. Making you go and find a
    different identifier is a command that does not work."""
    conn = conn_fresh()
    aid = artifact(conn, "research", task_id="task-9")
    out = ev.add_feedback(conn, aid, "good")
    assert out["generator"] == "research"
    out2 = ev.add_feedback(conn, "task-9", "meh")
    assert out2["generator"] == "research"
    try:
        ev.add_feedback(conn, aid, "wonderful")
        raise AssertionError("an unknown rating must be refused, not defaulted")
    except ValueError:
        pass


def test_a_parked_generator_is_not_claimed(tmp_path: Path) -> None:
    """The enforcement point. Everything else in the loop is a report; this is the
    SELECT that decides whether work happens, so a park that only shows up in
    `status` is decoration."""
    conn = conn_fresh()
    c = cfg(tmp_path, quality={"feedback_park_after": 2})
    for _ in range(2):
        ev.add_feedback(conn, artifact(conn, "brainstorm"), "bad")
    db.create_task(conn, chain_id=db.new_id(), kind="ideate", generator="brainstorm",
                   title="parked idea")
    allowed = ev.allowed_generators(c, conn)
    assert "brainstorm" not in (allowed or [])
    assert db.claim_next(conn, generators=allowed) is None, \
        "a parked generator was handed the GPU anyway"

    # and the other generator still runs, so the park is a brake and not a stop
    db.create_task(conn, chain_id=db.new_id(), kind="note", generator="research",
                   title="live work")
    got = db.claim_next(conn, generators=allowed)
    assert got is not None and got.generator == "research"


def test_a_manual_park_is_honoured_and_readable_the_next_morning(
        tmp_path: Path) -> None:
    conn = conn_fresh()
    c = cfg(tmp_path)
    assert "research" in (ev.allowed_generators(c, conn) or [])
    db.set_meta(conn, "parked_generators", json.dumps({"research": "on holiday"}))
    parked = ev.parked_generators(c, conn)
    assert parked.get("research") == "on holiday"
    assert "research" not in (ev.allowed_generators(c, conn) or [])


def test_nothing_declared_is_unfiltered_not_empty(tmp_path: Path) -> None:
    """`generators: {}` must not freeze the queue. `claim_next([])` would compile
    to `IN ()` and match nothing, so an undeclared config reports `None` and the
    claim stays unfiltered."""
    sparse = Config({"quality": {"publish_threshold": 3.5},
                     "daemon": {"data_dir": str(tmp_path)}}, tmp_path)
    conn = conn_fresh()
    assert ev.allowed_generators(sparse, conn) is None
    db.create_task(conn, chain_id=db.new_id(), kind="note", generator="research",
                   title="still runs")
    assert db.claim_next(conn, generators=ev.allowed_generators(sparse, conn)) \
        is not None


def test_state_reports_the_loop_in_one_object(tmp_path: Path) -> None:
    conn = conn_fresh()
    c = cfg(tmp_path, quality={"feedback_park_after": 1})
    ev.add_feedback(conn, artifact(conn, "brainstorm"), "bad")
    st = ev.state(conn, c)
    assert st["threshold"] == 3.5
    assert "brainstorm" in st["parked"]
    assert "brainstorm" not in st["allowed"]
    assert st["ema"]["brainstorm"] == 1.0


def test_the_json_in_the_digest_is_the_json_that_was_judged(tmp_path: Path) -> None:
    """A regression guard against the digest inventing scores: the number it prints
    is read back from the row the evaluator wrote, byte for byte."""
    conn = conn_fresh()
    t = db.create_task(conn, chain_id=db.new_id(), kind="evaluate",
                       generator="research", title="e")
    r = ev.parse_eval('{"usefulness":4,"accuracy":3,"novelty":4,"actionability":4,'
                      '"relevance":4,"rationale":"ok"}', threshold=3.5)
    ev.record(conn, t.id, r)
    row = conn.execute("SELECT scores_json FROM evaluations WHERE task_id=?",
                       (t.id,)).fetchone()
    assert json.loads(row["scores_json"]) == r.scores
    assert json.loads(row["scores_json"])["accuracy"] == 3
    conn.close()
