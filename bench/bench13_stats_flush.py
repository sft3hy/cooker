#!/usr/bin/env python3
"""bench13: does omlx update stats.json *during* generation or at completion?

DISCOVERY §17 decided that byte-solely detection cannot separate a monitor's
8 KB admin poll from a real streamed reply at nettop's resolution. The proposed
corroboration — `stats.json`'s `total_requests` moves for inference and never
for monitoring — is only safe if the ledger updates *before or during* the
generation it represents. If omlx flushes only at completion, a long live
generation shows a flat ledger while bytes stream, and a naive
"bytes-but-flat-ledger => monitor" rule would un-pause the kitchen on top of
a live decode. That is the one mistake this measurement exists to prevent.

Method: watch stats.json every 250 ms while one small streaming request
(<=300 tok) runs. The machine must otherwise be quiet — this process holds the
wire it is measuring. Prints the ledger's move relative to first-delta and
stream-close, which is the whole answer.

Cost: one small request, ~200 completion tokens.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.request

TS = "http://100.122.197.81:8000"
MODEL = "Qwen3.8-Flash-Next-oQ4e-mtp"
STATS = pathlib.Path.home() / ".omlx" / "stats.json"
ENV = pathlib.Path.home() / "dev" / "autoresearch-service" / ".env"


def api_key() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("AR_LLM_API_KEY"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("AR_LLM_API_KEY not found (never hardcode it)")


def ledger() -> tuple[int, float]:
    d = json.loads(STATS.read_text())
    return int(d["total_requests"]), STATS.stat().st_mtime


def main() -> int:
    n0, m0 = ledger()
    print(f"baseline: total_requests={n0} mtime={m0:.2f}")
    events: list[str] = []
    t0 = time.time()
    stop = threading.Event()

    def watch() -> None:
        last = (n0, m0)
        while not stop.is_set():
            cur = ledger()
            if cur != last:
                events.append(f"LEDGER MOVES t+{time.time()-t0:6.2f}s "
                             f"total_requests={cur[0]}")
                last = cur
            time.sleep(0.25)

    th = threading.Thread(target=watch, daemon=True)
    th.start()

    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user",
                     "content": "List 12 kitchen appliances, one per line."}],
        "max_tokens": 300,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(f"{TS}/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {api_key()}",
                                        "Content-Type": "application/json"})
    first = close = None
    chunks = usage = 0
    with urllib.request.urlopen(req, timeout=60) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            if first is None:
                first = time.time() - t0
            if "usage" in line and "completion_tokens" in line:
                try:
                    j = json.loads(line[5:])
                    usage = j.get("usage", {}).get("completion_tokens", 0)
                except json.JSONDecodeError:
                    pass
            chunks += 1
    close = time.time() - t0
    time.sleep(1.5)  # one ledger flush window after close
    stop.set()
    th.join(timeout=2)

    print(f"stream: first data line t+{first:.2f}s, closed t+{close:.2f}s, "
          f"{chunks} chunks, {usage} completion tokens")
    for e in events:
        print(e)
    n1, _m1 = ledger()
    print(f"total_requests: {n0} -> {n1}")
    if not events:
        print("VERDICT: no ledger movement observed — flush cadence is "
              "coarser than 250ms sampling, or writes are batched. "
              "Ledger corroboration needs rethinking before it gates.")
    else:
        moved = float(events[0].split("t+")[1].split("s")[0])
        if moved < close:
            print(f"VERDICT: ledger moves DURING generation "
                  f"(t+{moved:.2f}s < close t+{close:.2f}s) => corroboration "
                  "is safe for long generations: monitor polls leave it flat "
                  "while a real decode is live.")
        else:
            print("VERDICT: ledger moves at/after completion only => it can "
                  "confirm inference happened but cannot see one in flight; "
                  "the sustained-stream test must carry ACTIVE_INFER.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
