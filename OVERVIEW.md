Here is the revised plan. The "Cooker Broker" proxy concept has been entirely removed. Instead, Cooker runs side-by-side with OpenCode and uses **passive detection** (checking ports, system idle time, and process activity) to trigger its own backoff and cancellation.

***

# The Cooker

## Always-On Background Intelligence for a Local LLM

### Goal

Build **Cooker**, a lightweight, always-on background work system for my local LLM that uses otherwise-idle inference capacity to continuously research, review, improve, and create useful artifacts from my own projects and interests.

The system runs on my **Mac Studio M5 Ultra with 256 GB unified memory**, using **Qwen 3.8 Flash through omlx** via an OpenAI-compatible API.

The primary interactive client is **OpenCode**.

### Core principle

> **Interactive GPU work always wins. Being at the computer is not hitting the GPU. Background work runs whenever omlx itself is not being asked for anything.**

*(Amended 2026-10-07 by Sam, verbatim: "it should run when I'm using the computer, just not when I'm hitting omlx via anything." The original clause read "Interactive work always wins" and the implementation had grown it into a presence curfew — HID, WAL and filesystem evidence closed the kitchen whenever a human was anywhere near the keyboard, which starved the daemon at its own desk.)*

Cooker is a polite sidecar client. It must never wrap, intercept, or supersede OpenCode or omlx. It watches **what reaches the GPU** — the server's ledger first (§20), bytes as tripwire, both failing closed — and when the GPU is being asked for anything it drops its own requests and backs off so the interactive client has full use of the LLM. Hands on the keyboard, browsers, compiles: not the GPU's business, not the kitchen's either.

*(Amended 2026-10-07, same owner: "please expose opencode as a tool that the cooker can work with: submitting prompts to opencode in the right directory, responding to its prompts when it asks questions." The prohibition was never *use* — it was *wrap*. Cooker now drives OpenCode through OpenCode's own documented HTTP API, opening **its own sessions** (visible in the session list, interruptible by anyone who sees them), in directories it owns, answered by policy, interrupted by the ledger like any other burn. It still never injects into anyone else's session, never intercepts traffic, never supersedes — the broker is still dead. §21.)*

---

# 0. Discovery Before Implementation

Produce a discovery report first before building.

## 0.1 Discover omlx

Determine:
* omlx process/container/service
* host and port
* OpenAI-compatible base URL
* available model IDs
* **whether HTTP client disconnects successfully halt generation on the backend** (Crucial for Cooker to cancel its own work)
* whether omlx exposes metrics or queue information (`/metrics` or similar) that Cooker can poll.

Run controlled inference benchmarks:

### Benchmark A — Interactive baseline
Run several identical small requests with 1 request at a time. Record TTFT (Time To First Token) and total latency.

### Benchmark B — Concurrency & Prefill Interference
Send a massive Cooker-style background prompt (10k+ tokens) and immediately send a small OpenCode-style prompt. 
* **Does the large background prefill cause a massive TTFT spike for the interactive request?**
* Define the maximum safe chunk size Cooker is allowed to send based on this interference.

---

# 0.2 Discover the existing SearXNG research system

Read `@HOMELAB-SERVICE-MAP.md` for the current setup. Identify how SearXNG is accessed and how the existing autoresearcher works.

**Reuse it.**
Do not create a second research pipeline if the existing system can be invoked as a library, CLI, or subprocess.

---

# 0.3 Survey projects and interests

Inventory `~/dev` and `~/homelab` (Read-only). Build an initial model of what exists and what is actively being worked on.

---

# 0.4 Discover available infrastructure

Check available macOS primitives: Python 3.11+, SQLite, launchd.
Determine the best way to passively detect human coding activity on macOS (e.g., `ioreg` for idle time, `lsof -i` for active connections to the omlx port, `ps` for OpenCode CPU spikes).

**Avoid** introducing external infrastructure like Redis or Celery.

---

# 0.5 Discovery report

Produce `~/dev/hobby/cooker/DISCOVERY.md` with benchmark results, recommended concurrency, and detection strategies, then proceed to implementation.

---

