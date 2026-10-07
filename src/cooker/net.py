"""Endpoint discovery.

The trap we already hit: `lsof :8000` shows *two* listeners — omlx on `*:8000`
and some stray `python -m http.server` on `127.0.0.1:8000`. The loopback bind is
more specific, so `http://127.0.0.1:8000` answers 404/501 from the stray while
omlx sits right behind it. A port number is not a service.

So Cooker never assumes. It finds the omlx server process, reads the addresses
*it* bound, and confirms the endpoint really serves our model before using it.
"""

from __future__ import annotations

import asyncio
import re
import socket
import subprocess  # fixed argv only, no shell, no interpolation
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cooker.config import Config

LSOF_TIMEOUT = 5.0


def _lsof(args: list[str]) -> str:
    """`-Fpcn` gives machine-readable field output (p<pid>/c<cmd>/n<addr>).
    The default table format truncates the command column, which is exactly the
    field we match process names against."""
    try:
        return subprocess.run(
            ["lsof", "-nP", "-Fpcn", *args], capture_output=True, text=True,
            timeout=LSOF_TIMEOUT,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


@dataclass(frozen=True)
class Listener:
    pid: int
    command: str
    address: str  # "*" | "0.0.0.0" | "127.0.0.1" | "192.168.1.20"
    port: int

    @property
    def is_wildcard(self) -> bool:
        return self.address in ("*", "0.0.0.0", "::")


def parse_lsof_listen(out: str) -> list[Listener]:
    """Parse `lsof -nP -iTCP -sTCP:LISTEN` into structured listeners."""
    listeners: list[Listener] = []
    pid, cmd = 0, ""
    for line in out.splitlines():
        if line.startswith("p"):
            pid, cmd = int(line[1:]), ""
        elif line.startswith("c"):
            cmd = line[1:]
        elif line.startswith("n"):
            for chunk in line[1:].split(","):
                m = re.match(r"^(.+?):(\d+)$", chunk.strip())
                if m and pid:
                    listeners.append(Listener(pid, cmd, m.group(1), int(m.group(2))))
    return listeners


def port_listeners(port: int) -> list[Listener]:
    return [ls for ls in parse_lsof_listen(_lsof(["-iTCP", "-sTCP:LISTEN"]))
            if ls.port == port]


def full_command(pid: int) -> str:
    """lsof's `c` field is the truncated executable basename — omlx shows up as
    plain 'Python'. argv via ps is the only reliable way to name it."""
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                              capture_output=True, text=True, timeout=2.0).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def omlx_pid(
    port: int, names: tuple[str, ...] | list[str] = ("omlx", "omlx-server")
) -> int | None:
    """Find the inference server. Matches on full argv, not the lsof command
    column, because macOS reports both omlx and a stray http.server as 'Python'."""
    for ls in port_listeners(port):
        if any(n in full_command(ls.pid).lower() for n in names):
            return ls.pid
    return None


def shadowed_on_loopback(port: int, omlx: int | None) -> Listener | None:
    """A process that is NOT omlx bound to 127.0.0.1:<port> shadows omlx there,
    because a specific bind beats a wildcard one."""
    for ls in port_listeners(port):
        if ls.pid != omlx and ls.address in ("127.0.0.1", "localhost"):
            return ls
    return None


def _default_route_ip() -> str | None:
    """Preferred source address for off-host traffic, without sending a packet."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 53))
            return str(s.getsockname()[0])
        except OSError:
            return None


def local_addrs() -> list[str]:
    """Addresses this host answers on, best-first: the interface with the default
    route, then the rest. OrbStack/Docker bridges also answer locally and would
    otherwise flood the candidate list with dead ends."""
    out: list[str] = []
    preferred = _default_route_ip()
    if preferred:
        out.append(preferred)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out


def normalize_base(url: str, port: int | None = None) -> str:
    """Canonical OpenAI base: scheme://host[:port]/v1. Preserves the scheme and
    the explicit port of what you pass in, so an https/home.arpa URL stays https
    and does not get rewritten onto the raw backend port."""
    raw = url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "//" in raw else f"//{raw}")
    host = parts.hostname
    if not host:
        return ""
    scheme = parts.scheme or "http"
    # The detection port is the raw omlx backend port, so it only belongs on an
    # http:// URL. Applying it to a portless https:// URL would send TLS traffic
    # straight at the backend and bypass Traefik entirely.
    if parts.port:
        netloc = f"{host}:{parts.port}"
    elif scheme == "http" and port:
        netloc = f"{host}:{port}"
    else:
        netloc = host
    if scheme == "https" and parts.port == 443:
        netloc = host
    if scheme == "http" and parts.port == 80:
        netloc = host
    return f"{scheme}://{netloc}/v1"


def candidate_bases(
    port: int,
    preferred: str | None = None,
    extra: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Ordered base URLs to try.

    Configured URLs come first, verbatim (that is how https://omlx.home.arpa
    survives). Raw-IP loopback fallbacks go LAST, because on this host
    127.0.0.1:8000 is occupied by an unrelated http.server.
    """
    out: list[str] = []

    def add(url: str) -> None:
        u = normalize_base(url, port)
        if u and u not in out:
            out.append(u)

    if preferred:
        add(preferred)
    for url in extra or ():
        add(url)
    for ip in local_addrs():
        add(f"http://{ip}:{port}/v1")
    for l_ in port_listeners(port):
        if not l_.is_wildcard:
            add(f"http://{l_.address}:{port}/v1")
    add(f"http://127.0.0.1:{port}/v1")
    return out


