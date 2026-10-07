"""chains: the DAG, and the only place that decides what comes next.

    research:  plan -> search -> fetch -> extract x N -> synthesize -> critique
               -> publish

Why fan-out is decided at `fetch` and not up front
---------------------------------------------------
`extract` fans out one stage per source, and `synthesize` fans in with
`dependencies` = the array of extract ids. The count is not known until the pages
are fetched, so the DAG is built as it runs.

The important consequence is *where* a bad source is dropped. `DEPS_SATISFIED` is
`(SUCCEEDED,)` and `propagate_blocked` parks anything whose dependency can never
succeed — so if we created an `extract` for a cookie wall or a 120-character stub,
that extract would fail, and parking would take `synthesize` down with it. One thin
page would kill the chain.

So unusable sources are filtered at `fetch`, before their children exist. A chain
either fans out over sources it has actually read, or it fails at `fetch` with a
reason. Partial success is then a non-event: every extract that exists has a real
page behind it, and if the endpoint dies mid-extract, parking the synthesizer is
the honest answer rather than a bug to design around.

Why `advance` lives here and not in the runner
----------------------------------------------
The runner's job is to do one stage well: build a prompt, stay cancellable, write
an artifact. Deciding topology is a different question with different invariants,
and putting it in the runner means the runner eventually knows what every generator
does. `advance` is a table of rules the runner calls after a stage succeeds.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from cooker import db, safety
from cooker.config import Config

# The research chain, as data. The order is documentation: the real edges are the
# dependencies built in `advance`, because a comment that drifts from the code is
# worse than no comment.
RESEARCH_CHAIN = ("plan", "search", "fetch", "extract", "synthesize", "critique",
                  "publish")

# Stages that cost no GPU. These run whenever the daemon is allowed to do *any*
# work, and they are what gives the UI something to show before inference is
# permitted at all.
FREE_KINDS = frozenset({"search", "fetch", "publish", "collect", "scan"})

# Stages that think. Kept explicit so that adding a kind to the chain without
# deciding this question is a visible gap rather than a silent GPU spend.
# `consolidate` is the audit generator's synthesis step (PLAN §5) and needs the
# model as much as `synthesize` does; leaving it out would have it refused as an
# unknown kind, or worse, run as a free stage that quietly produced nothing.
THINKING_KINDS = frozenset({"plan", "extract", "synthesize", "critique",
                            "analyze", "consolidate", "write", "draft", "generate"})

# The stage whose text *is* the artifact — exactly one per generator, read off
# PLAN §5: research `synthesize`, project-review `analyze`, homelab-audit
# `consolidate`, brainstorm `generate`, creation and digest `write`. `draft` is
# the kind the pre-M5 queue used and is still what `cooker add` produces.
# Everything else — plans, extracts, critiques — is scaffolding: kept on disk and
# reachable through `tasks.result_path`, but not listed as an output. Registering
# every stage's file turned `artifacts` into a scratch index and made the digest
# report twenty-one items for four chains' worth of stages.
DRAFT_KINDS = frozenset({"synthesize", "analyze", "consolidate", "generate",
                         "write", "draft"})


def slug(text: str, *, limit: int = 48) -> str:
    """Filesystem-safe, readable, and stable for the same input."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:limit].strip("-") or "untitled"


def normalize_topic(text: str) -> str:
    """The form we hash for the 14-day rule.

    Case, punctuation and whitespace are not different topics. Without this the
    same subject researched as "GPU scheduling" and "gpu scheduling." burns two
    budgets and looks like novelty.
    """
    s = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower())
    return re.sub(r"\s+", " ", s).strip()


def topic_hash(text: str) -> str:
    return hashlib.sha256(normalize_topic(text).encode()).hexdigest()


# --- scratch ------------------------------------------------------------


