# DISCOVERY — what this box actually does

Every number that shapes Cooker's config comes from here, not from intuition.
Benchmarks are in `bench/`, re-runnable. Machine-readable results live in the
session temp dir; the tables below are the distilled conclusions.

**Measured 2026-10-06 on the Mac Studio (arm64, macOS 27.0.1, 96 GB) running
omlx with `Qwen3.8-Flash-Next-oQ4e-mtp`.**

---

## 1. Prefill is atomic and exclusive — this is the whole design constraint

`chunked_prefill: false` on omlx. A background request's prefill cannot be
interrupted, and it holds the accelerator exclusively while it runs, so your
interactive time-to-first-token goes up by however many tokens we asked it to
swallow.

Interactive baseline TTFT when the box is genuinely idle: **0.139 s**.

| background prefill | prefill cost | prefill tok/s | your TTFT | penalty |
|---:|---:|---:|---:|---:|
| 1 000 tok | 0.37 s | 2 725 | 0.257 s | **1.8×** |
| 2 000 tok | 0.54 s | 3 716 | 0.421 s | **3.0×** |
| 4 000 tok | 1.02 s | 3 936 | 0.980 s | **7.0×** |
| 8 000 tok | 1.99 s | 4 020 | 1.967 s | **14.1×** |
| 16 000 tok | 3.80 s | 4 213 | 3.893 s | **28.0×** |

Penalty is linear in prefill size, exactly as you would expect from an atomic,
exclusive stage. Nothing saturates until ~4 000 tokens, where prefill throughput
plateaus around 4 000 tok/s.

**Decision:** `max_prefill_tokens_per_request: 1000`. It is the largest step on
the ladder that keeps you under 2× idle latency, and 3× was the agreed ceiling.
This is not a rate limit to be tuned up later; it is the boundary of "you do not
notice us".

## 2. Cancelling mid-prefill is vacuous — prevention is the mechanism

Same ladder, but aborting the big request as hard as possible (`SO_LINGER` with
linger=0, forcing an RST) the instant we detect a collision:

| probe after abort | TTFT | penalty |
|---|---:|---:|
| first probe | 2.417 s | **17.4×** |
| second probe | 0.137 s | 1.0× |
| third probe | 0.134 s | 1.0× |

The abort is only observed at the next byte, and no byte is ever sent until
prefill finishes. The socket stayed alive 3.78 s after we "cancelled" it. The
first probe still paid 17.4×; the box only recovered once the prefill completed
on its own.

**Decision:** cancellation protects **decode**, not prefill. Keep prefill small
so collisions are cheap, and stop trying to win the unwinnable race. The stage is
therefore the smallest unit of work Cooker recognises — it is already the
smallest unit omlx will interrupt.

## 3. Dropping the socket during decode really does free the box

Independent run (`bench/bench_disconnect.py`), aborting a long generation at 4 s:

| condition | mean TTFT |
|---|---:|
| idle baseline | 0.630 s |
| after aborted decode | 0.627 s → **1.00×** |
| alongside an uninterrupted big request | 2.223 s (worst probe) |

Post-abort latency is indistinguishable from idle. So `httpx` close-on-cancel is
a real preemption mechanism during decode, and we do not need an omlx-side API
for it.

## 4. This model streams `reasoning_content`, not `content`

`bench/bench4_thinking.py`, five ways to ask the same question:

| knob | TTFT | total | reasoning chars | answer chars | works? |
|---|---:|---:|---:|---:|---|
| baseline (thinking on) | 0.186 s | 0.825 s | 174 | 121 | yes, verbose |
| `chat_template_kwargs.enable_thinking=false` | 0.147 s | 0.396 s | **0** | 161 | **yes** |
| top-level `enable_thinking=false` | 0.100 s | 0.384 s | **0** | 161 | **yes** |
| `thinking:{type:"disabled"}` | 0.109 s | 0.534 s | 175 | 85 | **no — ignored** |
| `reasoning_effort:"low"` | 0.183 s | 1.072 s | 200 | 295 | no — barely throttles |

