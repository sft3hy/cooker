"""Tests for the CLI surface (M6).

Two rules govern this file.

Nothing here scrapes a real terminal or a real omlx: the commands are called with a
Namespace, exactly as argparse would call them, and output is captured. That keeps
the assertions about *content* rather than about whether the network cooperated.

And the formatting guards are not vanity. `status` printing `None`, or an ETA of
zero for work that has no ETA, is the machine telling you something it does not
know — the same category of error as a score read off a truncated draft, just in a
place where people trust it more because it is in a table.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from cooker import chains, cli, db
from cooker import eval as ev
from cooker.config import Config


def cfg(tmp_path: Path, **over: object) -> Config:
    base: dict = {
        "daemon": {"data_dir": str(tmp_path), "bind_host": "127.0.0.1",
                   "port": 8256,
                   "launchd_label": "com.homelab.cooker.test"},
        "safety": {"outputs_dir": str(tmp_path / "outputs"), "max_disk_gb": 2.0},
        "quality": {"publish_threshold": 3.5, "dedup_window_days": 14,
                    "feedback_ema_floor": 2.5, "feedback_park_after": 5,
                    "park_parked_generators": True, "url_dedup_days": 7},
        "generators": {"research": {"enabled": True, "weight": 3},
                       "brainstorm": {"enabled": True, "weight": 2}},
        "topics": {"file": str(tmp_path / "topics.md"),
                   "fallback": ["a fallback subject"], "max_live_research": 2},
        "inference": {"max_prefill_tokens_per_request": 1000,
                     "stage_max_tokens": {"evaluate": 320}},
        "research": {"evaluate_excerpt_chars": 2400, "max_queries": 1,
                     "max_sources": 2, "source_context_chars": 1200},
        "detect": {"omlx_port": 8000},
    }
    for section, key in over.items():
        base.setdefault(section, {}).update(key)  # type: ignore[attr-defined]
    return Config(base, tmp_path)


def conn_fresh() -> sqlite3.Connection:
    """A scratch database nothing else can see."""
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def conn_at(c: Config) -> sqlite3.Connection:
    """The database the CLI commands open themselves.

    `cmd_list`, `cmd_show` and `cmd_digest` take no connection argument: they open
    `cfg.db_path`. Seeding an in-memory database and then calling them tests a
    database they never read, and fails with "nothing matches" — which is what
    three of these tests did on their first run.
    """
    conn = db.connect(c.db_path)
    db.migrate(conn)
    return conn


def ns(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def publish(c: Config, conn: sqlite3.Connection, topic: str, overall: float) -> str:
    """An artifact with its chain's judgement, written through the real helpers."""
    chain = chains.seed(c, conn, topic)
    day = chains.day_dir(c) / "research"
    day.mkdir(parents=True, exist_ok=True)
    target = day / f"{chains.slug(topic)}.md"
    body = f"# {topic}\n\nA paragraph long enough to be worth excerpting here.\n"
    target.write_text(body, encoding="utf-8")
    h = ev.result_hash(topic, body)
    synth = db.create_task(conn, chain_id=chain, kind="synthesize",
                          generator="research", title=f"synthesize: {topic}")
    db.finish(conn, synth.id, db.SUCCEEDED, result_path=str(target), result_hash=h,
              input_tokens=800, output_tokens=1200, duration_ms=5000, ttft_ms=250)
    cli_mod = __import__("cooker.runner", fromlist=["x"])
    cli_mod.register_artifact(conn, synth, str(target), h, status=db.CANDIDATE)
    gate = db.create_task(conn, chain_id=chain, kind="evaluate", generator="research",
                         title=f"evaluate: {topic}", dependencies=[synth.id])
    db.finish(conn, gate.id, db.SUCCEEDED, score=overall)
    axes = {a: max(1, min(5, round(overall))) for a in ev.RUBRIC}
    verdict = "PUBLISH" if overall >= 3.5 else "REJECT"
    ev.record(conn, gate.id, ev.EvalResult(
        scores=axes, overall=overall, verdict=verdict,
        rationale="specific and sourced, one thin middle", draft_chars=len(body),
        evaluated_chars=len(body)))
    if verdict == "PUBLISH":
        pub = db.create_task(conn, chain_id=chain, kind="publish",
                             generator="research", title=f"publish: {topic}",
                             dependencies=[gate.id])
        db.finish(conn, pub.id, db.SUCCEEDED, result_path=str(target),
                  result_hash=h)
        cli_mod.promote_candidate(conn, pub, str(target), h, overall)
    return str(target)


