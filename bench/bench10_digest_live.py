#!/usr/bin/env python3
"""Test 3 — the first digest containing real, evaluated outputs.

Live against omlx and SearXNG. Nothing here is mocked: the chain runs, the model
plans, the web is fetched, the draft is written, a second pass scores it, and the
digest is rendered from the rows those stages wrote.

Why it goes through the runner and not the daemon
---------------------------------------------------
The daemon declined to start at all for 151 seconds before this bench was written,
and it was right: `opencode` was holding a streaming connection to omlx at a steady
~1.7 KB/s of decode tokens, which is above `detect.infer_min_bps`, so the detector
said ACTIVE_INFER and Cooker waited. Interactive wins. That is the spec working, but
it also means the daemon cannot be used to demonstrate a digest while the very session
writing this code occupies the GPU.

So this drives the same runner the daemon would drive, in the same order, against the
same endpoints, and says so in the record rather than quietly moving the goalposts.

What makes this a test and not a demo
-------------------------------------
The digest's claims are checked against the database, not against each other. The
score it prints must equal the score the evaluator recorded. The token it prints for
`cooker rate` must resolve to this artifact and no other. And if nothing cleared the
threshold, the bench passes only when the digest *says* so — a high score is not what
is being verified here.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooker import chains, db, digest, runner
from cooker import eval as eval_mod
from cooker.config import load_config

TOPIC = ("how does speculative decoding change tokens-per-second on Apple Silicon "
         "inference servers")


async def main() -> int:
    cfg = load_config().with_overrides(**{
        "research.max_queries": 1,
        "research.max_sources": 2,
        "research.source_context_chars": 1200,
        "search.max_results": 4,
    })
    topic = sys.argv[1] if len(sys.argv) > 1 else TOPIC
    threshold = float(cfg.get("quality.publish_threshold", 3.5))

    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    from cooker.llm import LLM
    llm = LLM(cfg)
    r = runner.StageRunner(cfg, conn, llm)

    print(f"threshold {threshold}   model {llm.model}")
    print(f"queue before: {db.queue_counts(conn)}")
    t_start = time.perf_counter()
    chain = chains.seed(cfg, conn, topic)
    print(f"chain {chain[:8]}  topic {topic[:64]}")

    order: list[str] = []
    fail: list[str] = []
    rc = 0
    try:
        guard = 0
        while guard < 25:
            guard += 1
            mine = [t for t in db.peek_runnable(conn, limit=50)
                   if t.chain_id == chain]
            task = mine[0] if mine else None
            if task is None:
                db.propagate_blocked(conn)
                st = chains.chain_state(conn, chain)
                if st["state"] == "complete":
                    break
                if not any(s["status"] == "QUEUED" for s in st["stages"]):
                    print(f"  chain settled: "
                          f"{[(s['kind'], s['status']) for s in st['stages']]}")
                    break
                continue
            t0 = time.perf_counter()
            out = await r(task)
            dt = time.perf_counter() - t0
            order.append(f"{task.kind}:{out.status}")
            tok = (f"{out.completion.prompt_tokens}in/"
                  f"{out.completion.completion_tokens}out" if out.completion
                  else "no GPU")
            kids = f" -> +{len(out.children)}" if out.children else ""
            print(f"  {task.kind:<11} {out.status:<9} {dt:6.2f}s {tok:<16}{kids}")
            if out.reason:
                print(f"     reason: {out.reason[:100]}")

        # --- the evaluation, from the row rather than from the narration -------
        ev_row = conn.execute(
            "SELECT e.overall, e.verdict, e.scores_json, e.rationale, t.result_path"
            " FROM evaluations e JOIN tasks t ON t.id = e.task_id"
            " WHERE t.chain_id=? ORDER BY e.id DESC LIMIT 1", (chain,)).fetchone()
        print()
        if ev_row is None:
            fail.append("no evaluation row for this chain: the gate never ran, so "
                        "anything published was published unjudged")
        else:
            axes = json.loads(ev_row["scores_json"] or "{}")
            print(f"evaluated: overall={ev_row['overall']:.2f} verdict={ev_row['verdict']}"
                  f" axes={axes}")
            print(f"  rationale: {str(ev_row['rationale'])[:110]}")
            if len(axes) != len(eval_mod.RUBRIC):
                fail.append(f"evaluation stored {len(axes)} axes, want "
                          f"{len(eval_mod.RUBRIC)} — a partial rubric still produced "
                          f"a verdict")
            expected = "PUBLISH" if float(ev_row["overall"]) >= threshold else "REJECT"
            if ev_row["verdict"] != expected:
                fail.append(f"verdict {ev_row['verdict']} contradicts overall "
                          f"{ev_row['overall']} against threshold {threshold}")

        art = conn.execute(
            "SELECT id, path, status, hash, generator FROM artifacts WHERE task_id IN"
            " (SELECT id FROM tasks WHERE chain_id=?) ORDER BY created_at DESC"
            " LIMIT 1", (chain,)).fetchone()
        if art is None:
            fail.append("no artifact row for this chain")
        else:
            print(f"artifact: {art['status']}  {art['path']}")
            if not await asyncio.to_thread(Path(str(art["path"])).exists):
                fail.append("the artifact row points at a file that is not there")

        # --- the digest: checked against the rows, not against itself ----------
        path, published = digest.write(cfg, conn, day=date.today())
        text = await asyncio.to_thread(path.read_text)
        print(f"\ndigest: {path}  ({published} above threshold)")
        wall = time.perf_counter() - t_start
        print(f"wall clock {wall:.1f}s for {len(order)} stage(s)\n")

        if ev_row is not None and art is not None:
            printed_score = f"{float(ev_row['overall']):.2f}"
            if printed_score not in text:
                fail.append(f"digest does not print the recorded score "
                          f"{printed_score}; it invented something else")
            if str(art["path"]).split("/outputs/")[-1] not in text and \
                    str(art["path"]) not in text:
                fail.append("digest does not cite the artifact's real path")
            if ev_row["verdict"] == "PUBLISH":
                if "Worth reading" not in text:
                    fail.append("a PUBLISH verdict did not appear under 'Worth reading'")
            else:
                if "Cooked, not published" not in text:
                    fail.append("a REJECT verdict did not appear under 'Cooked, not"
                               " published' — rejected work is kept, and the digest is"
                               " where kept means visible")
            if float(ev_row["overall"]) < threshold and published == 0 \
                    and "Nothing scored above the threshold" not in text:
                fail.append("nothing cleared the threshold and the digest did not"
                           " say so")

        # every rate token must resolve to the artifact it is printed under, and
        # the chain just cooked must be one of them
        pairs = re.findall(
            r"\*\*([\d.]+)\*\* .+? — `([^`]+)`[\s\S]{0,700}?cooker rate ([0-9a-f]{8})",
            text)
        if not pairs:
            fail.append("the digest printed no rate token; the loop is unreachable"
                        " from the only surface you have to read")
        for _score_s, path_s, tok in pairs:
            hit = eval_mod.resolve_artifact(conn, tok)
            if hit is None:
                fail.append(f"the printed token {tok} resolves to nothing")
                continue
            if Path(str(hit["path"])).name != Path(path_s).name:
                fail.append(f"the token {tok} printed under {path_s} resolves to "
                            f"{hit['path']} — the reader would rate the wrong thing")
        if art is not None and not any(
                Path(str(art["path"])).name == Path(p).name for _, p, _ in pairs):
            fail.append("the draft this chain cooked does not appear in the digest")
        if pairs:
            fb = eval_mod.add_feedback(conn, pairs[0][2], "good")
            print(f"rated the first listed item via its printed token:"
                  f" {fb['generator']} ema={fb['ema']}")

        print()
        print("digest follows:\n" + "-" * 60)
        print(text[:1400])
        print("-" * 60)

        if order and all(s.endswith(":SUCCEEDED") for s in order):
            print("\nVERDICT: PASS — the chain ran live, a second pass scored it, "
                  "and the digest's numbers are the rows' numbers.")
        else:
            print(f"\nVERDICT: BLOCK — stage outcomes: {order}")
            rc = 1
    finally:
        await llm.aclose()
        conn.close()

    if fail:
        print("\nBLOCK, because:")
        for f in fail:
            print(f"  - {f}")
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
