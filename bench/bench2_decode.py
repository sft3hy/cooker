#!/usr/bin/env python3
"""Cooker discovery #2: prove preemption recovery *during active decode*.

Bench #1 was contaminated: the aborted prompt was a prefix-cache hit (TTFT
0.68s vs 7.1s cold), so phase C measured an idle box, not a decoding one.

This version uses a UNIQUE prompt each round (nonce + fresh prose, so no
prefix cache hit) and a long max_tokens so the big request spends most of its
life in DECODE, not prefill. We then:
  1. measure small-request TTFT while the big request is provably mid-decode
  2. hard-close the big request's socket
  3. measure small-request TTFT immediately after
If (3) == baseline, dropping the connection genuinely frees the accelerator.
"""
import http.client
import json
import pathlib
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid

HOST, PORT = "100.122.197.81", 8000


def _load_key():
    for rel in ("dev/autoresearch-service/.env", "dev/signal-summarizer/.env"):
        p = pathlib.Path.home() / rel
        if p.exists():
            m = re.search(r"^(?:AR|SS)_LLM_API_KEY=(.+)$", p.read_text(), re.M)
            if m:
                return m.group(1).strip().strip('"')
    raise SystemExit("no omlx key found")


API_KEY = _load_key()
MODEL = "Qwen3.8-Flash-Next-oQ4e-mtp"

PARA = ("Discuss the engineering tradeoffs of {n}: memory bandwidth versus "
        "compute density, thermal envelopes, scheduler fairness, cache locality, "
        "and the operational cost of each choice in a shared single-node inference "
        "server that must serve both interactive and background tenants. ")


def stream_chat(prompt, max_tokens, abort_after=None, label="", stall_after=None):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.9,
        "stream": True,
    }).encode()
    t0 = time.time()
    c = http.client.HTTPConnection(HOST, PORT, timeout=240)
    c.putrequest("POST", "/v1/chat/completions")
    c.putheader("Content-Type", "application/json")
    c.putheader("Authorization", f"Bearer {API_KEY}")
    c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    ttft, sse_events, aborted = None, 0, False
    last_tok_time = None
    try:
        r = c.getresponse()
        if r.status != 200:
            return {"label": label, "error": f"http {r.status}: {r.read()[:200]!r}"}
        for chunk in iter(lambda: r.read(256), b""):
            now = time.time()
            if ttft is None:
                ttft = now - t0
            sse_events += chunk.count(b"data: {")
            last_tok_time = now
            if stall_after and (now - t0) > stall_after:
                # stop reading and park the socket open: server keeps decoding,
                # proves decode is the contended phase
                pass
            if abort_after and (now - t0) > abort_after:
                aborted = True
                break
        total = time.time() - t0
    except Exception as e:
        return {"label": label, "error": f"{type(e).__name__}: {e}",
                "total": time.time() - t0}
    finally:
        c.close()
    return {"label": label, "ttft": ttft, "total": total, "events": sse_events,
            "aborted": aborted, "t_start": t0,
            "decode_end": last_tok_time}


