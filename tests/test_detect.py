"""Detection tests.

The fixtures are *real* lsof output captured off this Mac on 2026-10-06, including
the stray `http.server` that shadows omlx on loopback. That shadow is the whole
reason `classify()` exists, so it is tested as a first-class case, not a footnote.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cooker import db
from cooker.config import Config
from cooker.detect import (
    ACTIVE_INFER,
    ACTIVE_USER,
    IDLE,
    Detector,
    Peer,
    Signals,
    SockView,
    _age_then,
    _split,
    classify,
    parse_conns,
    peer_summary,
)
from cooker.wire import DEAD, Wire

PORT = 8000
SERVER = 21409          # the omlx pid the fake sockets report as listening
WINDOW = 2.0            # the wire's window, matching detect.wire_window_seconds

# Captured live: OrbStack (Traefik) and opencode are dialling omlx over the
# Tailscale address; omlx holds *:8000; a stray http.server holds 127.0.0.1.
LSOF_LIVE = """p18634
cOrbStack Helper
f218
n100.122.197.81:59227->100.122.197.81:8000
f348
n100.122.197.81:58541->100.122.197.81:8000
p21409
cPython
f4
n*:8000
f10
n100.122.197.81:8000->100.122.197.81:58541
f11
n100.122.197.81:8000->100.122.197.81:58655
p74595
cPython
f3
n127.0.0.1:8000
p85164
copencode
f14
n100.122.197.81:58655->100.122.197.81:8000
"""

ARGV = {
    21409: "/opt/homebrew/opt/python@3.11/bin/python3.11 /opt/homebrew/bin/omlx-server serve",
    74595: "python3 -m http.server 8000 --bind 127.0.0.1",
    18634: "/Applications/OrbStack.app/Contents/MacOS/OrbStack Helper",
    85164: "/opt/homebrew/bin/opencode",
}

NAMES = ("omlx", "omlx-server")


def argv(pid: int) -> str:
    return ARGV.get(pid, "")


# --- parsing ------------------------------------------------------------


def test_parse_conns_sees_listen_and_established_in_one_call() -> None:
    conns = parse_conns(LSOF_LIVE)
    listeners = [c for c in conns if not c.remote]
    established = [c for c in conns if c.remote]
    assert {c.local for c in listeners} == {"*:8000", "127.0.0.1:8000"}
    # OrbStack dials twice, opencode once, omlx answers twice; the 4th listen
    # row is the stray http.server.
    assert len(established) == 5


def test_split_handles_wildcard_ipv4_and_ipv6() -> None:
    assert _split("*:8000") == ("*", 8000)
    assert _split("[::1]:8000") == ("[::1]", 8000)
    assert _split("nope") == ("nope", None)
    assert _split("host:notaport") == ("host", None)


def test_age_then_never_goes_negative() -> None:
    """A wall clock that steps backwards (NTP, or wake from sleep) must not make
    a cooldown last forever. This is the bug that printed
    `next_runnable_in_s: -1791353871.1`."""
    assert _age_then(100.0, 160.0) == 60.0
    assert _age_then(200.0, 160.0) == 0.0
    assert _age_then(None, 160.0) is None


# --- classification -----------------------------------------------------


def test_classify_names_the_server_and_flags_the_shadow() -> None:
    view = classify(parse_conns(LSOF_LIVE), port=PORT, server_names=NAMES, argv_of=argv)
    assert view.server_pids == (21409,)
    assert view.omlx_up
    assert "127.0.0.1" in view.shadow_addrs, "the http.server bind is a shadow"


def test_loopback_peers_belong_to_the_shadow_not_to_omlx() -> None:
    """A client dialling 127.0.0.1:8000 is talking to the stray http.server,
    which answers 404. Counting it as inference would put the kitchen into a
    preemption that is not happening."""
    with_curl = LSOF_LIVE + "p999\nccurl\nf3\nn127.0.0.1:51560->127.0.0.1:8000\n"
    view = classify(parse_conns(with_curl), port=PORT, server_names=NAMES, argv_of=argv)
    assert "curl" not in [c.command for c in view.clients]


def test_loopback_counts_as_inference_when_nothing_shadows_it() -> None:
    """The mirror case: with the shadow gone, loopback traffic IS inference and
    must not be dropped. Both directions are tested or the filter is a guess."""
    clean = LSOF_LIVE.replace("p74595\ncPython\nf3\nn127.0.0.1:8000\n", "")
    with_curl = clean + "p999\nccurl\nf3\nn127.0.0.1:51560->127.0.0.1:8000\n"
    view = classify(parse_conns(with_curl), port=PORT, server_names=NAMES, argv_of=argv)
    assert not view.shadow_addrs
    assert "curl" in [c.command for c in view.clients]


def test_our_own_connections_are_never_inference() -> None:
    """Cooker's own httpx sockets must not count, or the first background request
    would preempt itself, forever, in a tight loop."""
    ours = LSOF_LIVE.replace("p85164", "p4242").replace("copencode", "cooker")
    view = classify(
        parse_conns(ours), port=PORT, server_names=NAMES, our_pid=4242, argv_of=argv
    )
    assert "cooker" not in [c.command for c in view.clients]
    assert {c.command for c in view.clients} == {"OrbStack Helper"}


def test_an_unnamed_listener_is_not_assumed_to_be_omlx() -> None:
    """lsof's command column says 'Python' for both processes; only argv tells
    them apart. If argv comes back empty we must not crown it."""
    assert classify([], port=PORT, server_names=NAMES, argv_of=lambda _p: "") == SockView()
    assert not classify([], port=PORT, server_names=NAMES, argv_of=argv).omlx_up


def test_hid_regex_reads_the_real_ioreg_shape() -> None:
    from cooker.detect import _HID_RE

    out = '  |   "HIDIdleTime" = 37462416\n  |   "HIDIdleTime" = 999\n'
    vals = [int(m) for m in _HID_RE.findall(out)]
    assert min(vals) / 1e9 == pytest.approx(999e-9), "most recent activity wins"


# --- the state machine --------------------------------------------------


class Scripted:
    """A sampler that replays canned signals, so the state machine can be tested
    at a speed that is not 'sixty seconds per transition'."""

    def __init__(self) -> None:
        self.script: list[Signals] = []

    def push(self, sig: Signals) -> None:
        self.script.append(sig)

    async def sample(self, prev: Signals | None = None) -> Signals:
        return self.script.pop(0) if self.script else (prev or Signals())


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def up(sig: Signals, *, wire: bool = True) -> Signals:
    """Mark omlx as listening and the wire as watched, without overwriting the
    clients the test set.

    `wire=True` is the default because the detector normally can see the wire.
    A test that cares about not being able to see says `wire=False`, which is the
    only way 'blind' and 'quiet' stay different things in here as well as out
    there.
    """
    if sig.socks is None:
        sig.socks = SockView(server_pids=(SERVER,), listening=True)
    if sig.wire.up is False:
        sig.wire = (DEAD if not wire else
                   Wire(moved={}, window_s=WINDOW, ts=sig.ts, up=True, samples=2))
    return sig


def connected(*peers: Peer) -> SockView:
    """A listening omlx with these peers holding sockets open.

    Defaults to one silent OpenCode, because that is the real state of this box
    most of the day and the case that used to freeze the kitchen forever.
    """
    return SockView(server_pids=(SERVER,), listening=True,
                    clients=peers or (Peer(85164, "opencode"),))


def serving(sig: Signals, bps: float = 5376.0, pid: int = SERVER) -> Signals:
    """The inference server itself moving bytes: the trigger for ACTIVE_INFER.

    Stated separately from `up` on purpose. 'Someone is connected' and 'the
    server is answering' are the two claims the detector keeps apart, so the
    test helpers should not be able to say the first while meaning the second.
    5,376 B/s is measured, not chosen (bench #7: 10,752 bytes in a 2s window).
    """
    sig.wire = Wire(moved={pid: int(bps * WINDOW)}, window_s=WINDOW, ts=sig.ts,
                    up=True, samples=2)
    return sig


def cooking(pid: int = 85164, command: str = "opencode", bps: float = 6683.0) -> Peer:
    """A peer connected and moving bytes, for the `cooking` column in a snapshot.

    6,683 is measured rather than rounded (bench #7) because a test full of round
    numbers proves arithmetic and not calibration.
    """
    return Peer(pid, command, bps)


def cfg(**over: object) -> Config:
    base: dict = {
        "detect": {
            "interval_seconds": 1.0,
            "omlx_port": PORT,
            "omlx_proc_names": list(NAMES),
            "user_idle_seconds": 120,
            "opencode_wal_seconds": 30,
            "fs_activity_seconds": 60,
            "interactive_cooldown_seconds": 60,
            "idle_confirm_seconds": 60,
            "infer_min_bps": 256,
            "wire_interval_seconds": 1.0,
            "wire_window_seconds": 3.0,
        },
        "scheduler": {"max_concurrency": 1},
    }
    for key, value in over.items():
        if key in base["detect"]:
            base["detect"][key] = value
        elif key in base["scheduler"]:
            base["scheduler"][key] = value
        else:
            raise KeyError(f"not a knob in detect or scheduler: {key}")
    return Config(base, Path("."))


def make(cfg: Config, conn, scripted: Scripted, clock: Clock) -> Detector:
    return Detector(cfg, conn, sampler=scripted, clock=clock)


@pytest.mark.asyncio
async def test_inference_on_the_socket_is_active_immediately() -> None:
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(serving(up(Signals(ts=clk.t, socks=connected(cooking())))))
    state = await d.tick()
    assert state.state == ACTIVE_INFER
    assert "omlx serving" in state.reason, state.reason
    assert not state.ready
    assert state.cooking == ("opencode(85164)",)


@pytest.mark.asyncio
async def test_a_keepalive_is_not_inference_and_the_kitchen_still_lights() -> None:
    """The bug this test exists to keep fixed.

    OpenCode and Traefik hold an ESTABLISHED connection to :8000 for hours at
    zero throughput. If a socket were the trigger for ACTIVE_INFER the kitchen
    would never light and `cooker` would be a daemon that only ever says no. So:
    connected, silent, wire healthy => IDLE, and the reason says 'keepalive'
    rather than pretending nobody is home.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    quiet = Peer(85164, "opencode", 0.0)
    sc.push(up(Signals(ts=clk.t, socks=connected(quiet))))
    state = await d.tick()
    assert state.state == IDLE, "an open socket with no bytes is not a generation"
    assert state.reason == "keepalive: opencode", state.reason
    assert state.clients == ("opencode(85164)",)
    assert state.cooking == (), "nobody is generating"
    clk.advance(61)
    sc.push(up(Signals(ts=clk.t, socks=SockView(
        server_pids=(21409,), listening=True, clients=(quiet,)))))
    state = await d.tick()
    assert state.ready, "the keepalive must not starve the kitchen forever"


@pytest.mark.asyncio
async def test_blind_wire_stops_work_without_calling_it_inference() -> None:
    """No throughput data is not the same as no traffic.

    If nettop dies, a connected peer could be generating or idle and we cannot
    tell. ACTIVE_USER is the state that means 'finish what you started, start
    nothing new', which is exactly the honest answer; ACTIVE_INFER would be a
    guess, and IDLE would be a dangerous one.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(up(Signals(ts=clk.t, socks=connected(Peer(85164, "opencode", 0.0))),
               wire=False))
    state = await d.tick()
    assert state.state == ACTIVE_USER, state.state
    assert "blind" in state.reason
    assert not state.ready
    assert not state.wire_up


@pytest.mark.asyncio
async def test_the_floor_separates_a_keepalive_from_a_request() -> None:
    """Calibration, not vibes: the floor sits between measured silence and a
    measured stream (0 and 6,683 B/s), so it has room on both sides."""
    c, clk, sc = cfg(infer_min_bps=256), Clock(), Scripted()
    d = make(c, None, sc, clk)
    for bps, want in ((0.0, IDLE), (128.0, IDLE), (300.0, ACTIVE_INFER)):
        clk.advance(120)
        sig = up(Signals(ts=clk.t, socks=connected()), wire=False)
        sc.push(serving(sig, bps=bps))
        state = await d.tick()
        assert state.state == want, f"{bps} B/s read as {state.state}"


@pytest.mark.asyncio
async def test_many_peers_are_named_compactly_and_without_lies() -> None:
    """Three opencode processes and a Traefik must fit on an iPhone.

    Cutting a pid mid-character to save width is worse than the long line: it
    sends you looking for a process that does not exist.
    """
    peers = (cooking(), cooking(23788), cooking(85165), Peer(18634, "OrbStack", 900.0))
    assert peer_summary(peers) == "opencode x3, OrbStack"
    assert peer_summary((cooking(),)) == "opencode"
    assert peer_summary(()) == "unknown"


@pytest.mark.asyncio
async def test_keystrokes_are_busy_but_not_preempting() -> None:
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=4.0)))
    state = await d.tick()
    assert state.state == ACTIVE_USER
    assert state.reason == "hid 4s"
    assert not state.ready


@pytest.mark.asyncio
async def test_idle_takes_idle_confirm_before_it_says_ready() -> None:
    """No hysteresis here and the kitchen starts the instant you stop typing,
    which is the definition of a noisy neighbour."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=5.0)))
    await d.tick()
    assert d.state == ACTIVE_USER

    sc.push(up(Signals(ts=clk.t, hid_idle_s=400.0)))
    state = await d.tick()
    assert state.state == IDLE
    assert not state.ready, "quiet is not the same as proven quiet"
    assert any("idle confirm" in b for b in state.blockers)

    clk.advance(59)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=459.0)))
    state = await d.tick()
    assert not state.ready, "one second short"

    clk.advance(2)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=461.0)))
    state = await d.tick()
    assert state.ready, f"should be ready, blockers={state.blockers}"


