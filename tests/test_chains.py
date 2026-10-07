"""Tests for chains.py and the fan-out/fan-in the plan promises.

The property worth pinning is not "a task row was created". It is these:

    fan-out    `fetch` with 3 usable sources creates exactly 3 extracts, and
               creates none for the sources that were thin
    fan-in     `synthesize.dependencies` is the array of those extract ids, in
               order, and it is not claimable until every one has SUCCEEDED
    no wedge     a chain whose only source is thin fails at `fetch` rather than
               parking a synthesizer that can never run
    no GPU     `search` and `fetch` never reach the accelerator, even when the
               accelerator is perfectly healthy

Test 1 of the plan — empty queue self-refills and drains — is at the bottom, and
its dry-run half asserts that nothing was written.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from cooker import chains, daemon, db, runner, safety
from cooker import eval as eval_mod
from cooker import search as search_mod
from cooker.config import Config


def cfg(tmp_path: Path, **over: object) -> Config:
    base: dict = {
        "inference": {"base_url": "http://127.0.0.1:1/v1", "ca_file": None,
                     "model": "test-model", "api_key_env": "K", "api_key_file": None,
                     "max_prefill_tokens_per_request": 1000,
                     "max_tokens_floor": 512, "default_temperature": 0.7,
                     "request_timeout_seconds": 30, "connect_timeout_seconds": 5,
                     "thinking_off_stages": ["plan", "extract"]},
        "detect": {"omlx_port": 8000, "omlx_proc_names": ["omlx-server"]},
        "safety": {"max_disk_gb": 2.0, "outputs_dir": str(tmp_path / "outputs")},
        "scheduler": {"max_concurrency": 1, "tick_seconds": 0.05},
        "research": {"max_queries": 3, "max_sources": 4,
                    "source_context_chars": 2400},
        "search": {"searxng_url": "http://127.0.0.1:1/search", "ca_file": None,
                   "timeout_seconds": 5, "max_results": 8,
                   "fetch_max_bytes": 4096, "cache_ttl_days": 7,
                   "cache_max_mb": 1, "block_hosts": [],
                   "allow_private_networks": True},
        "quality": {"publish_threshold": 3.5, "dedup_window_days": 14},
        "generators": {"research": {"enabled": True, "weight": 3}},
        "topics": {"file": str(tmp_path / "topics.md"),
                   "fallback": ["a fallback subject for fresh installs"],
                   "max_live_research": 2},
        "daemon": {"data_dir": str(tmp_path), "bind_host": "127.0.0.1", "port": 8256},
    }
    for section, key in over.items():
        base[section].update(key)
    return Config(base, tmp_path)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


class NoGPU:
    """An llm stand-in that fails the test if anything asks it for a token."""

    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, req: object) -> object:
        self.calls += 1
        raise AssertionError(f"a free stage reached the GPU: {req}")  # type: ignore[attr-defined]

    def thinking_for(self, stage: str) -> bool:
        return False

    def state(self) -> dict:
        return {"gpu_calls": self.calls}

    async def aclose(self) -> None:
        return None


class FakeSearcher:
    """Answers with a fixed set of sources, two of which are deliberately thin."""

    def __init__(self, cfg: Config, *, pages: dict[str, str] | None = None,
                 search_ok: bool = True) -> None:
        self.cfg = cfg
        self.search_ok = search_ok
        self.pages = pages or {
            "https://a.example/1": "substantive content " * 60,
            "https://b.example/2": "also substantive here " * 60,
            "https://c.example/3": "cookie wall",           # too thin -> dropped
        }
        self.searches: list[str] = []
        self.fetched: list[str] = []

    async def search(self, q: str, *, max_results: int | None = None):
        self.searches.append(q)
        if not self.search_ok:
            return []
        return [search_mod.Result(url=u, title=f"page {i}", snippet="snip",
                                   engine="fake")
                for i, u in enumerate(self.pages)]

    async def fetch(self, url: str) -> search_mod.Page:
        self.fetched.append(url)
        text = self.pages.get(url, "")
        return search_mod.Page(url=url, text=text, status=200 if text else 404,
                                bytes_in=len(text))

    def state(self) -> dict:
        return {"searches": len(self.searches), "fetches": len(self.fetched)}

    async def aclose(self) -> None:
        return None


def kinds(conn: sqlite3.Connection, chain: str) -> dict[str, str]:
    return {r["kind"]: r["status"] for r in conn.execute(
        "SELECT kind, status FROM tasks WHERE chain_id=?", (chain,)).fetchall()}


# --- topics and dedup ---------------------------------------------------


def test_topics_file_parses_bullets_numbers_and_comments(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    Path(c.get("topics.file")).write_text(
        "# Topics\n\n- first topic here\n2. second topic there\n"
        "* third topic too\nbare fourth topic\n\n", encoding="utf-8")
    got = chains.read_topics(c)
    assert got == ["first topic here", "second topic there", "third topic too",
                   "bare fourth topic"], got


def test_the_dedup_rule_blocks_the_same_topic_not_a_lookalike(tmp_path: Path) -> None:
    """Case and punctuation are not different subjects. Without this, re-asking
    "GPU scheduling" as "gpu scheduling." costs a second budget and looks like
    novelty."""
    conn = db.connect(":memory:")
    db.migrate(conn)
    a = chains.topic_hash("Why Prefill Blocks OMlx!")
    b = chains.topic_hash("why prefill blocks omlx")
    assert a == b
    chains.mark_seen(conn, a, kind="topic", ref="chain-1")
    assert chains.seen_recently(conn, b, days=14)
    assert not chains.seen_recently(conn, chains.topic_hash("another subject"),
                                      days=14)
    conn.close()


def test_seed_creates_the_head_and_marks_the_topic_seen(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    Path(c.get("topics.file")).write_text("- how does X work\n", encoding="utf-8")
    chain = chains.seed(c, conn, "how does X work")
    rows = conn.execute("SELECT kind, status, chain_id FROM tasks").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "plan"
    assert rows[0]["chain_id"] == chain
    assert rows[0]["status"] == db.QUEUED
    # the consumed topic must not come round again inside the window; the
    # fallback list may still offer something else, and that is correct
    nxt = chains.next_topic(c, conn)
    assert nxt is None or chains.topic_hash(nxt[0]) != chains.topic_hash(
        "how does X work"), nxt
    conn.close()


def test_seed_refuses_when_paused_and_falls_back_when_out_of_topics(
        tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.set_meta(conn, "paused", "sam is presenting")
    assert chains.seed_research_if_thirsty(c, conn) == []
    assert int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]) == 0
    db.set_meta(conn, "paused", "")
    conn.execute("DELETE FROM meta")
    conn.commit()
    made = chains.seed_research_if_thirsty(c, conn)
    assert len(made) == 1, "the fallback list must bootstrap a fresh install"
    conn.close()


# --- the edges ----------------------------------------------------------


def test_plan_makes_search_and_search_makes_fetch(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    plan = conn.execute("SELECT id FROM tasks WHERE kind='plan'").fetchone()["id"]
    db.finish(conn, plan, db.SUCCEEDED)
    kids = chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (plan,)).fetchone()),
        result_text="- how does prefill work\n- what is chunked prefill\n")
    assert [k.kind for k in kids] == ["search"]
    assert kids[0].chain_id == chain
    assert kids[0].dependencies == [plan]
    assert len(kids[0].payload["queries"]) == 2
    conn.close()


def test_query_parsing_survives_bullets_numbers_json_and_prose(tmp_path: Path) -> None:
    """Each of these is a valid answer from a human's point of view. A chain that
    dies because the punctuation was unexpected is a bug in us, not in the model."""
    cases = [
        "- how does prefill work\n- what about chunked prefill\n",
        "1. how does prefill work\n2. what about chunked prefill\n",
        '["how does prefill work", "what about chunked prefill"]',
        "how does prefill work\nwhat about chunked prefill\n",
    ]
    for text in cases:
        q = chains._queries_from(text, cap=3)
        assert len(q) == 2, f"{text!r} -> {q}"
    assert chains._queries_from("Sure! Here are the search queries:\n"
                                 "- how does chunked prefill work\n", cap=3) == \
        ["how does chunked prefill work"], "the preamble is not a search query"
    assert chains._queries_from("", cap=3) == []
    assert chains._queries_from("too short", cap=3) == []


def test_plan_with_no_queries_stalls_rather_than_inventing_them(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    plan = conn.execute("SELECT id FROM tasks WHERE kind='plan'").fetchone()["id"]
    db.finish(conn, plan, db.SUCCEEDED)
    chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (plan,)).fetchone()),
        result_text="")
    assert kinds(conn, chain) == {"plan": db.SUCCEEDED}
    evs = db.tail_events(conn)
    assert any(e["type"] == "chain.stalled" for e in evs)
    conn.close()


def test_fan_out_makes_one_extract_per_usable_source_and_fans_in(
        tmp_path: Path) -> None:
    """The shape the plan promises: 3 candidates, 2 usable, so 2 extracts, and
    `synthesize.dependencies` is exactly those two ids."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    fetch_id = db.create_task(conn, chain_id=chain, kind="fetch",
                              generator="research", title="fetch",
                              payload={"topic": "gpu scheduling",
                                     "sources": [
                                         {"url": "https://a/1", "ok": True,
                                          "title": "A", "file": "source-0.txt"},
                                         {"url": "https://b/2", "ok": True,
                                          "title": "B", "file": "source-1.txt"},
                                         {"url": "https://c/3", "ok": False,
                                          "title": "C", "error": "thin"}]}).id
    db.finish(conn, fetch_id, db.SUCCEEDED)
    kids = chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (fetch_id,)).fetchone()))
    extracts = [k for k in kids if k.kind == "extract"]
    synth = [k for k in kids if k.kind == "synthesize"]
    assert len(extracts) == 2, "a thin source must not become an extract task"
    assert len(synth) == 1
    assert synth[0].dependencies == [e.id for e in extracts]

    # claimability: the synthesizer must be invisible until the extracts finish
    assert db.peek_runnable(conn, limit=10)
    runnable_ids = {t.id for t in db.peek_runnable(conn, limit=20)}
    assert synth[0].id not in runnable_ids, "fan-in ran before its inputs"
    for e in extracts:
        db.finish(conn, e.id, db.SUCCEEDED)
    assert synth[0].id in {t.id for t in db.peek_runnable(conn, limit=20)}, \
        "fan-in never became claimable"
    conn.close()


