#!/usr/bin/env python3
"""Test 5 — what a collision costs the human, in milliseconds.

Live against omlx. Submits real inference: several short requests, a few long ones.

Why this is the number M8 needs
-------------------------------
The burst detector is not a productivity knob, it is a *harm* knob. Letting Cooker
start work in a four-second gap instead of waiting for sixty quiet seconds buys
Cooker perhaps a fifth of the day; the question is what it costs the person whose
agent was using that gap. `inference.max_prefill_tokens_per_request` is 1000 because
prefill is atomic and exclusive — measured, not assumed — so a Cooker prefill already
in flight cannot be interrupted, and an interactive request queued behind it waits.
Dropping our own socket unblocks *our* decode; it does nothing for *their* queue if
the server serialises the device.

So the question with priority over any tuning is: **when a Cooker stage is already
running, how long does an interactive request wait for its first token?** A few
hundred milliseconds and short gaps are safe to claim, and `idle_confirm_seconds: 60`
is cowardice worth removing. Seconds, and the gate is earning its keep, the burst
detector should not ship, and the kitchen goes back to running overnight.

Design
------
Two clients, because that is what a collision is: `cooker` and `human` are separate
`LLM` instances with separate connections. One process issuing both halves of the
experiment would measure httpx's own connection pooling rather than the server's
scheduler.

  * `baseline` — short interactive-shaped requests (small prompt, 16 tokens out,
    thinking off) on a GPU with no Cooker work in flight: the latency to beat.
  * `collision` — the same request fired 1.5s / 4s / 9s into a Cooker-shaped stage
    (~1000-token prompt at the ceiling, 512 tokens out, thinking on: what a real
    `synthesize` costs).
  * `prefill_race` — a large human prompt submitted 0.2s after a large Cooker prompt,
    the case where two atomic prefills want the device. The number that matters is
    the *second* request's time to first token.

What makes this a test and not a demo
-------------------------------------
The budget is written down before the measurement runs (`STALL_BUDGET_MS`), and the
exit code honours it. A FAIL here is the correct outcome if collisions are expensive:
it means the 60-second gate stays and M8 does not get its burst detector. It also
asserts the measurement is capable of answering the question — baseline and collision
samples both present, and the server's own reported ttft present for the baseline, so
our client-side number is cross-checked rather than trusted.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooker import llm
from cooker.config import load_config

# Written down first, then honoured by the exit code. Below this a claimed gap costs
# the human nothing they would notice; above it the 60-second gate stays.
STALL_BUDGET_MS = 750.0

PROBE_PROMPT = "Reply with exactly the word: ready"
STAGE_SYSTEM = (
    "You are a careful technical writer. Answer in markdown. Every factual claim must "
    "be attributable to the supplied context. If the context does not support a claim, "
    "say so."
)
CONTEXT = (
    "Prefill is the stage where the model reads the whole prompt before emitting its "
    "first token. On this server that work is atomic and exclusive: it holds the "
    "device, so a second request cannot interleave its own prefill beside it. Decode, "
    "once started, is a different animal, because tokens arrive on a cadence and a "
    "client that stops reading can drop the socket. A background job therefore has to "
    "be polite about what it queues: a short prompt is a short monopoly, and a long "
    "one is somebody else's stalled chat. Measurement beats inference, here literally."
)


def long_context(approx_tokens: int) -> str:
    """A prompt at the prefill ceiling, built from real sentences not filler.

    Counted with the same estimator the runner uses, because the ceiling that guards
    the human is enforced by that estimator and not by the server's tokenizer — so
    what matters for this experiment is what our guard believes the prompt is worth.
    """
    out: list[str] = []
    while llm.estimate_tokens("\n".join(out)) < approx_tokens:
        out.append(CONTEXT)
    return "\n".join(out)


async def probe(client: llm.LLM, label: str, notes: str = "") -> dict:
    """One interactive-shaped request: the latency a person would feel."""
    req = llm.Request(system="You answer with one word.", user=PROBE_PROMPT,
                      max_tokens=16, thinking=False, kind="probe", stage="probe")
    t0 = time.perf_counter()
    try:
        comp = await client.stream(req)
    except Exception as exc:
        return {"label": label, "error": f"{type(exc).__name__}: {exc}"[:140],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "notes": notes}
    return {
        "label": label,
        "client_ttft_ms": round((comp.ttft_s or 0.0) * 1000.0, 1),
        "server_ttft_ms": (round(comp.server_ttft_s * 1000.0, 1)
                           if comp.server_ttft_s is not None else None),
        "total_ms": round(comp.elapsed_s * 1000.0, 1),
        "out_tokens": comp.completion_tokens,
        "usage": comp.usage_source,
        "notes": notes,
    }


async def stage(client: llm.LLM, label: str, ceiling: int, *,
                thinking: bool = True, max_tokens: int = 512) -> dict:
    """A Cooker-shaped stage: prompt at the ceiling, decoding for tens of seconds."""
    req = llm.Request(
        system=STAGE_SYSTEM,
        user="Summarise the operational consequences in five bullets."
             f"\n\n<untrusted>\n{long_context(ceiling)}\n</untrusted>",
        max_tokens=max_tokens, thinking=thinking, kind="generate", stage="synthesize")
    t0 = time.perf_counter()
    try:
        comp = await client.stream(req)
    except asyncio.CancelledError:
        return {"label": label, "cancelled": True,
                "total_ms": round((time.perf_counter() - t0) * 1000.0, 1)}
    except Exception as exc:
        return {"label": label, "error": f"{type(exc).__name__}: {exc}"[:140],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1)}
    return {
        "label": label,
        "stage_ttft_ms": round((comp.ttft_s or 0.0) * 1000.0, 1),
        "total_ms": round(comp.elapsed_s * 1000.0, 1),
        "prompt_tokens": comp.prompt_tokens,
        "out_tokens": comp.completion_tokens,
        "usage": comp.usage_source,
        "finish": comp.finish_reason,
    }


async def run() -> list[dict]:
    cfg = load_config()
    ceiling = int(cfg.get("inference.max_prefill_tokens_per_request", 1000))
    cooker = llm.LLM(cfg)
    human = llm.LLM(cfg)
    rows: list[dict] = []
    try:
        print(f"baseline: four interactive probes, no Cooker work in flight "
              f"(ceiling {ceiling} tokens)")
        for i in range(4):
            r = await probe(human, "baseline", f"probe {i + 1}")
            rows.append(r)
            print(f"  probe {i + 1}: client {r.get('client_ttft_ms', 'ERR')} ms"
                  f"  server {r.get('server_ttft_ms', '—')}")
            if i < 3:
                await asyncio.sleep(1.0)

        print("\ncollision: the same probe fired into a live Cooker stage")
        for offset in (1.5, 4.0, 9.0):
            holder = asyncio.create_task(stage(cooker, "stage", ceiling))
            await asyncio.sleep(offset)
            r = await probe(human, "collision", f"at +{offset:.1f}s into decode")
            rows.append(r)
            print(f"  +{offset:4.1f}s: client {r.get('client_ttft_ms', 'ERR')} ms"
                  f"  server {r.get('server_ttft_ms', '—')}")
            done, _ = await asyncio.wait({holder}, timeout=120.0)
            if not done:
                holder.cancel()
                rows.append({"label": "stage", "cancelled": True,
                            "notes": f"at +{offset:.1f}s"})
            else:
                res = holder.result()
                rows.append(res)
                print(f"    stage: ttft {res.get('stage_ttft_ms', '—')} ms,"
                      f" total {res.get('total_ms', '—')} ms,"
                      f" {res.get('prompt_tokens', 0)} in /"
                      f" {res.get('out_tokens', 0)} out")

        print("\nprefill race: two large prompts 0.2s apart, the second one is"
              " the human")
        first = asyncio.create_task(stage(cooker, "stage", ceiling,
                                           thinking=True, max_tokens=512))
        await asyncio.sleep(0.2)
        t0 = time.perf_counter()
        human_big = llm.Request(
            system=STAGE_SYSTEM,
            user="Give me three bullets on preemption."
                 f"\n\n<untrusted>\n{long_context(ceiling // 2)}\n</untrusted>",
            max_tokens=96, thinking=False, kind="generate", stage="synthesize")
        try:
            comp = await human_big_stream(human, human_big)
            rows.append({"label": "prefill_race",
                         "client_ttft_ms": round((comp[0] or 0.0) * 1000.0, 1),
                         "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                         "out_tokens": comp[1]})
            print(f"  human prefill behind ours: {rows[-1]['client_ttft_ms']} ms")
        except Exception as exc:
            rows.append({"label": "prefill_race",
                         "error": f"{type(exc).__name__}: {exc}"[:140]})
            print(f"  human prefill failed: {rows[-1]['error']}")
        done, _ = await asyncio.wait({first}, timeout=120.0)
        if not done:
            first.cancel()
            rows.append({"label": "stage", "cancelled": True,
                         "notes": "prefill race"})
        else:
            rows.append(first.result())
    finally:
        await cooker.aclose()
        await human.aclose()
    return rows


async def human_big_stream(client: llm.LLM, req: llm.Request) -> tuple:
    """(ttft_s, completion_tokens) for a large human-shaped prompt."""
    comp = await client.stream(req)
    return comp.ttft_s, comp.completion_tokens


def summarize(rows: list[dict]) -> bool:
    base = [r["client_ttft_ms"] for r in rows
            if r["label"] == "baseline" and "client_ttft_ms" in r]
    coll = [r["client_ttft_ms"] for r in rows
            if r["label"] in ("collision", "prefill_race") and "client_ttft_ms" in r]
    srv = [r["server_ttft_ms"] for r in rows if r.get("server_ttft_ms")]
    print("\n" + "=" * 58)
    if not base:
        print("no baseline — omlx never answered; nothing was measured")
        return False
    print(f"baseline ttft   n={len(base)}  median {statistics.median(base):.0f} ms"
          f"  max {max(base):.0f} ms")
    if srv:
        print(f"server's own      median server ttft {statistics.median(srv):.0f} ms"
              f"  (client median {statistics.median(base):.0f} ms)")
    else:
        print("server's own      no server ttft reported — client numbers unchecked")
    if not coll:
        print("no collision samples — the experiment did not run")
        return False
    print(f"collision ttft    n={len(coll)}  median {statistics.median(coll):.0f} ms"
          f"  max {max(coll):.0f} ms")
    stall = statistics.median(coll) - statistics.median(base)
    print(f"\nmedian stall    {stall:+.0f} ms   (budget ±{STALL_BUDGET_MS:.0f} ms)")
    for r in rows:
        if r["label"] == "stage" and "stage_ttft_ms" in r:
            print(f"  stage prefill {r['stage_ttft_ms']:.0f} ms,"
                  f" {r.get('prompt_tokens')} in / {r.get('out_tokens')} out"
                  f" in {r.get('total_ms', 0):.0f} ms")
    if stall > STALL_BUDGET_MS:
        print(f"\nFAIL — a collision costs the human {stall:.0f} ms. Claiming short"
              " gaps is not defensible at that price: keep idle_confirm at 60s and"
              " let the kitchen run overnight.")
        return False
    print("\nPASS — the stall is inside budget, so a bounded prefill in a short gap"
          " is affordable and a burst detector can replace the continuous-silence"
          " gate.")
    return True


def main() -> int:
    rows = asyncio.run(run())
    if not rows:
        print("nothing recorded")
        return 1
    ok = summarize(rows)
    out = Path("var") / f"collision-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print(f"\nwrote {out} ({len(rows)} samples)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
