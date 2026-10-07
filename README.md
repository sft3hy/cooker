# Cooker

A polite background intelligence sidecar for the home network. When the Mac Studio
is idle, Cooker researches, reviews and creates useful artifacts on `omlx`. The
moment you sit down and start typing — or OpenCode starts generating — it stops.

Interactive work always wins. That is not a tuning parameter, it is the design.

`OVERVIEW.md` is the spec of record. `PLAN.md` is the agreed v1 design and
milestones. `DISCOVERY.md` is the measurement log the numbers come from.

## Try it

```bash
uv sync --group dev
cooker doctor            # config, tools, ports, model, search — 0 fail = ready
cooker doctor --offline # skip the network probes
cooker probe -p "..."  # one real-size inference call, with token accounting
cooker db selftest     # prove the DAG queue works
cooker status          # queue summary as JSON
cooker watch --seconds 20   # the detector, live, one line per tick
cooker run --seconds 20     # dry-run: the loop, the decisions, no rows written
cooker run --live --seconds 600  # cook for real, only while the box is idle
```

`cooker run --live` spends GPU time, and says so on the way in — model, prefill
ceiling, endpoint. In dry-run it constructs no LLM object at all, so there is
nothing in the process holding a connection to omlx to misuse. `topics.md` is the
queue's front matter: add a line to queue research, delete one to withdraw it.

## Where things are

```
config.yaml          all the politeness knobs — nothing needs a code change
topics.md            what to research next; the queue refills from here
src/cooker/db.py     SQLite WAL store + the atomic DAG claim (BEGIN IMMEDIATE)
src/cooker/net.py    endpoint discovery: a port is not a service
src/cooker/wire.py   bytes per pid: the only honest signal that inference is happening
src/cooker/detect.py IDLE / ACTIVE_USER / ACTIVE_INFER, with the reason attached
src/cooker/scheduler.py when to cook, how many, and stopping when told
src/cooker/llm.py    cancellable streaming; the prefill ceiling enforced before send
src/cooker/search.py SearXNG + polite fetch: caps, cache, cooldowns, secret gate
src/cooker/chains.py the DAG — plan → search → fetch → extract×N → synthesize → …
src/cooker/runner.py one stage at a time: free kinds never reach the GPU
src/cooker/safety.py secrets in and out, untrusted fences, read-only git
src/cooker/cli.py    doctor / db / status / probe / watch / run / add / pause
tests/               the queue, detection, safety and chain invariants, executable
var/cooker.db        runtime state (gitignored)
outputs/             plated artifacts (gitignored)
```

## Two things this box taught us

**Prefill is atomic and exclusive.** `chunked_prefill: false`, so a background
request cannot be interrupted mid-prefill and it pushes interactive TTFT up
linearly: 1000 tokens costs 1.8× idle latency, 2000 costs 3.0×, 8000 costs 14×.
Cancelling mid-prefill buys nothing because no bytes arrive until it completes.
So prevention beats preemption, and `max_prefill_tokens_per_request: 1000` is
not a throttle — it is the ceiling that keeps you from noticing us.

**A port is not a service.** `lsof :8000` shows omlx on `*:8000` and an unrelated
`python -m http.server` on `127.0.0.1:8000`. The loopback bind wins, so loopback
answers 404 while omlx is healthy behind it. Cooker names the process by argv and
probes URLs; it never infers health from a port number.

## Status

| Milestone | State |
|---|---|
| M1 config + storage + DAG + doctor + discovery | **done** |
| M2 detector + scheduler (dry-run) | **done** |
| M3 cancellable LLM client + safety gates | **done** |
| M4 research DAG | **done** |
| M5 eval, dedup, digest | next |
| M6 CLI surface | |
| M7 pixel kitchen (Svelte + SSE) | |
| M8 launchd + Traefik + Pi-hole registration | |
| M9 remaining generators | |
| M10 IMPLEMENTATION.md + polish | |

Nothing is pushed. `git log` is the record: M4's DoD was Test 1 (empty queue
self-refills and drains), and `bench/bench9_research_chain_live.py` is the run
that shows it against the real SearXNG and the real omlx.
