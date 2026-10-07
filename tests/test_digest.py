"""Tests for the daily digest.

The digest's most important sentence is the negative one. An empty file and a
polite day are the same bytes, and the difference between "the daemon never got a
turn" and "it cooked all night and nothing was good enough" is precisely the thing
you cannot infer from an absence — so every negative case here asserts on words.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from cooker import chains, db, digest, runner, safety
from cooker import eval as ev
from cooker.config import Config


def cfg(tmp_path: Path, **over: object) -> Config:
    base: dict = {
        "quality": {"publish_threshold": 3.5, "dedup_window_days": 14,
                    "feedback_ema_floor": 2.5, "feedback_park_after": 5,
                    "park_parked_generators": True, "url_dedup_days": 7},
        "generators": {"research": {"enabled": True, "weight": 3}},
        "daemon": {"data_dir": str(tmp_path)},
        "safety": {"outputs_dir": str(tmp_path / "outputs")},
    }
    for section, key in over.items():
        base.setdefault(section, {}).update(key)  # type: ignore[attr-defined]
    return Config(base, tmp_path)


def publish_something(c: Config, conn: sqlite3.Connection, *, topic: str = "prefill",
                      overall: float = 4.2, verdict: str = "PUBLISHED",
                      body: str = "Prefill is atomic on omlx today, and chunked "
                                  "prefill changes that by interleaving shorter "
                                  "requests with longer ones.") -> str:
    """One artifact on disk and in the database, with the evaluation that judged
    it, because a digest row without a file behind it is the lie this tests."""
    chain = chains.seed(c, conn, topic)
    day = chains.day_dir(c)
    target = day / "research" / f"{chains.slug(topic)}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    h = ev.result_hash(topic, body)
    t = db.create_task(conn, chain_id=chain, kind="publish", generator="research",
                       title=f"publish: {topic}")
    db.finish(conn, t.id, db.SUCCEEDED if verdict == "PUBLISHED" else db.REJECTED,
              result_path=str(target), result_hash=h, score=overall,
              input_tokens=800, output_tokens=120, duration_ms=4200, ttft_ms=310)
    # `day_cost` attributes GPU time by `started_at`, which the real path stamps at
    # claim. A fixture that skips the claim reports an empty cost and the digest
    # prints zeros that mean nothing.
    conn.execute("UPDATE tasks SET started_at=? WHERE id=?", (db.now(), t.id))
    conn.execute(
        "INSERT INTO artifacts (id, task_id, generator, title, path, hash, score,"
        " scores_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (db.new_id(), t.id, "research", f"publish: {topic}", str(target), h, overall,
         None, verdict, db.now()))
    axes = {a: max(1, min(5, round(overall))) for a in ev.RUBRIC}
    conn.execute(
        "INSERT INTO evaluations (task_id, scores_json, overall, verdict, rationale,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (t.id, json.dumps(axes), overall,
         "PUBLISH" if overall >= 3.5 else "REJECT",
         "sourced and concrete, one thin section", db.now()))
    conn.commit()
    return str(target)


def test_an_empty_day_says_so_in_words(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    text = digest.render(c, conn, date.today())
    assert "Nothing scored above the threshold" in text
    assert "stages run: 0" in text
    assert "correctly declined" in text or "queue was empty" in text, \
        "an idle day must distinguish polite idleness from a dead daemon"
    conn.close()


def test_the_digest_lands_where_the_layout_says(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn)
    path, n = digest.write(c, conn)
    assert path == chains.day_dir(c) / "digest" / "digest.md"
    assert path.exists() and n == 1
    assert json.loads(next(
        e["data_json"] for e in db.tail_events(conn)
        if e["type"] == "digest.written"))["published"] == 1
    conn.close()


def test_a_published_item_carries_its_score_rationale_and_path(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    target = publish_something(c, conn, topic="chunked prefill", overall=4.2)
    text = digest.render(c, conn, date.today())
    assert "Worth reading" in text
    assert "4.20" in text
    assert "evaluator: sourced and concrete" in text
    assert "digest/digest.md" not in target, "the digest should not cite itself"
    assert "cooker rate" in text, "the loop has to be reachable from the digest"
    assert "research" in text
    conn.close()


def test_rejected_work_is_listed_not_deleted(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn, topic="weak topic", overall=2.4, verdict="REJECTED")
    text = digest.render(c, conn, date.today())
    assert "Nothing scored above the threshold" in text
    assert "Cooked, not published" in text
    assert "weak topic" in text
    assert "2.40" in text
    conn.close()


def test_the_cost_line_is_read_from_the_database(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn, topic="a")
    publish_something(c, conn, topic="b", overall=3.8)
    cost = digest.day_cost(conn, date.today())
    assert cost["input_tokens"] == 1600 and cost["output_tokens"] == 240
    assert cost["gpu_seconds"] == 8.4
    text = digest.render(c, conn, date.today())
    assert "1,600 in / 240 out" in text
    assert "2 passed" in text
    conn.close()


def test_the_same_content_twice_is_one_line(tmp_path: Path) -> None:
    """Candidate and published copies share a hash; two rows for one artifact make
    a two-item morning read as four."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    target = publish_something(c, conn)
    conn.execute(
        "INSERT INTO artifacts (id, task_id, generator, title, path, hash, score,"
        " scores_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (db.new_id(), "other", "research", "candidate copy", target,
         conn.execute("SELECT hash FROM artifacts").fetchone()["hash"], 4.2, None,
         "CANDIDATE", db.now()))
    conn.commit()
    items = digest.day_artifacts(conn, date.today())
    assert len(items) == 1, f"duplicate rows: {len(items)}"
    conn.close()