@pytest.mark.asyncio
async def test_cooldown_runs_from_when_we_saw_the_socket_clear() -> None:
    """Two clocks, and the conservative reading of each.

    The cooldown starts when we *observe* inference stop, not when we last saw it
    running: between those two ticks we have no idea what happened, and guessing
    'it stopped at the last thing I saw' is a 1-second bet with someone else's
    latency on the other side of it. idle_confirm then runs from the same moment.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(serving(up(Signals(ts=clk.t, socks=connected(cooking())))))
    await d.tick()
    assert d.state == ACTIVE_INFER

    clk.advance(10)
    sc.push(up(Signals(ts=clk.t)))
    state = await d.tick()
    assert state.state == IDLE, "the socket is clear"
    assert not state.ready
    assert "cooldown 60s" in state.blockers, state.blockers
    assert "idle confirm 60s" in state.blockers, state.blockers

    clk.advance(59)
    sc.push(up(Signals(ts=clk.t)))
    state = await d.tick()
    assert not state.ready, "one second short of both clocks"
    assert any(b.startswith("cooldown ") for b in state.blockers)

    clk.advance(2)
    sc.push(up(Signals(ts=clk.t)))
    state = await d.tick()
    assert state.ready, f"both clocks spent, got {state.blockers}"


@pytest.mark.asyncio
async def test_a_failed_socket_probe_is_blind_not_quiet() -> None:
    """lsof timing out must not read as 'nobody is generating'."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(Signals(ts=clk.t, failed=("sockets",)))
    state = await d.tick()
    assert state.state == ACTIVE_USER
    assert not state.ready
    assert any("blind" in b for b in state.blockers)