async def verify_base(
    client: httpx.AsyncClient, base: str, api_key: str | None, want_model: str | None,
    *, tries: int = 2,
) -> tuple[bool, str]:
    """Confirm `base` is really omlx serving our model. Two short retries —
    a busy inference box can take a moment to answer a control-plane GET."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last = ""
    for attempt in range(tries):
        try:
            r = await client.get(f"{base}/models", headers=headers, timeout=6.0)
            if r.status_code == 200:
                ids = [m.get("id") for m in r.json().get("data", [])]
                if want_model and want_model not in ids:
                    return False, f"200 but {want_model!r} absent (has {len(ids)})"
                return True, f"{len(ids)} models"
            if r.status_code == 401 and not api_key:
                return False, "401: no api key"
            last = f"HTTP {r.status_code}"
        except Exception as exc:
            last = f"{type(exc).__name__}"
        if attempt + 1 < tries:
            await asyncio.sleep(0.4)
    return False, last


def tls_context(ca_file: str | None):
    """SearXNG/autoresearch/omlx-over-Traefik use a smallstep root CA that lives
    on the host only. httpx wants a str or an SSLContext, never a Path."""
    import ssl

    p = Path(ca_file).expanduser() if ca_file else None
    return ssl.create_default_context(cafile=str(p)) if p and p.exists() else True


class Resolver:
    """Finds and caches the working inference endpoint.

    Resolution is not free (a few hundred ms of probes) and it only changes when
    someone reconfigs the homelab, so we cache the winner and re-validate on a
    slow tick, or immediately when a request fails in a way that smells like the
    endpoint moved.
    """

    def __init__(
        self, cfg: Config, ca_file: str | None, *, refresh_seconds: float = 300.0
    ) -> None:
        self.cfg = cfg
        self.tls = tls_context(ca_file)
        self.refresh_seconds = refresh_seconds
        self.base: str | None = None
        self.resolved_at = 0.0
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), verify=self.tls,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def resolve(self, *, force: bool = False) -> str | None:
        now = time.monotonic()
        if self.base and not force and now - self.resolved_at < self.refresh_seconds:
            return self.base
        client = await self.start()
        port = int(self.cfg.get("detect.omlx_port", 8000))
        key = api_key(self.cfg)
        want = self.cfg.get("inference.model")
        for cand in candidate_bases(
            port, self.cfg.get("inference.base_url"),
            self.cfg.get("detect.base_url_candidates", []),
        ):
            good, _detail = await verify_base(client, cand, key, want)
            if good:
                self.base, self.resolved_at = cand, now
                return cand
        self.base = None
        return None

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def api_key(cfg: Config) -> str | None:
    """Key comes from the environment first, then the autoresearch .env file
    (omlx's own settings.json has auth.api_key = null, so there is no key to
    read from the inference server itself)."""
    import os

    env = cfg.get("inference.api_key_env", "AR_LLM_API_KEY")
    if os.environ.get(env):
        return os.environ[env]
    path = cfg.get("inference.api_key_file")
    if path:
        p = Path(path).expanduser()
        if p.exists():
            for line in p.read_text().splitlines():
                k, _, v = line.partition("=")
                if k.strip() == env and v.strip():
                    return v.strip().strip("'\"")
    return None


def route_report(port: int, names: tuple[str, ...]) -> str:
    """Human explanation of why :port is unreachable, for the UI and doctor."""
    ls = port_listeners(port)
    if not ls:
        return f"nothing listening on :{port}"
    parts = [f"{l_.address}:{l_.port} pid {l_.pid} ({full_command(l_.pid)[:48]})"
             for l_ in ls]
    omlx = next((l_.pid for l_ in ls
                 if any(n in full_command(l_.pid).lower() for n in names)), None)
    shadow = shadowed_on_loopback(port, omlx)
    if shadow:
        return (f"omlx pid {omlx} binds *:{port}, but pid {shadow.pid} "
                f"({full_command(shadow.pid)[:40]}) binds 127.0.0.1:{port} "
                f"and shadows loopback")
    return ", ".join(parts)
