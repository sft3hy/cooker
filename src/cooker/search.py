"""search: the web half of the research chain. No GPU, no tokens, cancellable.

Two operations, and the reason they are not one function:
    `search` asks SearXNG and returns *candidates*. It costs nothing and returns
    ten-thousandths of the content.
    `fetch` retrieves one page and returns its text, capped. This is the
    operation that can hurt: an unbounded fetch of a 40 MB page is a disk
    incident and a network stall in one, and a stage that stalls cannot be
    preempted promptly.

Both are async httpx so `asyncio.CancelledError` lands between chunks. A network
stage that ignores cancellation is the same bug as an inference stage that does:
the daemon said stop and kept going, except here the thing being hogged is the
uplink rather than the accelerator.

Everything returned by `fetch` is untrusted by construction. It comes back as
`Page.text` in its raw form because the fence belongs at the point of use, not
here: `plan` quotes a snippet, `extract` gets the whole page fenced, and the
`prompt_tokens` budget differs between them. The gate is `safety.scan_secrets`,
applied here on the way in, so a page that happens to contain somebody's leaked
token does not smuggle it into a prompt two stages later.

Cache
-----
SearXNG is on the LAN and fast, but the internet behind it is not, and the
same source recurring across three topics is common enough that caching is worth
the disk. The cache is content-addressed by URL, has a TTL, and is bounded in
bytes: unbounded disk caching under a `max_disk_gb` promise is a broken promise.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from cooker import net, safety
from cooker.config import Config

# Only these schemes. `file://` would turn a search result into a local read.
#
# Without the colon, deliberately: `urlparse("http://x").scheme` is `"http"`, not
# `"http:"`, so an allow-list written with colons matches nothing and refuses
# everything. That bug is invisible until the first real fetch fails with
# "scheme http not allowed", which is exactly what the first real fetch does.
ALLOWED_SCHEMES = ("http", "https")

# Tag content dropped before the model ever sees it. `script` and `style` are
# the obvious ones; `noscript` is here because it often carries tracking markup
# that reads like content, and `iframe` because its src is a promise we won't keep.
DROP_TAGS = frozenset({"script", "style", "noscript", "iframe", "svg", "nav",
                       "footer", "header", "form", "button", "select", "option"})

# Blocks that carry text worth keeping, so paragraph breaks survive the strip.
BLOCK_TAGS = frozenset({"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5",
                        "h6", "tr", "td", "th", "blockquote", "pre", "article",
                        "section", "ul", "ol"})


class SearchRefused(RuntimeError):
    """A URL or query we will not act on. Raised, never fetched."""


@dataclass(frozen=True)
class Result:
    url: str
    title: str
    snippet: str
    engine: str = ""
    score: float = 0.0
    category: str = ""
    published: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title, "snippet": self.snippet,
                "engine": self.engine, "score": self.score,
                "category": self.category, "published": self.published}

    @property
    def host(self) -> str:
        try:
            return urlparse(self.url).netloc
        except ValueError:
            return "?"


@dataclass
class Page:
    url: str
    text: str = ""
    status: int = 0
    bytes_in: int = 0
    truncated: bool = False
    from_cache: bool = False
    fetched_at: float = 0.0
    error: str | None = None
    findings: tuple[safety.Finding, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """A page we can put in front of a model. 200 is necessary and not
        sufficient: a 200 that returns a cookie wall or a 40-character stub is
        worse than a failure, because it produces confident nonsense."""
        return (self.status == 200 and not self.error
                and len(self.text.strip()) >= 400)

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "status": self.status, "bytes_in": self.bytes_in,
                "chars": len(self.text), "truncated": self.truncated,
                "from_cache": self.from_cache, "usable": self.usable,
                "error": self.error,
                "redactions": [f.as_dict() for f in self.findings]}


class _Text(HTMLParser):
    """HTML to text, preserving paragraph shape.

    Shape matters more than purity here: `synthesize` reads this text, and a page
    flattened into one 80 kB line loses the structure that makes an answer
    attributable to a paragraph.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop = 0

    def handle_starttag(self, tag: str, _: list[tuple[str, str | None]]) -> None:
        if tag in DROP_TAGS:
            self._drop += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_TAGS and self._drop:
            self._drop -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._drop and data.strip():
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """Strip markup without flattening structure."""
    p = _Text()
    try:
        p.feed(raw)
    except Exception:
        # Fall back to a crude strip rather than losing the page: a page we
        # cannot parse still has text in it, and losing evidence silently is
        # worse than keeping it imperfectly.
        p.parts = [re.sub(r"<[^>]+>", "\n", raw)]
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t\x0b\f\r]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cache_dir(cfg: Config) -> Path:
    root = Path(str(cfg.data_dir)) / "cache" / "web"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _cache_path(cfg: Config, url: str) -> Path:
    key = _cache_key(url)
    d = _cache_dir(cfg) / key[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def cache_bytes(cfg: Config) -> int:
    root = _cache_dir(cfg)
    return sum(f.stat().st_size for f in root.rglob("*.json") if f.is_file())


def trim_cache(cfg: Config) -> int:
    """Evict oldest-first until the cache fits its own budget.

    Returns the number of entries removed. Eviction is by mtime, not by
    importance, because "importance" is a claim about a page we have not read.
    """
    limit = int(cfg.get("search.cache_max_mb", 256)) * 1024 * 1024
    root = _cache_dir(cfg)
    files = sorted((f for f in root.rglob("*.json") if f.is_file()),
                   key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    removed = 0
    for f in files:
        if total <= limit:
            break
        try:
            size = f.stat().st_size
            f.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    return removed


def cache_get(cfg: Config, url: str) -> Page | None:
    path = _cache_path(cfg, url)
    if not path.exists():
        return None
    ttl = float(cfg.get("search.cache_ttl_days", 7)) * 86400.0
    age = time.time() - path.stat().st_mtime
    if age > ttl:
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return Page(url=url, text=data.get("text", ""), status=int(data.get("status", 0)),
                bytes_in=int(data.get("bytes_in", 0)),
                truncated=bool(data.get("truncated", False)), from_cache=True,
                fetched_at=float(data.get("fetched_at", 0.0)))


def cache_put(cfg: Config, page: Page) -> None:
    path = _cache_path(cfg, page.url)
    try:
        path.write_text(json.dumps({"url": page.url, "status": page.status,
                                     "text": page.text, "bytes_in": page.bytes_in,
                                     "truncated": page.truncated,
                                     "fetched_at": page.fetched_at},
                                    separators=(",", ":")))
    except OSError:
        # A cache that cannot write is a cache that costs nothing, not an error
        # that kills the stage. The fetch already succeeded.
        return
    if cache_bytes(cfg) > int(cfg.get("search.cache_max_mb", 256)) * 1024 * 1024:
        trim_cache(cfg)


def _is_blocked_host(host: str) -> bool:
    """Loopback, link-local, metadata and RFC1918 — by range, not by string.

    The exact-spelling version of this check (`host == "127.0.0.1"`) lets every
    other address in 127.0.0.0/8 through, which is the entire loopback range.
    `::1` was likewise the only IPv6 spelling we caught, and a URL writes it
    bracketed anyway.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP, so a hostname. `localhost` is the one name that means
        # loopback without resolving, and dropping it here would open the whole
        # guard for the most obvious spelling of "this machine".
        return host == "localhost" or host.endswith(".localhost")
    return (ip.is_loopback or ip.is_link_local or ip.is_private
            or ip.is_reserved or ip.is_multicast)


def check_url(url: str, *, allow_private: bool = False) -> str:
    """Validate before any network call. A URL is an argument from the internet
    that we are about to act on, so this is the last place it can be refused.

    `allow_private` is the honest knob for "I want Cooker to read a service on my
    LAN". Default off: a research sidecar that will fetch any loopback URL is a
    port scanner with better manners, and cloud metadata endpoints live at
    169.254.169.254 for the specific purpose of not being reachable from outside.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SearchRefused(f"unparseable url: {url!r}") from exc
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SearchRefused(f"scheme {parsed.scheme or '(none)'} not allowed")
    try:
        host = parsed.hostname or ""
    except ValueError as exc:
        raise SearchRefused(f"unparseable host in {url!r}") from exc
    if not host:
        raise SearchRefused(f"no host in {url!r}")
    if not allow_private and _is_blocked_host(host):
        raise SearchRefused(f"refusing private/loopback host: {host}")
    return url


class Searcher:
    """SearXNG client plus a polite fetcher.

    One client, reused, so TLS handshakes are amortised and there is exactly one
    place that knows the CA. Sequential by construction: there is no pool of
    concurrent fetches here, because a fan-out of ten parallel fetches looks
    exactly like an attack from the origin's side and is indistinguishable from
    one in your own firewall logs.
    """

    def __init__(self, cfg: Config,
                 emit: Any | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.url = str(cfg.get("search.searxng_url", "http://127.0.0.1:8888/search"))
        self.timeout = float(cfg.get("search.timeout_seconds", 20))
        self.max_results = int(cfg.get("search.max_results", 8))
        self.fetch_max = int(cfg.get("search.fetch_max_bytes", 262144))
        # Read once, at construction: a knob that changes mid-run would let a
        # later URL through a gate an earlier one was refused by.
        self.allow_private = bool(cfg.get("search.allow_private_networks", False))
        self._emit_fn = emit
        self._client = client
        self._own_client = client is None
        self.searches = 0
        self.fetches = 0
        self.cache_hits = 0
        self.refusals = 0
        self.skipped_cooloff = 0
        # host -> monotonic deadline. Politeness state belongs to the client that
        # made the request, not to the stage that happened to notice.
        self._cooloff: dict[str, float] = {}
        self.cooloff_seconds = float(cfg.get("search.rate_limit_cooldown_seconds",
                                              300))
        # Consecutive failures. 1 Hz polling taught the detector the same lesson:
        # a dead upstream must be reported once, not hammered.
        self.consecutive_failures = 0

    def emit(self, type_: str, message: str, data: dict[str, Any] | None = None) -> None:
        if self._emit_fn is not None:
            self._emit_fn(type_, message, data or {})

    async def start(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # SearXNG is behind Traefik on a private CA; the pages it returns are
            # on the public internet with real certificates. One client, both
            # trust stores, because a client that trusts only the private CA
            # reports every origin as broken and reads like an outage.
            ca = self.cfg.get("search.ca_file")
            tls = net.tls_context_with_roots(ca) if self.url.startswith("https") \
                else True
            self._client = httpx.AsyncClient(
                verify=tls, timeout=httpx.Timeout(self.timeout),
                follow_redirects=True, max_redirects=5,
                headers={"User-Agent": "cooker/0.1 (personal research sidecar)"},
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # --- search ---------------------------------------------------------

    async def search(self, query: str, *, max_results: int | None = None) -> list[Result]:
        """Ask SearXNG. Returns [] on failure, never raises for a bad query.

        An empty list is the honest answer to "the upstream is down": the chain
        sees zero sources and fails at `search` rather than inventing sources at
        `synthesize`, which is the failure mode that produces a confident
        hallucination in a published artifact.
        """
        q = (query or "").strip()
        if not q:
            return []
        if self.consecutive_failures >= 3:
            self.refusals += 1
            self.emit("search.skipped",
                      f"search skipped, upstream down {self.consecutive_failures}x",
                      {"query": q})
            return []
        client = await self.start()
        n = max_results or self.max_results
        params = urlencode({"q": q, "format": "json", "language": "en"})
        sep = "&" if "?" in self.url else "?"
        try:
            r = await client.get(f"{self.url}{sep}{params}")
        except (httpx.HTTPError, OSError) as exc:
            self.consecutive_failures += 1
            self.emit("search.failed",
                      f"searxng unreachable ({type(exc).__name__}): {exc}",
                      {"query": q, "consecutive": self.consecutive_failures})
            return []
        if r.status_code != 200:
            self.consecutive_failures += 1
            self.emit("search.failed", f"searxng HTTP {r.status_code}",
                      {"query": q, "consecutive": self.consecutive_failures})
            return []
        self.consecutive_failures = 0
        self.searches += 1
        try:
            data = json.loads(r.text)
        except json.JSONDecodeError as exc:
            self.emit("search.failed", f"searxng returned non-JSON: {exc}",
                      {"query": q})
            return []
        out: list[Result] = []
        for row in data.get("results", []) or []:
            url = str(row.get("url") or "")
            if not url:
                continue
            if not self.keeps(url):
                continue
            out.append(Result(
                url=url, title=str(row.get("title") or "")[:200],
                snippet=str(row.get("content") or "")[:600],
                engine=str(row.get("engine") or ""),
                score=float(row.get("score") or 0.0),
                category=str(row.get("category") or ""),
                published=row.get("publishedDate")))
            if len(out) >= n:
                break
        self.emit("search.done",
                  f"{len(out)} result(s) for {q[:60]!r}",
                  {"query": q, "count": len(out),
                   "hosts": sorted({r_.host for r_ in out})})
        return out

    def keeps(self, url: str) -> bool:
        """Cheap gate: scheme, host, and the domains that are pure noise."""
        try:
            check_url(url, allow_private=self.allow_private)
        except SearchRefused:
            self.refusals += 1
            return False
        block = tuple(str(x) for x in (self.cfg.get("search.block_hosts", []) or []))
        host = urlparse(url).netloc.lower()
        if any(b in host for b in block):
            self.refusals += 1
            return False
        return True

    # --- fetch ----------------------------------------------------------

    async def fetch(self, url: str) -> Page:
        """Fetch one page, capped, cached, secret-scanned.

        The cap is enforced while streaming, not after: `resp.read()` on a 200 MB
        file allocates 200 MB before you get a chance to say no. Counting bytes as
        they arrive and closing the response the moment the budget is spent is
        the difference between a bounded stage and an out-of-memory kill, and an
        OOM kill cannot run a `finally`.

        A 429 or 403 puts the *host* in cooldown, honouring `Retry-After` when it
        is offered. Retrying a site that just told us to wait is how a research
        sidecar becomes the thing your firewall alerts on.
        """
        try:
            check_url(url, allow_private=self.allow_private)
        except SearchRefused as exc:
            self.refusals += 1
            return Page(url=url, error=str(exc))

        hit = cache_get(self.cfg, url)
        if hit is not None and hit.usable:
            self.cache_hits += 1
            return hit

        host = urlparse(url).hostname or ""
        until = self._cooloff.get(host, 0.0)
        if until > time.monotonic():
            self.skipped_cooloff += 1
            return Page(url=url, error=f"{host} in cooldown for "
                      f"{until - time.monotonic():.0f}s")

        client = await self.start()
        self.fetches += 1
        page = Page(url=url, fetched_at=time.time())
        raw = bytearray()
        truncated = False
        cap = self.fetch_max
        try:
            async with client.stream("GET", url) as resp:
                page.status = resp.status_code
                ctype = resp.headers.get("content-type", "")
                if resp.status_code in (403, 429, 503):
                    # Honour Retry-After when it is offered; otherwise sit out the
                    # configured window. Hammering a host that just said no is the
                    # behaviour that gets a home IP range blocked.
                    wait = self.cooloff_seconds
                    ra = resp.headers.get("retry-after", "")
                    if ra.strip().isdigit():
                        wait = min(float(ra), self.cooloff_seconds)
                    self._cooloff[urlparse(url).hostname or ""] = (time.monotonic()
                                                                    + wait)
                if resp.status_code != 200:
                    page.error = f"HTTP {resp.status_code}"
                    self.emit("fetch.refused",
                            f"{url[:90]} -> HTTP {resp.status_code}",
                            {"url": url, "status": resp.status_code,
                             "rate_limited": resp.status_code == 429})
                    return page
                if "html" not in ctype and "text" not in ctype and "xml" not in ctype:
                    page.error = f"unsupported content-type {ctype!r}"
                    self.emit("fetch.skipped",
                            f"{url[:90]} is {ctype!r}, not text",
                            {"url": url, "content_type": ctype})
                    return page
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    if len(raw) + len(chunk) > cap:
                        raw += chunk[: cap - len(raw)]
                        truncated = True
                        break
                    raw += chunk
        except (httpx.HTTPError, OSError) as exc:
            page.error = f"{type(exc).__name__}: {exc}"
            self.emit("fetch.failed", f"{url[:90]}: {page.error}",
                      {"url": url, **page.as_dict()})
            return page

        page.bytes_in = len(raw)
        page.truncated = truncated
        body = bytes(raw).decode("utf-8", errors="replace")
        # HTML gets stripped to text with its paragraph shape intact; a plain
        # text or JSON body is passed through, because running a JSON feed
        # through an HTML parser mangles it into something that only looks clean.
        page_text = html_to_text(body) if self._guess_type(url) == "html" else body
        # The gate. A page that carries somebody's token must not carry it into a
        # prompt two stages downstream, and "two stages downstream" is exactly
        # where nobody thinks to look.
        scan = safety.scan_secrets(page_text, source=url)
        page.text = scan.text
        page.findings = scan.findings
        if scan.findings:
            self.emit("safety.redaction",
                      f"redacted {sum(f.count for f in scan.findings)} span(s) "
                      f"from fetched page", {"url": url, **scan.as_dict()})
        if not page.usable:
            self.emit("fetch.thin", f"{url[:90]} yielded {len(page.text)}c "
                      f"(needs 400)", {"url": url, **page.as_dict()})
        else:
            cache_put(self.cfg, page)
        return page

    @staticmethod
    def _guess_type(url: str) -> str:
        low = url.lower()
        if low.endswith(".json"):
            return "json"
        if low.endswith(".txt") or low.endswith(".md"):
            return "text"
        return "html"

    def state(self) -> dict[str, Any]:
        return {"searches": self.searches, "fetches": self.fetches,
                "cache_hits": self.cache_hits, "refusals": self.refusals,
                "skipped_cooloff": self.skipped_cooloff,
                "in_cooloff": sorted(k for k, v in self._cooloff.items()
                                     if v > time.monotonic()),
                "consecutive_failures": self.consecutive_failures,
                "cache_mb": round(cache_bytes(self.cfg) / (1024 * 1024), 2)}


async def probe(cfg: Config, query: str = "homelab") -> tuple[int, str]:
    """Do we have search? Used by `cooker doctor` and by the chain before it
    commits a stage's worth of work to a dead upstream."""
    s = Searcher(cfg)
    try:
        results = await s.search(query, max_results=3)
    finally:
        await s.aclose()
    if results:
        return len(results), f"{results[0].host} — {results[0].title[:40]}"
    if s.consecutive_failures:
        return 0, f"searxng unreachable at {cfg.get('search.searxng_url')}"
    return 0, "searxng answered with no results"
