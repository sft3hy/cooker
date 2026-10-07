"""bench15 — the ledger answers the question the wire cannot (DISCOVERY §20).

Read-only corroboration + one tiny real generation. The question §17 left
open: does the omlx admin ledger (`idle_seconds`, `generating[]`,
`total_active_requests`) see inference the admin poller itself does NOT see?
The wire says 12.5 KB/s forever (the /admin/api/stats metronome, 2/s); the
ledger must say idle throughout — or this design does not hold.

Run once, alone, against the live box:

    .venv/bin/python bench/bench15_ledger_vs_wire.py

Phase A (30s): poll /admin/api/stats at 1Hz while the metronome runs.
Phase B: submit one real chat completion (max_tokens=8) and keep polling;
         print the per-tick ledger so we see prefilling/generating/active
         light up, idle_seconds reset, and completion land in total_requests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

STATS_URL = "http://100.122.197.81:8000/admin/api/stats"
HEALTH_URL = "http://100.122.197.81:8000/health"
GEN_URL = "http://100.122.197.81:8000/v1/chat/completions"


def api_key() -> str:
    env = subprocess.run(
        ["sh", "-c", "grep '^AR_LLM_API_KEY=' ~/dev/autoresearch-service/.env | cut -d= -f2-"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip().strip('"').strip("'")
    if not env:
        sys.exit("no AR_LLM_API_KEY")
    return env


def admin_key() -> str:
    out = subprocess.run(
        ["docker", "exec", "dashboard", "sh", "-c", "echo $OMLX_ADMIN_KEY"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if not out:
        sys.exit("no OMLX_ADMIN_KEY from dashboard container")
    return out


def post_json(url: str, payload: dict, cookie: str | None = None, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get_json(url: str, cookie: str | None = None, timeout: float = 4.0) -> dict:
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def login_cookie() -> str:
    req = urllib.request.Request(
        "http://100.122.197.81:8000/admin/api/login",
        data=json.dumps({"api_key": admin_key()}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.headers.get("Set-Cookie", "")
    if "omlx_admin_session=" not in raw:
        sys.exit(f"no admin cookie in Set-Cookie: {raw!r}")
    return raw.split(",")[0].split(";")[0]


def main() -> None:
    cookie = login_cookie()
    print(f"cookie ok; /health: {get_json(HEALTH_URL, cookie)['status']}")

    def tick_row(t0: float) -> tuple[dict, dict]:
        st = get_json(STATS_URL, cookie)
        am = st["active_models"]
        models = {m["id"]: m for m in am["models"]}
        ours = models.get("Qwen3.8-Flash-Next-oQ4e-mtp", {})
        snap = {
            "t": round(time.time() - t0, 1),
            "active": am["total_active_requests"],
            "waiting": am["total_waiting_requests"],
            "idle_s": ours.get("idle_seconds"),
            "pre": len(ours.get("prefilling") or []),
            "gen": len(ours.get("generating") or []),
            "req_total": st["total_requests"],
        }
        return snap, st

    print("\nphase A — metronome running, nobody generating (30 x 1Hz):")
    t0 = time.time()
    base_req = None
    for _ in range(30):
        snap, _st = tick_row(t0)
        base_req = base_req if base_req is not None else snap["req_total"]
        print(f"  t+{snap['t']:>5} active={snap['active']} waiting={snap['waiting']} "
              f"idle={snap['idle_s']} pre={snap['pre']} gen={snap['gen']} "
              f"req_total={snap['req_total']}")
        time.sleep(1.0)
    print(f"phase A: total_requests moved {snap['req_total'] - base_req} "
          f"while {round(snap['t'])}s of admin polling — expect 0")

    print("\nphase B — one real completion (thread), ledger polled at 10Hz:")
    import threading
    t1 = time.time()
    outcome: list[str] = []

    def generate() -> None:
        req = urllib.request.Request(
            GEN_URL,
            data=json.dumps({
                "model": "Qwen3.8-Flash-Next-oQ4e-mtp",
                "messages": [{"role": "user", "content": "Say pong."}],
                "max_tokens": 8, "temperature": 0.0,
            }).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key()}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode())
            outcome.append(f"ok {out['choices'][0]['message']['content']!r} "
                           f"usage={out.get('usage')}")
        except Exception as exc:
            outcome.append(f"failed {type(exc).__name__}: {exc}")

    th = threading.Thread(target=generate)
    th.start()
    seen_busy = False
    last_print = 0.0
    while th.is_alive() or time.time() - t1 < 5.0:
        snap, _st = tick_row(t0)
        busy = snap["active"] > 0 or snap["pre"] > 0 or snap["gen"] > 0
        seen_busy = seen_busy or busy
        if busy or snap["t"] - last_print >= 1.0:
            last_print = snap["t"]
            print(f"  t+{snap['t']:>5} active={snap['active']} waiting={snap['waiting']} "
                  f"idle={snap['idle_s']} pre={snap['pre']} gen={snap['gen']} "
                  f"req_total={snap['req_total']}")
        time.sleep(0.1)
    th.join()
    print(f"  completion: {outcome[0] if outcome else 'no result'}")
    print(f"  ledger saw the generation: {seen_busy}")
    print(f"phase B wall: {time.time() - t1:.1f}s")


if __name__ == "__main__":
    main()