# --- status -------------------------------------------------------------


def test_status_on_an_empty_database_says_empty_not_none(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = conn_fresh()
    d = cli.collect_status(c, conn)
    assert d["next_runnable_s"] is None, "an empty queue has no ETA, not a zero one"
    text = cli.render_status(c, d)
    for bad in ("None", "nan", "{", "}", "[dict"):
        assert bad not in text, f"status leaked {bad!r}:\n{text}"
    assert "empty" in text
    assert "topics.md is empty" in text, "an empty backlog must be named"
    conn.close()


def test_status_names_backoff_instead_of_hiding_it_in_a_count(tmp_path: Path) -> None:
    """"3 queued" is the number; "3 waiting 42s after 2 preemptions" is the
    situation, and only the second one tells you whether to do anything."""
    c = cfg(tmp_path)
    conn = conn_fresh()
    t = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                       generator="research", title="in backoff")
    conn.execute("UPDATE tasks SET not_before=?, preemptions=? WHERE id=?",
                 (db.now() + 42.0, 2, t.id))
    conn.commit()
    d = cli.collect_status(c, conn)
    assert len(d["backoff"]) == 1
    b = d["backoff"][0]
    assert b["generator"] == "research" and b["kind"] == "synthesize"
    assert 30 < b["remaining_s"] <= 42.5, b
    assert b["preemptions"] == 2
    text = cli.render_status(c, d)
    assert "backoff" in text and "2 preemption" in text
    assert "42s" in text or "41s" in text
    conn.close()