def scratch_dir(cfg: Config, chain_id: str) -> Path:
    """var/scratch/<chain_id>/ — chain working files, never a published artifact.

    Separate from `outputs/` on purpose: outputs is the thing you read and the
    thing `max_disk_gb` promises about, while scratch is intermediate pages that
    should be disposable at any moment. Mixing them means either the budget
    accounting is wrong or the useful files get evicted with the rubbish.
    """
    root = Path(str(cfg.data_dir)) / "scratch"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(chain_id))[:64]
    if not safe.strip("."):
        safe = "chain"
    d = root / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def scratch_path(cfg: Config, chain_id: str, name: str) -> Path:
    """Containment for scratch. Same rule as the workspace, restated rather than
    trusted: `..` is only a problem if you assume nobody sends it."""
    base = scratch_dir(cfg, chain_id).resolve()
    target = (base / str(name)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise safety.PathRefused(f"{target} is outside scratch {base}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_scratch(cfg: Config, chain_id: str, name: str, content: str,
                  *, source: str = "") -> tuple[Path, str, int]:
    """Write a scratch file, scanned. Returns (path, sha256, bytes)."""
    scan = safety.scan_secrets(content, source=source or name)
    p = scratch_path(cfg, chain_id, name)
    p.write_text(scan.text, encoding="utf-8")
    return p, hashlib.sha256(scan.text.encode()).hexdigest(), len(scan.text)


def read_scratch(cfg: Config, chain_id: str, name: str) -> str:
    p = scratch_path(cfg, chain_id, name)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


# --- topics ---------------------------------------------------------------


def topics_file(cfg: Config) -> Path:
    p = Path(str(cfg.get("topics.file", "topics.md")))
    if not p.is_absolute():
        p = Path(str(cfg.root)) / p
    return p


def read_topics(cfg: Config) -> list[str]:
    """`topics.md`: one bullet per line. Comments and blanks ignored.

    The file is the queue's front matter and a human's surface: you can add a
    topic by editing it, and remove one to take it back. It is the reason
    `topics.md` lives in the repo root rather than in the database, because a
    queue you cannot `cat` is a queue you cannot reason about at 11pm.

    That same readability is the hazard. A file you are allowed to annotate in
    prose means the *documentation* is in the file too, and a parser that accepts
    every non-comment line will happily queue "`#` lines are comments" as a
    research subject — which the live topics.md did, six lines of help text ahead
    of the real backlog, each one a research budget away from being generated.

    Three rules, in order of trust:

    * a bulleted or numbered line is a topic, always;
    * in a file that contains no markers at all, bare lines are topics too,
      because the plainest possible topics file is a list of bare lines and
      refusing to read it would move the bug rather than fix it;
    * in a file that does have markers, a bare line joins the queue only once the
      list has started, and never if it reads as a sentence.

    The middle case is the one that keeps the promise the file's own help text
    makes; the third is what stops wrapped prose — where a line ends mid-sentence,
    so "does it end with a full stop" cannot tell documentation from a subject —
    from being researched. If the rule still gets it wrong, it fails toward
    queueing a subject and burning one run, not toward dropping one silently, and
    `cooker status` prints the parsed count so a wrong read is visible at a glance.
    """
    p = topics_file(cfg)
    if not p.exists():
        return []
    raw = [ln.strip() for ln in p.read_text(encoding="utf-8",
                                             errors="replace").splitlines()]
    raw = [ln for ln in raw if ln and not ln.startswith("#")]
    marked_here = any(re.match(r"^([-*+]\s+\S|\d+[.)]\s+\S)", ln) for ln in raw)
    out: list[str] = []
    list_started = False
    for ln in raw:
        marked = bool(re.match(r"^([-*+]\s+\S|\d+[.)]\s+\S)", ln))
        text = re.sub(r"^([-*+]|\d+[.)])\s+", "", ln).strip()
        if len(text) < 8:
            continue
        if marked:
            list_started = True
            out.append(text)
            continue
        if re.search(r"[.!?]$", text):
            continue  # a sentence, in any position
        if marked_here and not list_started:
            continue  # prose sitting above the list
        out.append(text)
    return out


def seen_recently(conn: sqlite3.Connection, h: str, *, days: float) -> bool:
    cutoff = db.now() - days * 86400.0
    row = conn.execute("SELECT first_seen FROM seen WHERE hash=? AND kind=?",
                       (h, "topic")).fetchone()
    return bool(row) and float(row["first_seen"]) >= cutoff


def mark_seen(conn: sqlite3.Connection, h: str, *, kind: str, ref: str) -> None:
    ts = db.now()
    conn.execute(
        "INSERT INTO seen (hash, kind, ref, first_seen, last_seen, hits)"
        " VALUES (?,?,?,?,?,1)"
        " ON CONFLICT(hash) DO UPDATE SET last_seen=excluded.last_seen,"
        " hits=hits+1",
        (h, kind, ref, ts, ts))
    conn.commit()


def next_topic(cfg: Config, conn: sqlite3.Connection,
               *, exclude_hash: str | None = None) -> tuple[str, str] | None:
    """First topic not blocked by the 14-day rule.

    Falls back to the config list so a fresh install with no `topics.md` still
    has somewhere to get its first subject from, rather than quietly doing
    nothing forever and looking healthy while it does it.
    """
    window = float(cfg.get("quality.dedup_window_days", 14))
    candidates = read_topics(cfg) + [str(t) for t in
                                     (cfg.get("topics.fallback", []) or [])]
    for t in candidates:
        h = topic_hash(t)
        if h == exclude_hash:
            continue
        if seen_recently(conn, h, days=window):
            continue
        return t, h
    return None


# --- seeding ------------------------------------------------------------


def seed(cfg: Config, conn: sqlite3.Connection, topic: str,
         *, priority: int = 500, source: str = "topics.md") -> str:
    """Create the head of a research chain and return its chain_id."""
    chain = db.new_id()
    db.create_task(conn, chain_id=chain, kind="plan", generator="research",
                   title=topic[:120], prompt=(
                       "Break this subject into the distinct questions that would "
                       "have to be answered to write a useful brief on it.\n\n"
                       f"Subject: {topic}\n\n"
                       "Reply with 3 to 4 search queries, one per line, no numbering, "
                       "no preamble, each under 90 characters."),
                   payload={"topic": topic, "source": source}, priority=priority)
    mark_seen(conn, topic_hash(topic), kind="topic", ref=chain)
    return chain


def seed_research_if_thirsty(cfg: Config, conn: sqlite3.Connection,
                              *, limit: int = 1) -> list[str]:
    """Self-refill: the queue's reason for existing.

    Refuses when paused, when the queue already holds research, and when the
    generator is disabled — three different silences that must be distinguishable
    in the log, because "it never cooked" needs an answer that isn't a shrug.
    """
    if db.get_meta(conn, "paused") is not None:
        return []
    if not bool(cfg.get("generators.research.enabled", True)):
        return []
    live = int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE generator='research' AND status IN "
        "('QUEUED','RUNNING','PAUSED')").fetchone()[0])
    if live >= int(cfg.get("topics.max_live_research", 2)):
        return []
    picked = next_topic(cfg, conn)
    if picked is None:
        db.emit(conn, "topics.exhausted",
                message="no topic left outside the dedup window",
                data={"file": str(topics_file(cfg))})
        return []
    topic, _ = picked
    made = [seed(cfg, conn, topic)]
    db.emit(conn, "topics.seeded", message=topic[:90],
            data={"chain_id": made[0], "live": live + 1})
    return made


