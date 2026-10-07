"""runner: turns a claimed stage into work, and work into an artifact.

The seam between the scheduler (which decides *when* and *how many*) and the
chains (which decide *what comes next*). M3 was the smallest honest version —
one prompt, one completion, one file. M4 adds the two things a DAG needs and
nothing else:

    - **dispatch by kind**, because `search` and `fetch` cost network and disk
      and must not spend a token. A chain that asks the model to "do the search"
      is a chain that pays for an HTTP client with a GPU.
    - **fan-in by reading what siblings saved**, because the synthesizer must see
      the extracts that actually happened, not a summary of what we hoped they'd
      say. It reads their saved files by dependency id.

What is non-negotiable, whatever runs through here:

    - content goes through `safety.scan_secrets` on the way *in*. A secret that
      reaches the model is gone from this machine's point of view.
    - retrieved context is fenced as untrusted data, never spliced as prose.
    - the artifact is scanned on the way *out*, because an artifact written first
      and scrubbed later has been on disk with the secret in it.
    - cancellation propagates. The `except CancelledError` records and re-raises:
      a stage that swallows a cancel is a stage still cooking after the kitchen
      said stop, and in a `fetch` that means still pulling bytes too.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cooker import chains, db, safety
from cooker import eval as eval_mod
from cooker import opencode as opencode_mod
from cooker import search as search_mod
from cooker.config import Config
from cooker.llm import (
    CHARS_PER_TOKEN,
    LLM,
    Completion,
    EndpointGone,
    PrefillOver,
    Request,
    estimate_tokens,
)

# Kinds that must never reach the accelerator. `plan` is not in here: deciding
# what to search for is exactly the kind of thing the box is for, and it costs a
# few hundred tokens to save thousands of irrelevant fetches.
FREE_KINDS = frozenset({"search", "fetch", "publish", "collect", "scan",
                        "consolidate"})


def short_title(topic: str, limit: int = 90) -> str:
    """A readable heading for a document whose subject may be an essay.

    The heading is display, not record: the full text lives in the payload,
    on disk, and at the document's foot. A verbatim lengthy title is how a
    document starts as a mirror of its own prompt — which the judge, reading
    a budget-bound front slice, then fairly scores as "truncated fragment
    of the prompt" (1.0, measured, 2026-10-07)."""
    topic = topic.strip()
    return topic if len(topic) <= limit else topic[:limit - 1].rstrip() + "…"


# The brief a deep-dive hands opencode. It is written for an assistant
# with no human on the glass: every question it might ask is pre-answered
# here, because "answer its prompts when it asks questions" is the owner's
# ask and the honest implementation answers them twice — once in the brief,
# once in the permission/form tenders when the model asks anyway (§21).
DELEGATE_BRIEF = """You are running unattended as Cooker's delegated research \
tool on Sam's home server. Nobody is watching a terminal: do not ask \
questions — state assumptions instead, and answer every form or prompt by \
proceeding.

Subject of the deep-dive: {topic}

Work only inside the current working directory. Use web search if a provider \
is configured; otherwise work from what you know and mark every gap honestly \
as "unverified". Never read .env files, keys, or anything outside this \
directory.

Deliver, as your final assistant message (not as a file): a markdown brief of \
500-900 words with exactly these sections:

## What it is
## Why it matters on a home server
## How to try it
## Sources

Sources lists the real URLs you actually used, or says "none — internal \
knowledge, gaps marked unverified." """


@dataclass
class Outcome:
    task_id: str
    status: str
    path: str | None = None
    reason: str | None = None
    completion: Completion | None = None
    children: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status, "path": self.path,
                "reason": self.reason, "children": list(self.children),
                "completion": self.completion.as_dict() if self.completion else None}


