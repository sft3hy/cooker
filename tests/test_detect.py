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
from cooker.ledger import LedgerView
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
            "claim_quiet_seconds": 4.0,
            "idle_confirm_seconds": 60,
            "infer_min_bps": 256,
            "wire_interval_seconds": 1.0,
            "wire_window_seconds": 3.0,
            "ledger": {
                "enabled": True,
                "url_candidates": [],
                "ca_file": None,
                "admin_key_env": "COOKER_TEST_ADMIN_KEY",
                "admin_key_file": None,
                "interval_seconds": 1.0,
                "request_timeout_seconds": 2.0,
                "stale_seconds": 4.0,
                "infer_margin_seconds": 5.0,
            },
        },
        "scheduler": {"max_concurrency": 1},
    }
    for key, value in over.items():
        if key in base["detect"]:
            base["detect"][key] = value
        elif key.startswith("ledger."):
            base["detect"]["ledger"][key.split(".", 1)[1]] = value
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
async def test_idle_needs_a_measured_pause_before_it_says_ready() -> None:
    """Hysteresis, with the number taken from the machine instead of guessed.

    Without hysteresis the kitchen starts the instant you stop typing, which is the
    definition of a noisy neighbour. With sixty seconds of it the kitchen never
    starts at all: measured during a live agent session, the gaps between bursts are
    2-5s (DISCOVERY §16), so a 60s gate refused every tick of a trace in which 33%
    of the time nothing was being served.

    So the claim gate is `claim_quiet_seconds` - four seconds, past the typical next
    burst and the shortest interval a 3s window polled every 2s could honestly report -
    while `idle_confirm_seconds` keeps its original meaning as the gate on
    *concurrency*, which is the request that shares a generation's throughput rather
    than queueing behind it.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=5.0)))
    await d.tick()
    assert d.state == ACTIVE_USER

    sc.push(up(Signals(ts=clk.t, hid_idle_s=400.0)))
    state = await d.tick()
    assert state.state == IDLE
    assert not state.ready, "quiet is not the same as proven quiet"
    assert any("quiet 4s" in b for b in state.blockers), state.blockers
    assert state.quiet_s == 0.0

    clk.advance(3)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=440.0)))
    state = await d.tick()
    assert not state.ready, "three seconds is still inside the typical next burst"

    clk.advance(1)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=459.0)))
    state = await d.tick()
    assert state.ready, "four measured seconds of quiet earns one bounded stage"
    assert not state.ramped, "and nowhere near enough for four"
    assert "idle confirm" in state.ramp_note, state.ramp_note

    clk.advance(57)
    sc.push(up(Signals(ts=clk.t, hid_idle_s=516.0)))
    state = await d.tick()
    assert state.ready and state.ramped, "a minute of quiet earns the ladder"


@pytest.mark.asyncio
async def test_cooldown_gates_the_ramp_and_not_the_first_pot() -> None:
    """Two clocks, now answering two different questions.

    The cooldown starts when we *observe* inference stop, not when we last saw it
    running: between those ticks we have no idea what happened, and guessing "it
    stopped at the last thing I saw" is a one-second bet with someone else's latency
    on the other side. What the measurement changed is what that bet stakes. A
    bounded stage started too early costs a measured +95ms median and +200ms when
    fired into a live decode, so the cooldown no longer withholds the first pot.
    Concurrency genuinely does share a decode, so the cooldown still withholds that.
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
    assert not state.ready, "the pause has only just begun"
    assert "cooldown 60s" in state.ramp_note, state.ramp_note
    assert "idle confirm 60s" in state.ramp_note, state.ramp_note

    clk.advance(4)
    sc.push(up(Signals(ts=clk.t)))
    state = await d.tick()
    assert state.ready, "one bounded stage fits, and the collision is cheap"
    assert not state.ramped, "two pots still wait for the cooldown"
    assert "cooldown" in state.ramp_note, state.ramp_note

    clk.advance(57)
    sc.push(up(Signals(ts=clk.t)))
    state = await d.tick()
    assert state.ready and state.ramped, f"both clocks spent, got {state.ramp_note}"


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