# --- the table of edges ---------------------------------------------------


# The shapes a helpful model uses to introduce a list. Not a query, and a chain
# that searches for "Here are the queries" gets nothing back.
_PREAMBLE = re.compile(
    r"(?i)^(sure|certainly|here(?:'| a)? are|here is|below are|below is|"
    r"the following (?:are|is)|i (?:will|can|have)|okay|ok)\b")


def _queries_from(text: str, *, cap: int) -> list[str]:
    """Parse the planner's answer into queries.

    Defensive on purpose: the model may answer in bullets, numbers, JSON, or
    prose. Each of those is a valid answer from a human's point of view, and the
    chain dying because the punctuation was unexpected is a bug in us, not it.
    """
    out: list[str] = []
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            arr = json.loads(stripped)
            if isinstance(arr, list):
                for x in arr:
                    q = str(x if isinstance(x, str) else x.get("query", "")).strip()
                    if len(q) > 12:
                        out.append(q[:120])
        except (json.JSONDecodeError, AttributeError):
            out = []
    if not out:
        for line in stripped.splitlines():
            s = re.sub(r"^[-*\d.)\s]+", "", line).strip().strip('"')
            # A planner that answers "Sure! Here are the queries:" before the
            # queries is being helpful, and that preamble is not a search query.
            # It would otherwise go to SearXNG verbatim and return nonsense.
            if s.endswith(":"):
                continue
            if _PREAMBLE.match(s):
                continue
            if len(s) > 12 and s.count("|") == 0:
                out.append(s[:120])
    seen: set[str] = set()
    uniq = [q for q in out if not (q.lower() in seen or seen.add(q.lower()))]
    return uniq[:cap]