def test_a_secret_in_an_artifact_is_redacted_in_the_digest(tmp_path: Path) -> None:
    """The artifact on disk is the operator's own file, but the digest is the thing
    that gets screenshotted and pasted into issues, so it is scanned on its way
    out regardless."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    body = ("Prefill is atomic. The key is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            " and that is the whole note.")
    publish_something(c, conn, body=body)
    path, _ = digest.write(c, conn)
    out = path.read_text()
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in out, "secret in digest"
    assert safety.scan_secrets(out).clean
    assert any(e["type"] == "safety.redaction" for e in db.tail_events(conn)), \
        "a redaction nobody recorded is a silent edit of the record"
    conn.close()


def test_a_parked_generator_is_named_in_the_digest(tmp_path: Path) -> None:
    c = cfg(tmp_path, quality={"feedback_park_after": 2})
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn)
    for _ in range(2):
        aid = conn.execute("SELECT id FROM artifacts LIMIT 1").fetchone()["id"]
        ev.add_feedback(conn, aid, "bad")
    text = digest.render(c, conn, date.today())
    assert "Parked" in text and "research" in text
    assert "unfiltered" not in text
    conn.close()


def test_the_rate_token_in_the_digest_actually_resolves(tmp_path: Path) -> None:
    """The failure this guards against was real and invisible until you tried it.

    ids here are `uuid7`, which is time-ordered: the leading 48 bits are the
    creation timestamp, so the first eight hex characters are identical for every
    artifact cooked in the same ~18 hours. A digest that prints `id[:8]` prints a
    token that matches everything from today and identifies nothing, and `rate`
    then refuses two-of-two on a two-item morning. The digest prints the random
    tail, so this reads the tokens back out of the rendered text and rates each one.
    """
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn, topic="chunked prefill", overall=4.4)
    publish_something(c, conn, topic="sprite palettes", overall=4.2)
    text = digest.render(c, conn, date.today())

    tokens = re.findall(r"cooker rate ([0-9a-f]{8}) good", text)
    assert len(tokens) == 2, f"expected a rate token per item: {tokens}"
    assert len(set(tokens)) == 2, "both artifacts printed the same token"

    landed = set()
    for tok in tokens:
        landed.add(ev.add_feedback(conn, tok, "good")["artifact_id"])
    assert len(landed) == 2, "both ratings landed on the same artifact"

    # the prefix form, which is the one that collides, is refused and not guessed
    prefix = conn.execute("SELECT id FROM artifacts LIMIT 1").fetchone()["id"][:8]
    try:
        ev.add_feedback(conn, prefix, "meh")
        raise AssertionError("an ambiguous prefix must be refused, not resolved")
    except ValueError as exc:
        assert "matches" in str(exc)
    conn.close()


def test_stage_prefixes_do_not_leak_into_titles(tmp_path: Path) -> None:
    """The queue puts `publish:` on every row. A digest that prints it is a digest
    where every headline starts with the same word."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn, topic="gpu scheduling", overall=2.2,
                     verdict="REJECTED")
    text = digest.render(c, conn, date.today())
    assert "publish: gpu scheduling" not in text
    assert "gpu scheduling" in text
    conn.close()


