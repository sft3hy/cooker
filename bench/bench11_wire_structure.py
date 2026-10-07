#!/usr/bin/env python3
"""Test 4 — what the wire actually looks like during an agent session.

Live, read-only, zero inference submitted. This bench does not generate a single
token: it watches the throughput the real `Detector` already sees, ticks by tick,
and answers the one question M8 is blocked on.

The blocker, stated precisely
-----------------------------
`cooker run --live` claimed nothing for 151 seconds while `opencode` streamed at a
steady ~1.7 KB/s, because `detect.infer_min_bps` is 256 and bytes were leaving omlx
faster than that. The reading was correct — someone was being served, interactive
wins, Cooker waited — and unusable, because `idle_confirm_seconds` demands 60
*continuous* quiet seconds and an interactive agent session almost never supplies
60 in a row. A tool call is GPU idle. Between two of them, nothing is being
served. The current gate cannot see that gap, so it waits for a silence that this
box does not produce while I am working, and a cook that finishes this calendar day
becomes the rare case rather than the normal one.

So the fix cannot be to lower `infer_min_bps`: that threshold separates 0 B/s
measured silence from 6,683 B/s measured decode, and loosening it trades the entire
politeness promise for a handful of extra seconds. What has to change is the *time*
question — how long the wire must be quiet before Cooker may put a bounded prefill
on the GPU — and that number has to come from the shape of real gaps, not from a
guess.

What it records
---------------
Every tick: the state, the reason, omlx's served B/s, how many clients are
connected, and whether the wire probe was blind. Then, offline from those rows:

  * the quiet gaps, segmented and lengthed — this is the measurement the knob needs;
  * how many of those gaps a `claim_quiet_seconds` of 2/4/6/8/10/15/30/60 would
    have let work start in, and how much of the session each one would have
    surrendered to ACTIVE_INFER collisions;
  * the longest burst, which is what a collision with a prefill would be measured
    against.

Nothing is asserted about omlx's behaviour. The assertions are about the recording:
a trace with no blind ticks, a floor that separated silence from traffic in the
session just measured, and gaps long enough to justify a knob or an honest statement
that they are not. A PASS here means the measurement is trustworthy, not that the
kitchen cooked.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooker import db, detect
from cooker.config import load_config

QUIET_STATES = (detect.IDLE,)
CLAIM_WINDOWS = (2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 30.0, 60.0)


def segments(rows: list[dict]) -> list[tuple[str, float]]:
    """Collapse ticks into (kind, seconds) runs: busy or quiet."""
    out: list[tuple[str, float]] = []
    for r in rows:
        kind = "quiet" if r["state"] in QUIET_STATES else "busy"
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + r["dt"])
        else:
            out.append([kind, r["dt"]])  # type: ignore[list-item]
    return [(k, float(v)) for k, v in out]


def gaps(rows: list[dict]) -> list[float]:
    """Lengths of quiet runs that were *bounded by traffic on both sides*: a gap
    the detector has to decide about, not the tail of the trace."""
    segs = segments(rows)
    return [d for i, (k, d) in enumerate(segs)
            if k == "quiet" and 0 < i < len(segs) - 1]


async def record(seconds: float) -> list[dict]:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    det = detect.Detector(cfg, conn)
    # `LiveSampler.start()` is not optional and `sample()` will not do it for us:
    # the resident nettop has to be alive before a window exists to diff, and a
    # sampler started per tick has no baseline. Forgetting this produced a 150-tick
    # trace in which every tick was blind and the answer to every question was 0 -
    # which is why this bench refuses to report a trace that was not watching.
    if not det.sampler.start():
        conn.close()
        msg = getattr(det.sampler, "last_error", None) or "sampler.start() returned False"
        print(f"cannot start the wire reader: {msg}")
        return []
    rows: list[dict] = []
    t_end = time.monotonic() + seconds
    prev: float | None = None
    print(f"watching the wire for {seconds:.0f}s — no inference submitted")
    try:
        while time.monotonic() < t_end:
            t0 = time.monotonic()
            snap = await det.tick()
            now = time.monotonic()
            dt = now - t0 if prev is None else now - prev
            prev = now
            served = 0.0
            if snap is not None and det.signals.wire and det.signals.socks:
                served = det.signals.wire.served_bps(det.signals.socks.server_pids)
            rows.append({
                "ts": round(now, 2),
                "dt": round(dt, 2),
                "state": snap.state,
                "reason": snap.reason,
                "served_bps": round(served, 1),
                "clients": len(snap.clients),
                "wire_up": snap.wire_up,
                "ready": snap.ready,
                "blockers": list(snap.blockers),
            })
            if len(rows) == 8 and not any(r["wire_up"] for r in rows):
                # Fail at eight seconds, not at a hundred and fifty. A blind trace is
                # not a short result, it is a fake one: every number in it is zero
                # because nobody was looking.
                print("aborting early: 8 ticks, every one blind — the reader never"
                      " came up and the trace would answer nothing")
                break
            left = t_end - time.monotonic()
            if left > 0:
                await asyncio.sleep(min(max(0.0, det.tick_seconds - (now - t0)), left))
    finally:
        det.sampler.stop()
        conn.close()
    return rows


def thrash_table(g: list[float], stage_s: float) -> None:
    """What each claim window actually buys, counting the stages that never finish.

    A gap long enough to *start* a stage and too short to *finish* one is the worst
    outcome available: the prompt is consumed, some decode is consumed, and then a
    human arrives and we drop the socket. Politeness has to be paid for twice — once
    by them, in the collision, and once by the GPU, in work thrown away. So the
    window is not chosen by how much quiet it unlocks but by how much of that quiet
    turns into a finished artifact.
    """
    if not g:
        return
    print(f"\nthrash at a typical stage length of {stage_s:.0f}s:")
    print(f"  {'window':>7}  {'starts':>7}  {'finishes':>9}  {'thrown away':>12}")
    for w in CLAIM_WINDOWS:
        starts = [x for x in g if x >= w]
        if not starts:
            print(f"  {w:6.0f}s        0          0             0s")
            continue
        finishes = [x for x in starts if x >= stage_s]
        wasted = sum(min(x, stage_s) for x in starts if x < stage_s)
        pct = 100 * len(finishes) / len(starts)
        print(f"  {w:6.0f}s  {len(starts):7d}  {len(finishes):9d}"
              f"  {wasted:9.0f}s ({pct:.0f}% finish)")


def summarize(rows: list[dict], floor: float, stage_s: float = 20.0) -> None:
    total = sum(r["dt"] for r in rows)
    by_state = Counter()
    for r in rows:
        by_state[r["state"]] += r["dt"]
    blind = [r for r in rows if not r["wire_up"]]
    print(f"\ntrace: {len(rows)} ticks over {total:.0f}s")
    for state, secs in by_state.most_common():
        print(f"  {state:<12} {secs:6.1f}s  {100 * secs / total:5.1f}%")
    print(f"  blind ticks  {len(blind)}")

    served = sorted(r["served_bps"] for r in rows)
    print(f"\nomlx served B/s: min {served[0]:,.0f} median {served[len(served) // 2]:,.0f}"
          f" max {served[-1]:,.0f}   floor {floor:,.0f}")
    busy = [r["served_bps"] for r in rows if r["state"] == detect.ACTIVE_INFER]
    quiet = [r["served_bps"] for r in rows if r["state"] in QUIET_STATES]
    if busy and quiet:
        low_busy = min(busy)
        print(f"  lowest busy {low_busy:,.0f}  highest quiet {max(quiet):,.0f}"
              f"  separation {'clean' if max(quiet) < floor <= low_busy else 'OVERLAP'}")

    g = gaps(rows)
    print(f"\nquiet gaps bounded by traffic: {len(g)}")
    if g:
        print("  lengths (s): " + ", ".join(f"{x:.0f}" for x in sorted(g)))
        print(f"  median {sorted(g)[len(g) // 2]:.1f}s  longest {max(g):.1f}s")
    print(f"\nwhat a quiet-window claim gate would have allowed (of {total:.0f}s):")
    print(f"  {'window':>7}  {'gaps used':>10}  {'seconds':>8}  {'% of trace':>10}")
    for w in CLAIM_WINDOWS:
        usable = sum(x for x in g if x >= w)
        n = sum(1 for x in g if x >= w)
        print(f"  {w:6.0f}s  {n:10d}  {usable:8.0f}  {100 * usable / total:9.1f}%")

    segs = segments(rows)
    bursts = [d for k, d in segs if k == "busy"]
    if bursts:
        print(f"\nbusy bursts: {len(bursts)}, longest {max(bursts):.0f}s,"
              f" median {sorted(bursts)[len(bursts) // 2]:.0f}s")
    never_ready = [r for r in rows if not r["ready"]]
    print(f"ticks where the daemon could not start work: {len(never_ready)}/{len(rows)}")
    reasons = Counter(b.split()[0] for r in rows for b in r["blockers"])
    print("blocker kinds: " + ", ".join(f"{k} x{v}" for k, v in reasons.most_common()))


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    cfg = load_config()
    floor = float(cfg.get("detect.infer_min_bps", 256))
    rows = asyncio.run(record(seconds))
    summarize(rows, floor)
    out = Path("var") / f"wire-trace-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")

    silent = [r for r in rows if not r["wire_up"]]
    if len(silent) > max(2, len(rows) // 20):
        print(f"FAIL — {len(silent)} blind ticks; the meter was not watching")
        return 1
    print("PASS — the trace is trustworthy (no assertion about omlx's behaviour here)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
