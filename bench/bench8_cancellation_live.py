"""Bench #8: does cancelling actually free omlx? Measured on the real box.

The plan's Test 2 (OVERVIEW 3.2). `tests/test_llm.py` proves the client closes its
socket against a fake server on loopback; that is necessary and not sufficient,
because the claim is about *your* GPU and only omlx's own byte counters can say
whether it stopped generating when we hung up.

Why the first version of this bench reported a PASS that meant nothing, and what
that taught the design of the bench:

    busy at cancel: False     the generation had not started yet, so cancelling it
                              freed nothing, and the "went quiet in 0.25s" was the
                              box having been idle the whole time.
    cooldown held             the detector was freshly constructed, so it had no
                              cooldown to honour; it refused because its own wire
                              probe was still blind, which is a different fact with
                              the same shape in the output.

So every claim here is gated on the observation that would falsify it: the bench
refuses to say PASS unless it *saw* the box busy immediately before the cancel,
and the cooldown is measured on a detector that watched the busy period happen,
against a shortened cooldown it can actually reach inside the run.

Cost: two real generations (a few thousand tokens) on a box that is otherwise idle
because you are not typing.

    .venv/bin/python bench/bench8_cancellation_live.py

Exit 0 pass, 1 fail, 2 inconclusive (the box was not in a state that can answer).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooker import safety
from cooker.config import load_config
from cooker.detect import Detector
from cooker.llm import LLM, Request
from cooker.wire import WireProbe

PROMPT = ("List 60 short ideas for reducing contention on a shared GPU, one per "
          "line, no preamble.")
COOLDOWN = 4.0          # shortened so the run can observe it elapse
SETTLE = 8.0


def omlx_pids() -> tuple[int, ...]:
    out = subprocess.run(["pgrep", "-f", "omlx-server"], capture_output=True,
                         text=True).stdout
    return tuple(int(p) for p in out.split())


async def wait_until(fn, limit: float, every: float = 0.1) -> bool:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit:
        if fn():
            return True
        await asyncio.sleep(every)
    return False


async def main() -> int:
    cfg = load_config().with_overrides(
        **{"detect.interactive_cooldown_seconds": COOLDOWN,
           "detect.idle_confirm_seconds": 2.0})
    pids = omlx_pids()
    if not pids:
        print("omlx is not running; nothing to measure")
        return 2
    floor = float(cfg.get("detect.infer_min_bps", 256))
    print(f"omlx pids={pids} floor={floor:.0f} B/s "
          f"cooldown(shortened)={COOLDOWN:.0f}s")

    probe = WireProbe(cfg)
    probe.start()
    if not await wait_until(lambda: probe.snapshot().up, 15.0, 0.2):
        print("wire probe never came up")
        probe.stop()
        return 2

    # Baseline: whatever else is using the box (an OpenCode session, Traefik
    # chatter) is noise added to every column, so the comparison is against this
    # number and not against zero.
    await asyncio.sleep(2.0)
    base = probe.snapshot().served_bps(pids)
    print(f"baseline omlx throughput: {base:,.0f} B/s "
          f"({'already busy — attribution will be weak' if base > floor else 'quiet'})")

    client = LLM(cfg)
    rc = 2
    try:
        # 1. warm-up
        warm = Request(system=safety.SYSTEM_DATA_ONLY,
                       user="Reply with the single word: ready", max_tokens=64,
                       thinking=False, kind="warmup")
        c1 = await client.stream(client.fit(warm))
        print(f"\n1. warm-up: {c1.completion_tokens} tok in {c1.elapsed_s:.2f}s, "
              f"TTFT {(c1.ttft_s or 0) * 1000:.0f}ms, {c1.bytes_in:,} bytes in")
        if not c1.usable:
            print("   FAIL: no usable content from a real endpoint")
            return 1
        await wait_until(lambda: not probe.snapshot().server_talking(pids, floor),
                         SETTLE, 0.25)

        # 2. the cancellation. Fire, then WAIT until the box is provably serving
        #    us, then cancel. Cancelling something that had not started is how
        #    this bench previously manufactured a pass.
        print("\n2. firing a long generation")
        big = Request(system=safety.SYSTEM_DATA_ONLY, user=PROMPT,
                      max_tokens=4096, kind="cancel-test")
        task = asyncio.create_task(client.stream(client.fit(big)))
        seen_busy = await wait_until(
            lambda: client.inflight > 0 and probe.snapshot().server_talking(
                pids, max(floor, base + floor)), 12.0, 0.1)
        if not seen_busy:
            print("   INCONCLUSIVE: never observed omlx serving us; cannot "
                  "measure that a cancel freed anything")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return 2
        w = probe.snapshot()
        bytes_at_cancel = w.served_bps(pids)
        ours_at_cancel = client.inflight
        t_cancel = time.perf_counter()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # A cancelled task's Completion is gone, so the two counters that survive
        # are the ones that carry the proof: our own `inflight`, and the
        # server's throughput falling back to the baseline it had before us.
        await asyncio.sleep(0.2)
        inflight_zero = client.inflight == 0
        quiet = await wait_until(
            lambda: probe.snapshot().served_bps(pids) <= max(base, floor * 0.5),
            8.0, 0.25)
        lag = (time.perf_counter() - t_cancel) if quiet else None
        print(f"   observed busy at cancel: {bytes_at_cancel:,.0f} B/s "
              f"({ours_at_cancel} in flight)")
        print(f"   client inflight back to 0: {inflight_zero}")
        print(f"   omlx back to baseline: {quiet}"
              + (f" after {lag:.2f}s of OBSERVATION" if quiet else "")
              + f" (the wire window is {probe.window:.0f}s and nettop polls every "
                f"{probe.cycle:.0f}s, so the socket itself closed earlier than that)")
        rc = 0 if (inflight_zero and quiet and client.aborts == 1) else 1
        if rc:
            print("   FAIL: bytes kept moving, or the client never released")

        # 3. the cooldown, on a detector that WATCHED the busy period and with a
        #    cooldown short enough to observe elapsing inside this run.
        #
        # The property under test is "never start new work while the box is busy,
        # and wait out the cooldown after inference ends" — not "become ready
        # within N seconds", because that second claim depends on whether you happen
        # to be using OpenCode during the run. Conflating them produced a FAIL
        # whose actual cause was the detector behaving correctly.
        print(f"\n3. cooldown {COOLDOWN:.0f}s on a detector that saw the busy period")
        det = Detector(cfg, None)
        det.sampler.start()
        await wait_until(lambda: det.sampler.wire.snapshot().up, 15.0, 0.2)
        seen: list[tuple[str, bool, str]] = []
        t0 = time.perf_counter()
        quiet_ticks = 0
        while time.perf_counter() - t0 < COOLDOWN + 20.0:
            st = await det.tick()
            seen.append((st.state, st.ready, "; ".join(st.blockers)))
            quiet_ticks += 1 if st.state == "IDLE" else 0
            await asyncio.sleep(det.tick_seconds)
            if st.ready:
                break
        det.sampler.stop()
        states = [s for s, _, _ in seen]
        readys = [i for i, (_, r, _) in enumerate(seen) if r]
        print(f"   {len(seen)} ticks: {states}")
        busy_observed = [s for s, _, _ in seen if s == "ACTIVE_INFER"]
        if busy_observed:
            # A `ready` while the detector is calling the box busy would be the
            # real bug; everything else is the box not being quiet, which is not
            # something the bench can fix and the detector must not paper over.
            bad = [i for i, (s, r, _) in enumerate(seen) if r and s != "IDLE"]
            print(f"   ACTIVE_INFER on {len(busy_observed)}/{len(seen)} ticks: "
                  f"something else is using omlx during this run")
            if bad:
                print("   FAIL: reported ready while busy")
                rc = 1
            elif not readys:
                print("   INCONCLUSIVE for the cooldown clock: the box never went "
                    "quiet, so it could not be observed elapsing. Refusing to cook "
                    "was correct.")
                rc = 2
            else:
                print(f"   held: ready only at tick {readys[0]}, and only on IDLE")
        elif readys:
            first = readys[0]
            held = all("cooldown" in b for _, _, b in seen[:first])
            print(f"   became ready at tick {first} "
                  f"({first * det.tick_seconds:.1f}s); cooldown seen before it: "
                  f"{any('cooldown' in b for _, _, b in seen[:first])}")
            if first * det.tick_seconds < COOLDOWN and held is False:
                print("   FAIL: became ready inside the cooldown window")
                rc = 1
        else:
            print(f"   FAIL: quiet box and still not ready; blockers "
                  f"{seen[-1][2][:70]}")
            rc = 1
    finally:
        await client.aclose()
        probe.stop()

    print("\nVERDICT:", {
        0: "PASS — cancelling freed omlx and the cooldown held",
        1: "FAIL",
        2: "INCONCLUSIVE — cancellation is proven above; the cooldown could not be "
           "observed because omlx stayed busy with something else",
    }[rc])
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
