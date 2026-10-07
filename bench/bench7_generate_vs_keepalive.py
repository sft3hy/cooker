"""Bench #7: bytes, not sockets, are what prove inference is happening.

Why this exists
---------------
Cooker's whole politeness argument depends on knowing when someone else is
generating. The first detector answered that with `lsof -iTCP:8000
-sTCP:ESTABLISHED`, and that answer is wrong: OpenCode and Traefik hold pooled
connections open on the inference port for hours at literally zero throughput, so
ACTIVE_INFER latches and the kitchen never lights. This bench is the measurement
that forced the change (recorded in DISCOVERY.md), written so it can be re-run
whenever someone doubts the numbers — including future me, who will.

What it does
------------
Runs the shipping `WireProbe` and the shipping socket probe side by side for
~30 seconds, fires two real streamed generations through the middle, and prints
both columns per second. A keepalive holds zero; a stream moves kilobytes. The
gap between the quiet peak and the busy floor is the calibration for
`detect.infer_min_bps`.

    .venv/bin/python bench/bench7_generate_vs_keepalive.py

Cost: one nettop, one lsof per second, two real generations (~2k tokens).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooker import net
from cooker.config import load_config
from cooker.detect import peer_summary
from cooker.wire import WireProbe

PROMPT = ("Write 700 words on why a background batch job should yield its GPU to "
          "an interactive user, naming concrete mechanisms and their trade-offs.")
WATCH = 32.0
FIRE_AT = (5.0, 21.0)


def client_pids(port: int) -> dict[int, str]:
    """Who holds a connection to the inference port, by command name. This is
    the address book: it names the peers, it does not say what they are doing."""
    out = subprocess.run(["lsof", "-nP", "-Fpcn", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
                         capture_output=True, text=True).stdout
    peers: dict[int, str] = {}
    pid = 0
    for line in out.splitlines():
        if line.startswith("p"):
            with contextlib.suppress(ValueError):
                pid = int(line[1:])
        elif line.startswith("c") and pid:
            peers[pid] = line[1:]
    return peers


async def generation(cfg, label: str) -> tuple[float, float]:
    """One real streamed request: (ttft, total_seconds)."""
    t0 = time.perf_counter()
    ttft: float | None = None
    async with httpx.AsyncClient(verify=net.tls_context(cfg.get("inference.ca_file")),
                                 timeout=180) as c, c.stream(
        "POST", f"{cfg.get('inference.base_url')}/chat/completions",
        headers={"Authorization": f"Bearer {net.api_key(cfg)}"},
        json={"model": cfg.get("inference.model"),
              "messages": [{"role": "user", "content": PROMPT}],
              "max_tokens": 600, "temperature": 0.7, "stream": True,
              "chat_template_kwargs": {"enable_thinking": False}},
    ) as r:
        if r.status_code != 200:
            raise RuntimeError(f"{label}: HTTP {r.status_code} "
                             f"{(await r.aread())[:200]!r}")
        async for _ in r.aiter_lines():
            if ttft is None:
                ttft = time.perf_counter() - t0
    return ttft or -1.0, time.perf_counter() - t0


async def main() -> int:
    cfg = load_config()
    port = int(cfg.get("detect.omlx_port", 8000))
    floor = float(cfg.get("detect.infer_min_bps", 256))
    probe = WireProbe(cfg)
    if not probe.start():
        print(f"cannot start nettop: {probe.last_error}")
        return 2
    print(f"watching :{port} for {WATCH:.0f}s, two real generations inside it "
          f"(floor {floor:.0f} B/s)")
    print(f"peers at start: {client_pids(port)}\n")

    rows: list[dict] = []
    pending: asyncio.Task | None = None
    fires = list(FIRE_AT)
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < WATCH:
            await asyncio.sleep(1.0)
            now = time.monotonic() - t0
            peers = client_pids(port)
            wire = probe.snapshot()
            busy = [p for p in peers if wire.rate(p) >= floor]
            rows.append({"t": now, "peers": len(peers), "wire_up": wire.up,
                         "busy": busy, "peers_set": dict(peers),
                         "peak": max((wire.rate(p) for p in peers), default=0.0)})
            if fires and now >= fires[0]:
                fires.pop(0)
                pending = asyncio.create_task(generation(cfg, f"gen@{now:.0f}s"))
                print(f"{now:5.0f}  >>> generating")
            elif pending and pending.done():
                ttft, total = pending.result()
                pending = None
                print(f"{now:5.0f}  <<< done: TTFT {ttft:.2f}s in {total:.2f}s")
    finally:
        probe.stop()

    print(f"\n{'t':>5} {'peers':>5} {'wire':>5} {'busy':>5} {'peak B/s':>10}  who")
    for r in rows:
        who = peer_summary([type("P", (), {"command": n, "pid": p})()
                            for p, n in r["peers_set"].items()])
        print(f"{r['t']:5.0f} {r['peers']:5d} {r['wire_up']!s:>5} "
              f"{len(r['busy']):5d} {r['peak']:10.0f}  {who[:34]}")

    quiet = [r for r in rows if not r["busy"]]
    loud = [r for r in rows if r["busy"]]
    print("\n=== separation ===")
    if not (quiet and loud):
        print("  inconclusive: every second looked the same")
        return 1
    qpeak = max(r["peak"] for r in quiet)
    lfloor = min(r["peak"] for r in loud)
    lmax = max(r["peak"] for r in loud)
    print(f"  quiet seconds    {len(quiet):>3}   peak {qpeak:>10.0f} B/s")
    print(f"  busy seconds     {len(loud):>3}   floor {lfloor:>10.0f} B/s  "
          f"peak {lmax:>10.0f} B/s")
    print(f"  separation       {'clean' if qpeak < floor <= lfloor else 'NO'} "
          f"(floor {floor:.0f} sits between {qpeak:.0f} and {lfloor:.0f})")
    print(f"  socket count held {min(r['peers'] for r in quiet)}-"
          f"{max(r['peers'] for r in rows)} across BOTH states => a socket is "
          f"not a generation")
    ok = qpeak < floor <= lfloor
    print(f"\nVERDICT: {'bytes discriminate cleanly' if ok else 'needs recalibration'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
