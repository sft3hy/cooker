# HANDOVER — Cooker, for the next agent

Read this before touching anything. It exists because the expensive part of this
project is not the code, it is the measurements and the five or six mistakes that
produced them, and those are not visible in the source.

Written 2026-10-07 on `main` at `b8f9523`. Everything below was verified against
disk and `git log` as of that commit. Where something is *not* true, it says so.

---

## 1. What Cooker is

An always-on background intelligence sidecar on Sam's Mac Studio. It uses **idle
omlx capacity** to research, review and create markdown artifacts for his projects,
fronted by a **fun 8-bit pixel-kitchen web UI** at `https://cooker.home.arpa`.

Two constraints outrank every feature:

1. **Interactive always wins.** If a human or another agent is using the GPU, Cooker
   does not start, and if it is mid-stage it drops the HTTP connection. It is a
   guest in someone else's house.
2. **Verification must be honest.** Never report a PASS, a test count, a file, or a
   commit that was not actually produced. This has gone wrong twice (M5, and once
   mid-M4). `PLAN.md` and `DISCOVERY.md` are the record; if they disagree with disk,
   disk wins and the doc gets corrected.

**`OVERVIEW.md` is the spec of record. `PLAN.md` is the milestone plan and status.
`DISCOVERY.md` is the measurement log — read §12–§16 first, they are the live-box
facts.** Do not re-derive what is already measured; see §4 below.

---

## 2. Direct answers

**Do we have a frontend?** **No.** Zero. There is no `web/`, no `server.py`, no
`package.json` anywhere in the repo, and `node`/`npm` are **not on this shell's
PATH**. `cooker run` computes the bind address (`127.0.0.1:8256`) and `doctor` checks
the port is free, but nothing listens and nothing is served. M7 is entirely unwritten.
Its absence is the single largest visible gap, and it is blocked on installing node.

**What's left?** M7 (frontend, unwritten + needs node), M8 (launchd/Traefik/Pi-hole
registration ceremony; the *detector* half landed today), M9 (six of eight generators
are stubs), M10 (`IMPLEMENTATION.md`). Plus one live proof and one threshold decision.
Detail and ordering in §5.

---

## 3. State of the build

| milestone | state | evidence / what it means |
|---|---|---|
| M1 storage, queue, doctor | done | `cooker db selftest`; SQLite WAL, atomic DAG claim |
| M2 detection by bytes | done | `cooker watch`; 0 B/s idle vs 5,376 B/s busy separation |
| M3 cancellable inference, safety gate | done | `bench8_cancellation_live.py` — 1,711 B/s busy → inflight 0 → baseline |
| M4 research DAG | done | `bench9_research_chain_live.py` Test 1 PASS, 7/7 stages |
| M5 gate, dedup, feedback, digest | done | `bench10_digest_live.py` Test 3 PASS — evaluated live, digest rendered from the rows' own numbers |
| M6 CLI surface | done | `status add pause resume list show rate digest doctor`, 19 tests in `tests/test_cli.py` |
| **M7 kitchen UI** | **not started** | no node/npm; nothing to build |
| M8 daemon + registration | **half** | detector re-gated today (§4/§5); **launchd, Traefik, Pi-hole and the `HOMELAB-SERVICE-MAP.md` ceremony are undone** |
| M9 remaining generators | not started | `chains.advance` emits `chain.stub` for `deep-dive project-review homelab-audit brainstorm creation`; an orphan `brainstorm/generate` stage sits QUEUED as proof |
| M10 IMPLEMENTATION.md | not started | definition of done, written at the end |

~7,800 lines in `src/cooker/`, **218 tests passing**, ruff clean over
`src tests bench`, `cooker doctor` 0 fail / 2 warn (launchd absent — honest).

**Nothing is unstaged.** `main` is pushed through `7d1c23c`; `1b94afa` and `b8f9523`
were committed after the last push — **push before you start.**

---

## 4. Measured facts — do not re-derive these