def test_zero_usable_sources_fails_at_fetch_and_wedges_nothing(tmp_path: Path) -> None:
    """If we created the extract first and let it fail, `propagate_blocked` would
    park the synthesizer, and one cookie wall would kill the whole chain."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    fetch_id = db.create_task(conn, chain_id=chain, kind="fetch",
                              generator="research", title="fetch",
                              payload={"topic": "t", "sources": [
                                  {"url": "https://c/3", "ok": False}]}).id
    kids = chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (fetch_id,)).fetchone()))
    assert kids == [], "no usable source means no children"
    assert not conn.execute("SELECT 1 FROM tasks WHERE kind='synthesize'").fetchone()
    assert not conn.execute("SELECT 1 FROM tasks WHERE status='PARKED'").fetchone()
    conn.close()


def test_critique_and_publish_follow_the_draft(tmp_path: Path) -> None:
    """M5 shape: synthesize -> critique -> evaluate -> publish, and the verdict
    on the evaluate row is what decides whether `publish` is ever created.

    The old version of this test asserted critique -> publish. That was the M4
    shape, where nothing judged the work between those two edges, so the chain
    published whatever came out of critique. Asserting the old shape now would
    mean asserting a gate that no longer exists.
    """
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    synth = db.create_task(conn, chain_id=chain, kind="synthesize",
                          generator="research", title="s",
                          payload={"topic": "t"}).id
    db.finish(conn, synth, db.SUCCEEDED)
    kids = chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (synth,)).fetchone()))
    assert [k.kind for k in kids] == ["critique"]

    crit = kids[0]
    db.finish(conn, crit.id, db.SUCCEEDED)
    kids2 = chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (crit.id,)).fetchone()))
    assert [k.kind for k in kids2] == ["evaluate"]

    ev = kids2[0]
    db.finish(conn, ev.id, db.SUCCEEDED, score=4.2)
    db.merge_payload(conn, ev.id, {"score": 4.2, "verdict": "PUBLISH"})
    row = db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (ev.id,)).fetchone())
    kids3 = chains.advance(c, conn, row)
    assert [k.kind for k in kids3] == ["publish"]
    assert kinds(conn, chain)["critique"] == db.SUCCEEDED
    conn.close()


def test_a_reject_leaves_no_publish_stage_to_run(tmp_path: Path) -> None:
    """Rejected work must not have a publish task waiting on a forgotten flag.

    If `advance` created publish anyway, then publishing depends on every caller
    remembering to check the score. Creating nothing at all means the gate cannot
    be bypassed by a code path someone forgot to update.
    """
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    ev = db.create_task(conn, chain_id=chain, kind="evaluate",
                        generator="research", title="e",
                        payload={"topic": "t", "score": 2.6, "verdict": "REJECT"})
    db.finish(conn, ev.id, db.SUCCEEDED, score=2.6)
    assert chains.advance(c, conn, db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (ev.id,)).fetchone())) == []
    kinds_now = kinds(conn, chain)
    assert "publish" not in kinds_now
    rejected = [e for e in db.tail_events(conn) if e["type"] == "chain.rejected"]
    assert rejected, "the rejection has to be visible, not just absent"
    assert json.loads(rejected[-1]["data_json"] or "{}")["score"] == 2.6
    conn.close()


def test_an_unknown_generator_says_so_instead_of_going_quietly(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    t = db.create_task(conn, chain_id=db.new_id(), kind="analyze",
                      generator="project-review", title="review x")
    assert chains.advance(c, conn, t) == []
    assert any(e["type"] == "chain.stub" for e in db.tail_events(conn))
    conn.close()


def test_parked_when_a_dependency_cannot_succeed(tmp_path: Path) -> None:
    """The wedge that `propagate_blocked` exists for: an extract dies, so the
    synthesizer must be parked, not retried forever."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "t")
    e1 = db.create_task(conn, chain_id=chain, kind="extract", generator="research",
                        title="e1").id
    e2 = db.create_task(conn, chain_id=chain, kind="extract", generator="research",
                        title="e2").id
    s = db.create_task(conn, chain_id=chain, kind="synthesize", generator="research",
                      title="s", dependencies=[e1, e2]).id
    db.finish(conn, e1, db.SUCCEEDED)
    assert db.propagate_blocked(conn) == 0, "one failure must not park the fan-in"
    db.finish(conn, e2, db.FAILED, error="endpoint gone")
    assert db.propagate_blocked(conn) == 1
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (s,)).fetchone()[
        "status"] == db.PARKED
    conn.close()


