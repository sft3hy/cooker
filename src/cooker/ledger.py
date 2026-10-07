"""Ledger: what omlx says about its own bus — the question bytes could never answer.

Why this module exists
---------------------
For three milestones the wire was the trigger (bench #7, §16, §19) and the wire
was honest about one thing only: *somebody moved bytes*. §17 found the
corollary the design had been pretending away — a monitor moves bytes too — and
§17's follow-up buried the last hope of separating them by shape: byte size does
not separate poll from answer; only the source does.

The source turned out to be talking the whole time. Measured live
(`bench/bench15_ledger_vs_wire.py`, DISCOVERY §20):

    while `/admin/api/stats` polls hammered :8000 at 2/s (12.5 KB/s, forever):
        total_active_requests   0        idle_seconds  6.0 -> 37.8, monotonic
        total_requests          1426 -> 1426   (the metronome moved nothing)

    one real completion (8 tokens, 0.26s):
        total_requests          1426 -> 1427   idle_seconds reset to 1.1

The server keeps a ledger of *inference*, and admin traffic is not inference to
it. Polling that ledger adds bytes to the wire, which used to disqualify it —
but the pollution only matters while bytes are the judge. When the ledger is the
judge, a poll is a question, not an event: bytes demote to the preemption
tripwire they were always good at, and the ledger answers the question the meter
was built to guess: *is the model actually busy*.

What `idle_seconds` buys beyond counters: a generation that starts and finishes
between two polls still resets it, so the poll's own blind spot is plugged by
the server's memory. A metronome never resets it, whatever cadence it runs at —
cadence drifts (the §17 poll went 5s -> 2/s within a day without anyone
deciding to), and the ledger makes that irrelevant to the gate.

Nothing in here is load on the GPU. One small GET per interval, reused over one
TLS connection, served by the server's event loop in single-digit milliseconds.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class LedgerView:
    """The server's own account of its bus, as of the last successful read.

    `up` False means the read failed or went stale: then every other field is
    the *last known* value, not the current one, and `detect` must fall back to
    the wire and say so. The distinction between "the server is idle" and "we
    cannot ask the server" is the same quiet-vs-blind line the wire module draws.
    """

    ts: float = 0.0
    up: bool = False
    # Requests the server counts as being served right now, and queued behind.
    active: int = 0
    waiting: int = 0
    # Per-model live state, summed. These lists can miss a request shorter than
    # the poll gap (bench15 saw a 0.26s completion slip through them) —
    # `idle_s` is what catches those.
    generating: int = 0
    prefilling: int = 0
    # Seconds since this model last did real work — the server's own idle clock,
    # immune to monitor traffic. None until the first read, or when no model is
    # loaded (nothing can be busy, nothing has an idle clock to read).
    idle_s: float | None = None
    total_requests: int = 0
    # Our own in-flight requests, subtracted from `active` when anyone asks who
    # else is home. Cooker must not preempt itself reading its own ledger.
    own: int = 0
    base: str | None = None
    error: str | None = None

    @property
    def blind(self) -> bool:
        return not self.up

    @property
    def others_busy(self) -> bool:
        """Somebody other than us looks busy on the inference paths.

        Safety asymmetry in one line: we subtract our own in-flight count from
        `active` (so we do not preempt ourselves reading our own ledger) but
        never from `waiting` — a queue we cannot attribute has to be read as
        somebody waiting for us to move, not as our own backlog. `generating`
        is deliberately not in here: with one of ours in flight it would light
        too, and self-preemption is its own old bug; `idle_s` covers sub-poll
        generations that the counters miss.
        """
        return max(0, self.active - self.own) > 0 or self.waiting > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "up": self.up,
            "active": self.active,
            "waiting": self.waiting,
            "generating": self.generating,
            "prefilling": self.prefilling,
            "idle_s": round(self.idle_s, 1) if self.idle_s is not None else None,
            "total_requests": self.total_requests,
            "own": self.own,
            "base": self.base,
            "error": self.error,
        }


DEAD_LEDGER = LedgerView()


def parse_stats(payload: dict[str, Any], *, now: float, base: str,
               own: int = 0) -> LedgerView:
    """`/admin/api/stats` -> LedgerView, tolerating missing keys.

    The shape is omlx 0.7.0's (captured in bench15); a field that has moved is
    a missing observation, not a zero — `idle_s` of None means unknown, and
    `detect` treats unknown as something to fall back for, never as quiet.
    """
    am = payload.get("active_models") or {}
    models = am.get("models") or []
    idles = [m.get("idle_seconds") for m in models
             if isinstance(m.get("idle_seconds"), (int, float))]
    gen = sum(len(m.get("generating") or []) for m in models)
    pre = sum(len(m.get("prefilling") or []) for m in models)
    return LedgerView(
        ts=now, up=True,
        active=int(am.get("total_active_requests") or 0),
        waiting=int(am.get("total_waiting_requests") or 0),
        generating=gen, prefilling=pre,
        idle_s=min(idles) if idles else None,
        total_requests=int(payload.get("total_requests") or 0),
        own=own, base=base,
    )


class LedgerProbe:
    """One small GET per interval against omlx's admin API, answered from the
    server's live memory.

    Endpoints are tried in configured order and the working one is stickied
    until it fails twice in a row — switching back and forth across two healthy
    endpoints every poll is how a meter gets twitchy. The login cookie lives in
    the httpx session jar; a 401 re-logins once, then reports blindness rather
    than retrying into a loop (the admin session is omlx's to expire, ours to
    notice).
    """

    def __init__(
        self,
        cfg: Any = None,
        *,
        clock: Callable[[], float] = time.time,
        own_busy: Callable[[], int] | None = None,
    ) -> None:
        get = getattr(cfg, "get", lambda _k, d=None: d)
        self.clock = clock if clock is not None else time.time
        self.own_busy = own_busy or (lambda: 0)
        self.candidates: list[str] = [
            str(u).rstrip("/")
            for u in (get("detect.ledger.url_candidates", []) or [])
        ]
        self.ca_file = get("detect.ledger.ca_file", None)
        self.timeout = float(get("detect.ledger.request_timeout_seconds", 2.0))
        self.stale_after = float(get("detect.ledger.stale_seconds", 4.0))
        self.interval = float(get("detect.ledger.interval_seconds", 1.0))
        self.key_env = str(get("detect.ledger.admin_key_env", "AR_OMLX_ADMIN_KEY"))
        self.key_file = get("detect.ledger.admin_key_file", None)
        self._client: httpx.Client | None = None
        self._base: str | None = None
        self._fails = 0
        self._last = DEAD_LEDGER
        self._logged_in = False
        self._login_err: str | None = None

    # --- credentials -------------------------------------------------------

    def _admin_key(self) -> str | None:
        """Env first, then the autoresearch .env — the same ladder net.api_key
        climbs for the inference key, and the same reason: no secrets in the
        repo, no secrets in the plist."""
        env = os.environ.get(self.key_env)
        if env:
            return env.strip()
        if self.key_file:
            p = Path(str(self.key_file)).expanduser()
            with contextlib.suppress(OSError):
                for line in p.read_text().splitlines():
                    k, _, v = line.partition("=")
                    if k.strip() == self.key_env and v.strip():
                        return v.strip().strip("'\"")
        return None

    # --- plumbing ----------------------------------------------------------

    def _ctx(self) -> ssl.SSLContext | None:
        if not self.ca_file:
            return None
        ctx = ssl.create_default_context(cafile=str(Path(str(self.ca_file)).expanduser()))
        return ctx

    def _client_get(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                verify=self._ctx() if self.ca_file else False,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
            )
        return self._client

    def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None
        self._logged_in = False

    def _login(self, client: httpx.Client, base: str) -> bool:
        """Login never overwrites `_last`: a failed handshake is blindness with
        a reason, not an erasure of what we knew a second ago."""
        key = self._admin_key()
        if not key:
            self._login_err = f"no {self.key_env}"
            return False
        try:
            r = client.post(f"{base}/admin/api/login",
                            json={"api_key": key}, timeout=self.timeout)
        except httpx.HTTPError as exc:
            self._login_err = f"login {type(exc).__name__}"
            return False
        self._logged_in = r.status_code == 200
        if not self._logged_in:
            self._login_err = f"login {r.status_code}"
        return self._logged_in

    def _blind(self, now: float, why: str) -> LedgerView:
        """The last known numbers, flagged blind. A wall of zeros would be a
        lie; a wall of *old true* numbers honestly labelled blind is the
        detector's contract with itself."""
        if self._last.ts == 0.0:
            return LedgerView(ts=now, up=False, error=why)
        return dataclasses.replace(self._last, ts=now, up=False, error=why)

    # --- the poll ----------------------------------------------------------

    def poll(self) -> LedgerView:
        """Ask the server. Never raises: every failure ends as `up=False` with
        the reason attached, because a daemon that cannot ask goes back to the
        wire and says so, rather than inventing quiet. The last known numbers
        survive a failed read — the freshness flag changes, the data does not,
        so the UI can honestly say "idle 37s as far as we knew it, but blind".
        """
        now = self.clock()
        own = int(self.own_busy())
        if not self.candidates:
            return self._blind(now, "no ledger urls configured")
        client = self._client_get()
        order = ([self._base] if self._base else []) + [
            u for u in self.candidates if u != self._base
        ]
        err = self._login_err or "unreachable"
        for base in order:
            if not self._logged_in and not self._login(client, base):
                err = self._login_err or f"login failed on {base}"
                continue
            try:
                r = client.get(f"{base}/admin/api/stats", timeout=self.timeout)
                if r.status_code == 401:
                    self._logged_in = False
                    if not self._login(client, base):
                        err = self._login_err or f"relogin failed on {base}"
                        continue
                    r = client.get(f"{base}/admin/api/stats", timeout=self.timeout)
                if r.status_code != 200:
                    err = f"stats {r.status_code} on {base}"
                    continue
                view = parse_stats(r.json(), now=now, base=base, own=own)
                self._base, self._fails = base, 0
                self._last = view
                return view
            except httpx.HTTPError as exc:
                err = f"{type(exc).__name__} on {base}"
                continue
        self._fails += 1
        if self._fails >= 2:
            self._base = None  # the stickied route is dead; walk the list again
        return self._blind(now, err)

    def view(self) -> LedgerView:
        """What we last learned, stale-flagged. Same quiet-vs-blind contract as
        the wire's `snapshot`: numbers may be true, but nobody starts work on
        them if they are too old. `replace` rather than a dict rebuild: a
        rebuilt snapshot can silently lose the timestamp that made it fresh."""
        snap = self._last
        if not snap.up:
            return snap
        if snap.ts == 0.0 or self.clock() - snap.ts > self.stale_after:
            return dataclasses.replace(snap, up=False,
                                       error=f"stale {self.clock() - snap.ts:.0f}s")
        return snap