class StageRunner:
    """Callable handed to the scheduler as `stage_runner`.

    The scheduler wraps this in `asyncio.wait_for`, so the timeout belongs to it
    and not here: two owners of one deadline is how a stage gets half-cancelled.
    """

    def __init__(self, cfg: Config, conn: sqlite3.Connection, llm: LLM,
                 searcher: search_mod.Searcher | None = None,
                 oc: opencode_mod.OpenCode | None = None) -> None:
        self.cfg = cfg
        self.conn = conn
        self.llm = llm
        # The delegation tool exists only when the daemon is live and the
        # owner has enabled it; dry-run has no hands, which is the same
        # guarantee the missing llm object gives for the GPU socket.
        self.oc = oc
        self.searcher = searcher or search_mod.Searcher(
            cfg, emit=self.emit, client=getattr(llm, "shared_client", None))
        self.runs = 0
        self.aborts = 0
        # Per-stage wall clock. The scheduler's timeout protects the stage; this
        # is what tells us *where* the time went, which is the difference between
        # "fetch is slow" and "the model is slow" in a way a human can act on.
        self.last_stage_ms: dict[str, float] = {}

    def emit(self, type_: str, message: str, data: dict[str, Any] | None = None) -> None:
        db.emit(self.conn, type_, message=message, data=data or {})

    # --- context ---------------------------------------------------------

    def _context_blocks(self, task: db.Task) -> list[tuple[str, str]]:
        """Everything this stage is allowed to read, as (source, text) pairs.

        Fan-in lives here. `synthesize` is handed the files its dependencies
        actually wrote, read back from disk by id: if an extract saved a cookie
        wall, the synthesizer sees the cookie wall, and the artifact is wrong in a
        way a human can diagnose rather than wrong in a way only a forensic
        analyst could.
        """
        blocks: list[tuple[str, str]] = []
        cap = int(self.cfg.get("research.source_context_chars", 2400))
        payload = task.payload or {}

        # Legacy/manual: a caller that hands us text directly.
        if payload.get("context"):
            blocks.append((str(payload.get("context_source", "retrieved")),
                          str(payload["context"])[:cap]))

        if task.kind == "extract":
            src = payload.get("source") or {}
            name = str(src.get("file") or "")
            text = chains.read_scratch(self.cfg, task.chain_id, name) if name else ""
            if not text:
                blocks.append((str(src.get("url") or "source"),
                              str(src.get("snippet") or "")[:cap]))
            else:
                blocks.append((str(src.get("url") or "source"), text[:cap]))

        elif task.kind == "synthesize":
            rows = self.conn.execute(
                "SELECT id, kind, title, result_path, status FROM tasks WHERE id IN ({})"
                .format(",".join("?" * len(task.dependencies))),
                tuple(task.dependencies)).fetchall() if task.dependencies else []
            for r in rows:
                if r["status"] != db.SUCCEEDED or not r["result_path"]:
                    continue
                p = Path(str(r["result_path"]))
                if not p.exists():
                    # A succeeded stage whose file is gone is a real inconsistency
                    # and belongs in the log, not silently skipped.
                    self.emit("chain.missing_file",
                            f"{r['kind']} {r['id'][:8]} saved nothing at {p.name}",
                            {"task_id": task.id})
                    continue
                blocks.append((str(r["title"]), p.read_text(encoding="utf-8")[:cap]))

        elif task.kind == "critique":
            draft = payload.get("draft") or ""
            if not draft and task.dependencies:
                r = self.conn.execute(
                    "SELECT result_path FROM tasks WHERE id=?",
                    (task.dependencies[0],)).fetchone()
                if r and r["result_path"] and Path(r["result_path"]).exists():
                    draft = Path(r["result_path"]).read_text(encoding="utf-8")
            if draft:
                blocks.append(("draft under review", draft[:cap * 2]))

        return blocks

    def prompt_for(self, task: db.Task) -> Request:
        """Build the request. Secrets scanned in, retrieved context fenced."""
        if task.kind == "evaluate":
            # A different prompt shape entirely: the evaluator scores work rather
            # than producing it, and if it is handed the same "produce a note"
            # instruction it will write a new note and score that, which measures
            # nothing about the thing we meant to judge.
            return self._eval_request(task)
        body = task.prompt or (
            f"Produce a short, useful note on: {task.title}. "
            f"Be concrete; no preamble.")
        scan = safety.scan_secrets(body, source=f"task:{task.id}.prompt")
        if scan.blocked:
            raise safety.PathRefused(f"task prompt withheld: {scan.as_dict()}")
        if scan.findings:
            self.emit("safety.redaction",
                      f"redacted {sum(f.count for f in scan.findings)} secret-shaped "
                      f"span(s) from task prompt",
                      {"task_id": task.id, **scan.as_dict()})
        user = scan.text
        for source, text in self._context_blocks(task):
            inner = safety.scan_secrets(text, source=source)
            if inner.blocked:
                self.emit("safety.withheld",
                          f"context from {source} withheld (never-inline path)",
                          {"task_id": task.id, **inner.as_dict()})
                continue
            if inner.findings:
                self.emit("safety.redaction",
                          f"redacted {sum(f.count for f in inner.findings)} span(s) "
                          f"from retrieved context",
                          {"task_id": task.id, "source": source,
                           **inner.as_dict()})
            user += "\n\n" + safety.fence(source, inner.text)
        thinking = task.kind not in set(self.cfg.get("inference.thinking_off_stages",
                                                      []) or [])
        # Per-stage output budget. `synthesize` thinks for several hundred tokens
        # before it writes anything, and a flat floor cuts its answer off
        # mid-sentence — a truncated brief that reads as finished is worse than
        # one that admits it ran out.
        budgets = self.cfg.get("inference.stage_max_tokens", {}) or {}
        budget = int(budgets.get(task.kind,
                                  self.cfg.get("inference.max_tokens_floor", 512)))
        return Request(system=safety.SYSTEM_DATA_ONLY, user=user,
                       thinking=thinking, kind=task.kind, stage=task.kind,
                       max_tokens=budget)

    def _draft_text(self, task: db.Task) -> str:
        """The work under review: the synthesize stage's saved artifact.

        `draft_path` first, and this ordering is the whole point. `evaluate`
        depends on `critique` in the DAG, so resolving purely through
        `dependencies` hands the evaluator the *review* of the draft and it
        scores the review — a 4.6 for a critique of a two-sentence nothing, filed
        against the draft. The recorded path is the fact about what was drafted;
        the dependency edges are the fact about ordering, and only the first one
        names the content.
        """
        recorded = read_text_if_any(task.payload.get("draft_path"))
        if recorded.strip():
            return recorded
        for dep in list(task.dependencies or []):
            row = self.conn.execute(
                "SELECT result_path FROM tasks WHERE id=?", (dep,)).fetchone()
            if row and row["result_path"]:
                text = read_text_if_any(row["result_path"])
                if text.strip():
                    return text
        return ""

    def _excerpt_budget(self, topic: str, sources: list[str]) -> tuple[int, int]:
        """How many characters of the draft the evaluator may read, and the
        ceiling it has to fit inside.

        Computed, not configured-and-hoped. A budget written down as "2400" is a
        guess about tokenisation, and the guess was wrong: 2,400 characters of
        draft plus this system prompt lands at ~1,200 tokens against a ceiling of
        1,000, so `evaluate` refused its own stage and the chain stalled at the
        gate. Asking the same estimator the ceiling is enforced with gives a number
        that cannot drift when the system prompt or the source list changes.
        """
        ceiling = int(self.cfg.get("inference.max_prefill_tokens_per_request",
                                    1000))
        base = eval_mod.eval_prompt(topic, "", sources, max_chars=0)
        overhead = (estimate_tokens(eval_mod.EVAL_SYSTEM)
                    + estimate_tokens(base))
        # 48 tokens of slack, the same allowance `fit()` reserves for its own
        # "context trimmed" note, which is longer than this one but same order.
        room = int((ceiling - overhead - 48) * CHARS_PER_TOKEN)
        cap = int(self.cfg.get("research.evaluate_excerpt_chars", 2400))
        return max(0, min(cap, room)), ceiling

    def _eval_request(self, task: db.Task) -> Request:
        draft = self._draft_text(task)
        sources = [str(s.get("url")) for s in
                  (task.payload.get("sources") or []) if s.get("url")]
        budgets = self.cfg.get("inference.stage_max_tokens", {}) or {}
        topic = str(task.payload.get("topic") or task.title)
        # the judge reads a budget-bound FRONT slice; a lengthy order quoted
        # twice (instruction header AND draft footer) spends the window on
        # the prompt and starves the brief -- the exact arithmetic of the
        # first lengthy deep-dive's 1.0. The judge gets a readable topic;
        # the full order lives in the payload, on disk, and at the draft's
        # foot for whoever audits. The scored window is the brief.
        if len(topic) > 120:
            topic = topic[:118].rstrip() + "…"
        if not draft.strip():
            # Nothing to score. An evaluator that invents a draft to score is how
            # a nonexistent artifact gets a 4.8 in the database.
            raise safety.PathRefused("evaluate asked with no draft to read")
        excerpt, ceiling = self._excerpt_budget(topic, sources)
        if excerpt <= 0:
            raise PrefillOver(
                "the evaluator's own instruction leaves no room for the draft under "
                f"the ceiling of {ceiling} tokens; shorten EVAL_SYSTEM or raise "
                "nothing — raise the ceiling only against a measurement")
        text = eval_mod.eval_prompt(topic, draft, sources, max_chars=excerpt)
        ask = estimate_tokens(eval_mod.EVAL_SYSTEM) + estimate_tokens(text)
        if ask > ceiling:
            # Arithmetic, not advice: refuse here rather than let `fit()` discover
            # there is no fence to trim and fail a stage that was ours to size.
            raise PrefillOver(f"evaluation prompt is ~{ask} tokens > {ceiling}")
        return Request(system=eval_mod.EVAL_SYSTEM, user=text, thinking=False,
                       kind=task.kind, stage=task.kind,
                       max_tokens=int(budgets.get("evaluate", 320)))

    # --- dispatch --------------------------------------------------------

    async def __call__(self, task: db.Task) -> Outcome:
        self.runs += 1
        if task.kind == "delegate":
            # Neither free nor thinking in the usual sense: the GPU burns,
            # but through opencode's hands on the same omlx, so the ledger
            # sees the traffic and the kitchen subtracts it as ours (§21).
            return await self._delegate(task)
        if task.kind in FREE_KINDS:
            return await self._free(task)
        return await self._thinking(task)

    # --- the delegated stage: opencode does the reaching ------------------

    async def _delegate(self, task: db.Task) -> Outcome:
        """M9: the deep-dive burns other hands. One opencode session, in a
        workspace directory that must already exist (verified live: the
        server creates sessions for missing directories and only refuses at
        prompt time), tended by policy while it runs, interrupted the
        instant the ledger says someone else wants the GPU.
        """
        t0 = time.perf_counter()
        topic = str(task.payload.get("topic") or task.title)
        if self.oc is None:
            reason = "delegation asked with no opencode client (dry-run?)"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"delegate: {reason}",
                      {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=reason)
        prompt = DELEGATE_BRIEF.replace("{topic}", topic)
        scan = safety.scan_secrets(prompt, source=f"delegate:{task.id}.prompt")
        if scan.blocked:
            db.finish(self.conn, task.id, db.REJECTED,
                      error=f"safety: {scan.as_dict()}",
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.rejected", "delegate prompt withheld by safety",
                      {"task_id": task.id})
            return Outcome(task.id, db.REJECTED, reason="prompt withheld")
        workdir = chains.day_dir(self.cfg) / "delegations" / task.id
        await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
        try:
            res = await self.oc.run(title=f"deep-dive: {topic[:80]}",
                                     prompt=scan.text, workdir=workdir)
        except asyncio.CancelledError:
            self.aborts += 1
            self.emit("runner.cancelled",
                      f"deep-dive.delegate cancelled after "
                      f"{time.perf_counter() - t0:.2f}s",
                      {"task_id": task.id})
            raise
        except opencode_mod.OpenCodeError as exc:
            db.finish(self.conn, task.id, db.FAILED, error=str(exc),
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"delegate: {exc}",
                      {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=str(exc))
        if not res.text.strip():
            reason = f"opencode returned no text (notes: {'; '.join(res.notes)})"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.empty", f"delegate: {reason}",
                      {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=reason)
        flags = safety.looks_instructed(res.text)
        if flags:
            self.emit("safety.injection_flags",
                      f"delegated brief carries instruction-shaped text: "
                      f"{', '.join(flags)}", {"task_id": task.id})
        # The heading is a readable title, NOT the order verbatim. A lengthy
        # order under `# {topic}` puts the whole prompt at the very top of
        # the document, and the evaluator reads a *front* slice (its excerpt
        # is budget-bound against the prefill ceiling) — so the judge scores
        # its own restated prompt and calls the brief truncated. It saw
        # exactly that on the first lengthy order and fairly scored 1.0:
        # 888 of its ~1,104-char window was the H1. Short heading up top,
        # the full order preserved intact in the footer for audit — the
        # words the GPU reads are the brief, and nothing is lost, it is
        # just not the first thing a reader (or judge) meets.
        doc = (f"# {short_title(topic)}\n\n{res.text}\n\n---\n\n"
               f"*delegated to opencode session {res.session_id}; "
               f"{res.permissions_granted}/{res.permissions_seen} permissions "
               f"granted (policy), {res.forms_answered} form(s) answered; "
               f"{'completed' if res.completed else 'stopped early'}*\n\n"
               f"*order as written:* {topic}\n")
        try:
            path, digest = await asyncio.to_thread(
                safety.write_workspace_file, self.cfg, task.id,
                "deep-dive-delegate.md", doc)
        except safety.PathRefused as exc:
            db.finish(self.conn, task.id, db.FAILED, error=str(exc))
            return Outcome(task.id, db.FAILED, reason=str(exc))
        db.merge_payload(self.conn, task.id, {
            "draft_path": str(path), "session_id": res.session_id,
            "delegation": {"completed": res.completed,
                           "permissions_seen": res.permissions_seen,
                           "granted": res.permissions_granted,
                           "forms": res.forms_answered,
                           "notes": res.notes[:5]}})
        db.finish(self.conn, task.id, db.SUCCEEDED, result_path=str(path),
                  result_hash=digest, input_tokens=res.input_tokens,
                  output_tokens=res.output_tokens,
                  ttft_ms=(res.first_text_s or 0) * 1000,
                  duration_ms=(time.perf_counter() - t0) * 1000)
        register_artifact(self.conn, task, str(path), digest,
                          status=db.CANDIDATE)
        self.emit("runner.stage",
                  f"deep-dive.delegate -> {path.name} ({len(res.text)} chars, "
                  f"{res.output_tokens} tok via opencode, "
                  f"{time.perf_counter() - t0:.1f}s)",
                  {"task_id": task.id, "gpu": True, "via": "opencode",
                   "session_id": res.session_id})
        kids = chains.advance(self.cfg, self.conn, task, result_text=res.text)
        self.last_stage_ms[task.kind] = round((time.perf_counter() - t0) * 1000, 1)
        return Outcome(task.id, db.SUCCEEDED, path=str(path),
                       children=tuple(k.id for k in kids))

    # --- the free stages: network and disk, never the GPU --------------

    async def _free(self, task: db.Task) -> Outcome:
        t0 = time.perf_counter()
        try:
            if task.kind == "search":
                out = await self._search(task, t0)
            elif task.kind == "fetch":
                out = await self._fetch(task, t0)
            elif task.kind == "publish":
                out = await self._publish(task, t0)
            else:
                out = Outcome(task.id, db.FAILED,
                              reason=f"free kind {task.kind!r} unimplemented")
                db.finish(self.conn, task.id, db.FAILED, error=out.reason)
        except asyncio.CancelledError:
            self.aborts += 1
            self.emit("runner.cancelled",
                      f"{task.generator}.{task.kind} (no GPU) cancelled after "
                      f"{time.perf_counter() - t0:.2f}s",
                      {"task_id": task.id, "search": self.searcher.state()})
            raise
        except Exception as exc:
            self.emit("runner.failed", f"{task.kind}: {type(exc).__name__}: {exc}",
                      {"task_id": task.id})
            db.finish(self.conn, task.id, db.FAILED,
                      error=f"{type(exc).__name__}: {exc}",
                      duration_ms=(time.perf_counter() - t0) * 1000)
            return Outcome(task.id, db.FAILED, reason=str(exc))
        self.last_stage_ms[task.kind] = round((time.perf_counter() - t0) * 1000, 1)
        return out

    async def _search(self, task: db.Task, t0: float) -> Outcome:
        queries = [str(q) for q in (task.payload.get("queries") or [])][:8]
        if not queries:
            reason = "no queries to run"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"search: {reason}", {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=reason)
        results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for q in queries:
            # Sequential. Ten parallel fetches looks like an attack from the
            # origin's side and is indistinguishable from one in your own logs.
            for r in await self.searcher.search(q):
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                results.append(r.as_dict())
        db.merge_payload(self.conn, task.id, {"results": results})
        db.finish(self.conn, task.id, db.SUCCEEDED if results else db.FAILED,
                  error=None if results else "searxng returned nothing usable",
                  duration_ms=(time.perf_counter() - t0) * 1000)
        if not results:
            self.emit("runner.failed", "search: no candidates",
                      {"task_id": task.id, "search": self.searcher.state()})
            return Outcome(task.id, db.FAILED, reason="no candidates")
        self.emit("runner.stage",
                  f"search: {len(queries)} queries -> {len(results)} candidates",
                  {"task_id": task.id, "gpu": False,
                   **self.searcher.state()})
        kids = chains.advance(self.cfg, self.conn, task)
        return Outcome(task.id, db.SUCCEEDED, children=tuple(k.id for k in kids))

    async def _fetch(self, task: db.Task, t0: float) -> Outcome:
        cap = int(self.cfg.get("research.max_sources", 4))
        candidates = list(task.payload.get("candidates") or [])
        if not candidates:
            reason = "no candidates to fetch"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            return Outcome(task.id, db.FAILED, reason=reason)
        sources: list[dict[str, Any]] = []
        kept = 0
        for cand in candidates:
            # `cap` is a budget of *sources that worked*, not of candidates looked
            # at. Stopping at cap examined means two 403s spend the whole budget
            # and the chain dies with zero sources while candidates 3 and 4 sat
            # unread — which is exactly the failure this stage exists to avoid.
            if kept >= cap:
                break
            url = str(cand.get("url") or "")
            page = await self.searcher.fetch(url)
            entry = {"url": url,
                    "title": str(cand.get("title") or url)[:120],
                    "host": urlparse(url).hostname or "?",
                    "status": page.status, "chars": len(page.text),
                    "bytes": page.bytes_in, "truncated": page.truncated,
                    "from_cache": page.from_cache, "ok": page.usable,
                    "error": page.error}
            if page.usable:
                # Written per source so `extract` reads one page and pays one
                # prefill, rather than every stage re-reading every page. Numbered
                # by the kept count, because refused candidates now sit between
                # usable ones and a gap in the numbering is a missing file.
                name = f"source-{kept}.txt"
                _, digest, size = await asyncio.to_thread(
                    chains.write_scratch, self.cfg, task.chain_id, name, page.text,
                    source=url)
                entry["file"], entry["sha256"], entry["saved"] = name, digest, size
                kept += 1
            sources.append(entry)
        db.merge_payload(self.conn, task.id, {"sources": sources})
        usable_count = sum(1 for s in sources if s["ok"])
        if usable_count == 0:
            reason = f"0 of {len(sources)} candidates yielded usable text"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"fetch: {reason}",
                      {"task_id": task.id, "sources": sources})
            return Outcome(task.id, db.FAILED, reason=reason)
        db.finish(self.conn, task.id, db.SUCCEEDED,
                  duration_ms=(time.perf_counter() - t0) * 1000)
        self.emit("runner.stage",
                  f"fetch: {usable_count}/{len(sources)} usable, "
                  f"{sum(s['bytes'] for s in sources):,} bytes, no GPU",
                  {"task_id": task.id, "gpu": False,
                   "search": self.searcher.state()})
        # advance fans out the extracts and builds the fan-in edge.
        kids = chains.advance(self.cfg, self.conn, task)
        return Outcome(task.id, db.SUCCEEDED, children=tuple(k.id for k in kids))

    async def _publish(self, task: db.Task, t0: float) -> Outcome:
        # The gate, checked at the gate. The absence of a publish stage would
        # normally mean this never runs, but a hand-queued publish or a chain
        # built by an older version of `advance` should not get a free pass: the
        # threshold is enforced where the work leaves the building, not only where
        # the DAG was drawn.
        ev = self.conn.execute(
            "SELECT overall, verdict FROM evaluations WHERE task_id IN ({})"
            " ORDER BY created_at DESC LIMIT 1"
            .format(",".join("?" * max(1, len(task.dependencies)))),
            tuple(task.dependencies) or ("",)).fetchone() if task.dependencies else None
        threshold = float(self.cfg.get("quality.publish_threshold", 3.5))
        score = float(ev["overall"]) if ev else 0.0
        verdict = str(ev["verdict"]) if ev else "UNEVALUATED"
        if verdict != "PUBLISH" or score < threshold:
            reason = (f"not cleared the threshold: {verdict} at {score:.2f} "
                     f"(needs >= {threshold})")
            db.finish(self.conn, task.id, db.REJECTED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("publish.blocked", f"{task.title[:60]}: {reason}",
                      {"task_id": task.id, "score": score, "verdict": verdict})
            return Outcome(task.id, db.REJECTED, reason=reason)

        src = task.payload.get("draft_path")
        if not src and task.dependencies:
            row = self.conn.execute(
                "SELECT result_path FROM tasks WHERE id=?",
                (task.dependencies[0],)).fetchone()
            src = row["result_path"] if row else None
        text = await asyncio.to_thread(read_text_if_any, src) if src else ""
        if not text.strip():
            reason = "nothing to publish"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"publish: {reason}", {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=reason)
        scan = safety.scan_secrets(text, source=f"publish:{task.title[:40]}")
        if scan.findings:
            self.emit("safety.redaction",
                      f"redacted {sum(f.count for f in scan.findings)} span(s) "
                      f"at publish", {"task_id": task.id, **scan.as_dict()})
        day = chains.day_dir(self.cfg)
        # §7 layout: outputs/YYYY-MM-DD/<generator>/<topic>.md. A flat day
        # directory turns a morning with research and reviews and a digest into
        # eleven indistinguishable markdown files, and the digest links into these
        # paths, so the structure is the reader's index.
        target = day / task.generator / f"{slug_dirname(task)}.md"
        # The specced hash: sha256(prompt || normalized_result). Using the raw
        # text hash instead would call two notes distinct because one has a
        # trailing space, which is not the duplicate anyone meant.
        digest = eval_mod.result_hash(
            task.prompt or str(task.payload.get("topic") or ""), scan.text)
        dup = eval_mod.find_duplicate(self.conn, digest, exclude_id=task.id)
        if dup:
            # Not an error and not a success: the work exists, it is the same work
            # you already have, and the audit trail should say that in so many
            # words rather than quietly re-publishing.
            reason = f"duplicate of artifact {dup[:8]}"
            db.finish(self.conn, task.id, db.REJECTED, error=reason,
                      result_hash=digest,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("publish.duplicate", f"{task.title[:60]}: {reason}",
                      {"task_id": task.id, "duplicate_of": dup})
            return Outcome(task.id, db.REJECTED, reason=reason)
        # The write and the finish are ordered deliberately: the file exists and
        # is hashed before the row claims it does. The other order leaves a row
        # pointing at a file that is not there yet, which `synthesize` reads back
        # as an empty document and publishes as a confident nothing.
        await asyncio.to_thread(write_text, target, scan.text)
        db.finish(self.conn, task.id, db.SUCCEEDED, result_path=str(target),
                  result_hash=digest,
                  duration_ms=(time.perf_counter() - t0) * 1000)
        promote_candidate(self.conn, task, str(target), digest, score)
        eval_mod.mark_result_seen(self.conn, digest, task.id)
        self.emit("runner.published",
                  f"{task.generator}: {target.name} "
                  f"({len(scan.text)} chars, no GPU)",
                  {"task_id": task.id, "path": str(target), "gpu": False})
        chains.advance(self.cfg, self.conn, task)
        return Outcome(task.id, db.SUCCEEDED, path=str(target))

    # --- the thinking stages ----------------------------------------------

    async def _stream_with_retry(self, req: Request, task: db.Task) -> Completion:
        """Ask once. If the model thought itself out of an answer, ask again with
        room for one.

        This model streams its reasoning before its reply, so a tight output budget
        buys `finish_reason=length` with nothing but thinking behind it. Measured
        live today: `critique` spent its 900 tokens on 4,364 characters of reasoning
        and produced no content, and the stage failed. Widening the answer budget is
        not the concession it looks like — the M3 finding is that decode is
        preemptible and fair while it is the *prefill* ceiling that protects the
        interactive user — but it is still a concession, so it happens once, capped,
        and is recorded as an event rather than folded into the default silently.
        """
        comp = await self.llm.stream(req)
        if comp.usable or comp.finish_reason != "length":
            return comp
        ceiling = int(self.cfg.get("inference.length_retry_max_tokens", 3000))
        if req.max_tokens >= ceiling:
            return comp
        next_budget = min(ceiling, int(req.max_tokens * 2))
        self.emit("runner.length_retry",
                  f"{task.kind} used all {req.max_tokens} tokens on "
                  f"{len(comp.reasoning)}c of reasoning with no answer; retrying at "
                  f"{next_budget}",
                  {"task_id": task.id, "stage": task.kind, "from": req.max_tokens,
                   "to": next_budget, "reasoning_chars": len(comp.reasoning)})
        return await self.llm.stream(replace(req, max_tokens=next_budget))

    async def _thinking(self, task: db.Task) -> Outcome:
        t0 = time.perf_counter()
        try:
            req = self.prompt_for(task)
        except (safety.PathRefused, safety.GitRefused) as exc:
            db.finish(self.conn, task.id, db.REJECTED, error=f"safety: {exc}",
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.rejected", f"{task.kind} refused by safety: {exc}",
                      {"task_id": task.id})
            return Outcome(task.id, db.REJECTED, reason=str(exc))

        # Bound before the await: a stream that fails at connect time never
        # returns a Completion, and every failure path below wants to report on
        # it. An unbound name in the error handler is the worst place in this
        # codebase to discover a bug, because it hides behind the first one.
        comp: Completion | None = None
        try:
            comp = await self._stream_with_retry(req, task)
        except asyncio.CancelledError:
            # Record, then propagate. Without the accounting the events table
            # shows a stage that vanished and a GPU busy for nine seconds with
            # nothing to show for it, which reads as a metering bug rather than
            # the preemption it was.
            self.aborts += 1
            self.emit("runner.cancelled",
                      f"{task.generator}.{task.kind} cancelled after "
                      f"{time.perf_counter() - t0:.2f}s",
                      {"task_id": task.id, "llm": self.llm.state()})
            raise
        except (EndpointGone, PrefillOver) as exc:
            db.finish(self.conn, task.id, db.FAILED, error=str(exc),
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"{task.kind}: {exc}", {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=str(exc), completion=comp)
        except Exception as exc:
            db.finish(self.conn, task.id, db.FAILED,
                      error=f"{type(exc).__name__}: {exc}",
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"{task.kind}: {type(exc).__name__}: {exc}",
                      {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=str(exc), completion=comp)

        if not comp.usable:
            reason = f"no content (finish={comp.finish_reason}, " \
                     f"reasoning={len(comp.reasoning)}c)"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      input_tokens=comp.prompt_tokens,
                      output_tokens=comp.completion_tokens,
                      ttft_ms=(comp.ttft_s or 0) * 1000,
                      duration_ms=comp.elapsed_s * 1000)
            self.emit("runner.empty", f"{task.kind} produced no usable answer",
                      {"task_id": task.id, **comp.as_dict()})
            return Outcome(task.id, db.FAILED, reason=reason, completion=comp)

        if task.kind == "evaluate":
            return self._evaluate(task, comp, t0)

        flags = safety.looks_instructed(comp.text)
        if flags:
            self.emit("safety.injection_flags",
                      f"{task.kind} output carries instruction-shaped text: "
                      f"{', '.join(flags)}", {"task_id": task.id})
        doc = self.document(task, comp, flags)
        try:
            path, digest = await asyncio.to_thread(
                safety.write_workspace_file, self.cfg, task.id,
                f"{task.generator}-{task.kind}.md", doc)
        except safety.PathRefused as exc:
            db.finish(self.conn, task.id, db.FAILED, error=str(exc))
            return Outcome(task.id, db.FAILED, reason=str(exc), completion=comp)

        payload_patch: dict[str, Any] = {}
        if task.kind == "synthesize":
            payload_patch["draft_path"] = str(path)
        if payload_patch:
            db.merge_payload(self.conn, task.id, payload_patch)

        db.finish(self.conn, task.id, db.SUCCEEDED, result_path=str(path),
                  result_hash=digest, input_tokens=comp.prompt_tokens,
                  output_tokens=comp.completion_tokens,
                  ttft_ms=(comp.ttft_s or 0) * 1000,
                  duration_ms=comp.elapsed_s * 1000)
        # An artifact is an output, not a stage. Every thinking stage registered a
        # row here, which turned `artifacts` into a scratch index — plan, extract
        # and critique files listed alongside real outputs, so the digest reported
        # "21 artifacts today" when today was four stages of one chain plus a
        # draft. The draft is the output-in-waiting, the thing publish promotes,
        # and the only intermediate worth a row. The others are still kept and still
        # reachable through `tasks.result_path`; kept and listed are different words.
        if task.kind in chains.DRAFT_KINDS:
            register_artifact(self.conn, task, str(path), digest, status=db.CANDIDATE)
        self.emit("runner.stage",
                  f"{task.generator}.{task.kind} -> {path.name} "
                  f"({len(comp.text)} chars, {comp.completion_tokens} tok, "
                  f"{comp.elapsed_s:.1f}s, usage={comp.usage_source})",
                  {"task_id": task.id, "path": str(path), "gpu": True,
                   **comp.as_dict()})
        kids = chains.advance(self.cfg, self.conn, task, result_text=comp.text)
        self.last_stage_ms[task.kind] = round((time.perf_counter() - t0) * 1000, 1)
        return Outcome(task.id, db.SUCCEEDED, path=str(path), completion=comp,
                       children=tuple(k.id for k in kids))

    def _evaluate(self, task: db.Task, comp: Completion, t0: float) -> Outcome:
        """Parse the rubric, store the judgement, decide whether publish exists.

        An unparsable judgement is a FAILED stage, not a REJECT. "The judge could
        not read the score sheet" and "the work was not good enough" are different
        facts, and collapsing them means a formatting hiccup silently becomes a
        quality decision that you cannot tell apart from the real thing later.
        """
        threshold = float(self.cfg.get("quality.publish_threshold", 3.5))
        ev = eval_mod.parse_eval(comp.text, threshold=threshold)
        # How much of the draft the judge actually read, recorded alongside the
        # score it produced from that text.
        draft = self._draft_text(task)
        sources = [str(s.get("url")) for s in
                   (task.payload.get("sources") or []) if s.get("url")]
        excerpt, _ceiling = self._excerpt_budget(
            str(task.payload.get("topic") or task.title), sources)
        ev.draft_chars = len(draft)
        ev.evaluated_chars = min(len(draft), excerpt)
        eval_mod.record(self.conn, task.id, ev)
        db.merge_payload(self.conn, task.id, {"score": ev.overall,
                                              "verdict": ev.verdict,
                                              "rationale": ev.rationale})
        if not ev.parsed:
            reason = "evaluation output did not parse"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      input_tokens=comp.prompt_tokens,
                      output_tokens=comp.completion_tokens,
                      ttft_ms=(comp.ttft_s or 0) * 1000,
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed",
                      f"evaluate: {reason}; raw={ev.raw[:120]!r}",
                      {"task_id": task.id, **ev.as_dict()})
            return Outcome(task.id, db.FAILED, reason=reason, completion=comp)

        db.finish(self.conn, task.id, db.SUCCEEDED, score=ev.overall,
                  input_tokens=comp.prompt_tokens,
                  output_tokens=comp.completion_tokens,
                  ttft_ms=(comp.ttft_s or 0) * 1000,
                  duration_ms=(time.perf_counter() - t0) * 1000)
        self.emit("runner.evaluated",
                  f"{task.payload.get('topic', task.title)[:60]} -> "
                  f"{ev.overall:.2f} {ev.verdict}",
                  {"task_id": task.id, **ev.as_dict()})
        kids = chains.advance(self.cfg, self.conn, task, result_text=comp.text)
        return Outcome(task.id, db.SUCCEEDED, completion=comp,
                       children=tuple(k.id for k in kids))

    # --- the document ----------------------------------------------------

    def document(self, task: db.Task, comp: Completion,
                 flags: tuple[str, ...]) -> str:
        """The artifact. The header carries provenance and accounting, because an
        output with no record of what it cost is not auditable."""
        lines = [
            f"# {task.title}",
            "",
            f"- generator: `{task.generator}`  kind: `{task.kind}`",
            f"- task: `{task.id}`  chain: `{task.chain_id}`",
            f"- model: `{self.llm.model}`  {comp.elapsed_s:.1f}s, "
            f"TTFT {(comp.ttft_s or 0) * 1000:.0f}ms ({comp.usage_source}), "
            f"finished: {comp.finish_reason or '?'}",
            f"- tokens: {comp.prompt_tokens} in / {comp.completion_tokens} out",
        ]
        if comp.gen_tps:
            lines.append(f"- speed: {comp.gen_tps:.0f} tok/s decode")
        if flags:
            lines.append(f"- **injection flags (not blocked, flagged):** "
                         f"{', '.join(flags)}")
        if comp.reasoning:
            lines += ["", "## reasoning (excerpt)", "",
                      comp.reasoning[:600].strip(), ""]
        lines += ["## result", "", comp.text.strip(), ""]
        if comp.finish_reason == "length":
            # Loud, in the artifact, not in a log nobody reads: a truncated answer
            # that looks complete is the failure mode of every summariser ever
            # written, and the reader deserves to know.
            lines += ["", "> **truncated**: the model hit its output budget "
                       f"({comp.completion_tokens} tokens). Raise "
                       "`inference.stage_max_tokens` for this stage or narrow the "
                       "subject.", ""]
        return "\n".join(lines)

    def state(self) -> dict[str, Any]:
        return {"runs": self.runs, "aborts": self.aborts,
                "stage_ms": self.last_stage_ms,
                "search": self.searcher.state(), **self.llm.state()}


def read_text_if_any(path: str | Path | None) -> str:
    """Read a file that might not exist. Its own function so it can be handed to
    `to_thread` without a lambda wrapping a conditional, which is how a blocking
    read hides inside an async function."""
    if not path:
        return ""
    p = Path(str(path))
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def slug_dirname(task: db.Task) -> str:
    return f"{chains.slug(task.payload.get('topic') or task.title)}"


def promote_candidate(conn: sqlite3.Connection, task: db.Task, path: str,
                      digest: str, score: float | None) -> None:
    """Turn the chain's draft row into the published row instead of adding another.

    The published file is the synthesize draft with its secrets redacted, so it is
    the same artifact at a later stage of being real, not a new artifact. Two rows
    for one output means every reader has to deduplicate by hash before it can
    count anything, which is how a four-stage chain started reporting a four-item
    day. If there is no candidate — a hand-queued publish, or a chain that arrived
    from an older binary — a row is created, because a published file that appears
    nowhere is the same as work that never happened.
    """
    row = conn.execute(
        "SELECT id FROM artifacts WHERE status=? AND task_id IN"
        " (SELECT id FROM tasks WHERE chain_id=?) ORDER BY created_at DESC"
        " LIMIT 1", (db.CANDIDATE, task.chain_id)).fetchone()
    if row:
        conn.execute("UPDATE artifacts SET status=?, path=?, hash=?"
                     ", score=? WHERE id=?",
                     (db.PUBLISHED, path, digest, score, row["id"]))
    else:
        conn.execute(
            "INSERT INTO artifacts (id, task_id, generator, title, path, hash, score,"
            " scores_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (db.new_id(), task.id, task.generator, task.title, path, digest, score,
             None, db.PUBLISHED, db.now()))
    conn.commit()


def register_artifact(conn: sqlite3.Connection, task: db.Task, path: str,
                      digest: str, *, status: str = db.CANDIDATE) -> None:
    """Record an artifact. `CANDIDATE` is what a thinking stage produces: it
    exists, it is readable, and nothing has judged it yet. `publish` promotes it.
    A stage that wrote a file and recorded nothing is an artifact that never
    appears in the kitchen, which is the same as not having done the work."""
    conn.execute(
        "INSERT OR REPLACE INTO artifacts (id, task_id, generator, title, path,"
        " hash, score, scores_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (db.new_id(), task.id, task.generator, task.title, path, digest, None,
         None, status, db.now()))
    conn.commit()