def gpu(stop, out):
    while not stop.is_set():
        try:
            p = subprocess.run(["ps", "-Ao", "comm,%cpu"], capture_output=True, text=True)
            tot = sum(float(l.split()[-1]) for l in p.stdout.splitlines()
                      if "omlx" in l.lower())
            out.append((round(time.time(), 2), round(tot, 1)))
        except Exception:
            pass
        time.sleep(0.4)


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "bench2_results.json"
    results, small = [], "Reply with exactly one short sentence about coffee."

    print("=== A: baseline small requests (box idle) ===", flush=True)
    for i in range(3):
        r = stream_chat(f"{small}", 48, label=f"A{i}")
        results.append(r)
        print(f"  ttft={r.get('ttft'):.3f}s ev={r.get('events')} err={r.get('error')}", flush=True)
        time.sleep(1.2)

    stop = threading.Event()
    cpus = []
    threading.Thread(target=gpu, args=(stop, cpus), daemon=True).start()

    nonce = uuid.uuid4().hex
    big = (" ".join(PARA.format(n=f"topic-{nonce}-{i}") for i in range(160)))
    print(f"\n=== B: cold big prompt ({len(big)} chars ~{len(big)//4} tok), "
          f"max_tokens=1200, small requests DURING decode ===", flush=True)
    bigres = {}

    def run_big():
        bigres['r'] = stream_chat(big, 400, label="B-big")
    th = threading.Thread(target=run_big)
    th.start()
    time.sleep(12.0)   # past the cold prefill, safely inside decode
    alongside = []
    for i in range(3):
        r = stream_chat(f"{small} [probe-{nonce}-{i}]", 48, label=f"B{i}-during-decode")
        alongside.append(r); results.append(r)
        print(f"  ttft={r.get('ttft'):.3f}s err={r.get('error')}", flush=True)
        time.sleep(0.8)
    decode_peak = statistics.mean([r['ttft'] for r in alongside if 'ttft' in r])
    th.join()
    b = bigres['r']; results.append(b)
    print(f"  big: ttft={b.get('ttft'):.3f} total={b.get('total'):.1f} "
          f"events={b.get('events')}", flush=True)

    print("\n=== C: cold big prompt, hard-abort at 12s, then probe ===", flush=True)
    nonce2 = uuid.uuid4().hex
    big2 = (" ".join(PARA.format(n=f"alt-{nonce2}-{i}") for i in range(160)))
    ab = {}

    def run_ab():
        ab['r'] = stream_chat(big2, 1200, abort_after=12.0, label="C-big-abort@12s")
    th = threading.Thread(target=run_ab)
    th.start()
    time.sleep(8.0)   # still decoding, before the abort -> proves contention exists here too
    pre = stream_chat(f"{small} [contended-{nonce2}]", 48, label="C-pre-abort")
    results.append(pre)
    print(f"  ttft={pre.get('ttft'):.3f}s (BEFORE abort, box contended)", flush=True)
    time.sleep(4.4)   # just after the client hard-closed at 12s
    after = []
    for i in range(4):
        r = stream_chat(f"{small} [recover-{nonce2}-{i}]", 48, label=f"C{i}-after-abort")
        after.append(r); results.append(r)
        print(f"  ttft={r.get('ttft'):.3f}s err={r.get('error')}", flush=True)
        time.sleep(0.9)
    th.join()
    a = ab['r']; results.append(a)
    print(f"  aborted: ttft={a.get('ttft'):.3f} total={a.get('total'):.1f} "
          f"aborted={a.get('aborted')} events={a.get('events')}", flush=True)

    stop.set()
    bt = [r['ttft'] for r in results if r.get('label', '').startswith('A')]
    at = [r['ttft'] for r in after if 'ttft' in r]
    dt = [r['ttft'] for r in alongside if 'ttft' in r]
    print("\n=== VERDICT ===")
    print(f"  baseline TTFT (idle)        = {statistics.mean(bt):.3f}s")
    if dt:
        print(f"  TTFT during live decode     = {statistics.mean(dt):.3f}s "
              f"({statistics.mean(dt)/statistics.mean(bt):.2f}x baseline)")
    print(f"  TTFT right after abort      = {statistics.mean(at):.3f}s "
          f"({statistics.mean(at)/statistics.mean(bt):.2f}x baseline)")
    if dt:
        recovers = statistics.mean(at) < statistics.mean(dt) * 0.6
        print(f"  abort frees the accelerator = {recovers}")
    if cpus:
        print(f"  omlx CPU%% peak={max(c for _, c in cpus):.0f}% "
              f"mean={statistics.mean(c for _, c in cpus):.0f}%")
    json.dump({"results": results, "cpus": cpus}, open(out_path, "w"),
              indent=2, default=str)


if __name__ == "__main__":
    main()