# 1. Architecture

Cooker runs entirely independent of OpenCode, communicating with omlx as a peer client.

```text
       ┌────────────────────┐          ┌────────────────────┐
       │      OpenCode      │          │       Cooker       │
       │  (Interactive Use) │          │  (Background Use)  │
       └─────────┬──────────┘          └─────────┬──────────┘
                 │                               │
                 │                               ▼
                 │                      ┌────────────────┐
                 │                      │   Scheduler &  │
                 │                      │ Backoff Engine │
                 │                      └────────┬───────┘
                 │                               │
                 ▼                               ▼
       ┌────────────────────────────────────────────────────┐
       │                     omlx / LLM                     │
       └────────────────────────────────────────────────────┘
```

Generators create work. The SQLite queue stores it. Workers execute it. The Scheduler monitors system activity to decide if it should hit the brakes.

---

# 2. Persistent Queue

Use SQLite: `~/dev/hobby/cooker/cooker.db` (with WAL mode enabled).

## Task schema

Includes basic fields (`id`, `status`, `prompt`, `payload_json`), timestamps, and tracking metrics (`input_tokens`, `preemptions`).
Crucially, include **DAG support**:
* `parent_task_id` (UUID)
* `dependencies` (JSON array of UUIDs that must complete first)

## Task states
`QUEUED`, `RUNNING`, `PAUSED`, `SUCCEEDED`, `FAILED`, `PARKED`, `CANCELLED`, `REJECTED`.

---

# 3. Scheduler & Passive Backoff

Cooker does not control omlx. It only controls itself. 

## 3.1 Passive GPU-Access Detection

Signals, ranked by what they can actually prove (every demotion below was earned
by a live failure, DISCOVERY §11–§20):

1. **omlx ledger (primary)**: `GET /admin/api/stats` — `total_active_requests`,
   `generating[]`, per-model `idle_seconds`. The server's own memory; monitors
   provably never move it, a 0.26-second completion does (§20). This is what
   answers "is anyone hitting omlx".
2. **Wire bytes (tripwire + blind-mode judge)**: `nettop` per-flow across
   :8000 with an endpoint filter. Bytes prove traffic, never purpose — they
   preempt a running stage (cheap, +95ms measured) and they stand the kitchen
   down when the ledger is blind, but they do not classify while the ledger can
   answer.