These cost real GPU time and real dead-ends to obtain. Trust them until you have a
measurement that contradicts one, and then write the new one into `DISCOVERY.md`
rather than quietly editing the old.

| fact | value | consequence |
|---|---|---|
| decode throughput, ordinary stage | **~200 tok/s** (880 tok in 4.0s) | **an earlier note said 8 tok/s and it was wrong ~5×** — it was reasoning-mode on a large prompt, and it had silently become the planning number for stage length. A 4s gap hosts a `plan` stage. |
| quiet gaps during a live agent session | **19 gaps in 179s, all 2–5s, median 2.2s, max 5.2s** | 60s of continuous silence does not exist while he works. `idle_confirm: 60` refused 100% of that trace while 33% of it was genuinely idle. |
| collision cost: interactive ttft behind a Cooker stage | **+95ms median, +200ms into live decode, +350ms prefill race** (baseline 100ms) | the unbounded 60s gate was guarding a fifth of a second. Prefill ceiling 1000 tokens ≈ 60–320ms monopoly. |
| wire floor `infer_min_bps` | **256** — lowest busy 470, highest quiet 99 | still correct, but margin is ~2.6× not ~26×. A dribbly generation can read quiet. Unverified against slow-decode workloads. |
| prefill | **atomic and exclusive** | prevention beats preemption: never submit over the ceiling; cancellation protects decode (1.00×) but cannot un-run a prefill. |
| omlx SSE | sends **keepalive frames before the first token** | naive "time to first byte" lies (0.004s vs real 0.6s). Stamp TTFT only on the first delta with non-empty `content`. |
| usage | only with `stream_options.include_usage` | server numbers win; every number labelled `server` / `server:prompt-only` / `estimated`. |
| sockets | **not** an inference signal | OrbStack and Traefik hold :8000 open for hours at 0 B/s. Bytes are the trigger. Also means "no clients connected" is never true — do not build a tier on it. |
| nettop streaming | `nettop -L 0` block-buffers, first line at 8.22s | use short `nettop -L 2 -s 1` polls (~1.25s). A resident probe is an 8s blind spot on the only question that matters. |
| lsof cost | ~190ms CPU/call, 23% of a core at 1 Hz | one combined LISTEN+ESTABLISHED query per 2s; everything else rides slower cadences. |

**Two live results that are still missing, and are not failures of the design:**
- Nothing has cleared the 3.5 publish threshold in production (verdicts so far
  2.40/2.80/3.00/3.00/3.40, novelty 1–2). "Worth reading" has unit coverage only.
  **The threshold stands** — do not lower it to get a green demo.
- `bench12_collision_live.py` PASSED (+95ms), and the re-gated detector has **not
  yet been demonstrated claiming work during a busy session** (see §5.1).

---

## 5. What is left, in the order that unblocks the most

### 5.1 First: prove the gate, or correct it (1–2 hours, no new deps)
`b8f9523` split `ready` (4s quiet → one bounded stage) from `ramped` (60s → the
ladder), moved `interactive_cooldown` to the ramp gate, and added `quiet_streaks` /
`typical_gap_s`. **It has not been seen to work.** A 120s live run right after the
change claimed nothing, because the session driving it generated continuously and its
gaps were ~2s.

Do this:
```
.venv/bin/cooker run --live -s 300        # then stop generating text for ~8s at a time
```
Watch for `start:` / `plate:` lines. If it never claims across several genuine
pauses, the gate is wrong, not the workload — in that order, prove the gate wrong
before blaming the box. `tail -f var/` and `cooker status` are the instruments.

### 5.2 M7 — the frontend (the big visible thing; needs node)
Blocked on **installing node** (`brew install node`, or `mise`/`fnm`). Confirm with
`node --version` before promising anything.

- `src/cooker/server.py`: FastAPI on **`127.0.0.1:8256`** (loopback only; Traefik
  reaches it via `host.docker.internal`). `GET /api/events` is an SSE tail of the
  `events` table, 500ms poll, replayable, no broker. `collect_status()` +
  `render_status()` in `cli.py` already produce the payload shape — `cooker status
  --json` is that interface and it is tested, so build against it rather than
  inventing a second one.