# --- free stages never touch the GPU ------------------------------------


async def test_search_and_fetch_never_reach_the_accelerator(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    gpu = NoGPU()
    searcher = FakeSearcher(c)
    r = runner.StageRunner(c, conn, gpu, searcher=searcher)  # type: ignore[arg-type]
    chain = chains.seed(c, conn, "gpu scheduling")

    search_task = db.create_task(conn, chain_id=chain, kind="search",
                                 generator="research", title="search",
                                 payload={"topic": "gpu scheduling",
                                        "queries": ["q one", "q two"]})
    out = await r(search_task)
    assert out.status == db.SUCCEEDED, out.as_dict()
    assert gpu.calls == 0, "a search stage spent a token"
    assert searcher.searches == ["q one", "q two"]

    fetch_task = conn.execute("SELECT * FROM tasks WHERE kind='fetch'").fetchone()
    assert fetch_task is not None, "search did not hand off to fetch"
    out2 = await r(db.Task.from_row(fetch_task))
    assert out2.status == db.SUCCEEDED, out2.as_dict()
    assert gpu.calls == 0, "a fetch stage spent a token"
    # 3 candidates, the thin one dropped
    kept = [s for s in db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (fetch_task["id"],)).fetchone()
    ).payload["sources"] if s["ok"]]
    assert len(kept) == 2, kept
    assert int(conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='extract'"
                            ).fetchone()[0]) == 2, kinds(conn, chain)
    conn.close()


