"""The delegation tool (M9): opencode as Cooker's hands.

Two layers are tested. The pure policy (decide/answer/ladder) needs nothing.
The loop runs against a scripted fake of the live :4096 server — every
response shape copied from the device probe of 2026-10-07 (§21), including
the one that burned a guess: permission replies take `decision`, not
`response`, and a fake that accepted either would hide a client that
silently rejects everything in the real world.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cooker import opencode
from cooker.config import Config

PW = "probe-password"


def cfg(tmp_path: Path, **over: object) -> Config:
    base: dict = {
        "daemon": {"data_dir": str(tmp_path), "bind_host": "127.0.0.1",
                   "port": 8256},
        "safety": {"outputs_dir": str(tmp_path / "outputs"), "max_disk_gb": 2.0},
        "quality": {"publish_threshold": 3.5},
        "generators": {"deep-dive": {"enabled": True, "weight": 1}},
        "topics": {"fallback": ["x"]},
        "detect": {}, "scheduler": {"max_concurrency": 1}, "ui": {},
        "orders": {"max_pending": 3},
        "opencode": {"enabled": True, "base_url": "http://127.0.0.1:4099",
                    "agent": "build", "poll_seconds": 0.02,
                    "max_minutes": 0.02, "auto_permission": "once"},
    }
    # every override lands in the opencode section — silently dropping one
    # (like base_url) means the client quietly aims at the REAL server
    base["opencode"].update(over)
    return Config(base, tmp_path)


# --- pure policy ----------------------------------------------------------

def test_ladder_env_then_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCODE_PASSWORD", " from-env ")
    assert opencode.password_ladder(cfg(tmp_path)) == "from-env"
    monkeypatch.delenv("OPENCODE_PASSWORD")
    f = tmp_path / "edge.env"
    f.write_text('OTHER=1\nOPENCODE_PASSWORD="from-file"\n')
    assert opencode.password_ladder(cfg(tmp_path, password_file=str(f))) == "from-file"


def test_deny_beats_auto_once_even_when_auto_is_generous() -> None:
    c = cfg(Path("/unused"), auto_permission="once")
    danger = {"action": "bash", "resources": ["rm -rf /tmp/anything"]}
    assert opencode.decide_permission(danger, c) == "reject"
    keyreach = {"action": "read", "resources": ["~/homelab/edge/.env"]}
    assert opencode.decide_permission(keyreach, c) == "reject"
    harmless = {"action": "read", "resources": ["README.md"]}
    assert opencode.decide_permission(harmless, c) == "once"


def test_always_is_not_an_option() -> None:
    """`always` saves a permission past the stage that earned it. The code
    refuses the mode itself, whatever the config says."""
    c = cfg(Path("/unused"), auto_permission="always")
    assert opencode.decide_permission(
        {"action": "read", "resources": ["a.md"]}, c) == "reject"


def test_forms_are_answered_with_options_or_the_safe_sentence() -> None:
    c = cfg(Path("/unused"))
    a = opencode.answer_form({"fields": [{"name": "q",
                                           "options": [{"value": "yes"}, "no"]}]}, c)
    assert a["q"] == "yes"
    b = opencode.answer_form({"fields": [{"name": "q"}]}, c)
    assert "read-only" in b["q"]


# --- the loop, against a scripted fake of the live server ----------------

class FakeOCServer:
    """Answers exactly like the 2.0.24 build on this box: `{data: ...}`
    envelopes, messages with top-level typed items, an idle sentinel with an
    outcome, and permission requests that vanish once replied."""

    def __init__(self, hang: bool = False) -> None:
        # hang=True never reports idle: it is how a real minutes-long agent
        # turn looks from the outside, and what cancel/deadline are for.
        self.hang = hang
        self.replies: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        self.prompts: list[str] = []
        self.created: list[dict] = []
        self.basic_seen: str | None = None
        self.round = 0
        self.answered: set[str] = set()
        self.interrupted = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def authed(self) -> bool:
                outer.basic_seen = self.headers.get("Authorization")
                want = ("Basic " + base64.b64encode(f"opencode:{PW}".encode())
                      .decode())
                return self.headers.get("Authorization") == want

            def send(self, obj: object, code: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not self.authed():
                    return self.send({"_tag": "UnauthorizedError"}, 401)
                p = self.path
                if p == "/api/info":
                    return self.send({"version": "2.0.24"})
                if p == "/api/session/active":
                    if outer.hang:
                        return self.send({"data": {"ses_fake":
                                                  {"type": "running"}}})
                    if outer.interrupted or outer.round >= 2:
                        return self.send({"data": {}})
                    return self.send({"data": {"ses_fake": {"type": "running"}}})
                if p == "/api/session/ses_fake":
                    return self.send({"data": {
                        "id": "ses_fake", "title": "t",
                        "tokens": {"input": 100, "output": 7,
                                   "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}}}})
                if p.endswith("/message"):
                    if outer.round < 1 or outer.hang:
                        return self.send({"data": []})
                    return self.send({"data": [
                        {"id": "msg_u", "type": "user", "text": "…"},
                        {"id": "msg_a", "type": "assistant", "agent": "build",
                         "content": [{"type": "reasoning", "text": "hmm"},
                                     {"type": "text",
                                      "text": "# brief\n\nreal content."}],
                         "tokens": {"input": 100, "output": 7}},
                        {"id": "msg_i", "type": "idle",
                         "outcome": "succeeded"},
                    ]})
                if p.endswith("/permission"):
                    rows = [{"id": "per_safe", "sessionID": "ses_fake",
                            "action": "read", "resources": ["docs/x.md"]},
                           {"id": "per_rm", "sessionID": "ses_fake",
                            "action": "bash", "resources": ["rm -rf out/"]}]
                    return self.send({"data": [r for r in rows
                                               if r["id"] not in outer.answered]})
                if p.endswith("/form"):
                    return self.send({"data": []})
                return self.send({"_tag": "NotFound"}, 404)

            def do_POST(self) -> None:
                if not self.authed():
                    return self.send({"_tag": "UnauthorizedError"}, 401)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                p = self.path
                if p == "/api/session":
                    outer.created.append(body)
                    return self.send({"data": {"id": "ses_fake"}})
                if p.endswith("/prompt"):
                    outer.prompts.append(body.get("text", ""))
                    outer.round = 1
                    return self.send({"data": {"id": "msg_u"}})
                if "/permission/" in p and p.endswith("/reply"):
                    rid = p.split("/permission/")[1].split("/")[0]
                    outer.answered.add(rid)
                    outer.replies.append((rid, body.get("decision", "?")))
                    return self.send({"data": {}})
                if p.endswith("/interrupt"):
                    outer.interrupted = True
                    outer.interrupts.append(p)
                    return self.send({"data": {"aborted": True}})
                return self.send({"_tag": "NotFound"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def fake():
    srv = FakeOCServer()
    yield srv
    srv.stop()


@pytest.fixture
def fake_hang():
    srv = FakeOCServer(hang=True)
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def _pw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_PASSWORD", PW)


def test_loop_grants_safe_rejects_dangerous_and_delivers_the_brief(
        tmp_path: Path, fake: FakeOCServer) -> None:
    c = cfg(tmp_path, base_url=fake.url)
    oc = opencode.OpenCode(c, emit=lambda *a, **k: None)
    wd = tmp_path / "wd"
    wd.mkdir()
    res = asyncio.run(oc.run(title="deep-dive: x", prompt="dig", workdir=wd))
    assert fake.created[0]["location"]["directory"] == str(wd)
    assert fake.basic_seen and fake.basic_seen.startswith("Basic ")
    assert res.completed and res.text.startswith("# brief")
    assert res.input_tokens == 100 and res.output_tokens == 7
    got = dict(fake.replies)
    assert got.get("per_safe") == "once", "safe reads are granted, once"
    assert got.get("per_rm") == "reject", "the deny list outranks the grant"
    assert oc.busy_session is None, "the tool releases the busy flag, always"


def test_workdir_must_exist_before_the_prompt(tmp_path: Path) -> None:
    oc = opencode.OpenCode(cfg(tmp_path), emit=lambda *a, **k: None)
    with pytest.raises(opencode.OpenCodeError, match="does not exist"):
        asyncio.run(oc.run(title="t", prompt="p", workdir=tmp_path / "gone"))


def test_cancellation_interrupts_the_session(tmp_path: Path,
                                              fake_hang: FakeOCServer) -> None:
    c = cfg(tmp_path, base_url=fake_hang.url, poll_seconds=0.05,
            max_minutes=5)
    oc = opencode.OpenCode(c, emit=lambda *a, **k: None)

    async def scene() -> None:
        task = asyncio.create_task(oc.run(title="t", prompt="p",
                                           workdir=tmp_path))
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scene())
    time.sleep(0.2)  # the best-effort interrupt lands on its own thread
    assert fake_hang.interrupts, \
        "a cancelled delegation must interrupt its session"
    assert oc.busy_session is None


def test_blind_server_fails_the_stage_not_the_loop(tmp_path: Path) -> None:
    oc = opencode.OpenCode(cfg(tmp_path, base_url="http://127.0.0.1:1"),
                           emit=lambda *a, **k: None)
    assert oc.up() is False
    wd = tmp_path / "wd"
    wd.mkdir()
    with pytest.raises(opencode.OpenCodeError):
        asyncio.run(oc.run(title="t", prompt="p", workdir=wd))
    assert oc.busy_session is None


def test_deadline_is_a_hard_stop(tmp_path: Path,
                                fake_hang: FakeOCServer) -> None:
    """The fake never reports the round idle until the prompt lands, so a
    deadline under the poll still returns control and releases the tool."""
    c = cfg(tmp_path, base_url=fake_hang.url, poll_seconds=0.02,
            max_minutes=0.001)
    oc = opencode.OpenCode(c, emit=lambda *a, **k: None)
    res = asyncio.run(oc.run(title="t", prompt="p", workdir=tmp_path))
    assert oc.busy_session is None
    assert res.session_id == "ses_fake"
    assert fake_hang.interrupted and any("deadline" in n for n in res.notes)