def advance(cfg: Config, conn: sqlite3.Connection, task: db.Task,
            *, result_text: str = "") -> list[db.Task]:
    """Create whatever the completed stage implies. Returns the new stages.

    The row is re-read from the database rather than trusted from the caller's
    copy. A stage's payload is written by the stage itself via `merge_payload`
    moments before this runs — the candidates `search` found, the sources `fetch`
    kept — and the object the runner still holds predates all of it. Trusting it
    means every hand-off sees an empty payload, stalls the chain, and looks like
    a SearXNG outage in the log rather than a stale pointer in the code.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
    if row is not None:
        task = db.Task.from_row(row)
    kind = task.kind
    gen = task.generator
    payload = dict(task.payload)
    made: list[db.Task] = []

    if gen != "research":
        # M9's generators get their own edges. Saying so, rather than returning an
        # empty list, is what makes "this chain went nowhere" a message instead of
        # a mystery.
        db.emit(conn, "chain.stub",
                message=f"{gen}.{kind} has no edges yet (M9)",
                data={"task_id": task.id})
        return made

    if kind == "plan":
        queries = _queries_from(result_text,
                                cap=int(cfg.get("research.max_queries", 3)))
        if not queries:
            db.emit(conn, "chain.stalled",
                    message=f"plan produced no usable queries for {task.title[:60]}",
                    data={"task_id": task.id, "raw": result_text[:200]})
            return made
        payload["queries"] = queries
        made.append(db.create_task(
            conn, chain_id=task.chain_id, kind="search", generator=gen,
            title=f"search: {task.title[:80]}", parent_task_id=task.id,
            dependencies=[task.id], payload=payload))

    elif kind == "search":
        results = payload.get("results") or []
        if not results:
            db.emit(conn, "chain.stalled",
                    message="search returned no candidates",
                    data={"task_id": task.id})
            return made
        payload["candidates"] = results
        made.append(db.create_task(
            conn, chain_id=task.chain_id, kind="fetch", generator=gen,
            title=f"fetch: {len(results)} candidate(s)", parent_task_id=task.id,
            dependencies=[task.id], payload=payload))

    elif kind == "fetch":
        sources = [s for s in (payload.get("sources") or []) if s.get("ok")]
        if not sources:
            db.emit(conn, "chain.stalled",
                    message="no usable source survived the fetch",
                    data={"task_id": task.id,
                        "candidates": len(payload.get("candidates") or [])})
            return made
        cap = int(cfg.get("research.max_sources", 4))
        extracts: list[str] = []
        for i, src in enumerate(sources[:cap]):
            child = db.create_task(
                conn, chain_id=task.chain_id, kind="extract", generator=gen,
                title=f"extract: {str(src.get('title') or src.get('host'))[:80]}",
                parent_task_id=task.id, dependencies=[task.id],
                payload={"topic": payload.get("topic", task.title),
                       "source": src, "index": i})
            extracts.append(child.id)
            made.append(child)
        # The fan-in. `dependencies` is the array that makes this a DAG rather
        # than a queue: nothing here may start until every extract above has
        # actually finished, and `propagate_blocked` parks it if one cannot.
        made.append(db.create_task(
            conn, chain_id=task.chain_id, kind="synthesize", generator=gen,
            title=f"synthesize: {payload.get('topic', task.title)[:80]}",
            parent_task_id=task.id, dependencies=extracts,
            payload={"topic": payload.get("topic", task.title),
                   "sources": sources[:cap]}))

    elif kind == "synthesize":
        made.append(db.create_task(
            conn, chain_id=task.chain_id, kind="critique", generator=gen,
            title=f"critique: {payload.get('topic', task.title)[:80]}",
            parent_task_id=task.id, dependencies=[task.id], payload=payload))

    elif kind == "critique":
        made.append(db.create_task(
            conn, chain_id=task.chain_id, kind="evaluate", generator=gen,
            title=f"evaluate: {payload.get('topic', task.title)[:80]}",
            parent_task_id=task.id, dependencies=[task.id], payload=payload))

    elif kind == "evaluate":
        # The edge *is* the gate. A rejected draft gets no publish stage at all,
        # so there is nothing to forget to skip; the rejection is recorded and the
        # artifact stays in the database as a rejected thing.
        verdict = str(payload.get("verdict") or "")
        score = payload.get("score")
        if verdict == "PUBLISH":
            made.append(db.create_task(
                conn, chain_id=task.chain_id, kind="publish", generator=gen,
                title=f"publish: {payload.get('topic', task.title)[:80]}",
                parent_task_id=task.id, dependencies=[task.id], payload=payload))
        else:
            db.emit(conn, "chain.rejected",
                    message=f"{payload.get('topic', task.title)[:70]} scored "
                           f"{score} and was not published",
                    data={"chain_id": task.chain_id, "task_id": task.id,
                        "score": score, "verdict": verdict or "UNKNOWN"})

    elif kind == "publish":
        db.emit(conn, "chain.complete", message=f"{task.title[:80]}",
                data={"chain_id": task.chain_id, "task_id": task.id})

    else:
        db.emit(conn, "chain.stub",
                message=f"unknown kind {kind!r} in research chain",
                data={"task_id": task.id})
    return made


# --- reporting ----------------------------------------------------------


def chain_state(conn: sqlite3.Connection, chain_id: str) -> dict[str, Any]:
    """The DAG as it stands, for `cooker status` and the kitchen's pot."""
    rows = conn.execute(
        "SELECT id, kind, status, title, dependencies, duration_ms, result_path,"
        " error FROM tasks WHERE chain_id=? ORDER BY created_at ASC",
        (chain_id,)).fetchall()
    stages = [dict(r) for r in rows]
    for s in stages:
        s["dependencies"] = json.loads(s.get("dependencies") or "[]")
    done = sum(1 for s in stages if s["status"] == db.SUCCEEDED)
    return {"chain_id": chain_id, "stages": stages, "done": done,
            "total": len(stages),
            "state": "complete" if all(s["status"] == db.SUCCEEDED
                                       for s in stages) and stages else "running"}


def day_dir(cfg: Config, *, day: date | None = None) -> Path:
    """outputs/YYYY-MM-DD/ — where published artifacts land.

    Built on `cfg.outputs_dir` rather than re-resolving the knob, because the
    quota in `safety.max_disk_gb` is measured against that property and a second
    definition of "outputs" is how the two stopped agreeing.
    """
    d = cfg.outputs_dir / (day or date.today()).strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d