async def test_a_blocked_candidate_never_reaches_the_fetcher(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)

    class LeakSearcher(FakeSearcher):
        async def fetch(self, url: str):
            raise AssertionError(f"we fetched a refused url: {url}")

    searcher = LeakSearcher(c, pages={})
    searcher.pages = {}
    r = runner.StageRunner(c, conn, NoGPU(), searcher=searcher)  # type: ignore[arg-type]
    chain = chains.seed(c, conn, "t")
    t = db.create_task(conn, chain_id=chain, kind="fetch", generator="research",
                      title="f",
                      payload={"candidates": [{"url": "http://127.0.0.1:8000/admin",
                                             "title": "nope"}]})
    out = await r(t)
    assert out.status == db.FAILED
    conn.close()


async def test_we_keep_looking_past_refused_candidates(tmp_path: Path) -> None:
    """`max_sources` is a budget of sources that worked. Stopping after that many
    *examined* means two 403s spend the entire budget and the chain dies with
    nothing while the good pages further down the list go unread."""
    c = cfg(tmp_path, research={"max_sources": 2})
    conn = db.connect(":memory:")
    db.migrate(conn)

    class Stubborn:
        def __init__(self) -> None:
            self.tried: list[str] = []

        async def fetch(self, url: str) -> search_mod.Page:
            self.tried.append(url)
            if "good" in url:
                text = "real content " * 80
                return search_mod.Page(url=url, text=text, status=200,
                                       bytes_in=len(text))
            return search_mod.Page(url=url, status=403, error="HTTP 403")

        def state(self) -> dict:
            return {}

        async def aclose(self) -> None:
            return None

    searcher = Stubborn()
    r = runner.StageRunner(c, conn, NoGPU(), searcher=searcher)  # type: ignore[arg-type]
    chain = db.new_id()
    t = db.create_task(conn, chain_id=chain, kind="fetch", generator="research",
                      title="f", payload={"topic": "t", "candidates": [
                          {"url": "https://bad1.example/x", "title": "b1"},
                          {"url": "https://bad2.example/y", "title": "b2"},
                          {"url": "https://good1.example/z", "title": "g1"},
                          {"url": "https://good2.example/w", "title": "g2"},
                          {"url": "https://good3.example/v", "title": "g3"}]})
    out = await r(t)
    assert out.status == db.SUCCEEDED, out.as_dict()
    sources = db.Task.from_row(
        conn.execute("SELECT * FROM tasks WHERE id=?", (t.id,)).fetchone()
    ).payload["sources"]
    kept = [s for s in sources if s["ok"]]
    assert len(kept) == 2, f"cap should be filled with usable sources: {sources}"
    assert len(searcher.tried) == 4, "should have stopped once the cap was full"
    extracts = conn.execute("SELECT COUNT(*) n FROM tasks WHERE kind='extract'"
                            ).fetchone()["n"]
    assert extracts == 2, f"fan-out follows the kept sources, got {extracts}"
    conn.close()