Corollary measured separately: at `max_tokens: 16` the model spends the entire
budget on reasoning and returns `finish_reason:"length"` with an **empty answer**.

**Decision:** (a) parse `reasoning_content` as a first-class field, (b) floor
`max_tokens` at 512 so reasoning cannot starve the answer, (c) send
`chat_template_kwargs: {enable_thinking: false}` for mechanical stages
(`extract`, `scan`, `collect`, `consolidate`, `publish`), which is measurably
~2× faster and produces no reasoning noise.

## 5. Keepalives contaminate every timing measurement

omlx is configured `sse_keepalive_mode: "chunk"` and emits
`data: {"model":"keepalive",...}` roughly every 0.01 s. Naïve instrumentation
stamps TTFT on the first chunk and reports sub-10 ms latencies that are fiction.

**Decision:** stamp TTFT only on the first delta that carries real
`content`/`reasoning_content`. `bench/bench3_sse.py` has the filter.

## 6. A port is not a service

`lsof` on this host, live:

```
omlx-server  pid 15222  binds *:8000
Python       pid 74595  binds 127.0.0.1:8000   <- stray http.server
                                                          from ~/dev/mm-visit-itinerary
```

The specific bind beats the wildcard bind, so `http://127.0.0.1:8000/v1/models`
answers **404** from the stray while omlx is healthy directly behind it. Two more
traps in the same area:

- lsof's command column truncates both processes to `Python`; process identity
  requires `ps -p <pid> -o args=`.
- `mini.home.arpa` does not resolve **on the host itself** (the Mac uses the
  router, not Pi-hole, for DNS), and `Samuels-Mac-Studio.local` answers **mDNS →
  127.0.0.1**, i.e. straight back into the shadow.

Working endpoints, verified from the host:

| URL | result |
|---|---|
| `https://omlx.home.arpa/v1/models` | **200, 34 ms, 8 models** (Traefik → Tailscale IP) |
| `http://192.168.1.20:8000/v1/models` | 200, 8 models |
| `http://127.0.0.1:8000/v1/models` | **404 — the shadow** |

**Decision:** prefer `https://omlx.home.arpa/v1` (registered, stable, survives
DHCP), then raw LAN IP, then loopback last. Cooker names the process by argv and
probes URLs before concluding anything about health. See `src/cooker/net.py`.

## 7. omlx has no queue or metrics API

`/metrics`, `/health`, `/admin/api/{queue,status,requests,jobs,inference,sessions}`
all **404**. What works: `/admin/api/login` (POST `{"api_key": …}` →
`omlx_admin_session` cookie) and `/admin/api/stats?scope=alltime|today`.

So there is no way to ask omlx "are you busy?" — which is why the detector is
built on `lsof` ESTABLISHED sockets plus HID idle plus WAL mtimes rather than on
a queue depth we cannot read.

`~/.omlx/usage.sqlite3` → `model_usage_hourly` is **hourly**, so cross-checking
our own accounting against the server's is a daily-granularity exercise only.

## 8. Detection signals that are free

- **OpenCode is detectable for nothing**: `lsof` shows an `opencode` PID with an
  ESTABLISHED connection to `:8000` while it generates, and
  `~/.local/share/opencode/opencode.db-wal` mtime moves while it writes.
- `ioreg -arc IOHIDSystem -k HIDIdleTime` gives real input idleness. Parse the
  **text** form: piping `ioreg -a` (plist) into `plistlib.load` fails because the
  stream is not seekable.
- omlx scheduler facts: `max_concurrent_requests: 8`, `decode_fairness: true`,
  `chunked_prefill: false`.

## 9. Search and delegation from the host

SearXNG is **container-only** — host `:8080` is Pi-hole, so a naive
`localhost:8080` search hits the admin UI, not the search engine. From the host:

```
https://searxng.home.arpa/search?format=json&q=…   # + SSL_CERT_FILE=~/homelab/edge/step/certs/root_ca.crt
```
Verified 200, 20–25 results, ~1.0–1.4 s.