@pytest.mark.asyncio
async def test_quiet_streaks_are_recorded_as_burst_structure() -> None:
    """The gap distribution, kept, because one threshold cannot describe it.

    `cooker status` should be able to say "this machine pauses for two seconds at a
    time" rather than "not ready", and stage sizing eventually needs the same number:
    a stage that cannot finish inside the typical gap should not be claimed at all,
    because a preempted stage pays for its prompt and delivers nothing. A streak is
    retired the moment traffic resumes, which is the only instant at which its
    length is known.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    assert d.typical_gap_s is None, "no history yet, so no opinion"

    sc.push(up(Signals(ts=clk.t)))
    await d.tick()                      # quiet begins
    clk.advance(5)
    sc.push(serving(up(Signals(ts=clk.t, socks=connected(cooking())))))
    await d.tick()                      # burst ends the first streak
    assert list(d.quiet_streaks) == [5.0], d.quiet_streaks

    clk.advance(3)
    sc.push(up(Signals(ts=clk.t)))
    await d.tick()                      # quiet again
    clk.advance(9)
    sc.push(serving(up(Signals(ts=clk.t, socks=connected(cooking())))))
    await d.tick()                      # second streak retired
    assert list(d.quiet_streaks) == [5.0, 9.0], d.quiet_streaks
    assert d.typical_gap_s == 5.0, "conservative middle, not the optimistic one"

    clk.advance(2)
    sc.push(up(Signals(ts=clk.t)))
    state = await d.tick()
    assert state.typical_gap_s == 5.0, "the snapshot carries it, so the runner need not"
    assert state.quiet_s is not None and state.quiet_s == 0.0


# --- the ledger judge (§20) ------------------------------------------------
#
# The wire cannot tell a monitor's bytes from a generation's bytes, and has now
# said so three separate times (§17's dashboard, §17's shape study, and the
# 2/s /admin/api/stats metronome that walled the gate shut on 2026-10-07).
# These tests pin the new judge: the server's ledger decides inference, bytes
# only trip the wire inside the ledger's inference-memory window, and the whole
# old byte regime runs untouched when the ledger is blind.

def metronome(clk: Clock, bps: float = 12_557.0) -> Signals:
    """Bytes on :8000 at the measured metronome rate (bench15: 12,557 B/s,
    which is two 6.4 KB /admin/api/stats responses per second) — with an
    explicitly idle ledger underneath them."""
    sig = up(Signals(ts=clk.t, socks=connected()))
    sig.wire = Wire(moved={SERVER: int(bps * WINDOW)}, window_s=WINDOW, ts=clk.t,
                    up=True, samples=2)
    return sig


def with_ledger(sig: Signals, **kw: object) -> Signals:
    """Stamp a live ledger read onto a signal. Defaults: server healthy and
    genuinely idle — the state the bytes meter kept refusing to believe."""
    kw.setdefault("idle_s", 60.0)
    kw.setdefault("total_requests", 1426)
    sig.ledger = LedgerView(ts=sig.ts, up=True, **kw)  # type: ignore[arg-type]
    return sig


@pytest.mark.asyncio
async def test_the_metronome_cannot_wall_the_gate_anymore() -> None:
    """The bug that killed every live run on 2026-10-07, pinned shut.

    12.5 KB/s forever on :8000 — while the ledger says nobody is generating and
    idle_seconds climbs. Old verdict: ACTIVE_INFER, forever, by construction.
    New verdict: quiet, and after the (unchanged) 4s window the gate opens with
    the metronome still running, because the server — the only party that knows
    what a request was — says nothing was.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(with_ledger(metronome(clk)))
    state = await d.tick()
    assert state.state == IDLE, state.reason
    assert "monitor traffic" in state.reason, state.reason
    assert not state.ready, "the first quiet tick is not yet four seconds"

    for _ in range(4):
        clk.advance(1)
        sc.push(with_ledger(metronome(clk), idle_s=64.0 + 0.0))
        state = await d.tick()
    assert state.ready, f"four ledger-quiet seconds must open the gate: {state.blockers}"
    assert state.ledger_up and state.ledger_idle_s is not None


