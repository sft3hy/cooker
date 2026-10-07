"""runner: turns a claimed stage into a prompt, a completion, and an artifact.

This is the seam between the scheduler (which decides *when* and *how many*) and
`llm.py` (which decides *how to ask*). M4 puts the generator chains behind it;
M3 is deliberately the smallest honest version, because it is the first code in
the system that spends real GPU time, and every guarantee the plan makes about
cancellation and safety has to be true before there are chains worth running.

What is non-negotiable here, whatever runs through it:

    - content goes through `safety.scan_secrets` on the way *in*. A secret that
      reaches the model is gone from this machine's point of view; the scan is the
      last place it can be caught.
    - retrieved context is fenced as untrusted data, never spliced as prose.
    - the artifact is scanned on the way *out* as well, because the model may
      have reproduced something it should not have, and an artifact written first
      and scrubbed later has been on disk with the secret in it.
    - cancellation propagates. The `except CancelledError` below records and
      re-raises: a stage that swallows a cancel is a stage still cooking after
      the kitchen said stop, and the GPU burn would be invisible.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from cooker import db, safety
from cooker.config import Config
from cooker.llm import LLM, Completion, EndpointGone, PrefillOver, Request


@dataclass
class Outcome:
    task_id: str
    status: str
    path: str | None = None
    reason: str | None = None
    completion: Completion | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status, "path": self.path,
                "reason": self.reason,
                "completion": self.completion.as_dict() if self.completion else None}


class StageRunner:
    """Callable handed to the scheduler as `stage_runner`.

    The scheduler wraps this in `asyncio.wait_for`, so the timeout belongs to it
    and not here: two owners of one deadline is how a stage gets half-cancelled.
    """

    def __init__(self, cfg: Config, conn: sqlite3.Connection, llm: LLM) -> None:
        self.cfg = cfg
        self.conn = conn
        self.llm = llm
        self.runs = 0
        self.aborts = 0

    def emit(self, type_: str, message: str, data: dict[str, Any] | None = None) -> None:
        db.emit(self.conn, type_, message=message, data=data or {})

    # --- the prompt ------------------------------------------------------

    def prompt_for(self, task: db.Task) -> Request:
        """Build the request. Secrets scanned in, retrieved context fenced.

        `task.prompt` is authored here (CLI or a generator we wrote), so it is
        scanned but not fenced. Anything under `payload["context"]` arrived from
        outside — a web page, a file, another model — and is fenced even if it
        reads innocently, because the whole point of the fence is that innocence
        is not something we can check.
        """
        body = task.prompt or (
            f"Produce a short, useful note on: {task.title}. "
            f"Be concrete; no preamble.")
        scan = safety.scan_secrets(body, source=f"task:{task.id}.prompt")
        if scan.blocked:
            raise safety.PathRefused(f"task prompt withheld: {scan.as_dict()}")
        if scan.findings:
            self.emit("safety.redaction",
                      f"redacted {sum(f.count for f in scan.findings)} secret-shaped "
                      f"span(s) from task prompt",
                      {"task_id": task.id, **scan.as_dict()})
        user = scan.text
        ctx = task.payload.get("context")
        if ctx:
            inner = safety.scan_secrets(str(ctx), source=str(
                task.payload.get("context_source", "payload")))
            if not inner.blocked and inner.findings:
                self.emit("safety.redaction",
                          f"redacted {sum(f.count for f in inner.findings)} span(s) "
                          f"from retrieved context",
                          {"task_id": task.id, **inner.as_dict()})
            user += "\n\n" + safety.fence(
                str(task.payload.get("context_source", "retrieved")), inner.text)
        stage = task.kind
        return Request(system=safety.SYSTEM_DATA_ONLY, user=user,
                       thinking=self.llm.thinking_for(stage), kind=task.kind,
                       stage=stage,
                       max_tokens=int(self.cfg.get("inference.max_tokens_floor", 512)))

    # --- the stage -------------------------------------------------------

    async def __call__(self, task: db.Task) -> Outcome:
        self.runs += 1
        t0 = time.perf_counter()
        try:
            req = self.prompt_for(task)
        except (safety.PathRefused, safety.GitRefused) as exc:
            db.finish(self.conn, task.id, db.REJECTED, error=f"safety: {exc}",
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.rejected", f"{task.kind} refused by safety: {exc}",
                      {"task_id": task.id})
            return Outcome(task.id, db.REJECTED, reason=str(exc))

        # Bound before the await: a stream that fails at connect time never
        # returns a Completion, and every failure path below wants to report on
        # it. An unbound name in the error handler is the worst place in this
        # codebase to discover a bug, because it hides behind the first one.
        comp: Completion | None = None
        try:
            comp = await self.llm.stream(req)
        except asyncio.CancelledError:
            # Record, then propagate. The accounting matters: without it the
            # events table shows a stage that vanished, and a GPU that was busy
            # for nine seconds with nothing to show for it looks like a bug in
            # the meter rather than an aborted request, which is what it was.
            self.aborts += 1
            self.emit("runner.cancelled",
                      f"{task.generator}.{task.kind} cancelled after "
                      f"{time.perf_counter() - t0:.2f}s",
                      {"task_id": task.id, "llm": self.llm.state()})
            raise
        except (EndpointGone, PrefillOver) as exc:
            db.finish(self.conn, task.id, db.FAILED, error=str(exc),
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"{task.kind}: {exc}", {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=str(exc), completion=comp)
        except Exception as exc:
            db.finish(self.conn, task.id, db.FAILED,
                      error=f"{type(exc).__name__}: {exc}",
                      duration_ms=(time.perf_counter() - t0) * 1000)
            self.emit("runner.failed", f"{task.kind}: {type(exc).__name__}: {exc}",
                      {"task_id": task.id})
            return Outcome(task.id, db.FAILED, reason=str(exc), completion=comp)

        if not comp.usable:
            # Reasoning with no answer (§4): the model spent the budget thinking.
            reason = f"no content (finish={comp.finish_reason}, " \
                     f"reasoning={len(comp.reasoning)}c)"
            db.finish(self.conn, task.id, db.FAILED, error=reason,
                      input_tokens=comp.prompt_tokens,
                      output_tokens=comp.completion_tokens,
                      ttft_ms=(comp.ttft_s or 0) * 1000,
                      duration_ms=comp.elapsed_s * 1000)
            self.emit("runner.empty", f"{task.kind} produced no usable answer",
                      {"task_id": task.id, **comp.as_dict()})
            return Outcome(task.id, db.FAILED, reason=reason, completion=comp)

        flags = safety.looks_instructed(comp.text)
        if flags:
            self.emit("safety.injection_flags",
                      f"{task.kind} output carries instruction-shaped text: "
                      f"{', '.join(flags)}", {"task_id": task.id})
        name = f"{task.generator}-{task.kind}.md"
        doc = self.document(task, comp, flags)
        try:
            # Off the loop. The write is small, but "small" is a claim about the
            # average artifact and the daemon is not allowed to bet the preemption
            # path on an average: a 20MB log pasted into a doc would stall the
            # tick that frees the GPU.
            path, digest = await asyncio.to_thread(
                safety.write_workspace_file, self.cfg, task.id, name, doc)
        except safety.PathRefused as exc:
            db.finish(self.conn, task.id, db.FAILED, error=str(exc))
            return Outcome(task.id, db.FAILED, reason=str(exc), completion=comp)

        db.finish(self.conn, task.id, db.SUCCEEDED, result_path=str(path),
                  result_hash=digest, input_tokens=comp.prompt_tokens,
                  output_tokens=comp.completion_tokens,
                  ttft_ms=(comp.ttft_s or 0) * 1000,
                  duration_ms=comp.elapsed_s * 1000)
        self.conn.execute(
            "INSERT OR REPLACE INTO artifacts (id, task_id, generator, title, path,"
            " hash, score, scores_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (db.new_id(), task.id, task.generator, task.title, str(path), digest,
             None, None, "CANDIDATE", db.now()))
        self.conn.commit()
        self.emit("runner.published",
                  f"{task.generator}.{task.kind} -> {path.name} "
                  f"({len(comp.text)} chars, {comp.completion_tokens} tok, "
                  f"{comp.elapsed_s:.1f}s)",
                  {"task_id": task.id, "path": str(path), **comp.as_dict()})
        return Outcome(task.id, db.SUCCEEDED, path=str(path), completion=comp)

    # --- the document ----------------------------------------------------

    def document(self, task: db.Task, comp: Completion,
                 flags: tuple[str, ...]) -> str:
        """The artifact. Header carries provenance and accounting, because an
        output with no record of what it cost and what it read is not auditable."""
        lines = [
            f"# {task.title}",
            "",
            f"- generator: `{task.generator}`  kind: `{task.kind}`",
            f"- task: `{task.id}`  chain: `{task.chain_id}`",
            f"- model: `{self.llm.model}`  {comp.elapsed_s:.1f}s, "
            f"TTFT {(comp.ttft_s or 0) * 1000:.0f}ms",
            f"- tokens: {comp.prompt_tokens} in / {comp.completion_tokens} out",
        ]
        if flags:
            lines.append(f"- **injection flags (not blocked, flagged):** "
                         f"{', '.join(flags)}")
        if comp.reasoning:
            lines += ["", "## reasoning (excerpt)", "",
                      comp.reasoning[:600].strip(), ""]
        lines += ["## result", "", comp.text.strip(), ""]
        return "\n".join(lines)

    def state(self) -> dict[str, Any]:
        return {"runs": self.runs, "aborts": self.aborts, **self.llm.state()}
