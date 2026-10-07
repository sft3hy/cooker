"""digest: `outputs/YYYY-MM-DD/digest/digest.md`, the only UI you have to read.

The spec's requirement that matters most is the negative one: *if nothing scored
above the threshold, the digest says so*. A digest that silently omits a bad day
is indistinguishable from a daemon that silently did nothing, and the difference
between those two states is precisely what you cannot tell from an empty file.

So every section here answers a question you would otherwise have to ask:

    what is worth reading      published, scored, with the evaluator's rationale
    what was cooked and rejected   the ones below threshold, and why — kept
    what it cost               tokens, GPU seconds, preemptions, time in each state
    what it is waiting for     the reason it is idle right now, in its own words

Numbers are computed from the database, never from memory. An empty day reports
zeros, not absences, and a day where the daemon was polite and therefore did
almost nothing says that explicitly — because "it cooked all night" and "it never
got a turn" both look like an empty morning unless the file distinguishes them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from cooker import chains, db, safety
from cooker import eval as eval_mod
from cooker.config import Config


def day_bounds(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day).timestamp()
    return start, start + 86400.0


def _score_for(conn: sqlite3.Connection, chain_id: str) -> dict[str, Any]:
    """The chain's judgement, whichever stage's task the evaluation hung on.

    The evaluator is its own stage, so its row is keyed to the `evaluate` task
    while the artifact on disk belongs to `synthesize`. Joining an artifact straight
    to its own task_id finds nothing for either of them and reports a scored,
    rejected draft as *unjudged* — the worst error a digest can make, because it
    claims the gate never ran on the one thing the gate did turn away.
    """
    empty = {"overall": 0.0, "verdict": "UNEVALUATED", "axes": "",
             "partial": ""}
    if not chain_id:
        return empty
    row = conn.execute(
        "SELECT e.overall, e.verdict, e.scores_json, e.draft_chars,"
        " e.evaluated_chars FROM evaluations e JOIN tasks t ON t.id = e.task_id"
        " WHERE t.chain_id=? ORDER BY e.created_at DESC LIMIT 1",
        (chain_id,)).fetchone()
    if not row:
        return empty
    try:
        scores = json.loads(row["scores_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        scores = {}
    axes = " ".join(f"{k[:4]}={v}" for k, v in sorted(scores.items()))
    partial = ""
    draft_chars = int(row["draft_chars"] or 0)
    read_chars = int(row["evaluated_chars"] or 0)
    if draft_chars and read_chars < draft_chars:
        # The label is the whole point: without it a score read off two thirds of a
        # document is indistinguishable from a score read off the document.
        partial = (f"scored on the first {read_chars:,} of {draft_chars:,}"
                   f" characters")
    return {"overall": float(row["overall"]), "verdict": str(row["verdict"]),
            "axes": axes, "partial": partial}


def day_artifacts(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    """Today's outputs, judged by their chain.

    Two filters, both load-bearing.

    *Only outputs.* Rows registered before the staging fix are plans, extracts and
    critiques — scaffolding that exists and is kept on disk, but is not something
    the digest should offer to read. What counts as an output is decided by the
    stage that wrote it, never by its status: filtering on status would hide
    rejected work, and rejected work staying visible is the rule.

    *Scored by chain.* The judgement sits on the `evaluate` task; the file belongs
    to `synthesize`. Joining the two directly reports a judged, rejected draft as
    never judged.
    """
    lo, hi = day_bounds(day)
    # An artifact is an output if the stage that wrote it was a drafting stage or
    # the publish stage. Filtering on status instead would hide rejected work, and
    # rejected work staying visible is the whole rule: a rejection is information.
    outputs = sorted(chains.DRAFT_KINDS | {"publish"})
    ph = ",".join("?" * len(outputs))
    rows = conn.execute(
        "SELECT a.id, a.task_id, a.generator, a.title, a.path, a.hash, a.status,"
        " t.chain_id, t.kind, t.score, t.input_tokens, t.output_tokens,"
        " t.duration_ms, t.ttft_ms"
        " FROM artifacts a LEFT JOIN tasks t ON t.id = a.task_id"
        f" WHERE a.created_at >= ? AND a.created_at < ?"
        f"   AND (t.kind IS NULL OR t.kind IN ({ph}))"
        " ORDER BY COALESCE(t.score, 0) DESC, a.created_at ASC",
        (lo, hi, *outputs)).fetchall()
    out: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for r in rows:
        # One row per artifact *content*: synthesize registers the draft and
        # publish promotes that same row, but a chain from before that change holds
        # two, and showing both makes a two-item day look like a four-item day.
        if r["hash"] in seen_hashes:
            continue
        seen_hashes.add(str(r["hash"]))
        out.append({**dict(r), **_score_for(conn, str(r["chain_id"] or ""))})
    return out
    return out


def day_cost(conn: sqlite3.Connection, day: date) -> dict[str, Any]:
    lo, hi = day_bounds(day)
    # COALESCE, not just started_at: a stage invoked without going through
    # `claim_next` — a retry, a direct runner call, anything that did not take the
    # write lock — has no started_at, and counting only claimed stages reported
    # 490 tokens for a day that had genuinely spent several thousand. A stage that
    # finished today ran today, however it got there.
    row = conn.execute(
        "SELECT COUNT(*) AS stages,"
        " COALESCE(SUM(input_tokens),0) AS tin,"
        " COALESCE(SUM(output_tokens),0) AS tout,"
        " COALESCE(SUM(duration_ms),0) AS ms,"
        " COALESCE(SUM(CASE WHEN preemptions>0 THEN 1 ELSE 0 END),0) AS pre,"
        " COALESCE(AVG(ttft_ms),0) AS ttft"
        " FROM tasks WHERE COALESCE(started_at, finished_at) >= ?"
        " AND COALESCE(started_at, finished_at) < ?", (lo, hi)).fetchone()
    return {"stages": int(row["stages"] or 0), "input_tokens": int(row["tin"] or 0),
            "output_tokens": int(row["tout"] or 0),
            "gpu_seconds": round(float(row["ms"] or 0) / 1000.0, 1),
            "preemptions": int(row["pre"] or 0),
            "avg_ttft_ms": round(float(row["ttft"] or 0), 0)}


def _why_idle(conn: sqlite3.Connection) -> str:
    """The detector's own last word. A digest that says "idle" when the truth is
    "busy with you, and I am waiting 60 seconds out of politeness" is a digest
    that tells you the wrong thing about your own machine.

    The type list is the daemon's actual vocabulary, checked against the event
    table rather than guessed at. Getting this wrong produces "no state recorded
    yet" on a database holding a hundred and seventeen `bus.state` rows, which is
    a confidently stated falsehood in the one line written for a human.
    """
    row = conn.execute(
        "SELECT type, message FROM events WHERE type IN "
        "('bus.state','sched.hold','dry.would_seed','topics.exhausted',"
        "'search.failed','chain.stalled','chain.rejected','fetch.failed',"
        "'chain.stub','daemon.stop') ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return "no state recorded yet"
    return f"{row['type']}: {str(row['message'] or '')[:90]}"


def render(cfg: Config, conn: sqlite3.Connection, day: date) -> str:
    threshold = float(cfg.get("quality.publish_threshold", 3.5))
    items = day_artifacts(conn, day)
    cost = day_cost(conn, day)
    published = [i for i in items if i["overall"] >= threshold]
    rejected = [i for i in items if 0 < i["overall"] < threshold]
    unjudged = [i for i in items if i["verdict"] == "UNEVALUATED"]

    lines = [
        f"# Cooker digest — {day.strftime('%A %d %B %Y')}",
        "",
        f"Threshold {threshold}. {len(published)} passed, {len(rejected)} rejected"
        + (f", {len(unjudged)} unjudged" if unjudged else "") + ".",
        "",
    ]

    if not published:
        # The spec's negative case, stated plainly rather than left to inference.
        lines += [
            "**Nothing scored above the threshold today.**",
            "",
            f"- stages run: {cost['stages']}   gpu seconds: {cost['gpu_seconds']}"
            f"   tokens: {cost['input_tokens']:,} in / {cost['output_tokens']:,} out",
            f"- preemptions: {cost['preemptions']}",
            f"- current reason: {_why_idle(conn)}",
            "",
        ]
        if cost["stages"] == 0:
            lines.append("It ran no stages. Either the queue was empty and nothing "
                        "seeded, or the machine was busy with you and Cooker "
                        "correctly declined.")
        elif rejected:
            lines.append("It cooked things and judged them not worth your time. "
                        "Those are listed below — rejected work stays, because a "
                        "rejection is information.")
        else:
            lines.append("It cooked, but nothing reached evaluation.")
        lines.append("")
    else:
        lines += ["## Worth reading", ""]
        for i in published:
            lines.append(f"### {_title(str(i['title']))}")
            lines.append("")
            lines.append(f"- score **{i['overall']:.2f}** ({i['axes']})")
            if i.get("partial"):
                lines.append(f"- *{i['partial']}*")
            rel = _relative(cfg, str(i["path"]))
            lines.append(f"- read: `{rel}`")
            rationale = _rationale(conn, str(i["chain_id"] or ""))
            if rationale:
                lines.append(f"- evaluator: {rationale}")
            excerpt = _excerpt(str(i["path"]), cfg)
            if excerpt:
                lines += ["", f"> {excerpt}", ""]
            lines.append(f"- rate it: `cooker rate "
                         f"{eval_mod.short_id(str(i['id']))} good|meh|bad`")
            lines.append("")

    if rejected:
        lines += ["## Cooked, not published", ""]
        for i in rejected:
            note = f" *({i['partial']})*" if i.get("partial") else ""
            lines.append(f"- **{i['overall']:.2f}** {_title(str(i['title']))[:80]} "
                         f"({i['axes']}){note} — `{_relative(cfg, str(i['path']))}`")
            reason = _rationale(conn, str(i["chain_id"] or ""))
            if reason:
                lines.append(f"  - evaluator: {reason}")
            # Ratings on rejected work matter most: this is where you can say the
            # judge was wrong, and the EMA is the only thing that remembers.
            lines.append(f"  - disagree? `cooker rate "
                         f"{eval_mod.short_id(str(i['id']))} good`")
        lines.append("")

    if unjudged:
        lines += ["## Cooked, never judged", ""]
        for i in unjudged:
            lines.append(f"- {_title(str(i['title']))[:80]} — the evaluate stage "
                         f"did not run or did not parse for this one")
        lines.append("")

    lines += ["---", "",
              f"**Day cost:** {cost['stages']} stages, {cost['gpu_seconds']}s of GPU,"
              f" {cost['input_tokens']:,} in / {cost['output_tokens']:,} out,"
              f" {cost['preemptions']} preemption(s),"
              f" avg TTFT {cost['avg_ttft_ms']:.0f}ms.",
              "",
              f"**Allowed generators:** {_allowed_text(cfg, conn)}",
              ""]
    parked = eval_mod.parked_generators(cfg, conn)
    if parked:
        lines += ["**Parked:**"] + [f"- `{g}` — {r}" for g, r in parked.items()] + [""]
    counts = db.queue_counts(conn)
    # `or` binds looser than `+`, so `prefix + joined or "empty"` can never fall
    # back to empty: the prefix is always present, so the concatenation is always
    # truthy. Join first, default second.
    # Only live states belong in the headline: "37 succeeded" at 07:00 says nothing
    # about what the queue will do next, and burying `2 blocked` under it does.
    live = {k: v for k, v in counts.items()
            if k in ("QUEUED", "RUNNING", "PENDING", "BLOCKED") and v}
    queued = ", ".join(f"{v} {k.lower()}" for k, v in live.items())
    done = ", ".join(f"{v} {k.lower()}" for k, v in counts.items()
                     if k in ("SUCCEEDED", "FAILED", "REJECTED", "CANCELLED") and v)
    lines.append(f"**Queue:** {queued or 'empty'}")
    if done:
        lines.append(f"**Today, terminal:** {done}")
    lines.append("")
    return "\n".join(lines)


def _allowed_text(cfg: Config, conn: sqlite3.Connection) -> str:
    """`unfiltered` and `none` are different sentences and must not print the
    same one: the first means nothing was excluded because nothing was declared,
    the second means every generator is switched off."""
    allowed = eval_mod.allowed_generators(cfg, conn)
    if allowed is None:
        return "unfiltered (no generators declared in config)"
    return ", ".join(allowed) if allowed else "none — everything is disabled or parked"


def _title(raw: str) -> str:
    """The topic, without the stage prefix the queue puts on every row.

    Stripped in one place rather than per-section so a new stage name cannot show
    up as "`synthesize: gpu scheduling`" in the digest of all things.
    """
    text = str(raw)
    for prefix in ("publish: ", "synthesize: ", "critique: ", "evaluate: ",
                   "extract: ", "plan: ", "fetch: "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def _relative(cfg: Config, path: str) -> str:
    try:
        return str(Path(path).relative_to(Path(cfg.data_dir).parent))
    except ValueError:
        return str(path)


def _rationale(conn: sqlite3.Connection, chain_id: str) -> str:
    """The evaluator's own sentence, read back from the row it wrote.

    Keyed by chain for the same reason `_score_for` is: the evaluator's row hangs
    off the `evaluate` task, not the artifact's. Read rather than re-derived — a
    second inference call to find out why something scored 4.2 is the wrong shape
    of expensive for a file you open every morning.
    """
    if not chain_id:
        return ""
    row = conn.execute("SELECT e.rationale FROM evaluations e"
                       " JOIN tasks t ON t.id = e.task_id WHERE t.chain_id=?"
                       " ORDER BY e.created_at DESC LIMIT 1", (chain_id,)).fetchone()
    return str(row["rationale"])[:300] if row and row["rationale"] else ""


def _excerpt(path: str, cfg: Config, *, limit: int = 300) -> str:
    p = Path(str(path))
    if not p.exists():
        return ""
    body = p.read_text(encoding="utf-8", errors="replace")
    text = body.split("## result", 1)[1] if "## result" in body else body
    for line in text.splitlines():
        s = line.strip()
        if len(s) > 40 and not s.startswith(("#", "-", ">", "*", "[")):
            return s[:limit] + ("…" if len(s) > limit else "")
    return ""


def digest_path(cfg: Config, day: date) -> Path:
    """outputs/YYYY-MM-DD/digest/digest.md, per the output layout."""
    return chains.day_dir(cfg, day=day) / "digest" / "digest.md"


def write(cfg: Config, conn: sqlite3.Connection,
          *, day: date | None = None) -> tuple[Path, int]:
    """Render and write the digest. Returns the path and the published count."""
    day = day or date.today()
    text = render(cfg, conn, day)
    scan = safety.scan_secrets(text, source=f"digest:{day.isoformat()}")
    if scan.findings:
        db.emit(conn, "safety.redaction",
                message=f"redacted {sum(f.count for f in scan.findings)} span(s) "
                      f"from the digest",
                data={"day": day.isoformat(), **scan.as_dict()})
    target = digest_path(cfg, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(scan.text, encoding="utf-8")
    items = day_artifacts(conn, day)
    threshold = float(cfg.get("quality.publish_threshold", 3.5))
    published = sum(1 for i in items if i["overall"] >= threshold)
    db.emit(conn, "digest.written", message=f"{target} ({published} published)",
            data={"path": str(target), "published": published,
                 "chars": len(scan.text)})
    return target, published