def test_the_digest_reads_the_chains_judgement_not_the_artifacts_own_task(
        tmp_path: Path) -> None:
    """The exact live failure this pins.

    `synthesize` owns the draft, `evaluate` owns the judgement, and they are
    different tasks in the same chain. Joining an artifact to evaluations by its
    own task_id finds nothing, so a draft the gate turned away at 2.40 was printed
    as "the evaluate stage did not run" — the digest claiming the gate never ran
    on the one thing the gate did judge.
    """
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "speculative decoding")
    day = chains.day_dir(c)
    target = day / "research" / "speculative-decoding.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# draft\n\nSpeculative decoding drafts tokens ahead and "
                      "verifies them in one pass, which is where the speed comes "
                      "from.\n", encoding="utf-8")
    draft = db.create_task(conn, chain_id=chain, kind="synthesize",
                           generator="research", title="synthesize: speculative decoding")
    db.finish(conn, draft.id, db.SUCCEEDED, result_path=str(target),
              result_hash="k" * 64)
    conn.execute("UPDATE tasks SET started_at=? WHERE id=?", (db.now(), draft.id))
    runner.register_artifact(conn, draft, str(target), "k" * 64, status="CANDIDATE")

    ev_task = db.create_task(conn, chain_id=chain, kind="evaluate",
                             generator="research", title="evaluate: speculative decoding",
                             dependencies=[draft.id])
    db.finish(conn, ev_task.id, db.SUCCEEDED, score=2.4)
    ev.record(conn, ev_task.id, ev.EvalResult(
        scores={"usefulness": 2, "accuracy": 3, "novelty": 1, "actionability": 2,
                "relevance": 4}, overall=2.4, verdict="REJECT",
        rationale="leans on one source and goes no further"))
    conn.commit()

    items = digest.day_artifacts(conn, date.today())
    assert len(items) == 1
    assert items[0]["overall"] == 2.4, "the chain's score did not reach its artifact"
    assert items[0]["verdict"] == "REJECT"

    text = digest.render(c, conn, date.today())
    assert "Cooked, not published" in text
    assert "2.40" in text
    assert "evaluator: leans on one source" in text
    assert "never judged" not in text, "the digest claims the gate never ran"
    assert "Nothing scored above the threshold" in text
    conn.close()


def test_yesterday_is_not_today(tmp_path: Path) -> None:
    """The digest is per-day. A digest that ignores its date argument reports
    yesterday's work as today's, which is the kind of wrong that nobody notices
    until the numbers stop adding up."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    publish_something(c, conn)
    today = digest.day_artifacts(conn, date.today())
    assert len(today) == 1
    yesterday = digest.day_artifacts(conn, date.fromordinal(
        date.today().toordinal() - 1))
    assert yesterday == [], "yesterday's digest contains today's work"
    conn.close()


def test_topics_md_prose_is_not_a_research_topic(tmp_path: Path) -> None:
    """The file is allowed to explain itself, and the parser must know the
    difference. The live topics.md carried six lines of help text ahead of the
    real backlog, and the first thing the daemon would ever research was
    "`#` lines are comments"."""
    (tmp_path / "topics.md").write_text(
        "# Topics\n"
        "\n"
        "One topic per line (`-` bullets and bare lines both work).\n"
        "Delete a line to withdraw a topic; add one to queue it.\n"
        "\n"
        "## Backlog\n"
        "\n"
        "- why does a long prefill block other requests\n"
        "- traefik middlewares worth running\n",
        encoding="utf-8")
    c = cfg(tmp_path, topics={"file": str(tmp_path / "topics.md")})
    ts = chains.read_topics(c)
    assert ts == ["why does a long prefill block other requests",
                  "traefik middlewares worth running"], ts
    assert not any("`" in t or "topic per line" in t for t in ts), ts


def test_a_bare_lines_topics_file_still_works(tmp_path: Path) -> None:
    """No bullets at all means bare lines are the topics: the plainest possible
    file must still be readable, or the fix has just moved the bug."""
    (tmp_path / "topics.md").write_text(
        "why does a long prefill block other requests\n"
        "what a NAS gets wrong about backups\n", encoding="utf-8")
    c = cfg(tmp_path, topics={"file": str(tmp_path / "topics.md")})
    assert len(chains.read_topics(c)) == 2