`autoresearch.home.arpa` `/health` → 200. Deep dives delegate there; Cooker owns
the everyday cancellable pipeline.

## 10. Platform facts that changed code

- Python 3.14.7, SQLite **3.53.4**, `uuid.uuid7()` exists natively.
- `uv` at `/opt/homebrew/bin/uv`; no `pipx`; system Python has no `httpx` —
  a venv is mandatory, not stylistic.
- **No `timeout` command** on macOS. Timeouts must be in-process.
- `~/.omlx/settings.json → auth.api_key` is `null`; the working key is
  `AR_LLM_API_KEY` in `~/dev/autoresearch-service/.env` (10 chars, verified).

---

## 11. A socket is not a generation — bytes are (bench #7)

This is the finding that rewrote the detector, and it was expensive to find because
the wrong signal fails silently: `lsof` answers confidently, and the answer is wrong.

Measured 2026-10-07, in windows where a tool was executing and no tokens were being
produced:

| candidate signal | measured behaviour | verdict |
| --- | --- | --- |
| `lsof -iTCP:8000 -sTCP:ESTABLISHED` | 2-6 connections, permanently. Traefik keeps an upstream alive, OpenCode keeps a pool | **useless as a trigger.** A socket is a relationship, not an event. As the ACTIVE_INFER trigger it latches forever and the kitchen never lights |
| omlx cumulative `cputime` | 3.90 CPU-seconds inside a 4.05 second window serving nothing: the server busy-spins | **useless.** Busy does not imply busy *with inference* |
| `ps -o %cpu` | a decayed average: 0.7% and 110% from the same spin, tens of seconds apart | **useless live.** Answers a question about ten seconds ago |
| `~/.omlx/stats.json` | three successful HTTP 200s moved neither the counters nor the mtime | dead as a live signal; fine for nightly accounting |
| `nettop` bytes per pid | exactly `0` with the sockets wide open; `10,752` bytes in a 2s window on one real streamed reply | **the signal.** Two orders of magnitude of separation, and it goes back to zero |

So `ACTIVE_INFER := omlx itself moved bytes in the last window`. The trigger is the
*server's* throughput rather than a peer's, because on this box requests arrive
through Traefik in OrbStack: the process named on the socket is the proxy's, not the
caller's, so attributing a request to a command name would be a guess while bytes
leaving omlx is a fact. `cooker/wire.py` implements it and
`bench/bench7_generate_vs_keepalive.py` re-runs the measurement.

Three sub-findings, each of which produced a plausible wrong number rather than a
crash:

**nettop buffers a piped stdout.** A resident `nettop -L 0` delivered its first line
**8.22s** after start and the next batch **8s later**: libc block-buffers stdout when
it is not a terminal. A resident probe therefore has an eight second blind spot on the
one question the daemon exists to answer fast. Short `nettop -L 2 -s 1` runs return in
**1.25s**, consistently, and print two cumulative samples so the diff is still honest.
The probe polls rather than streams.

**`os.times()` indices.** Indices 0 and 1 are this process's own user/sys time;
subprocesses are 2 and 3. Reading 0 and 1 reported `0.04 cores` for a `cooker watch`
run that actually cost `1.01`, because the daemon is nearly idle and its children are
not. `os.wait4()` gives a specific child's rusage and is what the probe uses.

**Probe cost is not additive.** nettop run alone in a shell costs ~60ms CPU. Inside
the detector, with lsof, ioreg and `ps -Axo` walking the same kernel tables
concurrently, the same run measured ~1,590ms. Per-probe figures in the cost table are
therefore indicators; the whole-process line is the number to trust. `cooker watch`
prints both, and says so when they disagree by enough to matter.

Whole `cooker watch`: **16.3s CPU over 16.2s wall = 1.01 cores = 3.4% of 30 cores**,
of which 15.7s is subprocesses. `lsof` remains the expensive one, so the socket probe
dropped from 1 Hz to 0.5 Hz (23% -> 11.5% of a core) now that it only names peers
rather than deciding state.

