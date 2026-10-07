# bench — discovery measurements

One-off probes that produced the numbers in `../DISCOVERY.md`. Kept so the
conclusions stay checkable rather than folklore.

They pin `100.122.197.81:8000` (omlx on the Tailscale interface) on purpose.
**Do not change that to `127.0.0.1`** — on this host loopback is shadowed by an
unrelated `http.server`, and you will get 404s and conclude omlx is dead. See
DISCOVERY.md §6.

They hammer the accelerator for tens of seconds. Run them when nobody is using
the box, or don't run them: the conclusions are already recorded, and the
conservative defaults in `config.yaml` do not depend on re-measurement.

| script | answers | config it sets |
|---|---|---|
| `bench_disconnect.py` | does dropping the socket free omlx? | preemption during decode |
| `bench2_decode.py` | decode throughput under contention | concurrency ramp |
| `bench4_thinking.py` | which thinking knob actually works | `thinking_off_stages` |
| `bench5_prefill.py` | the prefill ladder | `max_prefill_tokens_per_request` |

Requires the venv (`uv sync`) and the API key in `~/dev/autoresearch-service/.env`.

`bench6_abort_prefill.py` (does a hard abort shorten a prefill?) is deliberately
unrun — it costs an interactive session ~4 s, which is the harm this project
exists to avoid. It runs in a genuinely idle window.