@pytest.mark.asyncio
async def test_the_ledger_calls_inference_even_when_the_wire_says_quiet() -> None:
    """The ledger is stricter, not looser: a request it counts is ACTIVE_INFER
    even on a silent wire — a queued-but-not-yet-streaming generation is real,
    and bytes-only would have missed it entirely."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(with_ledger(up(Signals(ts=clk.t)), active=1, waiting=2, idle_s=0.3))
    state = await d.tick()
    assert state.state == ACTIVE_INFER
    assert "ledger" in state.reason and "queued" in state.reason, state.reason
    assert not state.ready


@pytest.mark.asyncio
async def test_we_do_not_preempt_ourselves_reading_our_own_ledger() -> None:
    """Our stage in flight reads as active=1 on the server. Without the own
    subtraction the daemon would preempt itself every tick — which is exactly
    how the pre-ledger daemon never survived a live stage at all."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sig = metronome(clk)  # our own streamed bytes, on the wire
    sc.push(with_ledger(sig, active=1, own=1, idle_s=0.8))
    state = await d.tick()
    assert state.state != ACTIVE_INFER, "active=own is the kitchen cooking, not the human returning"
    assert state.ledger_own == 1


@pytest.mark.asyncio
async def test_bytes_the_ledger_just_reset_are_inference_not_noise() -> None:
    """A 0.26s completion (bench15) never lights the counters, but it resets
    idle_seconds, and bytes with a freshly-reset clock are the request the
    counters missed. Inside the margin: inference. The metronome never resets
    the clock, so this catches the miss without resurrecting the wall."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(with_ledger(metronome(clk), idle_s=1.2))
    state = await d.tick()
    assert state.state == ACTIVE_INFER, state.reason
    assert "active 1.2s ago" in state.reason, state.reason


@pytest.mark.asyncio
async def test_the_reset_window_has_an_edge() -> None:
    """Outside the margin the same bytes are monitors again — the window exists
    for the missed completion, not as a second byte wall. Configurable knob,
    tested on both sides of its own number."""
    c, clk, sc = cfg(**{"ledger.infer_margin_seconds": 5.0}), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sc.push(with_ledger(metronome(clk), idle_s=6.0))
    state = await d.tick()
    assert state.state == IDLE and "monitor" in state.reason, state.reason


@pytest.mark.asyncio
async def test_a_typing_human_outranks_monitor_traffic() -> None:
    """Order of judges: the ledger's 'nobody is generating' is not 'nobody is
    coming back'. HID evidence still outranks the calmest ledger, because the
    gate is about what happens next, not only about what is happening now."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sig = with_ledger(metronome(clk))
    sig.hid_idle_s = 3.0
    sc.push(sig)
    state = await d.tick()
    assert state.state == ACTIVE_USER, state.reason
    assert not state.ready


@pytest.mark.asyncio
async def test_a_resident_subscriber_is_not_a_user() -> None:
    """Landmine #16, the sequel, on :4096.

    A connected opencode client with a stale WAL is a parked TUI or a dashboard
    subscriber, not someone typing. This exact evidence — `opencode socket
    OrbStack Helper(18634)` alone — kept ACTIVE_USER permanent on 2026-10-07
    while the box was empty, because a relationship was being read as an event.
    """
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sig = up(Signals(ts=clk.t, oc_wal_age_s=45.0,
                    oc_clients=(Peer(18634, "OrbStack Helper"),)))
    sc.push(sig)
    state = await d.tick()
    assert state.state == IDLE, f"a silent subscriber must not wall the kitchen: {state.reason}"

    clk.advance(1)
    sig2 = up(Signals(ts=clk.t, oc_wal_age_s=3.0,
                      oc_clients=(Peer(18634, "OrbStack Helper"),)))
    sc.push(sig2)
    state = await d.tick()
    assert state.state == ACTIVE_USER, "the same socket WITH a fresh WAL is a live session"
    assert "opencode socket" in state.reason, state.reason


@pytest.mark.asyncio
async def test_stale_ledger_falls_back_to_the_old_byte_judge() -> None:
    """When we cannot ask the server, bytes are the trigger again — unchanged,
    safety-first. This test is the fallback, verbatim: same bytes as the
    metronome, ledger up=False, and the verdict must be the pre-§20 one."""
    c, clk, sc = cfg(), Clock(), Scripted()
    d = make(c, None, sc, clk)
    sig = metronome(clk)  # ledger stays DEAD_LEDGER — blind
    sc.push(sig)
    state = await d.tick()
    assert state.state == ACTIVE_INFER, "blind + bytes = the old honest panic"
    assert "omlx serving" in state.reason
    assert not state.ledger_up
