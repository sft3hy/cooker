"""Bench #9: the research chain, run for real. Test 1 of the plan.

Empty queue -> a topic is seeded -> plan/search/fetch/extract x N/synthesize/
critique/publish -> the queue drains and a file exists that cites real sources.

What this bypasses, and why it says so
---------------------------------------
It drives the runner directly rather than through the detector, because the
detector's job is to refuse to cook while you are typing — and while an OpenCode
session is live, that refusal is *always* correct, so a run that waited for the
gate would report "nothing happened" and prove nothing about the DAG. The gate is
proven separately: bench #8 for preemption, `test_dry_run_reports_a_topic_and_
writes_no_tasks` for seeding honesty, `test_fan_out_..._fans_in` for claimability.

Cost, deliberately small: 1 query, 2 sources, ~5 small completions, every prefill
under the 1 000-token ceiling `llm.py` enforces before the bytes leave the process.

    .venv/bin/python bench/bench9_research_chain_live.py [topic]
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooker import chains, db, runner
from cooker.config import load_config

TOPIC = "why does a long prefill block other requests on a local inference server"


async def main() -> int:
    cfg = load_config().with_overrides(**{
        "research.max_queries": 1,
        "research.max_sources": 2,
        "research.source_context_chars": 1200,
        "search.max_results": 4,
    })
    topic = sys.argv[1] if len(sys.argv) > 1 else TOPIC

    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    from cooker.llm import LLM
    llm = LLM(cfg)
    r = runner.StageRunner(cfg, conn, llm)

    print(f"queue before: {db.queue_counts(conn)}")
    started = db.queue_counts(conn)["QUEUED"] == 0
    chain = chains.seed(cfg, conn, topic)
    print(f"seeded from an {'empty' if started else 'occupied'} queue: {chain[:8]}")

    rc = 1
    try:
        order: list[str] = []
        guard = 0
        while guard < 25:
            guard += 1
            # Only this chain's stages. `claim_next` happily picks up the leftover
            # demo stages from M1 and M2 sitting in the same database, which is
            # correct behaviour for the daemon and noise in a bench that is trying
            # to show one DAG resolving stage by stage.
            mine = [t for t in db.peek_runnable(conn, limit=50)
                   if t.chain_id == chain]
            task = mine[0] if mine else None
            if task is None:
                db.propagate_blocked(conn)
                st_now = chains.chain_state(conn, chain)
                if st_now["state"] == "complete":
                    break
                pending = [f"{s['kind']}={s['status']}" for s in st_now["stages"]
                          if s["status"] != "SUCCEEDED"]
                # Nothing runnable and nothing waiting means this chain is done or
                # dead; spinning another twenty times tells us neither.
                if not any(s["status"] == "QUEUED" for s in st_now["stages"]):
                    print(f"  chain settled; pending: {pending}")
                    break
                print(f"  nothing claimable in this chain; pending: {pending}")
                continue
            t0 = asyncio.get_event_loop().time()
            out = await r(task)
            dt = asyncio.get_event_loop().time() - t0
            order.append(f"{task.kind}:{out.status}")
            # A stage either spent tokens or it did not; there is no third case,
            # so this is a branch and not a hasattr probe guessing at a schema.
            tok = (f"{out.completion.prompt_tokens}in/"
                   f"{out.completion.completion_tokens}out"
                   if out.completion else "no GPU")
            extra = ""
            if out.children:
                extra = f" -> +{len(out.children)} stage(s)"
            print(f"  {task.kind:<11} {out.status:<9} {dt:6.2f}s {tok:<16}{extra}")
            if out.status != db.SUCCEEDED:
                print(f"     reason: {out.reason}")
            db.propagate_blocked(conn)

        st = chains.chain_state(conn, chain)
        kinds_done = [s["kind"] for s in st["stages"]]
        print(f"\nstage order: {kinds_done}")
        print(f"chain: {st['done']}/{st['total']} {st['state']}")
        extracts = [s for s in st["stages"] if s["kind"] == "extract"]
        synth = [s for s in st["stages"] if s["kind"] == "synthesize"]
        extract_ids = [e["id"] for e in extracts]
        fan_in = bool(synth) and all(d in extract_ids
                                      for d in synth[0]["dependencies"])
        print(f"fan-out: {len(extracts)} extracts; fan-in deps resolved: {fan_in}")

        published = list(chains.day_dir(cfg).glob("*.md"))
        newest = max(published, key=lambda p: p.stat().st_mtime) if published else None
        body = newest.read_text() if newest else ""
        # Print the RESULT section, not the first prose in the file: the reasoning
        # excerpt sits above it, and quoting that made a complete artifact look
        # like a chain of thought that never got to the answer.
        result = body.split("## result", 1)[1].split(">", 1)[0].strip() \
            if "## result" in body else ""
        if newest:
            print(f"\npublished: {newest.name} ({len(body):,} chars)")
            print("  " + "-" * 68)
            for line in result.splitlines():
                if line.strip():
                    print(f"  {line[:100]}")
                    if len([c for c in result.splitlines() if c.strip()]) > 12:
                        break
            print("  " + "-" * 68)
            truncated = "truncated" in body

        # Judged on THIS chain. The global queue also holds the M1/M2 demo
        # stages that were in the database before this run, and their leftovers are
        # not this test's failure to explain.
        mine_queued = [s for s in st["stages"] if s["status"] == "QUEUED"]
        drained = not mine_queued
        ok = (st["state"] == "complete" and len(extracts) >= 1 and newest is not None
              and drained and not truncated and len(result) > 200)
        rc = 0 if ok else 1
        gpu_kinds = ("plan", "extract", "synthesize", "critique")
        free_kinds = ("search", "fetch", "publish")
        print(f"\ndrained: {drained}   "
              f"gpu stages: {sum(1 for k in kinds_done if k in gpu_kinds)}   "
              f"free stages: {sum(1 for k in kinds_done if k in free_kinds)}   "
              f"truncated: {truncated}")
        print("\nVERDICT:", "PASS — the queue self-filled, the DAG resolved, and "
              "a complete artifact was published" if ok else
              f"FAIL — stages above, truncated={truncated}, "
              f"result_chars={len(result)}")
        return rc
    finally:
        await llm.aclose()
        with contextlib.suppress(Exception):
            await r.searcher.aclose()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