def test_paused_is_the_first_thing_you_see(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = conn_fresh()
    db.set_meta(conn, "paused", "sam is presenting")
    text = cli.render_status(c, cli.collect_status(c, conn))
    body = text.splitlines()[2:]  # past the title and the rule
    assert body and "PAUSED" in body[0], \
        f"a paused daemon must lead the panel:\n{text}"
    assert "sam is presenting" in text
    conn.close()


def test_status_shows_the_loop_and_the_park_reason(tmp_path: Path) -> None:
    c = cfg(tmp_path, quality={"feedback_park_after": 2})
    conn = conn_fresh()
    publish(c, conn, "gpu scheduling", 4.2)
    aid = conn.execute("SELECT id FROM artifacts").fetchone()["id"]
    for _ in range(2):
        ev.add_feedback(conn, aid, "bad")
    d = cli.collect_status(c, conn)
    assert "research" in d["generators"]["parked"]
    text = cli.render_status(c, d)
    assert "PARKED" in text and "research" in text
    assert "ema" in text.lower()
    conn.close()


def test_status_disk_and_digest_paths_are_the_real_ones(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = conn_fresh()
    publish(c, conn, "chunked prefill", 4.4)
    d = cli.collect_status(c, conn)
    assert d["disk"]["bytes"] > 0, "an artifact was written and the disk says zero"
    assert d["today"]["published"] == 1
    assert Path(d["digest"]).parent.name == "digest"
    text = cli.render_status(c, d)
    assert "MB" in text
    conn.close()


def test_status_json_is_the_payload_the_ui_will_read(tmp_path: Path) -> None:
    """The --json shape is what the kitchen's SSE feed will carry in M7, so it is
    asserted as an interface, not as a debugging convenience."""
    c = cfg(tmp_path)
    conn = conn_fresh()
    d = cli.collect_status(c, conn)
    blob = json.dumps(d, default=str)
    back = json.loads(blob)
    for key in ("counts", "backoff", "parked_stages", "generators", "today",
                "disk", "topics", "next_runnable_s"):
        assert key in back, f"{key} missing from the status payload"
    assert back["today"]["stages"] == 0
    conn.close()


# --- list and show -----------------------------------------------------


def test_list_prints_ids_that_resolve(tmp_path: Path, capsys) -> None:
    """The whole loop depends on `list` printing something `rate` accepts. That
    is a round trip, so it is tested as one."""
    c = cfg(tmp_path)
    conn = conn_at(c)
    publish(c, conn, "chunked prefill fairness", 4.2)
    publish(c, conn, "nas backup mistakes", 2.8)
    conn.close()
    assert cli.cmd_list(c, ns(what="all", limit=20)) == 0
    out = capsys.readouterr().out
    toks = [t for t in out.split() if len(t) == 8 and all(
        ch in "0123456789abcdef" for ch in t)]
    assert len(toks) >= 2, out
    conn = db.connect(c.db_path)
    for tok in toks[:2]:
        assert eval_resolve(conn, tok) is not None, f"{tok} resolves to nothing"
    conn.close()


def eval_resolve(conn: sqlite3.Connection, ref: str):
    return ev.resolve_artifact(conn, ref)


def test_list_on_an_empty_database_is_not_a_crash(tmp_path: Path,
                                                   capsys) -> None:
    c = cfg(tmp_path)
    assert cli.cmd_list(c, ns(what="all", limit=20)) == 0
    out = capsys.readouterr().out
    assert "none yet" in out
    assert "refills itself" in out


def test_show_an_artifact_gives_score_judge_path_and_the_rate_command(
        tmp_path: Path, capsys) -> None:
    c = cfg(tmp_path)
    conn = conn_at(c)
    target = publish(c, conn, "chunked prefill fairness", 4.2)
    aid = conn.execute("SELECT id FROM artifacts").fetchone()["id"]
    conn.close()
    assert cli.cmd_show(c, ns(id=ev.short_id(aid))) == 0
    out = capsys.readouterr().out
    assert "4.20" in out
    assert "specific and sourced" in out
    assert Path(target).name in out
    assert f"cooker rate {ev.short_id(aid)}" in out
    assert "UNEVALUATED" not in out


def test_show_a_stage_reports_tokens_and_events(tmp_path: Path, capsys) -> None:
    c = cfg(tmp_path)
    conn = conn_at(c)
    t = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                      generator="research", title="draft it")
    db.finish(conn, t.id, db.SUCCEEDED, input_tokens=1234, output_tokens=567,
              ttft_ms=300, duration_ms=4000)
    conn.close()
    assert cli.cmd_show(c, ns(id=t.id)) == 0
    out = capsys.readouterr().out
    assert "1,234 in / 567 out" in out
    assert "4000ms" in out
    conn.close()


def test_show_an_unknown_id_fails_loudly(tmp_path: Path, capsys) -> None:
    c = cfg(tmp_path)
    assert cli.cmd_show(c, ns(id="zzzzzzzz")) == 1
    assert "nothing matches" in capsys.readouterr().out


# --- add ---------------------------------------------------------------


def test_add_a_topic_queues_a_chain_not_a_lonely_stage(tmp_path: Path,
                                                         capsys) -> None:
    """OVERVIEW §9: `cooker add "topic"` injects *research*. A single `plan` row
    with no chain would be a queue entry, not research."""
    c = cfg(tmp_path)
    assert cli.cmd_add(c, ns(topic=["why", "does", "prefill", "block"], kind=None,
                              generator="research", title=None, prompt=None,
                              priority=500, chain=None)) == 0
    out = capsys.readouterr().out
    assert "research chain" in out
    conn = db.connect(c.db_path)
    db.migrate(conn)
    rows = conn.execute("SELECT kind, chain_id FROM tasks ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["plan"]
    assert "the rest appear" in out, "the single stage needs its reason stated"
    st = chains.chain_state(conn, str(rows[0]["chain_id"]))
    assert st["total"] >= 1
    conn.close()


def test_add_nothing_refuses(tmp_path: Path, capsys) -> None:
    c = cfg(tmp_path)
    rc = cli.cmd_add(c, ns(topic=[], kind=None, generator="research", title=None,
                            prompt=None, priority=500, chain=None))
    assert rc == 2, "an empty add must not quietly queue nothing"
    assert "needs a topic" in capsys.readouterr().out


def test_add_a_seen_topic_warns_and_queues_anyway(tmp_path: Path,
                                                    capsys) -> None:
    """The 14-day rule stops the daemon repeating itself; a human typing the same
    topic again is not the daemon repeating itself."""
    c = cfg(tmp_path)
    topic = "traefik middlewares worth running"
    conn = db.connect(c.db_path)
    db.migrate(conn)
    chains.mark_seen(conn, chains.topic_hash(topic), kind="topic", ref="earlier")
    conn.close()
    rc = cli.cmd_add(c, ns(topic=[topic], kind=None, generator="research",
                            title=None, prompt=None, priority=500, chain=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "queueing it anyway" in out, "the override must be said out loud"


# --- helpers -----------------------------------------------------------


def test_resolve_task_accepts_the_tail_list_prints(tmp_path: Path) -> None:
    conn = conn_fresh()
    t = db.create_task(conn, chain_id=db.new_id(), kind="plan", generator="research",
                       title="x")
    # full id, then the eight-character tail `list` and `digest` print
    assert str(ev.resolve_task(conn, t.id)["id"]) == t.id
    assert str(ev.resolve_task(conn, ev.short_id(t.id))["id"]) == t.id
    assert ev.resolve_task(conn, "zz") is None, "two characters is not an identity"
    conn.close()


def test_resolve_task_refuses_an_ambiguous_ref(tmp_path: Path) -> None:
    conn = conn_fresh()
    ids = []
    for _ in range(2):
        ids.append(db.create_task(conn, chain_id=db.new_id(), kind="plan",
                                   generator="research", title="t").id)
    # two stages whose tails collide: manufacture it rather than wait for chance
    tail = ev.short_id(ids[0])
    conn.execute("UPDATE tasks SET id=? WHERE id=?",
                 (ids[1][:-8] + tail, ids[1]))
    conn.commit()
    try:
        ev.resolve_task(conn, tail)
        raised = False
    except ValueError:
        raised = True
    assert raised, "an ambiguous stage ref must be refused, not guessed"
    conn.close()


def test_digest_path_and_day_bounds_stay_honest(tmp_path: Path) -> None:
    from cooker import digest
    c = cfg(tmp_path)
    d = date(2026, 10, 7)
    lo, hi = digest.day_bounds(d)
    assert hi - lo == 86400.0 and lo < hi
    assert digest.digest_path(c, day=d).parent.name == "digest"


def test_dir_bytes_counts_and_tolerates_a_missing_tree(tmp_path: Path) -> None:
    assert cli.dir_bytes(tmp_path / "nope") == 0
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_bytes(b"x" * 1000)
    assert cli.dir_bytes(tmp_path) >= 1000


def test_the_disk_guard_measures_where_the_artifacts_actually_are(
        tmp_path: Path) -> None:
    """The bug this pins.

    `Config.outputs_dir` resolved relative paths against the config root while
    `chains.day_dir` built `data_dir/outputs`, so the daemon published into
    `var/outputs/` and `cooker doctor` reported "0.0 MB of 2.0 GB" about an empty
    `outputs/` beside it. `safety.max_disk_gb` was therefore never going to fire,
    which is the one job that number has.
    """
    # a *relative* data_dir is what the shipped config uses, and it is the only
    # shape that exercises the resolution where the two definitions disagreed
    c = cfg(tmp_path, daemon={"data_dir": "var"},
            safety={"outputs_dir": "outputs", "max_disk_gb": 2.0})
    assert c.data_dir == tmp_path / "var", c.data_dir
    assert c.outputs_dir == tmp_path / "var" / "outputs", c.outputs_dir
    assert chains.day_dir(c).parent == c.outputs_dir, \
        "the writer and the quota disagree about where outputs live"
    conn = conn_at(c)
    publish(c, conn, "chunked prefill", 4.4)
    conn.close()
    d = cli.collect_status(c, db.connect(c.db_path))
    assert d["disk"]["bytes"] > 0, \
        "an artifact was published and the disk guard still says zero"
    assert c.outputs_dir.exists()
    assert not (tmp_path / "outputs").exists(), \
        "a phantom outputs dir is the original bug, still lurking"