- `web/`: **Svelte + Vite**, compiled to static assets served by the daemon.
- Design invariants (from `OVERVIEW.md` §9): logical canvas **480×270**, integer
  scale factor, `image-rendering: pixelated` so sprites never smear — 1×→2× on an
  iPhone, 4×+ on a 32" monitor. One pot per generator. Vendored OFL pixel font
  (`Press Start 2P`) for display, system mono for body text. SFX default **off**.
- Tailnet-trusted: **no auth**. Do not add a login.

### 5.3 M8 — registration ceremony (do not skip; it is mandatory)
1. Install launchd `com.homelab.cooker` (KeepAlive) — `doctor` currently warns it is
   absent, and that warn must become a pass.
2. Add `cooker.home.arpa` to `HOSTS` in `~/homelab/edge/scripts/02-pihole-wildcard.sh`,
   re-run (idempotent).
3. Add router + `service: cooker → host.docker.internal:8256` to
   `~/homelab/edge/traefik/dynamic/services.yml`. **Keep the flush-left comment
   invariant intact — there was a TLS incident on 2026-10-04 from breaking it.**
4. Append to `~/HOMELAB-SERVICE-MAP.md`: the `:8256` port row, a Service Entry,
   dependency edges (`cooker → omlx, searxng, autoresearch`), and one dated Changelog
   line. **No secret values in that file.**

### 5.4 M9 — the stub generators
`brainstorm` is cheapest to make real first (it will prove the queue drains a generator
that is not `research`). `deep-dive` delegates to autoresearch over HTTP — note
`POST /jobs/{id}/cancel` is **cooperative**, checked between phases, so it is fine for
deep-dives and wrong for latency-critical small steps. Register artifacts only for
`chains.DRAFT_KINDS`; publish *promotes* the candidate row, never inserts a second.

### 5.5 Loose ends
- **`bench6_abort_prefill.py` does not exist.** `PLAN.md:207` and `DISCOVERY.md:330`
  both cite it as "written and ready". It was written into the ephemeral temp overlay
  (§6) and vanished. **You must write it, not look for it.** The prefill ceiling
  (1000) goes up only on a clean win from it, in a genuinely idle window.
- Re-check the floor (256 B/s) against a deliberately slow-decoding workload; the
  margin measured 2.6×, not the ~26× the original calibration implied.

---

## 6. Landmines — each of these has already cost time

1. **`/var/folders/6k/.../T/opencode` is a virtual overlay for the write tool and
   does not persist between calls.** Files written there vanish. Write project files
   into the repo; use shell heredocs for scratch. This is how `bench6` disappeared.
2. **No `timeout` command on this macOS zsh.** Use the shell tool's own timeout.
3. **`uuid7` ids**: the leading 8 hex chars are a *millisecond timestamp* and collide
   for ~18 hours. The display handle is the **random tail** — `eval.short_id()`, last
   8. `resolve_artifact`/`resolve_task` do exact → task_id → unique tail → unique
   prefix, and **refuse ambiguity**. `list` prints tails, so `rate` must accept tails.
4. **`db.emit(conn, type_, *, message=…, data=…)` is keyword-only.**
   `Config.with_overrides(**dotted)` refuses unknown keys; test `cfg()` helpers raise
   `KeyError` on unregistered knobs. **Register every new knob in `config.yaml` *and*
   the helper dicts**, or every run dies on the first read.
5. **Async tests must not call `asyncio.run()` while a fixture server lives on
   pytest's loop** — it hangs until timeout and masquerades as a dead upstream.
6. **CLI commands open `cfg.db_path` themselves.** `cmd_list`/`cmd_show`/`cmd_digest`
   take no connection: seeding an in-memory database and calling them tests a database
   they never read. Use the `conn_at(cfg)` helper in `tests/test_cli.py`.