async def test_a_stage_that_runs_out_of_budget_says_so_in_the_artifact(
        tmp_path: Path) -> None:
    """§4's failure mode, made visible. A flat output budget cut `synthesize` off
    mid-sentence and the file read as finished. `finish_reason=length` must appear
    in the document, not only in a log nobody opens."""
    c = cfg(tmp_path, inference={"stage_max_tokens": {"synthesize": 64},
                                 "max_tokens_floor": 64})
    conn = db.connect(":memory:")
    db.migrate(conn)

    class Cutter:
        model = "test-model"

        async def stream(self, req):
            from cooker.llm import Completion
            assert req.max_tokens == 64, "the per-stage budget was ignored"
            # Only the synthesize stage runs out; the extract finishes normally,
            # so the warning has to be conditional on what actually happened and
            # not stamped on everything.
            cut = req.stage == "synthesize"  # type: ignore[attr-defined]
            return Completion(text="a sentence that gets cut off beca" if cut
                              else "a complete little extraction of the source.",
                              finish_reason="length" if cut else "stop",
                              completion_tokens=64, prompt_tokens=300, ttft_s=0.3,
                              elapsed_s=1.0, server_prompt=True,
                              server_completion=True)

        def thinking_for(self, stage: str) -> bool:
            return True

        def state(self) -> dict:
            return {}

        async def aclose(self) -> None:
            return None

    r = runner.StageRunner(c, conn, Cutter(), searcher=FakeSearcher(c))  # type: ignore[arg-type]
    t = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                      generator="research", title="s", payload={"topic": "t"})
    out = await r(t)
    body = Path(out.path or "").read_text()
    assert "finished: length" in body, body[:400]
    assert "truncated" in body, "a truncated answer must say so"
    # and a stage that finished normally must NOT carry the warning
    t2 = db.create_task(conn, chain_id=db.new_id(), kind="extract",
                        generator="research", title="e", payload={"topic": "t"})
    out2 = await r(t2)
    assert "finished: stop" in Path(out2.path or "").read_text()
    assert "truncated" not in Path(out2.path or "").read_text()
    conn.close()


