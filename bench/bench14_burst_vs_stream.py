#!/usr/bin/env python3
"""bench14: burst-vs-stream shape of everything on omlx's wire (M8 gate work).

§17 established the box's steady state: a ~1,720 B/s plateau every 4-5s from
`edge-dashboard`'s admin poll (one 6,141-byte response, authed `/admin/api/stats`)
hairpinned through OrbStack, smeared by WireProbe's 3s ring into what looks like
continuous service. The gate therefore never reaches 4 quiet seconds and
`cooker serve --live` cooks nothing, forever, while looking broken.

The candidate fix is shape: a poll is ONE fat sample-second followed by silence;
a decode is MANY consecutive sample-seconds. This records the per-second byte
rows for the omlx pid(s) over a quiet stretch — quiet meaning the agent driving
this is not generating, which is exactly the window the gate wants to claim —
and classifies: one-second bursts spaced ~5s apart (pollers, collapsible by
shape) versus multi-second runs (real generations, which the sustained rule
must still catch).

Run it during a genuinely idle stretch. Cost: zero GPU (reads only), ~1.5
nettop-equivalents of CPU for the duration.
"""

from __future__ import annotations

import subprocess
import sys
import time


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    import contextlib
    om_pids = set()
    out = subprocess.run(["pgrep", "-f", "omlx"], capture_output=True, text=True)
    om_pids = {int(x) for x in out.stdout.split()}
    print(f"omlx pids: {sorted(om_pids)} — watching {seconds:.0f}s")
    p = subprocess.Popen(
        ["nettop", "-n", "-x", "-P", "-L", f"{int(seconds)}", "-s", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    t0 = time.time()
    cols: dict[str, int] = {}
    rows: list[tuple[float, dict[int, int]]] = []
    cur: dict[int, int] = {}
    assert p.stdout is not None
    for line in p.stdout:
        if time.time() - t0 > seconds + 4:
            p.terminate()
            break
        cells = line.rstrip().split(",")
        if cells[0] == "time":
            if cur:
                rows.append((time.time(), cur))
            cur = {}
            cols = {n: i for i, n in enumerate(cells) if n}
            continue
        if "bytes_out" not in cols or len(cells) <= cols["bytes_out"]:
            continue
        pid_s = cells[1].rpartition(".")[2]
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if pid not in om_pids:
            continue
        with contextlib.suppress(ValueError):
            cur[pid] = int(cells[cols["bytes_out"]])
    with contextlib.suppress(Exception):
        p.wait(timeout=3)
    if not rows:
        print("no omlx samples — probe died or names shifted; not an answer")
        return 1
    # deltas per sample-second, summed across omlx's pids
    d = [sum(rows[i][1].values()) - sum(rows[i - 1][1].values())
         for i in range(1, len(rows))]
    print("per-second bytes_out:", d)
    runs = 0
    i = 0
    while i < len(d):
        if d[i] > 256:
            j = i
            while j < len(d) and d[j] > 256:
                j += 1
            runs += 1
            if j - i == 1:
                print(f"burst at sample {i}: {d[i]:,} B in 1s (poll-shaped)")
            else:
                print(f"RUN at samples {i}-{j-1}: {j-i}s of traffic "
                      f"(generation-shaped)")
            i = j
        else:
            i += 1
    gaps = []
    prev = -1
    for i, v in enumerate(d):
        if v > 256:
            if prev >= 0:
                gaps.append(i - prev - 1)
            prev = i
    n_single = sum(1 for i, v in enumerate(d)
                   if v > 256 and (i == 0 or d[i-1] <= 256)
                   and (i == len(d) - 1 or d[i+1] <= 256))
    n_event = len(gaps) + 1 if prev >= 0 else 0
    print(f"events: {n_event}, all isolated 1-second bursts: "
          f"{n_single == n_event}; quiet gaps: {gaps}")
    if n_event == 0:
        print("VERDICT: wire was fully quiet — nothing to classify")
    elif n_single == n_event:
        print("VERDICT: every event is a 1s burst (poll-shaped); a rule of "
              "'ACTIVE_INFER needs >=2 consecutive sample-seconds' would "
              "leave the kitchen free to claim the inter-burst gaps")
    else:
        print("VERDICT: multi-second runs present — real generations on the "
              "wire; shape separation must keep counting them ACTIVE_INFER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
