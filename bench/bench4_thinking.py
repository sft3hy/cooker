#!/usr/bin/env python3
"""Discovery: how does this reasoning model behave, and can we throttle thinking?

Qwen3.8-Flash-Next-oQ4e-mtp streamed ONLY reasoning_content at max_tokens=16
and finished with finish_reason=length -> empty answer. Cooker must know:
  - which request knob disables or budgets thinking
  - how big a max_tokens we need so artifacts are never empty
  - real TTFT (first non-keepalive delta) per variant
"""
import http.client
import json
import pathlib
import re
import time

HOST, PORT, MODEL = "100.122.197.81", 8000, "Qwen3.8-Flash-Next-oQ4e-mtp"
KEY = re.search(r"AR_LLM_API_KEY=(.+)",
                (pathlib.Path.home() / "dev/autoresearch-service/.env").read_text()
                ).group(1).strip()


def run(extra, max_tokens, prompt, label):
    body = {"model": MODEL, "stream": True, "max_tokens": max_tokens,
            "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}]}
    body.update(extra)
    b = json.dumps(body).encode()
    t0 = time.time()
    c = http.client.HTTPConnection(HOST, PORT, timeout=180)
    c.putrequest("POST", "/v1/chat/completions")
    c.putheader("Content-Type", "application/json")
    c.putheader("Authorization", f"Bearer {KEY}")
    c.putheader("Content-Length", str(len(b)))
    c.endheaders(b)
    ttft_first_byte = ttft_real = None
    think, ans, keepalives, finish = [], [], 0, None
    try:
        r = c.getresponse()
        if r.status != 200:
            print(f"{label:<34} HTTP {r.status} {r.read()[:150]!r}")
            return None
        buf = b""
        while True:
            ch = r.read(256)
            if not ch:
                break
            if ttft_first_byte is None:
                ttft_first_byte = time.time() - t0
            buf += ch
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                line = frame.decode("utf8", "ignore").strip()
                if not line.startswith("data:"):
                    keepalives += 1
                    continue
                pl = line[5:].strip()
                if pl == "[DONE]":
                    continue
                try:
                    j = json.loads(pl)
                except Exception:
                    continue
                if j.get("model") == "keepalive":
                    keepalives += 1
                    continue
                if ttft_real is None:
                    ttft_real = time.time() - t0
                for choice in j.get("choices") or []:
                    d = choice.get("delta") or {}
                    if d.get("reasoning_content"):
                        think.append(d["reasoning_content"])
                    if d.get("content"):
                        ans.append(d["content"])
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        total = time.time() - t0
    except Exception as e:
        print(f"{label:<34} ERR {type(e).__name__}: {e}")
        return None
    finally:
        c.close()
    a = "".join(ans).strip()
    print(f"{label:<34} ttft1st={ttft_first_byte:.3f} ttftREAL={ttft_real or -1:.3f} "
          f"total={total:.2f} think={len(''.join(think)):>5}ch ans={len(a):>4}ch "
          f"finish={finish} keep={keepalives}")
    if a:
        print(f"{'':<34} ans: {a[:110]!r}")
    return {"label": label, "ttft": ttft_real, "total": total,
            "think_chars": len("".join(think)), "ans_chars": len(a),
            "finish": finish, "answer": a}


Q = "List three benefits of running a local LLM on Apple Silicon. Be terse."
print("=== knobs to disable/throttle thinking ===")
variants = [
    ({}, 400, "baseline (thinking on)"),
    ({"chat_template_kwargs": {"enable_thinking": False}}, 300, "chat_tpl enable_thinking=F"),
    ({"thinking": {"type": "disabled"}}, 300, "thinking:{type:disabled}"),
    ({"enable_thinking": False}, 300, "top-level enable_thinking=F"),
    ({"reasoning_effort": "low"}, 300, "reasoning_effort=low"),
]
out = []
for extra, mt, label in variants:
    out.append(run(extra, mt, Q, label))
    time.sleep(0.6)

print("\n=== max_tokens floor so answers are never empty (thinking on) ===")
for mt in (128, 256, 512):
    run({}, mt, Q, f"max_tokens={mt}")
    time.sleep(0.6)

print("\n=== terse non-reasoning job shapes cooker will actually run ===")
run({}, 200, "Return JSON only: {\"score\": 1-5}. Usefulness of: a daily digest of local-LLM tips.",
    "json-score job")
run({"chat_template_kwargs": {"enable_thinking": False}}, 200,
    "Return JSON only: {\"score\": 1-5}. Usefulness of: a daily digest of local-LLM tips.",
    "json-score no-think")

json.dump([o for o in out if o], open("bench4_thinking.json", "w"), indent=2)