async def test_publish_writes_no_secrets_and_promotes_the_artifact(
        tmp_path: Path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    r = runner.StageRunner(c, conn, NoGPU(), searcher=FakeSearcher(c))  # type: ignore[arg-type]
    chain = chains.seed(c, conn, "gpu scheduling")
    draft = tmp_path / "draft.md"
    draft.write_text("# brief\n\nAKIAIOSFODNN7EXAMPLE is the key.\n", encoding="utf-8")
    # The gate is real now: publish reads its dependency's evaluation, so this
    # test has to produce one. A publish that succeeds without it would mean the
    # threshold lives only in `advance` and any other path walks straight past it.
    ev = db.create_task(conn, chain_id=chain, kind="evaluate",
                        generator="research", title="evaluate: gpu scheduling")
    db.finish(conn, ev.id, db.SUCCEEDED, score=4.2)
    eval_mod.record(conn, ev.id, eval_mod.EvalResult(
        scores={a: 4 for a in eval_mod.RUBRIC}, overall=4.2, verdict="PUBLISH",
        rationale="accurate and actionable"))
    t = db.create_task(conn, chain_id=chain, kind="publish", generator="research",
                      title="publish: gpu scheduling",
                      dependencies=[ev.id],
                      payload={"topic": "gpu scheduling", "draft_path": str(draft)})
    out = await r(t)
    assert out.status == db.SUCCEEDED, out.as_dict()
    body = Path(out.path or "").read_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in body, "secret published"
    assert "[REDACTED:aws-access-key]" in body
    assert "research" in Path(out.path or "").parts, \
        "published outside the generator subdirectory the layout specifies"
    art = conn.execute("SELECT status, path FROM artifacts ORDER BY created_at"
                       ).fetchall()
    assert art[-1]["status"] == "PUBLISHED"
    assert chain in str(art[-1]["path"]) or out.path
    conn.close()


async def test_publish_refuses_work_that_was_never_judged(tmp_path: Path) -> None:
    """The gate at the gate, not only in the DAG.

    A publish whose dependency has no evaluation row is the case that matters:
    a hand-queued publish, or a chain built by an older binary. Refusing it costs
    one artifact; publishing it means the threshold is advisory, and every future
    "did we ever ship something unjudged?" question has no answer.
    """
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    r = runner.StageRunner(c, conn, NoGPU(), searcher=FakeSearcher(c))  # type: ignore[arg-type]
    chain = chains.seed(c, conn, "gpu scheduling")
    draft = tmp_path / "d.md"
    draft.write_text("# fine\n\nA perfectly ordinary paragraph about prefill.\n",
                     encoding="utf-8")
    t = db.create_task(conn, chain_id=chain, kind="publish", generator="research",
                      title="publish: unjudged",
                      payload={"topic": "t", "draft_path": str(draft)})
    out = await r(t)
    assert out.status == db.REJECTED, out.as_dict()
    assert not list(chains.day_dir(c).rglob("*.md")), \
        "an unjudged artifact reached the filesystem"
    blocked = [e for e in db.tail_events(conn) if e["type"] == "publish.blocked"]
    assert blocked, "the refusal has to be in the log, not just in the status"
    conn.close()


def test_promote_reuses_the_draft_row_instead_of_adding_one(
        tmp_path: Path) -> None:
    """One output, one row.

    Synthesize registers the draft; publish redacts it and writes the final file.
    Inserting a second row means every reader has to deduplicate by hash before it
    can count anything, and the digest's day count lies by the number of stages.
    """
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)
    chain = chains.seed(c, conn, "gpu scheduling")
    draft = db.create_task(conn, chain_id=chain, kind="synthesize",
                           generator="research", title="synthesize: gpu scheduling")
    runner.register_artifact(conn, draft, "/tmp/draft.md", "h" * 64,
                             status="CANDIDATE")
    assert conn.execute("SELECT COUNT(*) n FROM artifacts").fetchone()["n"] == 1
    pub = db.create_task(conn, chain_id=chain, kind="publish", generator="research",
                         title="publish: gpu scheduling", dependencies=[draft.id])
    runner.promote_candidate(conn, pub, "/tmp/out/final.md", "i" * 64, 4.2)
    rows = conn.execute("SELECT path, status, score FROM artifacts").fetchall()
    assert len(rows) == 1, f"publish added a row instead of promoting: {len(rows)}"
    assert rows[0]["status"] == "PUBLISHED"
    assert rows[0]["path"] == "/tmp/out/final.md"
    assert rows[0]["score"] == 4.2
    conn.close()


def test_a_publish_with_no_candidate_of_its_own_still_gets_recorded(
        tmp_path: Path) -> None:
    """Hand-queued work must not vanish from the record just because it skipped
    the stage that normally creates the row."""
    conn = db.connect(":memory:")
    db.migrate(conn)
    pub = db.create_task(conn, chain_id=db.new_id(), kind="publish",
                         generator="research", title="publish: handed over")
    runner.promote_candidate(conn, pub, "/tmp/x.md", "j" * 64, 4.0)
    assert conn.execute("SELECT status FROM artifacts").fetchone()["status"] == \
        "PUBLISHED"
    conn.close()


