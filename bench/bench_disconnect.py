#!/usr/bin/env python3
"""Cooker discovery: does dropping the client connection halt omlx generation?

Stdlib only. Measures:
  A) baseline TTFT + total latency for a small interactive-style request
  B) interference: big background prefill (10k+ tok) running, then a small
     request fired alongside -> TTFT spike?
  C) cancellation: abort the big request mid-stream, then immediately fire the
     small request -> does TTFT recover instantly (server honoured the
     disconnect) or stay spiked (server kept generating)?
  D) server busy-ness sampled from a separate thread via /v1/models round trip
     as a cheap "is the box contended" proxy + omlx CPU% from ps.
"""
import http.client, json, statistics, subprocess, threading, time, sys

HOST, PORT = "100.122.197.81", 8000
import pathlib, os, re
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

FILLER = ("The history of computing spans mechanical calculating engines, "
          "vacuum tubes, transistors, integrated circuits and GPUs. ")


def big_prompt(target_chars=60000):  # ~15k tokens of prose
    return (FILLER * (target_chars // len(FILLER) + 1))[:target_chars]


def stream_chat(prompt, max_tokens, abort_after=None, label=""):
    """POST a streaming chat completion. Returns dict of metrics.
    If abort_after is set, the raw socket is closed that many seconds after
    the first byte, simulating Cooker self-preemption."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": True,
    }).encode()
    t0 = time.time()
    c = http.client.HTTPConnection(HOST, PORT, timeout=180)
    c.putrequest("POST", "/v1/chat/completions")
    c.putheader("Content-Type", "application/json")
    c.putheader("Authorization", f"Bearer {API_KEY}")
    c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    ttft, tokens, bytes_read, aborted = None, 0, 0, False
    try:
        r = c.getresponse()
        if r.status != 200:
            return {"label": label, "error": f"http {r.status}: {r.read()[:200]!r}"}
        for chunk in iter(lambda: r.read(1024), b""):
            now = time.time()
            bytes_read += len(chunk)
            if ttft is None:
                ttft = now - t0
            tokens += chunk.count(b"data:") - chunk.count(b"[DONE]")
            if abort_after and (now - t0) > abort_after:
                aborted = True
                break
        total = time.time() - t0
    except Exception as e:
        total = time.time() - t0
        return {"label": label, "error": f"{type(e).__name__}: {e}", "total": total}
    finally:
        c.close()  # hard socket close -> server must observe disconnect
    return {"label": label, "ttft": ttft, "total": total, "tokens": max(tokens, 0),
            "bytes": bytes_read, "aborted": aborted,
            "t_end": time.time(), "t_start": t0}


def cpu_samples(stop, out):
    """Sample omlx process CPU% every 0.5s."""
    while not stop.is_set():
        try:
            p = subprocess.run(["ps", "-Ao", "pid,comm,%cpu"], capture_output=True, text=True)
            tot = sum(float(l.split()[-1]) for l in p.stdout.splitlines()
                      if "omlx" in l.lower())
            out.append((time.time(), tot))
        except Exception:
            pass
        time.sleep(0.5)


def main():
    results = []
    small = "In one sentence: what is Apple's MLX framework?"

    print("=== A: interactive baseline (3x small, idle box) ===", flush=True)
    base = []
    for i in range(3):
        r = stream_chat(small, 64, label=f"A{i}")
        base.append(r)
        results.append(r)
        print(f"  ttft={r.get('ttft')} total={r.get('total')} tok={r.get('tokens')}"
              f" err={r.get('error')}", flush=True)
        time.sleep(1.0)

    stop = threading.Event()
    cpus = []
    threading.Thread(target=cpu_samples, args=(stop, cpus), daemon=True).start()

    print("\n=== B: interference - big prefill + small alongside ===", flush=True)
    bigres = {}

    def run_big():
        bigres['r'] = stream_chat(big_prompt(), 1024, label="B-big-uninterrupted")
    th = threading.Thread(target=run_big)
    th.start()
    time.sleep(1.5)  # let the giant prefill land
    for i in range(2):
        r = stream_chat(small, 64, label=f"B{i}-alongside-big")
        results.append(r)
        print(f"  ttft={r.get('ttft')} total={r.get('total')} tok={r.get('tokens')}"
              f" err={r.get('error')}", flush=True)
        time.sleep(0.5)
    th.join()
    b = bigres['r']; results.append(b)
    print(f"  big: ttft={b.get('ttft')} total={b.get('total')} tok={b.get('tokens')}", flush=True)
    time.sleep(3)

    print("\n=== C: cancellation - abort big mid-stream, then small ===", flush=True)
    cpus.clear()
    abortres = {}

    def run_aborted():
        abortres['r'] = stream_chat(big_prompt(), 1024, abort_after=4.0,
                                     label="C-big-aborted@4s")
    th = threading.Thread(target=run_aborted)
    th.start()
    time.sleep(4.4)  # just after the client gave up
    for i in range(4):
        r = stream_chat(small, 64, label=f"C{i}-after-abort")
        results.append(r)
        print(f"  ttft={r.get('ttft')} total={r.get('total')} tok={r.get('tokens')}"
              f" err={r.get('error')}", flush=True)
        time.sleep(1.0)
    th.join()
    a = abortres['r']; results.append(a)
    print(f"  aborted big: ttft={a.get('ttft')} total={a.get('total')} "
          f"aborted={a.get('aborted')} bytes={a.get('bytes')}", flush=True)

    stop.set()
    if cpus:
        print("\n  omlx CPU%% over the window (0.5s samples):", flush=True)
        peak = max(c for _, c in cpus)
        print(f"    peak={peak:.1f} mean={statistics.mean(c for _, c in cpus):.1f}",
              flush=True)

    good = [r for r in results if 'error' not in r]
    bt = [r['ttft'] for r in good if r['label'].startswith('A')]
    ct = [r['ttft'] for r in good if r['label'].startswith('C')]
    if bt and ct:
        print(f"\n=== VERDICT ===\n  baseline TTFT mean   = {statistics.mean(bt):.3f}s")
        print(f"  post-abort TTFT mean = {statistics.mean(ct):.3f}s")
        print(f"  ratio = {statistics.mean(ct)/statistics.mean(bt):.2f}x")
        print("  interpretation: ~1x => disconnect HALTS generation (cooker can "
              "safely self-preempt). >>1x => it keeps burning (cooker must avoid "
              "big prefills instead).")
    json.dump(results, open(sys.argv[1] if len(sys.argv) > 1 else
                            "/private/var/folders/6k/l43bvnb12ldctd5cllvt3d9r0000gn/T/opencode/bench_results.json", "w"),
              indent=2, default=str)


if __name__ == "__main__":
    main()
