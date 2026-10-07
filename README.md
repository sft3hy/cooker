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
```

`cooker run` (the daemon) arrives in M2.

## Where things are

```
config.yaml          all the politeness knobs — nothing needs a code change
src/cooker/db.py     SQLite WAL store + the atomic DAG claim (BEGIN IMMEDIATE)
src/cooker/net.py    endpoint discovery: a port is not a service
src/cooker/cli.py    doctor / db / status / probe
tests/               the queue and discovery invariants, executable
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
| M2 detector + scheduler (dry-run) | next |
| M3 cancellable LLM client + safety gates | |
| M4 research DAG | |
| M5 eval, dedup, digest | |
| M6 CLI surface | |
| M7 pixel kitchen (Svelte + SSE) | |
| M8 launchd + Traefik + Pi-hole registration | |
| M9 remaining generators | |
| M10 IMPLEMENTATION.md + polish | |
