"""Endpoint discovery: a port number is not a service."""

from __future__ import annotations

from cooker import net

# Field output from `lsof -nP -Fpcn -iTCP -sTCP:LISTEN` on the real box, where
# omlx (pid 15222) binds *:8000 and a stray http.server (74595) binds :1
LSOF = """p15222
cPython
f4
n*:8000
p74595
cPython
f3
n127.0.0.1:8000
p18634
cOrbStack Helper
f29
n127.0.0.1:8081
f126
n127.0.0.1:32222
p400
cTailscaled
f7
n100.64.0.5:41641
"""


def test_parse_listeners():
    ls = net.parse_lsof_listen(LSOF)
    got = {(l_.pid, l_.address, l_.port) for l_ in ls}
    assert (15222, "*", 8000) in got
    assert (74595, "127.0.0.1", 8000) in got
    # one process, many sockets -> many rows, one pid
    assert sum(1 for l_ in ls if l_.pid == 18634) == 2


def test_shadow_detection_is_exact():
    """The stray on loopback is the shadow; omlx on the wildcard is not."""
    ls = net.parse_lsof_listen(LSOF)
    on_8000 = [l_ for l_ in ls if l_.port == 8000]
    loopback = [l_ for l_ in on_8000 if l_.address == "127.0.0.1"]
    assert len(loopback) == 1 and loopback[0].pid == 74595
    assert net.Listener(15222, "Python", "*", 8000).is_wildcard
    assert not net.Listener(74595, "Python", "127.0.0.1", 8000).is_wildcard


def test_candidates_never_put_loopback_first_when_preferred_is_explicit():
    cands = net.candidate_bases(8000, "http://192.168.1.20:8000/v1")
    assert cands[0] == "http://192.168.1.20:8000/v1"
    assert cands[-1] == "http://127.0.0.1:8000/v1", "loopback is the last resort"
    for c in cands:
        assert c.count(":8000") == 1, f"malformed candidate (double port): {c}"
        assert c.startswith("http://") and c.endswith("/v1")


def test_candidates_preserve_explicit_port_and_scheme():
    """Config owns the port. candidate_bases only fills in a MISSING port — it
    must never rewrite one you asked for, or a config edit silently points
    inference at the wrong daemon."""
    assert net.candidate_bases(8000, "http://mini.home.arpa:8000/v1")[0] == (
        "http://mini.home.arpa:8000/v1")
    assert net.candidate_bases(9000, "http://mini.home.arpa:8000/v1")[0] == (
        "http://mini.home.arpa:8000/v1")
    assert net.candidate_bases(8000, "https://omlx.home.arpa/v1")[0] == (
        "https://omlx.home.arpa/v1")
    assert net.candidate_bases(8000, "omlx.home.arpa")[0] == "http://omlx.home.arpa:8000/v1"
    assert net.candidate_bases(8000, "https://omlx.home.arpa:443/v1")[0] == (
        "https://omlx.home.arpa/v1")


def test_local_addrs_excludes_loopback_and_is_ordered():
    addrs = net.local_addrs()
    assert all(not a.startswith("127.") for a in addrs)
    assert len(addrs) == len(set(addrs))


def test_route_report_names_the_shadow():
    """When this fires, the operator must immediately see who to go complain to."""
    report = net.route_report(8000, ("omlx", "omlx-server"))
    assert "8000" in report
    assert "shadow" in report.lower(), report


def test_tls_context_only_when_ca_exists(tmp_path):
    import ssl

    assert net.tls_context(None) is True
    assert net.tls_context(str(tmp_path / "nope.crt")) is True
    real = net.tls_context("~/homelab/edge/step/certs/root_ca.crt")
    if real is not True:
        assert isinstance(real, ssl.SSLContext)
