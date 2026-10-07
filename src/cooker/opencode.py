"""OpenCode as a tool: the deep-dive generator's hands (M9).

The owner's amendment of 2026-10-07 ("expose opencode as a tool the cooker
can work with") turned the stub `deep-dive` pot into a real burner. This is
the client that drives it. Every shape in here was verified against the live
server on this box (DISCOVERY §21) — not copied from the newest docs, which
describe a future build:

* the box runs **opencode-v2 2.0.24** (homebrew tap formula, Mach-O arm64);
* `com.homelab.opencode` (launchd) serves `opencode serve --hostname
  127.0.0.1 --port 4096` with HTTP Basic on /api/* — username is always
  `opencode`, password `OPENCODE_PASSWORD`, from `~/homelab/edge/.env`
  (the wrapper's own comment documents this; it is the sanctioned
  automation entry, stable across restarts by design);
* prompt body is `{"text": ...}`; permission reply is
  `{"decision": "once"|"always"|"reject"}` — NOT "response"; a guess here
  would silently reject every request.

Honesty rules this module obeys:
* it drives ONLY sessions it created, addressed by their session ids;
* prompts land in Cooker-owned workspace directories that must already exist
  (the server answers `LocationNotFoundError` otherwise — verified live);
* permissions are answered by policy, one at a time (`once` is the default;
  `always` is refused by this code because saved permissions outlive the
  stage that earned them);
* cancellation interrupts the session — the GPU belongs to whoever asked
  first, and the ledger says the same about the traffic this causes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cooker.config import Config

USER = "opencode"
DEFAULT_BASE = "http://127.0.0.1:4096"

# Anything matching these in a permission request's serialized action or
# resources is refused, not granted. The list is deny-first on purpose:
# a research session has no reason to reach any of it, and the cost of a
# wrong "once" is a deleted home directory.
DENY_PATTERNS = [
    r"\brm\b", r"\bsudo\b", r"\bsu\b", r"\bgit\s+push\b", r"\bmv\b",
    r"\bkill\b", r"\bpkill\b", r"\bdd\b", r"\bchmod\b", r"\bchown\b",
    r"\blaunchctl\b", r"\bdocker\b", r"\breboot\b", r"\bshutdown\b",
    r"\bgrep\b.*\.env\b", r"\bcurl\b.*(-X\s*(POST|PUT|DELETE)|--data)",
    r"\bssh\b", r"\bscp\b", r"\baws\b", r"\bxargs\b", r">\s*/",
    r"\.env\b", r"id_rsa\b", r"\.ssh\b", r"\.aws\b", r"\.omlx\b",
]


def password_ladder(cfg: Config) -> str | None:
    """Where the opencode password lives, in order of trust:
    env, explicit file, the edge stack's .env. Never in config.yaml."""
    env = os.environ.get("OPENCODE_PASSWORD")
    if env:
        return env.strip()
    candidates = [str(cfg.get("opencode.password_file", "") or ""),
                 "~/homelab/edge/.env"]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_file():
            continue
        for line in path.read_text("utf-8").splitlines():
            if line.startswith("OPENCODE_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def decide_permission(request: dict[str, Any], cfg: Config) -> str:
    """The permission policy, pure: a live Permission.Request in, a decision
    out. Deny patterns are matched against the serialized request because a
    permission's danger lives in its command text, which rides in several
    fields at once."""
    blob = json.dumps(request, default=str).lower()
    for pat in (cfg.get("opencode.deny_patterns", None) or DENY_PATTERNS):
        if re.search(pat, blob):
            return "reject"
    mode = str(cfg.get("opencode.auto_permission", "once"))
    return mode if mode in ("once", "reject") else "reject"


def answer_form(form: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Answer an opencode form the way a careful janitor answers a
    clipboard: pick the listed option if there is one, say the safe
    sentence if there is not. `llm_answer` (a callable) may be injected to
    let the kitchen's own model phrase it; without one we never guess a
    blank into existence."""
    answer: dict[str, Any] = {}
    fields = form.get("fields") or []
    if isinstance(fields, dict):
        fields = [fields]
    for f in fields:
        name = str(f.get("name") or f.get("id") or "answer")
        opts = f.get("options") or []
        first = opts[0] if opts else None
        if isinstance(first, dict):
            answer[name] = first.get("value", first.get("id", "yes"))
        elif first is not None:
            answer[name] = first
        else:
            answer[name] = str(cfg.get("opencode.form_default_answer",
                                        "Proceed read-only; keep outputs in the workspace."))
    return answer


@dataclass
class DelegateResult:
    text: str = ""
    completed: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    first_text_s: float | None = None
    session_id: str = ""
    permissions_seen: int = 0
    permissions_granted: int = 0
    forms_answered: int = 0
    notes: list[str] = field(default_factory=list)


class OpenCodeBusy(Exception):
    """Another live delegation holds the tool. The scheduler's concurrency
    of 1 makes this a belt-and-braces guard, but the honest answer to two
    kitchens driving one opencode is one kitchen."""


class OpenCode:
    """One tool, one session at a time. All calls are to the loopback
    server over Basic auth, and every network primitive runs in a thread —
    the loop that serves SSE to the pantry is the same one that polls
    here."""

    def __init__(self, cfg: Config, *, emit: Callable[..., None],
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.emit = emit
        self.clock = clock
        self.base = str(cfg.get("opencode.base_url", DEFAULT_BASE)).rstrip("/")
        self._busy_session: str | None = None
        self._pending_interrupts: set = set()

    # --- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        pw = password_ladder(self.cfg) or ""
        tok = base64.b64encode(f"{USER}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def _call(self, method: str, path: str,
             body: dict[str, Any] | None = None,
             timeout: float = 30.0) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            with contextlib.suppress(Exception):
                detail = exc.read().decode("utf-8", "replace")[:200]
            raise OpenCodeError(f"{method} {path} -> {exc.code} {detail}") from exc
        except OSError as exc:
            # connection refused, DNS, timeouts: every transport failure is
            # reported as OpenCodeError so the stage fails with a cooker-
            # shaped message, never a raw socket letter in the events log.
            raise OpenCodeError(f"{method} {path}: {exc}") from exc

    @property
    def busy_session(self) -> str | None:
        """What the kitchen subtracts from the ledger's `active` while a
        delegation burns: one session, ours, by id we minted."""
        return self._busy_session

    def up(self) -> bool:
        try:
            self._call("GET", "/api/info", timeout=3.0)
            return True
        except Exception:
            return False

    # --- the loop ----------------------------------------------------------

    async def run(self, *, title: str, prompt: str, workdir: Path,
                  agent: str | None = None) -> DelegateResult:
        if self._busy_session:
            raise OpenCodeBusy(self._busy_session)
        workdir = Path(workdir)
        if not workdir.is_dir():
            # Verified live: the server accepts a session for a missing
            # directory and only refuses at prompt time with
            # LocationNotFoundError. Checking here makes the failure a
            # cooker-shaped message instead of a server-shaped surprise.
            raise OpenCodeError(f"delegation workdir does not exist: {workdir}")
        agent = agent or str(self.cfg.get("opencode.agent", "build"))
        poll = float(self.cfg.get("opencode.poll_seconds", 2.0))
        minutes = float(self.cfg.get("opencode.max_minutes", 12))
        res = DelegateResult()
        created = await asyncio.to_thread(
            self._call, "POST", "/api/session",
            {"location": {"directory": str(workdir)}, "title": title,
             "agent": agent})
        sid = str((created.get("data") or {}).get("id") or "")
        if not sid:
            raise OpenCodeError(f"session create returned no id: {created}")
        self._busy_session = sid
        res.session_id = sid
        self.emit("opencode.session",
                  f"delegated to opencode ({agent}): {title[:70]}",
                  {"session_id": sid, "directory": str(workdir)})
        t0 = self.clock()
        try:
            await asyncio.to_thread(
                self._call, "POST", f"/api/session/{sid}/prompt",
                {"text": prompt})
            seen_idle_before = 0
            while True:
                spent = self.clock() - t0
                if spent > minutes * 60:
                    res.notes.append(f"deadline after {spent:.0f}s")
                    await asyncio.to_thread(
                        self._call, "POST", f"/api/session/{sid}/interrupt", {})
                    break
                await asyncio.sleep(poll)
                await self._tend_permissions(sid, res)
                await self._tend_forms(sid, res)
                msgs = (await asyncio.to_thread(
                    self._call, "GET",
                    f"/api/session/{sid}/message", timeout=15.0)).get("data") or []
                idles = [m for m in msgs if m.get("type") == "idle"]
                if len(idles) > seen_idle_before:
                    res.completed = str(idles[-1].get("outcome")) == "succeeded"
                    res.notes.append(f"idle outcome="
                                    f"{idles[-1].get('outcome')}")
                    break
                texts = _assistant_text(msgs)
                if texts and res.first_text_s is None:
                    res.first_text_s = round(self.clock() - t0, 1)
            res.text = _assistant_text((await asyncio.to_thread(
                self._call, "GET", f"/api/session/{sid}/message",
                timeout=15.0)).get("data") or [])
            info = (await asyncio.to_thread(
                self._call, "GET", f"/api/session/{sid}")).get("data") or {}
            tok = info.get("tokens") or {}
            res.input_tokens = int(tok.get("input") or 0)
            res.output_tokens = int(tok.get("output") or 0)
        except asyncio.CancelledError:
            # The ledger said someone else wants the GPU. Our part of the
            # GPU is this session, so this session stops — now, in a thread,
            # fire-and-forget, because re-raising before the interrupt would
            # leave the burn running with nobody watching it.
            self.emit("opencode.abort",
                      f"interrupting delegated session {sid}",
                      {"session_id": sid})
            # Hold the handle: an unreferenced task is a GC-poofable
            # interrupt, and "we asked the GPU to stop" must not be a
            # maybe. The set keeps every fire visible until it lands.
            task = asyncio.create_task(asyncio.to_thread(
                self._interrupt_best_effort, sid))
            self._pending_interrupts.add(task)
            task.add_done_callback(self._pending_interrupts.discard)
            raise
        finally:
            self._busy_session = None
        return res

    def _interrupt_best_effort(self, sid: str) -> None:
        with contextlib.suppress(Exception):
            # the wire said stop; if the server is gone, the GPU agrees
            self._call("POST", f"/api/session/{sid}/interrupt", {}, timeout=5.0)

    async def _tend_permissions(self, sid: str, res: DelegateResult) -> None:
        try:
            reqs = (await asyncio.to_thread(
                self._call, "GET", f"/api/session/{sid}/permission",
                timeout=10.0)).get("data") or []
        except OpenCodeError:
            return
        for r in reqs:
            rid = str(r.get("id") or "")
            if not rid:
                continue
            res.permissions_seen += 1
            decision = decide_permission(r, self.cfg)
            try:
                await asyncio.to_thread(
                    self._call, "POST",
                    f"/api/session/{sid}/permission/{rid}/reply",
                    {"decision": decision}, timeout=10.0)
                if decision != "reject":
                    res.permissions_granted += 1
                self.emit("opencode.permission",
                          f"{decision} {str(r.get('action'))[:24]} "
                          f"{str(r.get('resources'))[:48]}",
                          {"session_id": sid, "request_id": rid,
                           "decision": decision})
            except OpenCodeError as exc:
                res.notes.append(f"permission reply failed: {exc}")

    async def _tend_forms(self, sid: str, res: DelegateResult) -> None:
        try:
            forms = (await asyncio.to_thread(
                self._call, "GET", f"/api/session/{sid}/form",
                timeout=10.0)).get("data") or []
        except OpenCodeError:
            return
        for f in forms:
            fid = str(f.get("id") or "")
            if not fid:
                continue
            answer = answer_form(f, self.cfg)
            try:
                await asyncio.to_thread(
                    self._call, "POST",
                    f"/api/session/{sid}/form/{fid}/reply",
                    {"answer": answer}, timeout=10.0)
                res.forms_answered += 1
                self.emit("opencode.form",
                          f"answered form {str(f.get('title'))[:40]}",
                          {"session_id": sid, "form_id": fid,
                           "answer": answer})
            except OpenCodeError as exc:
                res.notes.append(f"form reply failed: {exc}")


class OpenCodeError(Exception):
    pass


def _assistant_text(msgs: list[dict[str, Any]]) -> str:
    """The NEWEST assistant message's text parts, concatenated — and newest
    means by timestamp, not by list position, because `GET /message` on
    2.0.24 returns messages **newest-first** (verified live: the idle
    sentinel arrived at the head of the list). A collector that takes the
    last element of the list collects the oldest utterance, which is how
    the first real delegation "succeeded" with a 250-byte plate of
    *"I'll research this topic in parallel"* — conversation, not brief.
    Sorting by time makes the function correct against either order; the
    fake now matches the device. Reasoning parts stay skipped: what the
    model thought is not what it wrote."""
    best, best_ts = "", -1.0
    for m in msgs:
        if m.get("type") != "assistant":
            continue
        t = m.get("time") or {}
        ts = float(t.get("completed") or t.get("created") or 0)
        texts = [str(p.get("text")) for p in (m.get("content") or [])
                 if p.get("type") == "text" and p.get("text")]
        if texts and ts >= best_ts:
            best, best_ts = "\n\n".join(texts), ts
    return best.strip()
