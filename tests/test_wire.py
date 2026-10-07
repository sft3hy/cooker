"""Tests for the wire probe — the module that decides when Cooker may cook.

These are worth having because the first two versions of this probe were wrong in
ways that produced *plausible* numbers rather than crashes:

    sealing on row timestamps    every data row carries its own microsecond
                                 stamp, so the ring filled with single-row samples
                                 and the window was three ROWS long no matter what
                                 `wire_window_seconds` said.

    reading the pipe as text     text-mode iteration read-ahead holds nettop's
                                 output until a block fills, so the probe reported the
                                 same instant over and over and looked frozen.

A probe that returns zero when the truth is zero is indistinguishable from a probe
that returns zero because it is deaf, which is why `up` is a separate claim from
`moved`, and why there is a test for each.
"""

from __future__ import annotations

from cooker.wire import DEAD, Wire, WireProbe


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


HEADER = "time,,interface,state,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx,rtt_avg\n"


def sample(stamp: str, rows: list[tuple[str, int, int]]) -> bytes:
    """One nettop sample: header, then one row per process."""
    out = [HEADER]
    out += [f"{stamp},{name},,,{b_in},{b_out},0,0,0,0\n" for name, b_in, b_out in rows]
    return "".join(out).encode()


def feed(probe: WireProbe, blobs: list[bytes]) -> None:
    """Drive the reader over canned nettop output, as if the pipe had flushed.

    `up` is set the way `start()` would set it, because the alternative is every
    test quietly asserting against a deaf probe: the flag is what separates 'the
    stream is live and it says zero' from 'there is no stream', and a harness that
    left it False would make the interesting test impossible to write.

    The trailing header matters too and mirrors the real stream: nettop prints a
    header above every sample, and the header is what closes the sample before it.
    Without it the newest sample sits uncommitted in `_pending` and every test
    would assert against a window one sample short of the truth.
    """
    # `_ingest`, not `_read`: the reader's job is to *produce* text by polling
    # nettop, and feeding it text is the part we want to test. Calling `_read`
    # here would run the real nettop in the test suite.
    probe.up = True
    probe._ingest("".join(b.decode() for b in blobs) + HEADER)


def probe(interval: float = 1.0, window: float = 3.0) -> tuple[WireProbe, Clock]:
    clk = Clock()
    return WireProbe(interval_s=interval, window_s=window, clock=clk), clk


# --- the arithmetic the detector thresholds on -------------------------


def test_rate_divides_the_window_not_the_sample_count() -> None:
    w = Wire(moved={22630: 6000}, window_s=3.0, ts=0.0, up=True, samples=3)
    assert w.rate(22630) == 2000.0
    assert w.rate(999) == 0.0, "an unobserved pid is silent, not unknown here"
    assert Wire(moved={}, window_s=0.0, ts=0.0, up=False).rate(1) == 0.0


def test_server_talking_is_the_trigger_and_zero_is_not() -> None:
    """Measured separation: 0 B/s idle, 10,752 B in a 2s window generating."""
    busy = Wire(moved={22630: 10752}, window_s=2.0, ts=0.0, up=True, samples=2)
    idle = Wire(moved={22630: 0}, window_s=2.0, ts=0.0, up=True, samples=2)
    assert busy.server_talking((22630,), 256.0)
    assert not idle.server_talking((22630,), 256.0)
    assert not busy.server_talking((), 256.0), "no server, nobody to ask"
    assert busy.served_bps((22630,)) == 5376.0


def test_blind_is_stated_and_not_inferred() -> None:
    """The distinction the whole module exists for: empty because quiet, and
    empty because deaf, are different answers."""
    assert DEAD.moved == {}
    assert DEAD.blind
    assert not Wire(moved={}, window_s=2.0, ts=0.0, up=True).blind


# --- the reader ---------------------------------------------------------


def test_a_sample_seals_once_at_the_header() -> None:
    """The window is measured in samples, so the header is the boundary.

    Rows carry their own microsecond stamps; sealing on those made every row its
    own sample and the window meaningless.
    """
    p, _clk = probe(window=3.0)
    feed(p, [
        sample("01:00:01.000000", [("omlx-server.22630", 100, 50)]),
        sample("01:00:02.000000", [("omlx-server.22630", 150, 90)]),
        sample("01:00:03.000000", [("omlx-server.22630", 400, 3000)]),
    ])
    w = p.snapshot()
    assert w.samples == 2, "three samples, the first has no baseline to diff"
    assert w.moved[22630] == (150 - 100) + (90 - 50) + (400 - 150) + (3000 - 90)
    assert w.server_talking((22630,), 256.0)


def test_a_quiet_wire_reports_zero_and_stays_up() -> None:
    """The keepalive case: connections open, nothing moving, probe healthy.

    This is the state that used to latch ACTIVE_INFER forever. `up` true with
    zero bytes is the honest reading, and the only thing that lets the kitchen
    light on a machine where OpenCode never closes its socket.
    """
    p, _clk = probe()
    feed(p, [
        sample("01:00:01.000000", [("OrbStack Helper.18634", 500, 900)]),
        sample("01:00:02.000000", [("OrbStack Helper.18634", 500, 900)]),
    ])
    w = p.snapshot()
    assert w.up, "a quiet wire is not a broken one"
    assert w.moved[18634] == 0
    assert not w.server_talking((18634,), 256.0)


def test_counter_reset_reads_as_zero_not_as_traffic() -> None:
    """A restarted process's counters go backwards, and a naive subtraction turns
    that into an enormous number that looks exactly like a busy machine."""
    p, _clk = probe()
    feed(p, [
        sample("01:00:01.000000", [("omlx-server.22630", 9_000_000, 8_000_000)]),
        sample("01:00:02.000000", [("omlx-server.22630", 100, 50)]),
    ])
    assert p.snapshot().moved[22630] == 0


def test_stale_output_reports_blind_rather_than_idle() -> None:
    """If nettop stops writing, the last numbers stay true but they are no
    longer *now*, and 'start working' cannot rest on them."""
    p, clk = probe(interval=1.0, window=2.0)
    feed(p, [
        sample("01:00:01.000000", [("omlx-server.22630", 100, 50)]),
        sample("01:00:02.000000", [("omlx-server.22630", 900, 4000)]),
    ])
    assert p.snapshot().up
    clk.advance(p.stale_after + 1)
    snap = p.snapshot()
    assert not snap.up, "stale is blind, and blind must not be read as quiet"
    assert snap.moved[22630] > 0, "the last true numbers are still shown"


def test_the_ring_holds_the_configured_window() -> None:
    """`window_s` has to be the span actually covered, or the floor in bytes
    per second is calibrated against a span that does not exist."""
    p, _clk = probe(interval=1.0, window=4.0)
    assert p.capacity == 4
    blobs = [sample(f"01:00:0{i}.000000", [("omlx-server.22630", i * 10, i * 5)])
             for i in range(1, 8)]
    feed(p, blobs)
    assert p.snapshot().window_s == 4.0
    assert p.snapshot().samples == 4


def test_a_window_shorter_than_the_interval_is_refused_not_broken() -> None:
    """Interval 1s and window 0.5s would mean a window with no samples in it."""
    p, _clk = probe(interval=1.0, window=0.5)
    assert p.window >= 2.0
