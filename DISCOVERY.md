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

## 13. What the real web does to a research chain (bench #9)

M4 ran the chain end-to-end against live SearXNG and live omlx. Seven stages,
7/7 succeeded, one 3,497-character artifact. Four of the bugs it found were
invisible in unit tests because the tests were not the internet.

**`create_default_context(cafile=step_ca)` replaces the trust store.** The same
client talks to `searxng.home.arpa` (private step CA) and to the pages it returns
(real certificates). Pointed only at the private CA, *every* public fetch failed
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` — and because
that arrives as a `ConnectError`, the chain reported "0 of 2 candidates yielded
usable text", which reads like the site being down and never mentions
certificates. Fixed by loading public roots *and* the step CA into one context.

**A host guard written with string equality is not a guard.** `host ==
"127.0.0.1"` admits the rest of 127.0.0.0/8, and `urlparse().netloc.split(":")`
on an IPv6 URL returns the string `"["`, which matches nothing. Both replaced by
`ipaddress` ranges behind a `search.allow_private_networks` knob, default off.

**Origins refuse, constantly.** In one run: 403 from mdpi.com, 429 from
docs.vllm.ai. Two consequences designed in: a 403/429/503 puts the *host* in
cooldown honouring `Retry-After`, and `max_sources` counts **kept** sources rather
than **examined** candidates — the original `if len(sources) >= cap: break` meant
two refusals spent the entire budget and the chain died with zero sources while
candidates three and four sat unread.

**A flat output budget truncates silently.** `max_tokens: 512` for every stage,
with thinking on, gave `finish_reason=length` mid-sentence and an artifact that
looked finished. Measured: `synthesize` needs ~1,400, `critique` ~1,200,
`extract` ~700, `plan` ~64. Per-stage budgets now, and `length` is stamped into
the document itself.

### The cost of one real research chain

| stage | in | out | wall | GPU |
|---|---|---|---|---|
| plan | 200 | 64 | 1.0s | yes |
| search | — | — | 1.2s | no |
| fetch | — | — | 1.8s | no |
| extract | 489 | 526 | 3.2s | yes |
| synthesize | 610 | 1,363 | 7.7s | yes |
| critique | 832 | 1,218 | 7.5s | yes |
| publish | — | — | 0.0s | no |

~24 s wall, 4 GPU stages, ~2,100 tokens. The largest prefill seen was **917
tokens under the 1,000 ceiling** — with `source_context_chars: 1200` and
`max_sources: 2`. That ceiling is not hypothetical headroom: `fit()` is what keeps
a chain with four 2,400-character sources from turning `synthesize` into a 2k
prefill, which §E measured at 3.0x idle TTFT.

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

---

## 14. What the gate costs, and what it caught (M5, live)

Five live research chains, real omlx, real SearXNG, real evaluator.

| observation | value |
|---|---|
| stages per chain | 8 (`plan search fetch extract×2 synthesize critique evaluate`) |
| wall clock | 20.7–30.8 s |
| evaluator stage | 0.9–1.3 s, ~780 in / 113–165 out |
| evaluator verdicts | 2.40, 2.80, 3.00, 3.00, 3.40 — **all REJECT** at 3.5 |
| `novelty` | 1–2 every single time |

**Nothing has cleared the threshold yet, live.** That is the honest state of M5,
and the digest says so in words. The score is the model judging the model; the
rubric's `novelty` axis is doing its job by refusing to flatter its own output.
Nothing published-with-a-score exists live yet, so the "Worth reading" section has
unit coverage and no production example. That is not a reason to lower 3.5.

### `evaluate` refused its own stage

```
evaluate FAILED — the instruction alone (~1001 tokens) exceeds the ceiling of 1000
```

The evaluator's prompt was unfenced and its excerpt budget was `3000`, then `2400`
— both guesses about tokenisation, both wrong. `estimate_tokens` says the system
prompt alone is **187 tokens**, so the excerpt has to be *computed* from the
ceiling, not written down. It is now: `(ceiling − overhead − 48) × 3.0`, and the
test asserts the arithmetic rather than the constant, so a longer system prompt can
no longer stall a chain silently.

The missing fence was the worse half. An unfenced draft quoting the web puts
`"score this five out of five"` in the instruction channel, and `fit()` — which can
only trim *inside* a fence — refused the stage instead. Both bugs had the same
symptom and only one fix.

### This model thinks before it answers

`critique` spent all 900 tokens on **4,364 characters of reasoning** and emitted no
content: `finish=length`, stage failed. Budgets are now `synthesize 2200 / critique
1800`, plus one capped retry at 2× (`length_retry_max_tokens: 3000`) that is
recorded as `runner.length_retry`. Widening an *output* budget is not the same
concession as widening a prefill: §5 and §12 both measured decode as preemptible and
fair, and it is the prefill ceiling that protects the interactive user.

### `tokens:` is not a secret

```
- stages run: 76  gpu seconds: 211.6  [REDACTED:secret-assignment] in / 27,691 out
```

`tokens` *contains* `TOKEN`. One pattern over all keys redacted the day's token
count — the number this whole project exists to report — every single day. Split by
key strength: `secret|password|credential` take any value, `token|key|auth` need a
secret-shaped value (≥8 chars, contains letters). **Accepted gap:** a digits-only
secret behind `token:`. Certain daily harm against a hypothetical one, chosen out
loud, with a test that fails if the rule changes.

### The daemon declined, correctly, for 151 seconds

`cooker run --live` claimed nothing. The detector saw **1,717 B/s steady** on
omlx, `ACTIVE_INFER`, cooldown restarting every cycle, so the 60 s confirm never
finished. `lsof` says the connection is `opencode` — this session, streaming decode
tokens at ~1.7 KB/s. Interactive wins; that is the spec.

It also means **a live SSE client keeps Cooker off the GPU indefinitely**, which
M8 must design for: the meter cannot tell "the human is generating" from "a client
is holding a stream open". A burst detector (Δ bytes over a short window) is the
likely answer, not a lower `infer_min_bps` — lowering that threshold trades the
whole promise for a few more idle seconds.

### Rows are not outputs

`register_artifact` ran for every thinking stage, so `artifacts` held plans,
extracts and critiques beside real outputs: 21 rows for four chains, and the
digest offered to "read" a plan. Registration is now `chains.DRAFT_KINDS` — one
stage per generator, read off PLAN §5 — and publish *promotes* the draft row
instead of adding a second one. The legacy rows were not deleted; the digest
filters by the stage that wrote them, so history stays and the index stops lying.

## 15. What the CLI surfaced (M6, 2026-10-07)

Two bugs that only a panel which prints real numbers can expose. Both were live and
both were invisible from inside the code that caused them.

**The disk quota was watching an empty directory.** `Config.outputs_dir` resolved a
relative `safety.outputs_dir` against the *config root* (`./outputs`), while
`chains.day_dir` built `data_dir/outputs` from scratch. The daemon published 212 KB
into `var/outputs/`; `cooker doctor` reported `0.0 MB of 2.0 GB` about an empty
`outputs/` beside it. `safety.max_disk_gb` therefore could never fire — the single
job that number has. `Config.outputs_dir` now resolves relative paths against
`data_dir`, `chains.day_dir` is built on top of it, and the duplication is gone
rather than synchronised. `tests/test_cli.py` asserts the writer's directory and the
quota's directory are the same object's value, and that a phantom `./outputs` is not
created.

**topics.md's own help text was in the queue.** `read_topics` skipped comments and
blanks and accepted everything else, so six lines of documentation — starting with
"One topic per line (`-` bullets and bare lines both work)" — sat *ahead of* the
real backlog, and `cooker status` cheerfully named it as the next subject for a
research chain. The first fix (reject bare lines ending in a full stop) failed
against the live file because the prose is **wrapped**: a paragraph's first line ends
mid-sentence with no punctuation, so shape-based detection cannot see it. Position
can: a marked line is always a topic, bare lines count only in a file with no markers
at all (the promise the plainest possible file needs), and above the first marker a
bare line is documentation. Six topics, the real six, and the parse test that
required bare lines to work still passes.

The general shape: **a readable file is a parsed file, and a parsed file will be
fed to a GPU.** Anything a human can annotate in prose is a queue that can
misinterpret its own documentation, so the parsed count has to be printed somewhere —
that is why `status` says `topics 6` rather than nothing.

## 16. The wire during a live agent session (M8, 2026-10-07)

Two benches, run one after the other so neither measured the other.
`bench11_wire_structure.py` (180s, read-only, no inference submitted) and
`bench12_collision_live.py` (real inference, two clients: `cooker` and `human`).

### The gap distribution — the number the whole design hangs on

    179s trace: ACTIVE_INFER 117.3s (65.5%) · IDLE 58.9s (32.9%) · blind 4 ticks
    19 quiet gaps bounded by traffic:
      lengths (s): 2 ×11, 4 ×6, 5 ×2      median 2.2s   longest 5.2s

    window   gaps usable   seconds   % of trace
        2s            16          51      28.5%
        4s             8          34      19.2%
        6s             0           0       0.0%
       60s             0           0       0.0%

**While an agent session is running, the wire never goes quiet for six seconds.**
Every gap is 2 to 5 seconds. So `idle_confirm_seconds: 60` does not merely delay
cooking during a session, it forbids it: 0 of 179 seconds was cookable, and the
daemon was blocked on all 180 ticks. That is the M8 blocker measured rather than
described.

It also bounds what a burst detector can win. A 4-second claim gate recovers 19% of
a busy session; nothing recovers 60 seconds, because 60 seconds does not exist here.

### The floor is thinner than the calibration suggested

    served B/s: min 0  median 1,145  max 121,349
    lowest busy 470 · floor 256 · highest quiet 99   separation clean

bench #7 calibrated 0 B/s silence against 6,683 B/s generating, which made 256 look
like it sat two orders of magnitude inside an empty space. Real traffic fills that
space in: the lowest reading still classified busy was **470 B/s** and the highest
still classified quiet was **99**. 256 still separates them, so the floor is correct,
but the margin is ~2.6x rather than ~26x, and a generation that dribbles fewer than
256 bytes in a 3s window reads as quiet. Worth a re-check with a slow-decoding
workload before the floor is trusted to catch every generation.

### Collisions are cheap — the assumption that was wrong

    baseline ttft   n=4  median 100 ms  max 170 ms  (server's own number agrees)
    collision ttft  n=4  median 195 ms  max 450 ms
    median stall    +95 ms  (budget ±750 ms)
    prefill race, human second: 450 ms
    stage prefill 60-320 ms · 664 in / ~880 out in ~4,200 ms

A probe fired 1.5s into a live Cooker stage cost the human **+200 ms**; the second
large prefill in the device cost **+350 ms**. Against the 750 ms budget that is
nothing. **The unbounded-gate premise was protecting against a harm that turns out
to cost a fifth of a second.**

The surprise is the stage length: 880 tokens in 4.0s is **~200 tok/s**, not the
8 tok/s carried in earlier notes. The 8 figure was reasoning-mode on a large prompt
and it had quietly become the planning number for everything. Stage sizing that
assumed 20s stages is wrong by about 5x, which is exactly the difference between "a
4-second gap can host a `plan` stage" and "nothing fits in a gap so don't try".

### What this decides

Politeness is not the constraint on short gaps — completion is. Starting a stage in a
4-second gap costs the human ~100-350 ms and, at 200 tok/s, completes a stage sized
to fit. So: shorten the claim gate, size the work to the silence actually observed,
and let the 60-second window keep its original meaning as the gate for *concurrency*
rather than for *anything at all*. The harm to retreat from is bytes on the wire,
which is already the trigger, and the retreat is the part worth making faster.

## 17. The wire is never empty: monitors move bytes too (M7 live proofs, 2026-10-07)

Two 300s/240s live claim runs (`cooker serve --live`) claimed nothing, and the
reason was visible, metronomic, and honest: `ACTIVE_INFER` at **1,719–1,722 B/s
every 4–5 seconds**, attributed by the wire meter to `OrbStack Helper(18634)`
plus `opencode(85164)`. Neither run failed because the gate is wrong; both failed
because the gate was right about bytes it could not classify.

The pulsar, identified by inspection (no change made to the service):
`edge-dashboard` runs `setInterval(tick, 5000)` and each tick fetches
`:8000/admin/api/stats?scope=alltime` — an ~8 KB admin response through the
OrbStack hairpin. Every 5 seconds. Nettop's 3-second windows smear each burst
forward, so the observable silence between bursts ceilings at ~3–3.5s —
permanently under `claim_quiet_seconds: 4.0`. On a box running this dashboard,
**the 4-second window does not exist by construction**, and neither does the
60-second one.

The deeper correction to §16: bytes are the trigger, but bytes are not inference.
The dashboard's polling is byte-identical to a small streamed reply at the
granularity the meter sees (single burst, 2–4 KB/s for one window). Two faces of
the same lie: "connected and silent" is not generating (§11, keepalive), and
"moving bytes" is not necessarily generating either (§17, monitor).

What remains true and is now *more* important: `stats.json`'s
`total_requests` (already read by the detector for mtime) is the honest ledger —
a monitor poll does not move it, an inference does, whatever the wire says. And
`/health` is 182 B: asking omlx directly is cheap enough to be polite.

### What this decides

1. The claim gate's 4s stays — it is a measurement of *this box's* quiet and the
   box was not quiet. Do not lower it to make a demo pass (same rule as the
   publish threshold).
2. The fix belongs at the source or in corroboration, in that order:
   edge-dashboard's admin-stats cadence is Sam's call (one line), and the
   Cooker-side rule — ACTIVE_INFER requires bytes **plus** corroborated ledger
   movement or sustained multi-window streaming — depends entirely on one
   measurement not yet made: does omlx flush `stats.json` at request completion
   or continuously during generation? If completion-only, ledger corroboration
   is blind mid-generation and the sustained-stream test must carry the load.
   Measure before coding.

### §17 follow-up: the shape measurement (bench14) and why shape does not save the 4s gate

Per-second shape of omlx's bytes while the agent was quiet, two 25–30s runs:

    [0, 322, 6282, 0,0,0, 322, 6281, 0,0,0,0, 6602, 0,0,0,0, 6602, ...]
    quiet gaps between events: [3, 0, 4, 4, 2, 0]

The poll is one ~6.6 KB response that arrives split across one or two
sample-seconds (TLS boundary), every ~5s. So:

1. The plateau at 1,720 B/s was the ring smearing a burst, exactly as
   suspected — the wire is NOT busy for 3.5s, it is quiet for 3s at a time.
2. Shape separation cannot rescue the 4-second gate here, because the poll's
   cadence itself is 5s: quiet gaps top out at 4.0s against `claim_quiet: 4.0`.
   A rule of "ACTIVE_INFER requires >=2 consecutive sample-seconds" would still
   trip on the poll's own header/body split (322B + 6.3KB), and demoting
   <=2.5s bursts under a byte cap would also demote short *real* generations —
   batch and curl-shaped interactive traffic included, which is exactly what
   OVERVIEW Test 2 protects. Byte size does not separate poll from answer;
   only the source does, and nettop cannot see the source's URL.
3. The admin API can therefore not be polled by the detector without poisoning
   the meter it is meant to corroborate — its own 6.6 KB burst is the same
   shape the detector would be demoting. (§17's ledger idea survives only in a
   form where the ledger comes from something quieter than omlx's admin API.)

### What this decides, concretely (Sam's call, one line each)

- **Fix at the source (recommended):** bump `edge-dashboard`'s omlx-stats fetch
  from 5s to 30s (`setInterval(tick, 5000)` in its `server.js`; its docker
  stats stay 5s — that traffic never touches :8000). Quiet gaps become ~28s,
  the 4s gate opens several times a minute, a bounded stage fits inside every
  gap, and nothing about the safety asymmetry changes.
- If instead we ever demote short bursts cooker-side, that is a *relaxation* of
  false-idle risk and needs an explicit yes; the default keeps bytes the trigger
  and the asymmetry safety-first.

## §18 Ledger audit — the two NULL-score publishes (2026-10-07, pre-gate relics)

`cooker status` today reports `2 published`; `status --json` says `today.published: 2`.
The rows are real and the counter is honest, but they were **not judged**:

- `artifacts`: two PUBLISHED rows, `generator=research`, `score` NULL, `scores_json`
  empty, created 02:08:15 and 02:09:27 (local) —
  `why-does-a-long-prefill-block-other-requests-on…md` and
  `what-does-a-home-nas-get-wrong-about-backups-and…md`, both present on disk in
  `var/outputs/2026-10-07/`.
- `git log -S 'if verdict == "PUBLISH"' -- src/cooker/chains.py` → **4818637 @ 08:13**,
  the M5 evaluate-edge. Those chains completed at 02:08/02:09, five hours *before* the
  gate that requires a `PUBLISH` verdict to spawn a publish stage existed. M4-era
  `chains.advance` emitted `publish` unconditionally.
- The digest agrees: `Threshold 3.5. 0 passed, 5 rejected, 8 unjudged` — the two
  pre-gate rows are not, and cannot be, counted as passed.

**Consequence.** §4's "nothing cleared 3.5 in production" remains true for *judged*
work. The published files stand as honest output of an ungated prototype, not as
quality endorsements; the live UI will show them as served plates. Do not delete them
to make the story tidier — they are the physical proof that the gate arrived late,
which is the kind of fact this project keeps by writing down. If a tidy ledger is
wanted, annotate (add a `note`), don't erase. The current code cannot reproduce the
path: an unscored draft cannot spawn a publish stage (unit-covered by the M5 gate tests).

## §19 The rollup lie — a model download is not someone generating (2026-10-07)

Sam: *"it says paused, someone is generating, but omlx just has the model warm in
memory, it is not actually in use."* He was right, and the number in the log explains
it: `ACTIVE_INFER omlx serving 23,186,419 B/s`.

Measured live against the same kernel counters nettop reads:

- omlx (pid 22630) was pulling **~70 MB/s over `en1` from AWS us-west-2 `:443`**
  (multiple parallel CDN flows, `rx_ooo` enormous) — weights, ~8.2 GB lifetime
  `bytes_in`, straight into RAM. That is the "model warm in memory."
- Meanwhile the inference port was a whisper: the honest tailnet flow showed
  **1,618 B in / 13,052 B out**. Nobody was being served at 23 MB/s; something was
  being *downloaded* at 70.
- `nettop -P` (what the probe ran) rolls up every byte a process moved, on every
  interface, to every peer. The rollup cannot tell a download from a service, and
  `Wire._ingest` summed in+out of that rollup. Correct arithmetic, wrong question.

**Fix:** the probe runs per-flow (`nettop -n -x -L 2 -s 1`, no `-P`) and counts a
flow only when a **parsed endpoint port** is in `detect.omlx_port` — every honest
inference path (opencode direct over utun9, dashboard via OrbStack hairpin, LAN)
carries `:8000` on one side; a CDN download carries it on neither. Rollup rows are
read for process grouping only, never bytes; a rollup-only process seals as a
**zero observation** (quiet, not blind). Two ghosts died with it: the first block
contributes zero (no baseline ⇒ no lifetime-counter-divided-by-window fireworks),
and totals saturate at zero on falls (closed connections, restarts). Two parsing
traps verified live: nettop writes IPv6 ports with a **dot** (`addr.443`), and the
CDN's own address prefix `2603:8000:…` substring-matches `:8000` — ports are parsed
from the tail (`flow_port`), never substring-matched. Process names carry spaces
(`OrbStack Helper.18634`), so flow rows are recognised by `<->`, not by a space.

**After the fix, live, same download running:** the absurd 23 MB/s is gone; what
remains is honest — `7,748 B/s` attributed to a flow caught mid-request uploading
**284 KB/s to `:8000`**: opencode resending this very session's context. That is a
real client of the GPU (agents count as someone, commandment 1), so the kitchen
stands down correctly. The dashboard metronome (~1,720 B/s, §17) remains the
structural blocker; nothing was relaxed to get the honest reading: floor 256,
`claim_quiet: 4.0`, threshold 3.5 all untouched.

## §20 The ledger is the judge — bytes were demoted (bench15, live proof, 2026-10-07)

The metronome did not stop; it multiplied. At 11:xx the Traefik access log said:

    242 GET /admin/api/stats  in 120 s  ≈ 2/s, response 6,389 B each,
    client 192.168.117.1 (the host itself, via the published port — a browser
    tab on omlx.home.arpa's admin console), router omlx@file → 100.122.197.81:8000

nettop saw the same thing as the §17 dashboard poll (raw :8000, ~6.6 KB every
5 s, still there) *plus* a steady 12,557 B/s hairpin flow at :57352 — 2 × 6.4
KB per second, forever. That is the `7–9 k B/s` the UI had been honestly
reporting as ACTIVE_INFER. §17's one-line cadence fix was overtaken inside a
day without anyone deciding anything: **cadence drifts, monitors multiply, and
a bytes-shaped gate is structurally indefensible against them.** nettop cannot
see URLs (§17), and shape cannot separate them (bench14). The proxy sees URLs
and logs them; the server *is* the authority on what a request was.

The server's admin ledger, measured (`bench/bench15_ledger_vs_wire.py`):

    phase A, 32 s of /admin/api/stats polling at 1 Hz *while the metronome ran*:
        total_active_requests 0 · waiting 0 · generating/prefilling []
        idle_seconds          6.0 → 37.8 monotonic
        total_requests        1426 → 1426      ← the metronome moves nothing
    phase B, one real completion (8 tok, 0.26 s, ttft 0.19 s):
        total_requests        1426 → 1427 · idle_seconds reset to 1.1

The ledger ignores monitors and remembers inference. The live counters can miss
a sub-second request even at 10 Hz — `idle_seconds` catches those, and it is a
per-model clock measured by the party that does the work.

**What this decides.** `cooker/ledger.py`: one small GET per second (§8's
endpoint ladder, https first; ~6 KB; 5–8 ms of the server's event loop per the
access log's own durations). When the ledger is up, it *is* the judge:
ACTIVE_INFER iff `active − own > 0` or `waiting > 0`, or bytes with
`idle_seconds` freshly reset (the request the counters missed, margin 5 s);
bytes above the floor that the ledger will not call a generation are
**monitor traffic** and can no longer close the gate, at any cadence, from any
process, forever. When the ledger is blind, the §16 byte regime runs
*unchanged* — fail-closed, never fall-open. Gates untouched: `claim_quiet 4`,
`idle_confirm 60`, floor 256, threshold 3.5. Polling the ledger pollutes the
wire with… a poll. When bytes are no longer the judge, self-pollution is a
question, not an event — that dissolves §17's objection to itself.

Two further walls fell in the same session, reported honestly as collateral:

1. **`:4096` sockets were user evidence, unconditionally.** The landmine #16
   lesson (sockets are not *inference*) had not been applied to the
   user-evidence layer: a connected opencode client — a resident subscriber, a
   parked TUI, the dashboard's poller — made ACTIVE_USER permanent
   (`opencode.socket …` in every blocker) while the box was empty. Now the
   socket counts only while the WAL is also fresh: a relationship with no
   events is presence, not busy.
2. **The daemon had never survived its own stage.** The event table's entire
   history held one `sched.start`, and it said "would run" (dry-run). Under
   bytes-judge, our own streamed completion moves omlx's counters → ACTIVE_INFER
   → `_preempt_all` cancels our slot. The ledger closes the loop:
   `own` (the LLM client's inflight count, threaded through Kitchen) is
   subtracted from `active`; bytes + fresh idle + `own > 0` is "cooking: ours".

**Live proof.** With the metronome still hammering: first production
`sched.ready` and first real plate the daemon ever made —

    11:24:09 IDLE  ready to cook → start: slot0:brainstorm.generate
    11:24:18 ACTIVE_INFER model active 3.7s ago (ledger) → plate: slot0:generate

— held under a bounded bench window (11:20–11:27, `user_idle_seconds: 4`,
`opencode_wal_seconds: 4`, `fs_activity_seconds: 0`, generators brainstorm-only,
all reverted same day, git-visible): the permanent evidence walls were working
*as designed* — my own session kept the WAL fresh on every persisted token, and
a human at the machine keeps `hid < 120`. Interactive always wins; tonight it
cooks when the room actually empties. Follow-up owed: tomorrow morning, confirm
`status` shows a research chain seeded, run and judged unattended.

Operational notes: admin login is `POST /admin/api/login {"api_key":…}` →
**HttpOnly cookie in Set-Cookie** (the body is just `{"success":true}` — parsing
a `session_id` out of it KeyError'd once); `AR_OMLX_ADMIN_KEY` does *not*
authenticate `/v1` (401), and the admin session does not either — two auth
domains, two keys, both in the autoresearch `.env`; the key is never committed
and never in the plist (the daemon reads the same file ladder as the inference
key).

## §20b The presence curfew is struck — the wall is GPU access (owner's amendment, 2026-10-07)

Sam, verbatim: *"it should run when I'm using the computer, just not when I'm
hitting omlx via anything."* The commandment was always "interactive always
wins"; the implementation had grown it into a curfew — `hid < 120`, a fresh
WAL, a saved file, any :4096 subscriber — and the kitchen closed the moment
anyone touched the desk (`waiting — activity on the box`, 12:25:52, his doing,
his machine, his call). Amended: **the blockers are GPU traffic and blindness
to it. Nothing else.**

- `detect.presence_blocks: false` (config default; the code default stays
  true so a sparse config fails conservative). HID/WAL/fs/:4096 are now
  *annotation*: the reason reads `monitor traffic: 4,147 B/s, ledger idle ·
  human present: hid 0s` — the kitchen lit while hands are on keys, saying
  who is home rather than pretending nobody is. Legacy regime stays pinned by
  tests on the knob.
- The collision this accepts is the measured one (§16): +95 ms median TTFT,
  +350 ms into a prefill race — inside the 750 ms budget that was always the
  bar. What still stops work cold: the ledger's `active − own > 0` / waiting,
  unexplained bytes inside the idle margin, and blind (blind ≠ quiet, ever).
- Deployed live proof, same sitting: `ready to cook` 12:33:03 while he typed;
  then a full research chain — plan 68 tok, search, fetch, extract × N,
  **synthesize 3,455 tok in 18.2 s**, critique — with the ledger preempting
  and resuming around my own session traffic exactly as designed.
- **And a third bug, caught only because the gate finally let anything through:**
  the queue was starving with `ready` printed — `cooker resume` writes
  `paused=''`, and `seed_research_if_thirsty` asked `get_meta(...) is not
  None` while the scheduler asked `in (None,"","0")`. A resumed box answered
  *"paused"* to the seed forever. One definition now: `db.is_paused`, with the
  regression test the old test-suite dodged by deleting the meta instead of
  resuming. Landmine #9, second sentence: *an empty meta row is not a pause
  order.*

### §20b coda — the first judged plate (12:48:01)

Score **3.6 ≥ 3.5**, published by the *living* daemon: `what a polite background job
should do when it detects a human typing` — the machine writing about the very rule
its owner had just amended, and passing the judge on its own words. Two older
published rows carry NULL scores (§ pre-gate legacy); this one carries the stamp.
The same afternoon's books: 159 research stages, 75,430 output tokens, 567 GPU
seconds — then `topics.exhausted`: topics.md holds 16 subjects and the well ran
dry in one working day. Production is now inventory-limited, not gate-limited.

## §21 opencode as a tool — M9 by direct measurement (2026-10-07)

Sam: *"expose opencode as a tool that the cooker can work with: submitting
prompts to opencode in the right directory, responding to its prompts when it
asks questions, and generally just be able to use the tool."* The deep-dive pot
was a stub waiting for hands; these are the hands. Everything below was
measured against **this box's** running build before a line of client code was
written, because the docs site describes a future build and the running build
disagrees where it matters.

### The device, specifically

- **Binary**: homebrew tap formula `opencode-v2` **v2.0.24** (Mach-O arm64;
  the plain `opencode` formula at 2.0.20 is *not* installed).
- **Server A** (the one Cooker drives): launchd `com.homelab.opencode`,
  KeepAlive, `opencode serve --hostname 127.0.0.1 --port 4096` via
  `~/homelab/edge/opencode/run-opencode.sh`, cwd `~/homelab`. Up since
  Oct 5. **Auth is HTTP Basic on `/api/*`: username is always `opencode`,
  password `OPENCODE_PASSWORD` from `~/homelab/edge/.env`** — the wrapper's
  own comments document the design (stable across restarts so phone logins
  don't break; the SPA shell loads anonymously; Traefik fronts it as
  `https://opencode.home.arpa` through OrbStack's host loopback). `/openapi.json`
  through that Basic is the authoritative contract for 2.0.24 — **117 paths**.
- **Server B**: the auto-managed `opencode serve --service` (pid 23788,
  **ephemeral port 49374**) that the TUI and `opencode api` discover. The
  web UI at `/server/` is an SPA fallback returning HTML 200, not an API —
  `curl /server/api/info` returns the shell, and only Basic opens `/api/*`.
- **Shared DB**: both servers read `~/.local/share/opencode/opencode.db` —
  sessions are visible to both, including the one this very report is being
  written inside. The 1 active session on `/api/session/active` was me.
- **Agents**: Build, General, Explore, Compaction, Title, Summary, Plan.
  Model: `omlx/Qwen3.8-Flash-Next-oQ4e-mtp` from the global config — which
  points at `100.122.197.81:8000/v1`, the tailnet address of the same omlx
  the kitchen watches. Delegation spends the same GPU the gate protects; the
  ledger charges it the same way, and Cooker subtracts exactly one `own` for
  the one session whose id it minted.

### The contract, as corrected by the running build

| guess (from docs) | fact (2.0.24 live) |
|---|---|
| permission reply `{"response": …}` | **`{"decision": "once\|always\|reject"}`** — the wrong key rejects everything silently |
| session/messages under `data.parts` | messages are **top-level `{type, content:[…]}`**; assistant text lives in `content[].type=="text"`, reasoning is a separate part that must not be collected |
| busy-state polling | **`{type:"idle", outcome:"succeeded"}`** sentinel message is the completion oracle; `/api/session/active` is `{ses: {type:"running"}}` |
| prompt `/api/session/{id}/prompt` | ✓ `{"text": …}` — but the **session's location directory must already exist**: create accepts missing dirs, prompt answers `LocationNotFoundError` |
| form reply `{"answers": {…}}` | **`{"answer": {…}}`** |

### The architecture it forced

Cooker drives **Server A** directly over loopback Basic — not the CLI (a
subprocess per call is the wrong shape for a daemon), not Server B (its port
is ephemeral by design), never by injection into existing sessions. Credentials
come from a ladder (`OPENCODE_PASSWORD` env → explicit file → the edge `.env`)
and are never written into config.yaml or the plist — same discipline as every
other key on this box.

- **deep-dive chain**: `delegate → critique → evaluate → publish|reject` —
  the delegated brief faces **the same judge** as every research draft.
  Ordering work buys attention at the judge, never a pass (§ orders).
- **permissions**: answered one at a time (`once`). The code **refuses the
  `always` mode itself** — a saved permission outlives the stage that earned
  it. A deny list (rm/sudo/git push/.env/.ssh/network-mutating curl…)
  rejects first and beats every grant. Every answer is an event
  (`opencode.permission`) — the kitchen's yes-men must be auditable.
- **preemption**: the delegate stage is a GPU stage like any other. When the
  ledger says someone else is generating, the stage is cancelled, the
  session is **interrupted** (`POST /interrupt`), and the task takes the same
  paused→recovered path the LLM stages use. The ledger cannot attribute
  requests per client, so `own` subtracts exactly one for the one session id
  we minted — Sam prompting simultaneously makes `active` outrun `own` and
  preemption behaves exactly as §20 drew it.
- **orders**: `POST /api/orders {topic, kind: research|deep-dive}`. Cap of
  pending orders answers **429 with the number**, pause answers **409** —
  the counter is honest about why it won't take the ticket. Tickets come back
  with their stage progress.

Live proof, same sitting: order taken 13:52, gate opened on the first 4-second
gap (`start: slot0:deep-dive.delegate`), session created in the delegation
workspace, and then the honest part — two ledger preemptions at
`omlx serving 2 request(s)`, where `own` subtracted exactly the one session
Cooker minted and the second request was this report being written: the
kitchen yielded its own delegation to the conversation, recovered it on the
quiet, and completed it: **SUCCEEDED, 29,607 tokens in, 2,711 out, 157.4s
wall across two yields, `plate: slot0:delegate`**.

And the bug the live run caught that 269 tests did not: **`GET /message`
answers newest-first** (the idle sentinel rode at the head of the list in
the very first probe — the probe was read right, the collector written
wrong). Taking the last list element collected the *oldest* assistant
utterance, so the first plate was a 250-byte `"I'll research this topic in
parallel."` — conversation, not brief. Collection now sorts by timestamp and
is correct against either order; the fake serves newest-first like the
device, and the regression test asserts the newest turn wins. The poisoned
candidate was cancelled out of the chain's record with the reason in its
`error` column; a fresh order re-cooked the same topic. And the judge
settled it independently: the poisoned plate had already reached evaluation
and scored **2.8 REJECT — "the output is truncated mid-sentence, rendering
the actionable advice incomplete"**. The judge never saw the bug report; it
read the plate and refused to serve it. That is the argument for why the
judge sits at the end of every chain no matter whose hands cooked — when
the kitchen itself is wrong, the last gate is still honest.
