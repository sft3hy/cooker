"""Ledger tests.

The payload fixture is the *real* `/admin/api/stats` shape captured off omlx
0.7.0 on this box (2026-10-07, bench15) — trimmed to the fields the detector
reads, unmodified in structure. The probe is tested against a threaded fake
server on loopback because the contract that matters is the HTTP one: login,
cookie, 401-relogin, endpoint stickiness, and never raising.

The fake server lives in its own thread on its own event loop; the probe is
synchronous by design (`asyncio.to_thread` lane in the sampler), so tests stay
synchronous too. No asyncio.run() over a live loop (landmine #5).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cooker.config import Config
from cooker.ledger import DEAD_LEDGER, LedgerProbe, parse_stats

# Captured live, omlx 0.7.0, the exact nesting the parser must survive.
CAPTURED_STATS = {
    "total_tokens_served": 203256603,
    "total_requests": 1426,
    "active_models": {
        "total_active_requests": 0,
        "total_waiting_requests": 0,
        "models": [{
            "id": "Qwen3.8-Flash-Next-oQ4e-mtp",
            "active_requests": 0,
            "waiting_requests": 0,
            "waiting": [],
            "activities": [],
            "prefilling": [],
            "generating": [],
            "idle_seconds": 37.80488300323486,
        }],
    },
}


def test_parse_stats_reads_the_captured_shape() -> None:
    v = parse_stats(CAPTURED_STATS, now=100.0, base="http://x", own=0)
    assert v.up and v.active == 0 and v.waiting == 0
    assert v.idle_s == 37.80488300323486, "the clock comes from the server, verbatim"
    assert v.total_requests == 1426
    assert not v.others_busy


def test_parse_stats_is_honest_about_missing_not_zero() -> None:
    """A payload with no models loaded has no idle clock. That is *unknown*,
    and unknown must not reach `detect` as a number it can gate on."""
    v = parse_stats({"active_models": {"models": []}}, now=5.0, base="b")
    assert v.up and v.idle_s is None and v.active == 0


def test_own_inflight_is_subtracted_but_the_queue_never_is() -> None:
    """Our own stage must not preempt itself; somebody else's queued request
    must not be forgiven just because we are also busy."""
    ours = parse_stats({**CAPTURED_STATS, "active_models": {
        "total_active_requests": 1, "total_waiting_requests": 0,
        "models": [{"idle_seconds": 0.2, "generating": ["a"], "prefilling": []}]}},
        now=1.0, base="b", own=1)
    assert not ours.others_busy, "active=own is the kitchen cooking"
    crowded = parse_stats({**CAPTURED_STATS, "active_models": {
        "total_active_requests": 1, "total_waiting_requests": 1,
        "models": [{"idle_seconds": 0.1, "generating": ["a"], "prefilling": []}]}},
        now=1.0, base="b", own=1)
    assert crowded.others_busy, "someone queued behind us is not our own backlog"


# --- the fake admin server ------------------------------------------------


class FakeAdmin:
    """A tiny /admin/api/stats server with a session cookie and a switch.

    `busy` flips what the ledger says, `require` flips auth. Everything else —
    stickiness, timeouts, cookie persistence — is exercised through this.
    """

    COOKIE = "omlx_admin_session=testcookie"

    def __init__(self) -> None:
        self.busy = False
        self.require = True
        self.logins = 0
        self.polls = 0
        self.drop_cookie_on = 0  # 401 this poll number, once
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if body.get("api_key") == "k":
                    outer.logins += 1
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"{outer.COOKIE}; HttpOnly; Path=/")
                    self.end_headers()
                    self.wfile.write(b'{"success":true}')
                else:
                    self.send_response(401)
                    self.end_headers()

            def do_GET(self) -> None:
                outer.polls += 1
                if outer.require and self.headers.get("Cookie") != outer.COOKIE:
                    self.send_response(401)
                    self.end_headers()
                    return
                if outer.drop_cookie_on == outer.polls:
                    self.send_response(401)
                    self.end_headers()
                    return
                stats = json.loads(json.dumps(CAPTURED_STATS))
                am = stats["active_models"]
                if outer.busy:
                    am["total_active_requests"] = 2
                    am["models"][0]["idle_seconds"] = 0.4
                    am["models"][0]["generating"] = ["r1", "r2"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(stats).encode())

        outer._expired = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def cfg(urls: list[str], **over: dict) -> Config:
    detect: dict = {
        "ledger": {
            "enabled": True,
            "url_candidates": urls,
            "ca_file": None,
            "admin_key_env": "COOKER_TEST_ADMIN_KEY",
            "admin_key_file": None,
            "interval_seconds": 1.0,
            "request_timeout_seconds": 2.0,
            "stale_seconds": 4.0,
            "infer_margin_seconds": 5.0,
        },
    }
    for k, v in over.items():
        detect["ledger"][k] = v
    return Config({"detect": detect}, Path("."))


def probe(fake: FakeAdmin, *, own: int = 0, clock=None) -> LedgerProbe:
    import os

    os.environ["COOKER_TEST_ADMIN_KEY"] = "k"
    return LedgerProbe(cfg([fake.base]), clock=clock, own_busy=lambda: own)


def test_poll_logs_in_once_and_reads_the_ledger() -> None:
    fake = FakeAdmin()
    try:
        p = probe(fake)
        v = p.poll()
        assert v.up and v.active == 0 and fake.logins == 1
        assert v.total_requests == 1426 and v.idle_s == 37.80488300323486
        assert not v.others_busy
        v2 = p.poll()
        assert v2.up and fake.logins == 1, "the cookie lives in the session jar"
        fake.busy = True
        v3 = p.poll()
        assert v3.others_busy and v3.active == 2, "live state, not boot-time state"
        p.aclose()
    finally:
        fake.close()


def test_a_dead_endpoint_walks_to_the_next_candidate_and_comes_back() -> None:
    fake = FakeAdmin()
    try:
        p = LedgerProbe(cfg(["http://127.0.0.1:1", fake.base]))
        import os

        os.environ["COOKER_TEST_ADMIN_KEY"] = "k"
        v = p.poll()
        assert v.up and v.base == fake.base, "the dead port must fall through"
        base = p._base
        p.poll()
        assert p._base == base, "a healthy endpoint is stickied, not flip-flopped"
        p.aclose()
    finally:
        fake.close()


def test_expired_session_relogins_once_then_reports_honestly() -> None:
    fake = FakeAdmin()
    fake.drop_cookie_on = 2  # the second poll 401s despite the cookie
    try:
        p = probe(fake)
        assert p.poll().up and fake.logins == 1
        v = p.poll()
        assert v.up and fake.logins == 2, "401 => one re-login, not a retry loop"
        p.aclose()
    finally:
        fake.close()


def test_unreachable_is_not_quiet() -> None:
    """up=False with the reason attached: the detector must fall back to bytes,
    and the silence must never read as 'the server says idle'."""
    p = LedgerProbe(cfg(["http://127.0.0.1:1"]))
    import os

    os.environ["COOKER_TEST_ADMIN_KEY"] = "k"
    v = p.poll()
    assert not v.up and v.error
    assert p.view() is DEAD_LEDGER or not p.view().up
    p.aclose()


def test_staleness_flips_up_without_erasing_numbers() -> None:
    fake = FakeAdmin()
    try:
        t = [1000.0]
        p = probe(fake, clock=lambda: t[0])
        assert p.poll().up
        fake.close()  # nothing answers now, but the last good numbers remain
        t[0] += 10.0
        v = p.poll()  # transport failure: last-known, flagged
        assert not v.up
        v2 = p.view()
        assert not v2.up and v2.total_requests == 1426, "numbers persist, freshness does not"
    finally:
        fake.close()


def test_no_credentials_reports_the_missing_key_not_a_zero() -> None:
    import os

    os.environ.pop("COOKER_TEST_ADMIN_KEY", None)
    fake = FakeAdmin()
    try:
        p = LedgerProbe(cfg([fake.base]))
        v = p.poll()
        assert not v.up and "COOKER_TEST_ADMIN_KEY" in (v.error or "")
    finally:
        fake.close()