@pytest.mark.asyncio
async def test_omlx_down_is_not_a_reason_to_start_cooking() -> None:
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(Signals(ts=clk.t, socks=SockView(listening=False)))
    state = await d.tick()
    assert not state.ready
    assert any("omlx offline" in b for b in state.blockers)


@pytest.mark.asyncio
async def test_opencode_wal_and_fs_activity_also_mean_somebody_is_here() -> None:
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(up(Signals(ts=clk.t, oc_wal_age_s=2.0)))
    state = await d.tick()
    assert state.state == ACTIVE_USER and "opencode.db-wal" in state.reason

    sc.push(up(Signals(ts=clk.t, fs_age_s=10.0)))
    state = await d.tick()
    assert state.state == ACTIVE_USER and "fs 10s" in state.reason


@pytest.mark.asyncio
async def test_transitions_are_written_to_the_events_table() -> None:
    conn = db.connect(":memory:")
    db.migrate(conn)
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, conn, sc, clk)
    sc.push(up(Signals(ts=clk.t)))
    await d.tick()
    sc.push(serving(up(Signals(ts=clk.t, socks=connected(cooking())))))
    await d.tick()
    rows = db.tail_events(conn)
    types = [r["type"] for r in rows]
    assert "bus.state" in types
    hit = [r for r in rows if r["type"] == "bus.state"][-1]
    assert '"to":"ACTIVE_INFER"' in (hit["data_json"] or "")
    assert hit["severity"] == "warn", "a preemption should look like one"
    conn.close()


@pytest.mark.asyncio
async def test_live_sampler_reads_the_real_machine() -> None:
    """Integration, but cheap: proves the incantations still work on this macOS.
    `ioreg -k` and `sysctl pcblist_json` both looked reasonable and both failed,
    so this is the guard against a silent 'everything is idle' regression."""
    from cooker.detect import LiveSampler

    sampler = LiveSampler(cfg(), clock=time.time)
    sig = await sampler.sample()
    assert sig.hid_idle_s is not None, "HID idle must be readable"
    assert sig.hid_idle_s >= 0
    assert sig.socks is not None
    assert sig.socks.listening, "something listens on :8000 on this box"
    assert "sockets" not in sig.failed
    assert sig.cost_ms, "probe cost must be tracked, it is a budgeted resource"