7. **`Task` carries no token/error columns** — `prompt_tokens`, `error`, `result_path`,
   `not_before` are on the *row*, not the dataclass. Read the row you selected.
8. **`dry-run` must never write to `tasks` and never seed** — it logs
   `dry.would_seed`. A dry run that writes rows is a real run with a misleading name.
9. **Absent config is not a park order.** Parked generators are enforced at
   `claim_next(generators=eval.allowed_generators(...))`; `None` means unfiltered.
   An empty list compiles to `IN ()` and freezes the queue.
10. **Unparsable judgement is a FAILED stage, not a REJECT.** "The judge couldn't
    parse" ≠ "not good enough". Publish gates live at the gate, not only in the DAG.
11. **Fence draft content in the evaluator prompt.** Quoted web text otherwise reaches
    the evaluator as live instructions, and `fit()` can only trim inside a fence.
12. **Compute excerpt budgets, never hand-write them.** `runner._excerpt_budget` derives
    from the ceiling; a guessed constant stalled a live chain at ~1,001 tokens.
13. **`cfg.outputs_dir` resolves relative against `data_dir`.** It used to resolve
    against the repo root while `chains.day_dir` built its own path, so the disk quota
    watched an empty folder while 212 KB landed elsewhere and `max_disk_gb` could never
    fire. One definition; `day_dir` is built on the property.
14. **`read_topics` skips prose.** If the file has markers, bare lines above the first
    one are documentation. Help text in `topics.md` was being queued as research
    subjects. A readable file is a parsed file, and a parsed file gets fed to a GPU.
15. **Preemption bookkeeping is done synchronously by the supervisor**, never inside
    the cancelled coroutine.
16. **Sockets are not inference. `IDLE` reason `keepalive:` means connected-and-silent;
    no wire data means `ACTIVE_USER` reason `blind:`.** Blind ≠ quiet, always.

---

## 7. How to verify (the only commands that count)

```
.venv/bin/ruff check src tests bench
.venv/bin/python -m pytest -q                 # 218 today
.venv/bin/cooker doctor                        # expect 0 fail; launchd warn until M8
.venv/bin/cooker status                        # queue + backoff + gate
.venv/bin/cooker run --dry-run -s 30         # narrates, never writes
.venv/bin/cooker run --live -s 300           # real claims; costs GPU
```
Live benches (`bench/bench{8,9,10,11,12}_*.py`) hit the real box. Run them **one at
a time**: they each submit or measure traffic, and running two means one measures the
other. `bench11` is read-only; `bench12` submits real inference and asserts its own
decision threshold (+750ms) *before* measuring, so a FAIL there is a correct outcome
that should stop you, not inconvenience you.

---

## 8. Live access

- Model `Qwen3.8-Flash-Next-oQ4e-mtp`; endpoint preference
  `https://omlx.home.arpa/v1` → raw LAN IP → **loopback last** (a stray
  `http.server` shadows loopback and is not ours to kill).
- CA `~/homelab/edge/step/certs/root_ca.crt`; API key `AR_LLM_API_KEY` in
  `~/dev/autoresearch-service/.env` (**never commit the value**).
- SearXNG `https://searxng.home.arpa/search`; autoresearch `/health`, async jobs.
- Re-resolve omlx's pid with `pgrep -f omlx-server` — it changes on restart, and
  `lsof` reports the executable name (`Python`) not `comm` (`omlx-server`).
- Real origins rate-limit constantly (403/429/503 → per-host cooloff). Tests bind
  loopback and set `allow_private_networks: true`; the shipped default is **false**.

---

## 9. If you only do three things

1. `git push` — `1b94afa` and `b8f9523` may be local-only when you arrive.
2. Prove or correct the 4-second claim gate (§5.1). It is measured, reasoned,
   unit-tested and **undemonstrated**. Do not let that become a legend.
3. Install node and start M7. The project has a working brain and no face, and the
   pixel kitchen is the part Sam asked for twice.
