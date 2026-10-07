"""llm: the cancellable inference client. The only module that spends a GPU.

Cancellation is the whole design (OVERVIEW 3.2, DISCOVERY §3)
-------------------------------------------------------------
An interactive request must never queue behind ours. omlx has no cancel API, so
our contribution is to *stop asking*: drop the HTTP connection mid-stream and the
server stops generating. Measured (bench #2/#5) at a 1.00x ratio — cancelling
costs the interactive session nothing.

That only works if cancellation propagates, so the rules here are:

    - `asyncio.CancelledError` is never caught-and-swallowed. It is recorded and
      re-raised, because a swallowed cancellation is a stage that keeps cooking
      after the kitchen said stop.
    - the response is closed in a `finally`, not in an `except`, so a cancel that
      lands between `stream()` and the first byte still closes the socket.
    - whatever we did receive is returned to the caller as accounting
      (`aborted=True`, bytes and tokens so far) rather than discarded, so the
      events table shows a partially-consumed GPU and not a mystery.

Prefill is prevention, not preemption (DISCOVERY §1, §2)
---------------------------------------------------------
Prefill on omlx is atomic and exclusive: once it starts it cannot be interrupted,
and 2k tokens costs 3.0x idle TTFT while 1000 costs 1.8x. So the ceiling is
enforced *before* the request leaves, by trimming retrieved context off the end
until it fits. `max_prefill_tokens_per_request` is a hard refusal, not a warning,
because the expensive moment is the one you cannot cancel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx

from cooker import net, safety
from cooker.config import Config

# Characters per token for this model's chat template, measured conservatively:
# 3.0 chars/token overestimates token count for English prose, which is the
# wrong way to be wrong here. Underestimating prefill is the mistake that puts a
# 2k-token prefill on the box and triples someone's time-to-first-token.
CHARS_PER_TOKEN = 3.0


class PrefillOver(RuntimeError):
    """The prompt does not fit under the ceiling even after trimming context."""


class EndpointGone(RuntimeError):
    """No working omlx endpoint. The resolver already said why, in events."""


def estimate_tokens(text: str) -> int:
    """Cheap pre-flight estimate. The server's `usage.prompt_tokens` is the real
    number and we record that afterwards; this one decides whether to send."""
    return int(len(text) / CHARS_PER_TOKEN) + 1


@dataclass
class Completion:
    """What one streamed request produced, including the part we abandoned."""

    text: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # What the server actually reported versus what we had to guess. Separate
    # flags, not one label, because omlx's usage chunk reports one half and not
    # the other often enough that "server" would be a lie about the prompt count.
    server_prompt: bool = False
    server_completion: bool = False
    prompt_chars_est: int = 0
    server_ttft_s: float | None = None
    gen_tps: float | None = None
    prompt_tps: float | None = None
    ttft_s: float | None = None
    elapsed_s: float = 0.0
    bytes_in: int = 0
    aborted: bool = False
    error: str | None = None

    @property
    def usable(self) -> bool:
        """An answer we can publish. `reasoning` alone is not one: this model
        streams its thinking first, so a small budget buys
        `finish_reason=length` and a wall of reasoning with no answer (§4)."""
        return bool(self.text.strip())

    @property
    def usage_source(self) -> str:
        """`server` only when both halves were reported. Anything else says which
        half we invented, so a number in the database can be traced to whoever is
        responsible for it."""
        if self.server_prompt and self.server_completion:
            return "server"
        if self.server_prompt:
            return "server:prompt-only"
        if self.server_completion:
            return "server:completion-only"
        return "estimated"

    def as_dict(self) -> dict[str, Any]:
        return {
            "chars": len(self.text), "reasoning_chars": len(self.reasoning),
            "finish": self.finish_reason, "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "usage": self.usage_source,
            "ttft_s": round(self.ttft_s, 3) if self.ttft_s else None,
            "server_ttft_s": self.server_ttft_s, "gen_tps": self.gen_tps,
            "prompt_tps": self.prompt_tps,
            "elapsed_s": round(self.elapsed_s, 2), "bytes_in": self.bytes_in,
            "aborted": self.aborted, "error": self.error,
        }


@dataclass
class Request:
    system: str
    user: str
    max_tokens: int = 1024
    temperature: float = 0.7
    thinking: bool = True
    kind: str = "generate"
    stage: str = "generate"

    def wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": [{"role": "system", "content": self.system},
                         {"role": "user", "content": self.user}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
            # Measured (DISCOVERY §12): omlx sends a usage chunk *only* when
            # asked, and when it does it reports far more than the OpenAI schema —
            # its own time_to_first_token, prompt/generation durations and tok/s.
            # Without this flag every token figure in our database is a char/3
            # guess, and a guess in a budget column is how you get a budget that
            # nobody can trust.
            "stream_options": {"include_usage": True},
        }
        # omlx honours this only inside chat_template_kwargs; a top-level
        # `thinking` key is silently ignored, which reads as "it did not work"
        # when the token budget says otherwise.
        if not self.thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body


class LLM:
    """One streaming client, cancellable, with the prefill ceiling enforced
    before the bytes leave the process.

    Owns the `net.Resolver` so endpoint preference (https/home.arpa first, raw
    IPs next, loopback last — loopback is shadowed by a stray http.server that is
    not ours to kill) is decided in exactly one place.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        emit: Callable[[str, str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self._emit_fn = emit
        self.ceiling = int(cfg.get("inference.max_prefill_tokens_per_request", 1000))
        self.floor = int(cfg.get("inference.max_tokens_floor", 512))
        self.timeout = float(cfg.get("inference.request_timeout_seconds", 300))
        self.connect_timeout = float(cfg.get("inference.connect_timeout_seconds", 5))
        self.thinking_off = set(cfg.get("inference.thinking_off_stages", []) or [])
        self.model = cfg.get("inference.model")
        self.resolver = net.Resolver(
            cfg, cfg.get("inference.ca_file"),
            refresh_seconds=float(cfg.get("inference.resolve_refresh_seconds", 300.0)),
        )
        self.inflight = 0
        self.aborts = 0

    # --- plumbing --------------------------------------------------------

    def emit(self, type_: str, message: str, data: dict[str, Any] | None = None) -> None:
        if self._emit_fn is not None:
            self._emit_fn(type_, message, data or {})

    def thinking_for(self, stage: str) -> bool:
        """Mechanical stages do not need a chain of thought (§4): thinking burns
        the budget and returns no answer for a stage that extracts a field."""
        return stage not in self.thinking_off

    def fit(self, req: Request) -> Request:
        """Trim retrieved *evidence* until the prefill fits. Never the ask.

        The instruction stays whole and the evidence is cut from the end, because
        the thesis of a source is usually at the top of it while the tail is
        usually the part that could have been left out anyway. Truncating the
        question instead would produce a confident answer about a different
        question, which is the one failure mode here that nobody notices.

        So: no fence to trim, or the instruction alone will not fit, is a
        refusal. A refusal costs one stage; a silently re-asked question costs
        trust in every artifact after it.
        """
        total = estimate_tokens(req.system) + estimate_tokens(req.user)
        if total <= self.ceiling:
            return req
        open_tag = safety.FENCE_OPEN.split(" ")[0]
        fence_at = req.user.find(open_tag)
        instruction = req.user if fence_at < 0 else req.user[:fence_at]
        if estimate_tokens(req.system) + estimate_tokens(instruction) > self.ceiling:
            ask = estimate_tokens(req.system) + estimate_tokens(instruction)
            raise PrefillOver(f"the instruction alone (~{ask} tokens) exceeds "
                              f"the ceiling of {self.ceiling}")
        if fence_at < 0:
            raise PrefillOver(f"prompt is ~{total} tokens over the ceiling of "
                              f"{self.ceiling} with no untrusted block to trim")
        close_tag = safety.FENCE_CLOSE
        fence_end = req.user.rfind(close_tag)
        head = req.user[:fence_at]
        evidence = req.user[fence_at + len(open_tag):fence_end if fence_end >= 0
                            else len(req.user)]
        room = int((self.ceiling - estimate_tokens(req.system)
                    - estimate_tokens(head) - 48) * CHARS_PER_TOKEN)
        if room <= 0:
            raise PrefillOver("no room left for evidence under the ceiling")
        kept = evidence[:room]
        note = "\n[…context trimmed to fit the prefill ceiling of " \
               f"{self.ceiling} tokens]"
        trimmed = f"{head}{kept}{note}\n{close_tag}"
        after = estimate_tokens(req.system) + estimate_tokens(trimmed)
        if after > self.ceiling:
            # The ceiling is arithmetic, not advice. If the trim failed to achieve
            # it, refuse rather than ship an over-ceiling prefill.
            raise PrefillOver(f"trim still ~{after} tokens > {self.ceiling}")
        self.emit("llm.prefill_trim",
                  f"trimmed ~{total - after} tokens of context to fit "
                  f"{self.ceiling}", {"before_est": total, "after_est": after})
        return replace(req, user=trimmed)

    # --- the request ----------------------------------------------------

    async def stream(self, req: Request) -> Completion:
        """Stream a completion. Cancel this task and the socket closes.

        The cancellation path is the reason `resp.aclose()` lives in a `finally`:
        `CancelledError` inherits from `BaseException`, so an `except Exception`
        would not see it, and a socket left open after a cancel is exactly the
        thing this daemon promises not to leave open.
        """
        if self.floor and req.max_tokens < self.floor:
            req = replace(req, max_tokens=self.floor)
        req = self.fit(req)
        base = await self.resolver.resolve()
        if base is None:
            raise EndpointGone(net.route_report(
                int(self.cfg.get("detect.omlx_port", 8000)),
                tuple(self.cfg.get("detect.omlx_proc_names", ["omlx"]))))
        client = await self.resolver.start()
        body = req.wire()
        body["model"] = self.model
        out = Completion()
        # The estimate is taken here, while the prompt still exists as a whole,
        # so a server that omits usage can still be charged for its prefill.
        out.prompt_chars_est = len(req.system) + len(req.user)
        t0 = self.clock()
        self.inflight += 1
        try:
            async with client.stream(
                "POST", f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {net.api_key(self.cfg)}"},
                json=body,
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread())[:240]
                    out.error = f"HTTP {resp.status_code}: {detail!r}"
                    # The endpoint may have moved rather than failed; force the next
                    # caller to re-probe instead of hammering a dead route.
                    self.resolver.base = None
                    raise httpx.HTTPStatusError(out.error, request=resp.request,
                                               response=resp)
                async for _live in self._chunks(resp, out, t0):
                    continue
        except asyncio.CancelledError:
            # Record what we consumed, then propagate. The GPU is freed by the
            # unwinding of the `async with` above.
            out.aborted = True
            raise
        except (httpx.HTTPError, OSError) as exc:
            out.error = f"{type(exc).__name__}: {exc}"
            self.resolver.base = None
            raise
        finally:
            out.elapsed_s = self.clock() - t0
            # omlx's own measurement wins where it exists: ours includes the
            # Traefik hop, so it is an upper bound on the box's latency rather
            # than the box's latency.
            if out.server_ttft_s is not None:
                out.ttft_s = out.server_ttft_s
            self.inflight -= 1
            if out.aborted:
                self.aborts += 1
                self.emit("llm.abort",
                          f"aborted {req.kind} after {out.elapsed_s:.2f}s with "
                          f"{out.bytes_in:,} bytes in", out.as_dict())
        if out.error:
            raise httpx.HTTPError(out.error)
        return out

    async def _chunks(self, resp: httpx.Response, out: Completion,
                     t0: float) -> AsyncIterator[bool]:
        """Parse SSE, separating `reasoning_content` from `content` (§4).

        Both are accumulated: the reasoning is what tells a reviewer *why* an
        answer looks thin, and discarding it is why the first version of this
        looked like a model that produced empty answers.
        """
        async for raw in resp.aiter_bytes():
            if not raw:
                continue
            out.bytes_in += len(raw)
            for line in raw.decode(errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    if out.finish_reason is None:
                        out.finish_reason = "stop"
                    return
                with contextlib.suppress(json.JSONDecodeError):
                    self._absorb(json.loads(payload), out)
                    # TTFT is stamped at the first *token*, not the first byte.
                    # omlx flushes headers immediately, so the byte stamp reads
                    # ~5ms and flatters the box by two orders of magnitude on the
                    # one metric whose whole purpose is to describe a human's wait.
                    if out.ttft_s is None and (out.text or out.reasoning):
                        out.ttft_s = self.clock() - t0
            yield True

    @staticmethod
    def _absorb(chunk: dict[str, Any], out: Completion) -> None:
        choices = chunk.get("choices") or [{}]
        ch = choices[0] or {}
        delta = ch.get("delta") or {}
        if delta.get("reasoning_content"):
            out.reasoning += str(delta["reasoning_content"])
        if delta.get("content"):
            out.text += str(delta["content"])
        if ch.get("finish_reason"):
            out.finish_reason = str(ch["finish_reason"])
        usage = chunk.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        # A zero from the server means "not reported", not "free": prefill cost
        # is the number the entire budget system rests on, and a recorded 0 is
        # worse than an estimate because it reads as a fact.
        if pt > 0:
            out.prompt_tokens = pt
            out.server_prompt = True
        if ct > 0:
            out.completion_tokens = max(out.completion_tokens, ct)
            out.server_completion = True
        # Estimates are recomputed on every chunk. Setting them once with `if not
        # out.completion_tokens` freezes the value at whatever the *first* chunk
        # implied — which is how a 1,016-character answer was filed as having
        # cost one token.
        if not out.server_prompt and out.prompt_chars_est:
            out.prompt_tokens = int(out.prompt_chars_est / CHARS_PER_TOKEN) + 1
        if not out.server_completion:
            out.completion_tokens = estimate_tokens(out.text + out.reasoning)
        # The server's own timings, when it offers them. Our client-side stamp
        # includes the Traefik hop and our own scheduling; omlx's number is the
        # box admitting how long it actually took, which is the number worth
        # putting in front of a human.
        ttft = usage.get("time_to_first_token")
        if ttft is not None:
            out.server_ttft_s = float(ttft)
        gen = usage.get("generation_tokens_per_second")
        if gen is not None:
            out.gen_tps = float(gen)
        pre = usage.get("prompt_tokens_per_second")
        if pre is not None:
            out.prompt_tps = float(pre)

    # --- teardown -------------------------------------------------------

    async def aclose(self) -> None:
        await self.resolver.aclose()

    def state(self) -> dict[str, Any]:
        return {"inflight": self.inflight, "aborts": self.aborts,
                "ceiling": self.ceiling, "floor": self.floor,
                "model": self.model, "endpoint": self.resolver.base}