def test_the_drafting_stages_are_the_ones_named_in_the_plan() -> None:
    """The set is a reading of PLAN §5, so it is asserted against PLAN rather
    than against whatever the module happens to contain today."""
    assert {"synthesize", "analyze", "consolidate", "generate", "write"} <= \
        chains.DRAFT_KINDS
    # scaffolding never counts as an output
    assert not {"plan", "extract", "critique", "evaluate", "publish", "search",
                "fetch", "scan", "collect"} & chains.DRAFT_KINDS
    # and every drafting stage thinks: registering a candidate that was produced
    # without the model would mean an artifact nobody paid for
    assert chains.DRAFT_KINDS <= chains.THINKING_KINDS


async def test_an_intermediate_stage_leaves_no_artifact_row(tmp_path: Path) -> None:
    """The bug, pinned. `critique` produced a file and registered it, so the
    kitchen showed a critique of a draft as a second output of the same chain."""
    c = cfg(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)

    class Echo:
        model = "test-model"

        async def stream(self, req):
            from cooker.llm import Completion
            return Completion(text="some prose of substance for the stage",
                              finish_reason="stop", prompt_tokens=100,
                              completion_tokens=20, ttft_s=0.1, elapsed_s=0.5,
                              server_prompt=True, server_completion=True)

        def thinking_for(self, stage: str) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    r = runner.StageRunner(c, conn, Echo())  # type: ignore[arg-type]
    for kind in ("plan", "extract", "critique"):
        t = db.create_task(conn, chain_id=db.new_id(), kind=kind,
                           generator="research", title=f"{kind}: x")
        out = await r(t)
        assert out.status == db.SUCCEEDED, out.as_dict()
        assert Path(out.path or "").exists(), "the file must still be kept"
    assert conn.execute("SELECT COUNT(*) n FROM artifacts").fetchone()["n"] == 0, \
        "scaffolding was registered as output"

    t = db.create_task(conn, chain_id=db.new_id(), kind="synthesize",
                       generator="research", title="synthesize: x")
    assert (await r(t)).status == db.SUCCEEDED
    assert conn.execute("SELECT status FROM artifacts").fetchone()["status"] == \
        "CANDIDATE"
    conn.close()


# --- Test 1: empty queue self-refills and drains ----------------------


async def test_dry_run_reports_a_topic_and_writes_no_tasks(tmp_path: Path) -> None:
    """The M2 invariant, restated for M4.

    "Dry run" means the database is untouched. A dry run that queues work has
    become a real run with a misleading name, and the evidence would be rows the
    user never asked for.
    """
    c = cfg(tmp_path)
    Path(c.get("topics.file")).write_text("- how does prefill work\n",
                                           encoding="utf-8")
    conn = db.connect(":memory:")
    db.migrate(conn)
    k = daemon.Kitchen(c, conn, dry_run=True)
    assert k.llm is None, "dry-run must not even construct a GPU client"
    k._maybe_seed()
    assert int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]) == 0, \
        "dry-run wrote tasks"
    evs = db.tail_events(conn)
    assert any(e["type"] == "dry.would_seed" for e in evs), [e["type"] for e in evs]
    assert not any(e["type"] == "topics.seeded" for e in evs)
    conn.close()


