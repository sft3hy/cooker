#!/usr/bin/env python3
"""Discovery #5: the prefill ladder — sets max_prefill_tokens_per_request.

omlx runs with chunked_prefill:false, so a prefill is ATOMIC and EXCLUSIVE:
while a background prefill runs, nothing else gets tokens. This measures exactly
how long each prompt size hogs the accelerator, and what a concurrent
interactive request pays. Also: does aborting *during prefill* release early?
"""
import http.client
import json
import pathlib
import re
import statistics
import threading
import time

HOST, PORT, MODEL = "100.122.197.81", 8000, "Qwen3.8-Flash-Next-oQ4e-mtp"
KEY = re.search(r"AR_LLM_API_KEY=(.+)",
                (pathlib.Path.home() / "dev/autoresearch-service/.env").read_text()
                ).group(1).strip()

SENT = ("The old lighthouse keeper climbed the ninety-seven steps every dusk, "
        "trimming the wick until the brass was warm. ")


def stream(prompt, max_tokens, abort_after=None, tag="", think=False):
    body = {"model": MODEL, "stream": True, "max_tokens": max_tokens,
            "temperature": 0.2, "enable_thinking": think,
            "messages": [{"role": "user", "content": prompt}]}
    b = json.dumps(body).encode()
    t0 = time.time()
    c = http.client.HTTPConnection(HOST, PORT, timeout=600)
    c.putrequest("POST", "/v1/chat/completions")
    c.putheader("Content-Type", "application/json")
    c.putheader("Authorization", f"Bearer {KEY}")
    c.putheader("Content-Length", str(len(b)))
    c.endheaders(b)
    ttft = None
    chars = aborted = 0
    first_aborted_at = None
    try:
        r = c.getresponse()
        if r.status != 200:
            return {"tag": tag, "error": f"http {r.status} {r.read()[:150]!r}"}
        buf = b""
        while True:
            ch = r.read(512)
            if not ch:
                break
            buf += ch
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                ln = frame.decode("utf8", "ignore").strip()
                if not ln.startswith("data:"):
                    continue
                pl = ln[5:].strip()
                if pl == "[DONE]":
                    continue
                try:
                    j = json.loads(pl)
                except Exception:
                    continue
                if j.get("model") == "keepalive":
                    continue
                if ttft is None:
                    ttft = time.time() - t0
                for choice in j.get("choices") or []:
                    d = choice.get("delta") or {}
                    chars += len(d.get("content") or "") + len(d.get("reasoning_content") or "")
            if abort_after and (time.time() - t0) > abort_after:
                aborted = True
                first_aborted_at = time.time()
                break
        total = time.time() - t0
    except Exception as e:
        return {"tag": tag, "error": f"{type(e).__name__}: {e}"}
    finally:
        c.close()
    return {"tag": tag, "ttft": ttft, "total": total, "chars": chars,
            "aborted": aborted, "abort_at": first_aborted_at}


def prompt_for(tokens, nonce):
    reps = max(1, int(tokens * 4 / len(SENT)))
    return f"{nonce} " + SENT * reps


def main():
    nonce = str(int(time.time()))
    rows = []
    SMALL = "Name one fruit. Two words."

    print("=== idle baseline ===", flush=True)
    base = []
    for i in range(3):
        r = stream(f"{SMALL} b{i}", 16, tag=f"base{i}")
        base.append(r["ttft"])
        print(f"  idle ttft={r['ttft']:.3f}s", flush=True)
        time.sleep(0.5)
    bl = statistics.mean(base)

    print("\n=== prefill ladder (thinking OFF, exclusive prefill) ===", flush=True)
    print(f"  baseline ttft = {bl:.3f}s\n", flush=True)
    ladder = []
    for tok in (1000, 2000, 4000, 8000, 16000):
        holder = {}

        def go(tok=tok):
            holder['r'] = stream(prompt_for(tok, f"{nonce}-{tok}"), 8, tag=f"p{tok}")
        th = threading.Thread(target=go)
        th.start()
        time.sleep(0.20)
        inter = stream(f"{SMALL} i{tok}", 16, tag=f"interfere-during-{tok}-prefill")
        th.join()
        b = holder['r']
        pf = b.get("ttft") or 0
        it = inter.get("ttft")
        rate = tok / pf if pf else 0
        ladder.append({"tokens": tok, "prefill_s": pf, "tok_per_s": rate,
                       "interactive_ttft_s": it})
        rows.append({**b, **inter})
        ratio = (it / bl) if it else float("nan")
        print(f"  {tok:>6} tok -> prefill {pf:5.2f}s ({rate:6.0f} tok/s) | "
              f"interactive alongside: ttft={it if it else -1:.3f}s "
              f"({ratio:.1f}x idle)", flush=True)
        time.sleep(1.0)

    print("\n=== abort DURING prefill: does the box free up early? ===", flush=True)
    tok = 16000
    holder = {}

    def go2():
        holder['r'] = stream(prompt_for(tok, "abortprefill"), 8, abort_after=1.0,
                             tag="abort-in-prefill")
    th = threading.Thread(target=go2)
    th.start()
    time.sleep(1.4)
    after = []
    for i in range(3):
        r = stream(f"{SMALL} a{i}", 16, tag=f"after-abort-{i}")
        after.append(r.get("ttft"))
        print(f"  ttft={r.get('ttft'):.3f}s ({r.get('ttft')/bl:.1f}x idle)", flush=True)
        time.sleep(0.4)
    th.join()
    print(f"  aborted big: aborted={holder['r'].get('aborted')} "
          f"total={holder['r'].get('total'):.2f}s", flush=True)

    print("\n=== VERDICT ===")
    print(f"  idle interactive TTFT      = {bl:.3f}s")
    ok = [l for l in ladder if l['interactive_ttft_s']]
    for l in ladder:
        budget = l['interactive_ttft_s'] / bl if l['interactive_ttft_s'] else 0
        print(f"  {l['tokens']:>6} tok prefill -> {l['prefill_s']:5.2f}s exclusive, "
              f"interactive pays {budget:4.1f}x")
    safe = [l['tokens'] for l in ladder if l['interactive_ttft_s'] and
            l['interactive_ttft_s'] / bl < 3.0]
    print(f"\n  sizes keeping interactive under 3x idle: {safe or 'none'}")
    print(f"  RECOMMENDED max_prefill_tokens_per_request = "
          f"{max(safe) if safe else 1000}")
    json.dump({"ladder": ladder, "baseline": bl,
               "after_abort": after}, open("bench5_prefill.json", "w"), indent=2)


if __name__ == "__main__":
    main()
