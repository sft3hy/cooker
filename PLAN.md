# Cooker — Design & Implementation Plan

> Status: **v1 design agreed with Sam, 2026-10-06.** Spec of record is `OVERVIEW.md`;
> this is how we build it. Discovery numbers land in `DISCOVERY.md`.

---

## 0. Agreed constraints (from Sam)

| Decision | Choice |
|---|---|
| UI hosting | **Host process** on `127.0.0.1:8256`, fronted by Traefik `cooker.home.arpa → host.docker.internal:8256` (the `opencode.home.arpa` pattern). Daemon must be native: it needs `lsof`/`ioreg`/`ps`/`git`/`~/dev`. |
| Frontend | **Svelte + Vite**, compiled to static assets, served by the daemon. |
| Hero animation | **Pixel kitchen — one pot per generator.** |
| Research | **Hybrid** — own cancellable pipeline for everyday work, delegate deep-dives to `autoresearch.home.arpa`. |
| Daemon stack | **Python 3.14**, stdlib `sqlite3`/`asyncio`, `httpx` + FastAPI, `uv`-managed venv. |
| Inference | **Real but throttled**: 1 worker, small prefills, only when the machine is idle. |
| Auth | None (tailnet-trusted, like `status.home.arpa`). |
| Viewports | iPhone **and** laptop **and** 27"/32" monitor. Integer-scaled pixel art, responsive stacking. |
| Sound | 8-bit chiptune SFX, synthesised in WebAudio (no asset files), mute persisted, default off. |

## 1. Why this box is different (what discovery already told us)

These facts shape the design and are *not* in `OVERVIEW.md`:

1. **omlx binds `0.0.0.0:8000`** (its own `settings.json → server.host`), not tailnet-only as the service map claims. There is also a stray `python -m http.server 8000` bound to `127.0.0.1:8000` (PID 74595) coexisting via reuse. **Consequence:** every `lsof`-based detector must filter by *server PID identity*, not by port alone, or it will miscount.
2. **omlx has almost no observability surface.** `/metrics`, `/health`, `/admin/api/queue|status|requests|jobs` all 404. Live: only `/v1/models` (authed) and `/admin/api/login` + `/admin/api/stats` (cookie session). Side channels: `~/.omlx/stats.json` (mtime), `~/.omlx/usage.sqlite3` (`model_usage_hourly`), `/opt/homebrew/var/log/omlx.log`. **Consequence:** passive detection must be built from TCP state + HID idle + file mtimes. There is no queue to poll.
3. **`sse_keepalive_mode: "chunk"`** — omlx emits SSE keepalive frames before the first real token. Any naive "time to first byte" metric is a lie (measured 0.004 s vs a real 0.6 s). **Consequence:** the metrics collector parses SSE frames and stamps TTFT only on the first delta carrying non-empty `content`.
4. **`scheduler.max_concurrent_requests: 8`, `decode_fairness: true`, `chunked_prefill: false`.** `chunked_prefill: false` is the load-bearing one: a prefill is **atomic and exclusive**, so a 12k-token background prefill blocks everything for its duration, while concurrent *decodes* interleave fairly. This is exactly what the prefill ladder in `DISCOVERY.md` measures, and it is why `max_prefill_tokens_per_request` matters far more than worker count.
5. **Client disconnect halts generation** (bench #1: post-abort TTFT ratio **1.01×**, `OVERVIEW.md` §0.1 hope confirmed). Self-preemption by dropping the socket is viable. Being re-proven during *active decode* in bench #3.
6. **SearXNG is container-only** — host `:8080` is Pi-hole, not SearXNG. Cooker reaches it at `https://searxng.home.arpa/search?format=json&q=…` verified against the step-ca root at `~/homelab/edge/step/certs/root_ca.crt` (returns 200 + 20 results today). Cooker does **not** need to trust the private CA blindly: pin the bundle path in config.
7. **autoresearch is reusable over HTTP** (200 on `/health`), async jobs, SSE progress, `POST /jobs/{id}/cancel` is *cooperative* — checked between phases. Fine for deep-dives, wrong for latency-critical small steps. Hence the hybrid.
8. **OpenCode is detectable for free**: `lsof` shows `opencode:56428→:8000 ESTABLISHED` right now, and `~/.local/share/opencode/opencode.db-wal` has a live mtime. Two independent "someone is driving" signals.

## 2. Process model

One host process, supervised by launchd. It runs four cooperating asyncio groups:

```
                    ┌───────────────────────────────────────────┐
                    │  launchd: com.homelab.cooker (KeepAlive) │
                    └────────────────┬──────────────────────────┘
                                     ▼
   detector (1s) ──► BUS STATE ──► scheduler ──► worker pool (N=0..max)
      │            IDLE / ACTIVE_USER / ACTIVE_INFER   │
      │  lsof :8000, ioreg HIDIdleTime,               ├─► stage runners
      │  opencode.db-wal mtime, ps %cpu              │     ├─ llm.py   (httpx stream, cancellable)
      │                                                │     ├─ search.py  (searxng via Traefik)
      │                                                │     ├─ delegate.py(autoresearch + cancel)
      │                                                │     └─ safety.py  (secrets, injection, git-ro)
      └──────────────► events table ──► SSE ──► web/ (Svelte pixel kitchen)
```

- **detector** never blocks; each signal has its own cadence and a hard per-signal timeout so a stuck `lsof` cannot stall the loop.
- **scheduler** is the only component that may start or kill a worker.
- **server** (FastAPI) is read-mostly; it must never issue inference calls.

Single process means the UI is exactly as available as the daemon — acceptable, and it keeps WAL access local.

## 3. Detection & backoff engine

### 3.1 Signals → state

| Signal | Source | Cadence | Tells us |
|---|---|---|---|
| `wire` | `nettop -n -x -P -L 2 -s 1`, bytes moved by the **omlx server pid** in the window | 2 s poll | inference is happening **now**. NOT the socket: an ESTABLISHED :8000 peer is a keepalive that lasts for hours at exactly 0 bytes (DISCOVERY §11) |
| `infer_clients` | `lsof -nP -iTCP:8000 -sTCP:ESTABLISHED`, drop shadow addrs and our own PID | 2 s | names *who is connected*. It is an address book, not a trigger — it cannot distinguish generating from idle |
| `hid_idle` | `ioreg -arc IOHIDSystem -k HIDIdleTime` (text parse) | 2 s | typing/mouse in the last N seconds |
| `opencode_live` | ESTABLISHED → `127.0.0.1:4096`, plus `opencode.db-wal` mtime age | 2 s | an OpenCode session is open and writing |
| `fs_activity` | max mtime of `~/dev/*` + `~/homelab/*` depth-2 dirs and `.git/index` | 5 s | files were just saved |
| `llm_sidechannel` | mtime of `~/.omlx/stats.json` | 5 s | **demoted:** three real 200s moved neither the counters nor the mtime. Daily accounting only (§11) |
| `load` | `ps -Ao comm,%cpu` sums for `omlx`/`opencode` | 5 s | corroborating pressure signal |

**State, strictest wins:**
- `ACTIVE_INFER` — **omlx itself moved ≥ `infer_min_bps` in the wire window** → abort our in-flight request immediately, start the cooldown clock. A connected-but-silent peer is `IDLE` with reason `keepalive: …`, deliberately *not* ACTIVE_INFER: if a keepalive latched the state, the kitchen would never light (§11). If nettop is not running the state is `ACTIVE_USER` with reason `blind: …` — finish what is in flight, start nothing, because 0 and unknown are different answers.
- `ACTIVE_USER` — `hid_idle < user_idle_seconds` (default 120) **or** OpenCode WAL written in the last 30 s **or** `fs_activity < 60 s` → **do not start new stages**; let the current stage finish (bounded by `max_stage_seconds`) so work isn't thrown away. Typing is not the same as generating, so this is deliberately less twitchy than `ACTIVE_INFER`.
- `IDLE` — none of the above held for `idle_confirm_seconds` (default 60) → ramp workers toward `max_concurrency`.

**Cooldown:** after the last `ACTIVE_INFER`, wait `interactive_cooldown_seconds` (60) before the first new request. Hysteresis everywhere — no flapping.

Every transition writes an `events` row, so the UI can explain *why* it is stopped instead of just sitting there.

### 3.2 Cancellation
A stage runs in its own `asyncio.Task` around `httpx.AsyncClient.stream(...)`. On `ACTIVE_INFER`, an unbounded wait, or a manual `cooker pause`, the supervisor cancels the task; httpx closes the socket; omlx stops generating. The stage is recorded `PAUSED`, `preemptions += 1`, and the task is re-queued with `not_before = now + backoff(preemptions)`.

Stages are the unit of resumption, so a preemption costs one stage, not a job. `chunked_prefill: false` means a *stage* is the smallest unit omlx will let us interrupt anyway — cancelling mid-stage is meaningless.

### 3.3 Adaptive concurrency
```
IDLE          → workers ramp 1 → max_concurrency (start at 1; Sam's throttled rollout)
ACTIVE_USER   → ramp down to 0 new stages; finish current
ACTIVE_INFER  → abort everything in flight, cooldown 60 s
```
Plus a self-inflicted brake: if our own rolling p95 TTFT for background requests exceeds `latency_p95_ceiling_s`, halve concurrency. Optimum metric stays *useful artifacts/day*, not tokens/s.

## 4. Storage

`cooker.db`, WAL, `synchronous=NORMAL`, `busy_timeout=5000`.

**`tasks`** — one row per *stage instance*. This is what makes the DAG real: `research.extract` fans out one row per source, `research.synthesize` fans in with `dependencies` = the array of extract UUIDs. `parent_task_id` covers the linear case.

```
id TEXT PK (uuid7)          chain_id TEXT      kind TEXT        generator TEXT
title TEXT                  status TEXT         priority INT      prompt TEXT
payload_json TEXT          result_path TEXT    result_hash TEXT
parent_task_id TEXT        dependencies TEXT (JSON array of uuids)
attempts INT  preemptions INT  input_tokens INT  output_tokens INT
ttft_ms REAL  duration_ms REAL  score REAL  scores_json TEXT  error TEXT
not_before REAL  created_at REAL  started_at REAL  finished_at REAL
```
Statuses exactly as specced: `QUEUED RUNNING PAUSED SUCCEEDED FAILED PARKED CANCELLED REJECTED`.

Supporting tables: `events` (append-only log → SSE + animation), `feedback` (`cooker rate`), `seen` (url/topic hashes for the 14-day rule), `evaluations`, `daily_stats`.

**Claiming a task** is one atomic statement: `UPDATE … SET status='RUNNING', started_at=? WHERE id=(SELECT … QUEUED AND deps satisfied AND not_before<=now …) RETURNING id`, inside `BEGIN IMMEDIATE`. No worker can take the same row twice.

## 5. Generators (stage chains)

| Generator | Chain |
|---|---|
| `research` | `plan → search → fetch → extract×N → synthesize → critique → evaluate → publish` |
| `deep-dive` | `delegate.autoresearch` (POST `/research`, stream `/jobs/{id}/events`, `POST /cancel` on preemption) → `evaluate → publish` |
| `project-review` | `scan (no LLM) → analyze → evaluate → publish` |
| `homelab-audit` | `scan → check×N (per compose/env file) → consolidate → evaluate → publish` |
| `brainstorm` | `generate → critique (strict 5-point) → evaluate → publish` |
| `creation` | `spec → write → review → evaluate → publish` (writes only into an isolated workspace dir) |
| `digest` | `collect (no LLM) → write → publish` |

LLM-free stages (`scan`, `collect`) cost nothing and give the UI something to show before inference is allowed.

## 6. Quality, dedup, feedback
- `evaluate` stage emits strict JSON `{usefulness, accuracy, novelty, actionability, relevance}` 1–5; overall = mean; **≥ 3.5 publishes**, else `REJECTED` (kept, never silently dropped).
- Dedup: `sha256(prompt ‖ normalized_result)` → `result_hash`; `seen` blocks an equivalent topic for 14 days.
- Feedback: `cooker rate <id> good|meh|bad` → per-generator EMA. EMA below floor for N artifacts parks that generator until re-enabled. The scheduler stops pulling from what you don't read.

## 7. Safety
1. **Secrets before prompts** — regex scan (PEM blocks, `ghp_`/`github_pat_`, `sk-`, AWS `AKIA`, `xox[baprs]-`, `Authorization:`, `…_PASSWORD=`, `.env` file contents) redacts *and* logs the redaction. Any file going into a prompt passes this gate first.
2. **Prompt injection** — all fetched/derived text is fenced as `<untrusted source=… trust=none>`; the system prompt states it is data, never instructions. Code found in web content is quoted, never run, and quarantined to `*.rejected.md`.
3. **Read-only** — git only through a whitelisted `git_readonly()` (`status diff log show blame ls-files ls-tree remote-v`); deny-list enforced (`push commit checkout reset merge clean rebase stash`). Never interpolate a user string into a shell command.
4. **Sandbox** — creation stages write only under `outputs/YYYY-MM-DD/workspaces/<task-id>/`; path resolution is rejected outside it.
5. **Budgets** — `max_disk_gb`, `max_retries`, `max_task_runtime_minutes`, and a hard per-request `max_prefill_tokens_per_request` derived from the prefill ladder.

## 8. Backend for the kitchen

FastAPI on `127.0.0.1:8256`:
- `GET /api/state` — one snapshot the UI renders from: bus state, cooldown remaining, worker slots, and **one pot per generator** (stage, title, progress, elapsed, tokens), queue counts, today's artifact list, throughput/TTFT/preemption stats.
- `GET /api/events` — SSE tail of the `events` table (500 ms poll → survives restart, replayable, no broker).
- `GET /api/artifacts/{id}` — markdown of a plated artifact.
- `GET /api/health`, `GET /api/metrics` (JSON; ready to feed `status.home.arpa` later).
- static: `web/dist`.

## 9. Frontend — the pixel kitchen
- Logical canvas **480×270** scaled by an integer factor (`image-rendering: pixelated`) so sprites never smear — fills an iPhone width at 1×→2× and a 32" monitor at 4×+.
- Six burners = six generators. Pot states: **cold → filling (ingredients drop in) → simmer → rolling boil → PREEMPTED (smoke puff + `BACKOFF 47s`) → plated (click to read) → swept-off (rejected) → taped-off (parked)**.
- HUD: bus state badge, cooldown bar, tokens today, TTFT p95, preemptions, *useful artifacts today*.
- Palette: warm kitchen — charcoal-plum room, ember amber burners, cream plates, cyan steam. Not NES grey.
- Type: vendored OFL pixel font (`Press Start 2P`) for display, system mono for readable body text at small sizes on phone.
- SFX: WebAudio square/triangle blips — `klunk` on task start, filtered-noise bubbles while boiling, arpeggio on plate, descending buzz on preemption. Master mute in `localStorage`, default **off** (autoplay policy).
- Mobile: scene on top, HUD then task list stacked below; tap a pot → drawer with its live stage log.

## 10. Deliverables & milestones

Each milestone is independently useful and shippable. One at a time.

| # | Milestone | Proves |
|---|---|---|
| **M1** | Scaffold: uv venv, `config.yaml`, `db.py` schema + DAG claim test | SQLite queue + deps resolve, `cooker doctor` passes |
| **M2** | `detect.py` + `scheduler.py`, idle→work→preempt loop, dry-run | Passive detection correct; state machine transitions logged |
| **M3** | `llm.py` cancellable client + `safety.py`; real tiny generation | **Test 2**: socket drop frees omlx, cooldown honoured |
| **M4** | `search.py` + `research` chain (fan-out/fan-in DAG) | **Test 1**: empty queue self-refills and drains |
| **M5** | `eval.py` + dedup + `digest.py` | **Test 3**: real evaluated digest |
| **M6** | `cli.py` (`status add pause resume list show rate digest doctor`) | RLHF loop + ops |
| **M7** | `server.py` + Svelte kitchen, sprites, SSE, SFX, responsive | The fun visible thing |
| **M8** | launchd plist, Traefik route + Pi-hole A record, service-map + changelog update | `https://cooker.home.arpa` live, forever |
| **M9** | Remaining generators (`brainstorm`, `creation`, `audit`, `review`, `deep-dive`) + weights | Full §5 surface |
| **M10** | `IMPLEMENTATION.md` + measured DoD | §13 |

**Runtime ordering:** M1–M3 dry-run → M3 real-but-tiny → M4+ real. Nothing touches omlx before the cancellation path is proven on a 200-token request.

### Status (as of 2026-10-07)

| milestone | state | evidence |
|---|---|---|
| M1 storage, queue, doctor | **done** | `cooker db selftest`, 27 tests at the time |
| M2 detection by bytes | **done** | `cooker watch`; §11 — 0 vs 5,376 B/s separation |
| M3 cancellable inference, safety gate | **done** | `bench8_cancellation_live.py` — 1,711 B/s busy → inflight 0 → baseline |
| M4 research DAG | **done** | `bench9_research_chain_live.py` Test 1 PASS, 7/7 stages |
| M5 gate, dedup, feedback, digest | **done** | `bench10_digest_live.py` Test 3 PASS — 8 stages live, evaluated, digest rendered with the rows' own numbers |
| M6 CLI surface | **done** | `status add pause resume list show rate digest doctor` all live against a scratch DB; 19 tests in `tests/test_cli.py`; the ids `list` prints are asserted to resolve in `rate`/`show` |
| M7 kitchen UI | **done (live proof pending)** | node 26 installed; `server.py` (FastAPI on loopback:8256) + `web/` Svelte canvas kitchen built; 10 tests in `tests/test_server.py`, 228 total; `cooker serve` mounts kitchen+API+static, SSE replays by rowid, gate panel honest `stale` when detached |
| M8 launchd + Traefik + register | not started | see §11 ceremony |
| M9 remaining generators | not started | `chains.advance` emits `chain.stub` for them today; one orphan `brainstorm/generate` stage sits QUEUED as proof |
| M10 IMPLEMENTATION.md | not started | — |

**Pushed.** `main` is on GitHub at `df0eac1` plus the M6 work; the branch has
always been `main`, and an earlier note here calling it `master` was wrong in the
same breath as claiming nothing was pushed.

Carried forward, unresolved and named so:

- **A live SSE client keeps Cooker off the GPU indefinitely.** The detector reads
  `opencode`'s steady ~1.7 KB/s decode as `ACTIVE_INFER`, so `run --live` claimed
  nothing for 151 s (§14). Correct behaviour, unusable outcome. M8 needs a burst
  detector — Δ bytes over a short window — not a lower `infer_min_bps`.
- **No artifact has cleared 3.5 live yet.** Every verdict so far is a REJECT, so
  the "Worth reading" section has unit coverage and no production example.
- **`bench6_abort_prefill.py` still wants a genuinely idle window**, and the
  prefill ceiling goes up only on a clean win from it.

## 11. Ports & homelab registration (obligatory ceremony)
- Cooker binds **`127.0.0.1:8256`** — free, unregistered, loopback-only (matches how `opencode` stays off the LAN).
- Add `cooker.home.arpa` to `HOSTS` in `~/homelab/edge/scripts/02-pihole-wildcard.sh`, re-run (idempotent).
- Add router + `service: cooker → host.docker.internal:8256` to `~/homelab/edge/traefik/dynamic/services.yml` (hot-reload; keep the flush-left comment invariant intact — the 2026-10-04 TLS incident).
- Append `:8256` row to Port Register, a Service Entry, dependency-graph edges (`cooker → omlx, searxng, autoresearch`), and one dated Changelog line. **No secret values.**

## 12. Open questions I'll resolve with measurement, not opinion
1. Where does the prefill ladder put the safe ceiling — 2k? 4k? (`DISCOVERY.md` §E)
2. Does `lsof` at 1 s cadence cost enough to matter on this box? (measured in M2)
3. ~~Does `usage.sqlite3` commit frequently enough to be a usable inference signal?~~ **Answered:** no. Neither it nor `stats.json` moved across three successful generations; `nettop` bytes per pid is the signal, at 0 vs 5,376 B/s separation (§11).