async def test_live_seed_then_the_queue_drains(tmp_path: Path) -> None:
    """Test 1, the live half: empty queue, generators create the chain, the DAG
    resolves stage by stage, and the queue ends up empty again."""
    c = cfg(tmp_path)
    Path(c.get("topics.file")).write_text(
        "- why does a long prefill block other requests\n", encoding="utf-8")
    conn = db.connect(":memory:")
    db.migrate(conn)
    assert db.queue_counts(conn)["QUEUED"] == 0, "start from an empty queue"
    k = daemon.Kitchen(c, conn, dry_run=False)
    k.scheduler.stage_runner = None
    made = chains.seed_research_if_thirsty(c, conn)
    assert len(made) == 1
    chain = made[0]
    assert db.queue_counts(conn)["QUEUED"] == 1

    # walk the DAG exactly as the scheduler would: claim, run, advance
    answers = {
        "plan": "- why prefill blocks\n- what chunked prefill changes\n",
        "extract": "Prefill is atomic on omlx; chunking fixes it.",
        "synthesize": "Prefill is exclusive today; chunked prefill interleaves.",
        "critique": "Accurate, sourced, actionable. 4/5.",
        # strict JSON, five axes, overall 4.0 — above the 3.5 gate, so publish
        # exists. Shape matters more than the numbers here: a prose critique from
        # the evaluator stage must not be mistaken for a verdict.
        "evaluate": '{"usefulness":4,"accuracy":4,"novelty":3,'
                    '"actionability":4,"relevance":5,"rationale":"solid"}',
    }

    class TinyLLM:
        model = "test-model"

        def __init__(self) -> None:
            self.kinds: list[str] = []

        async def stream(self, req):
            from cooker.llm import Completion
            self.kinds.append(req.stage)  # type: ignore[attr-defined]
            return Completion(text=answers.get(req.stage, "not set"),
                             finish_reason="stop", prompt_tokens=200,
                             completion_tokens=40, ttft_s=0.2, elapsed_s=0.9,
                             server_prompt=True, server_completion=True)

        def thinking_for(self, stage: str) -> bool:
            return stage not in ("plan", "extract")

        def state(self) -> dict:
            return {}

        async def aclose(self) -> None:
            return None

    fake_pages = {
        "https://docs.omlx.dev/prefill": "Prefill is atomic. " * 80,
        "https://blog.example.com/chunked": "Chunked prefill interleaves. " * 80,
        "https://thin.example.com/": "cookie wall",
    }

    class FakeOrigin(search_mod.Searcher):
        """Stands in for searxng *and* the internet, so the test measures the
        DAG rather than the network."""

        async def search(self, q, *, max_results=None):
            return [search_mod.Result(url=u, title=f"t{i}", snippet="s")
                    for i, u in enumerate(fake_pages)]

        async def fetch(self, url):
            check = search_mod.check_url(url, allow_private=True)
            return search_mod.Page(url=check, text=fake_pages.get(url, ""),
                                   status=200, bytes_in=len(fake_pages.get(url, "")))

        async def aclose(self) -> None:
            return None

    llm = TinyLLM()
    r = runner.StageRunner(c, conn, llm, searcher=FakeOrigin(c))  # type: ignore[arg-type]

    guard = 0
    while guard < 40:
        guard += 1
        task = db.claim_next(conn)
        if task is None:
            db.propagate_blocked(conn)
            counts = db.queue_counts(conn)
            if not counts.get("QUEUED") and not counts.get("RUNNING"):
                break
            continue
        with contextlib.suppress(Exception):
            await r(task)
        db.propagate_blocked(conn)

    st = chains.chain_state(conn, chain)
    done_kinds = sorted(kinds(conn, chain))
    assert "extract" in done_kinds and "synthesize" in done_kinds, done_kinds
    assert st["done"] == st["total"], f"chain stalled at {st['done']}/{st['total']}"
    assert db.queue_counts(conn)["QUEUED"] == 0, "the queue never drained"
    # `evaluate` sits between critique and publish now: seven GPU-touching stages
    # where M4 had six, and the queue still ends empty.
    assert llm.kinds == ["plan", "extract", "extract", "synthesize", "critique",
                         "evaluate"], llm.kinds
    published = list(chains.day_dir(c).rglob("*.md"))
    assert published, "nothing published"
    assert safety.scan_secrets(published[0].read_text()).clean
    conn.close()


def test_wrapped_prose_above_the_list_is_not_queue(tmp_path: Path) -> None:
    """The bug, in the shape that defeated the obvious fix.

    The live topics.md explains itself in wrapped paragraphs, so a prose line can
    end mid-sentence with no full stop — "does it end with a period" cannot tell
    documentation from a subject. What does separate them is position: the list is
    the queue, and anything above the first marker is documentation. A bare line
    below the list is still a topic, which is the promise the existing parse test
    makes and which this must not break.
    """
    c = cfg(tmp_path)
    Path(c.get("topics.file")).write_text(
        "# Topics\n"
        "\n"
        "Cooker takes the first line whose subject has not been researched in the\n"
        "last fourteen days, builds a chain for it, and marks it seen so it does\n"
        "not repeat itself.\n"
        "\n"
        "## Backlog\n"
        "\n"
        "- why does a long prefill block other requests\n"
        "- traefik middlewares worth running\n"
        "a bare topic trailing the list\n",
        encoding="utf-8")
    got = chains.read_topics(c)
    assert got == ["why does a long prefill block other requests",
                   "traefik middlewares worth running",
                   "a bare topic trailing the list"], got