3. **Presence signals (annotation only, since §20b)**: HID idle, opencode WAL
   freshness, filesystem saves, :4096 subscribers. Read for the UI ("human
   present") and for the legacy `presence_blocks: true` regime; **they block
   nothing.** A connected socket is a relationship, not an event; a heartbeat
   is neither. `lsof ESTABLISHED` to omlx never meant "OpenCode is
   generating" — that sentence is deleted from this spec, twice measured wrong
   (§6, §11).

## 3.2 Self-Preemption

If Cooker detects interactive work while it is running a background task:
1. **Drop the connection**: Cooker immediately cancels its own `asyncio` HTTP request to omlx. (Assuming omlx kills generation on client disconnect).
2. Mark the task as `PAUSED` and increment `preemptions`.
3. Enter a cooldown phase (e.g., wait 60 seconds after the last detected GPU activity — `ACTIVE_INFER` ending, not the last keystroke — before sending any new requests).

Design tasks in **resumable stages** (`search → fetch → synthesize`) so cancelling mid-generation loses minimal work.

## 3.3 Adaptive Concurrency

Cooker monitors its own request latency.
```text
IDLE (GPU unclaimed — ledger idle, bytes quiet) -> +1 background worker (to max)
READY  (keyboard busy, GPU idle)                  -> keep cooking (§20b, 2026-10-07)
ACTIVE_INFER (anything hitting omlx)              -> Abort Cooker generation instantly
BLIND (cannot see the GPU)                        -> finish in flight, start nothing
```
*The old middle line — typing pauses everything — was the presence curfew
Sam struck on 2026-10-07; the keyboard never had claims on the GPU, only
the wire does.*

---

# 4. Configuration

`~/dev/hobby/cooker/config.yaml`

```yaml
llm:
  base_url: "http://127.0.0.1:8000/v1"
  model: "Qwen3.8-Flash-Next-oQ4e-mtp"

scheduler:
  max_concurrency: 2
  interactive_cooldown_seconds: 60
  max_prefill_tokens_per_request: 8000 # To prevent locking up omlx

limits:
  max_task_runtime_minutes: 30
  max_disk_gb: 20
  max_retries: 3
```

---

# 5. Task Generators

Triggered periodically by the daemon, generating tasks into SQLite. 

1. **Research**: Maintains `topics.md`. Workflow: `search → fetch → extract → synthesize → critique`.
2. **Project Review**: Inspects active repositories read-only.
3. **Homelab Audit**: Reviews compose files and configurations.
4. **Brainstorm**: Generates homelab/automation ideas, run through a strict 5-point critique.
5. **Creation**: Writes scripts or runbooks in isolated workspaces.
6. **Digest/Summarization**: Summarizes new material based on file hashes.

---

# 6. Quality Control & Deduplication

Every artifact gets a second-pass **Evaluation Task** (`usefulness`, `accuracy`, `novelty`, `actionability`, `relevance`). Threshold: Overall >= 3.5.

**Deduplication**: 
* Hash payloads to avoid exact duplicates.
* Do not re-research equivalent topics within `14` days.

**Feedback Loop**:
Track user feedback (`cooker rate <id> good`). If a generator (e.g., "Brainstorm") consistently scores poorly, the scheduler naturally stops pulling tasks from it.

---

# 7. Output Layout

Keep logic separate from outputs.
`~/dev/hobby/cooker/outputs/YYYY-MM-DD/` contains `research/`, `project-review/`, and `digest/`.

---

# 8. Daily Digest

Generate `outputs/YYYY-MM-DD/digest/digest.md`.
The only UI you actually have to read. If nothing scored > 3.5, the digest says so.

---

# 9. CLI

Command: `cooker`

```bash
cooker status            # View queue and current backoff state
cooker add "topic"       # Inject manual research
cooker pause / resume    # Manually halt the background worker
cooker list / show <id>  # Queue inspection
cooker rate <id> good    # RLHF loop
cooker digest            # Force generate today's digest
cooker doctor            # Validate SQLite, omlx, launchd
```

---

# 10. Safety & Observability

## Safety Rules
1. **Indirect Prompt Injection**: Strict system prompts when summarizing web content. NEVER execute bash scripts generated from web-tainted context.
2. **Read-Only**: `git diff`, `git status` only. Never `git push` or modify user branches.
3. **Secrets**: Regex-based secret scanning in Python *before* attaching files to a prompt.

## Metrics
Log TTFT, generation latency, preemptions, and CPU load. Optimize for **Useful Artifacts per Day**, not tokens per second.

---

# 11. Scheduling & Daemonization

Use **launchd**. 
* `RunAtLoad: true`
* `KeepAlive: true`
* Graceful shutdown on SIGTERM (ensures HTTP connections are dropped cleanly and SQLite WAL is synced).

---

# 12. Required Demonstration

### Test 1 — Self-refilling queue
Start with an empty queue. Show generators creating tasks, DAG dependencies resolving, and Cooker consuming them.

### Test 2 — Polite Sidecar (Preemption)
1. Let Cooker start a heavy background task.
2. Open a terminal and run `curl` to the omlx port, or trigger OpenCode.
3. **Demonstrate:** Cooker detects the non-Cooker connection (or system activity) → Cooker immediately drops its HTTP request to omlx → OpenCode generation proceeds instantly → Cooker waits 60s before resuming.

### Test 3 — Digest
Generate the first digest containing real, evaluated outputs.

---

# 13. Definition of Done

* [ ] Discovery & omlx prefill behavior mapped
* [ ] Passive detection (ports/idle time) implemented
* [ ] SQLite DAG queue works
* [ ] Self-cancellation/Backoff proven working
* [ ] Generators & evaluators work
* [ ] CLI & RLHF works
* [ ] launchd service installed
* [ ] Safety boundaries tested
* [ ] `IMPLEMENTATION.md` completed