**Consequence for the state machine.** Connected-and-silent is now `IDLE` with the
reason `keepalive: OrbStack Helper`, not `ACTIVE_INFER`. That single change is the
difference between a sidecar that cooks and a daemon that only ever says no. And blind
is its own answer: if nettop is not running, the reason is `blind: no throughput
data, cannot tell 0 from unknown` and the state is `ACTIVE_USER` — finish what is in
flight, start nothing — rather than either guess.

## 12. omlx will tell you the truth if you ask it (bench #9)

M3's first live run filed a stage as `prompt_tokens: 0, completion_tokens: 1` for
a 1,016-character answer, with `ttft_ms: 5`. Every one of those numbers was a
lie, and none of them were crashes — which is what makes them dangerous in a
budget column.

Three separate causes, three fixes:

| Symptom | Cause | Fix |
|---|---|---|
| `ttft_ms: 5` | stamped at the first **byte**; omlx flushes headers instantly | stamp at the first **token**, then prefer the server's own figure |
| `completion_tokens: 1` | fallback estimate ran once, on chunk one, then `if not tokens` was false forever | recompute from accumulated text every chunk until the server reports |
| `prompt_tokens: 0` | **the server was never asked** | send `stream_options: {"include_usage": true}` |

That third one is the discovery. Asked, omlx returns more than the OpenAI schema:

```
prompt_tokens: 179, completion_tokens: 202, time_to_first_token: 0.32,
prompt_tokens_per_second: 550.9, generation_tokens_per_second: 133.1
```

The decode rate corroborates §B (19–30 tok/s): those earlier runs were
`enable_thinking: true`, so their 604-character *reasoning* blocks were the
output — at 133 tok/s undiscriminating, which is the number that makes thinking
off for mechanical stages an obvious win rather than a hunch.

Prefill at ~551 tok/s puts the 1,000-token ceiling at ~1.8 s, matching §E's 1.8×
idle TTFT from a different direction.

**Consequence for the code.** `llm.py` now sends `include_usage` on every request
and labels every number it records: `usage: server` when omlx reported both halves,
`server:prompt-only` / `server:completion-only` when it reported one, `estimated`
when it reported neither. A zero is treated as "not reported" and never stored as
cost, because a recorded zero reads as a fact and is indistinguishable from never
having measured.

## Open question, deliberately deferred

**Does a hard abort mid-prefill eventually free the accelerator sooner than the
prefill completing on its own?** Bench #6
(`/private/var/.../bench6_abort_prefill.py`) is written and ready but has not
been run, because running it now would freeze this interactive session for ~4 s —
the exact harm Cooker exists to avoid. It will run in a genuinely idle window,
and until then the conservative 1 000-token ceiling stands. If it ever shows a
clean win, raising the ceiling is a one-line config change.

## Numbers → config

| finding | config key | value |
|---|---|---|
| prefill ladder (§1) | `inference.max_prefill_tokens_per_request` | `1000` |
| empty-answer floor (§4) | `inference.max_tokens_floor` | `512` |
| thinking off for mechanical stages (§4) | `inference.thinking_off_stages` | extract, scan, collect, consolidate, publish |
| stable endpoint (§6) | `inference.base_url` | `https://omlx.home.arpa/v1` |
| throttled rollout | `scheduler.max_concurrency` | `1` |
| no queue API (§7) | `detect.*` | socket + HID + WAL polling |
| bytes not sockets (§11) | `detect.wire_interval_seconds`, `wire_window_seconds` | `2.0`, `3.0` |
| quiet/busy separation (§11) | `detect.infer_min_bps` | `256` (measured 0 vs 5,376 B/s) |
| sockets only name peers (§11) | `detect.cadence_seconds.sockets` | `2.0` (was 1.0, 23% -> 11.5% of a core) |
| hysteresis everywhere | `detect.interactive_cooldown_seconds`, `idle_confirm_seconds` | `60`, `60` |
