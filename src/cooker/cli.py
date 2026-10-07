"""`cooker` command line.

M1 gives us `doctor`, `db`, and `status`. `run` arrives in M2 when the
detector and scheduler exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cooker import __version__, db, net
from cooker.config import Config, load_config

OK, WARN, FAIL, INFO = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m", "·"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0
        self.warned = 0

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))
        self.failed += level is FAIL
        self.warned += level is WARN
        print(f"{level} {name:<26} {detail}")

    def ok(self, n: str, d: str = "") -> None:
        self.add(OK, n, d)

    def warn(self, n: str, d: str = "") -> None:
        self.add(WARN, n, d)

    def fail(self, n: str, d: str = "") -> None:
        self.add(FAIL, n, d)

    def info(self, n: str, d: str = "") -> None:
        self.add(INFO, n, d)


# --- helpers --------------------------------------------------------------


def _free_port(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _established_peers(port: int) -> list[str]:
    out = subprocess.run(
        ["lsof", "-nP", "-Fpcn", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
        capture_output=True, text=True, timeout=5,
    ).stdout
    return sorted({ln[1:] for ln in out.splitlines() if ln.startswith("c")})


# --- doctor ---------------------------------------------------------------


async def _net_checks(cfg: Config, rep: Report) -> None:
    key = net.api_key(cfg)
    rep.info("api key", "found" if key else "MISSING")

    port = int(cfg.get("detect.omlx_port"))
    names = tuple(cfg.get("detect.omlx_proc_names", ["omlx", "omlx-server"]))
    omlx = net.omlx_pid(port, names)
    if omlx is None:
        rep.fail("omlx process", net.route_report(port, names))
        return
    rep.ok("omlx process", f"pid {omlx}")

    # The port is not the service. Two things can listen on :8000 and the
    # specific bind (127.0.0.1) wins over the wildcard one, so a stray
    # http.server can shadow omlx on loopback while omlx sits right behind it.
    # We never conclude "omlx is down" from a port: we name the process and we
    # probe URLs. Prefer https://omlx.home.arpa (Traefik) over raw IPs, and raw
    # IPs over loopback. See DISCOVERY.md for the measurements behind this.
    shadow = net.shadowed_on_loopback(port, omlx)
    if shadow:
        rep.warn("loopback shadowed",
                 f"pid {shadow.pid} ({net.full_command(shadow.pid)[:44]}) "
                 f"holds 127.0.0.1:{port}")

    res = net.Resolver(cfg, cfg.get("inference.ca_file"))
    try:
        client = await res.start()
        base = None
        for cand in net.candidate_bases(
            port, cfg.get("inference.base_url"),
            cfg.get("detect.base_url_candidates", []),
        ):
            good, detail = await net.verify_base(
                client, cand, key, cfg.get("inference.model"), tries=1)
            (rep.ok if good else rep.info)("probe", f"{cand} -> {detail}")
            if good and base is None:
                base = cand
        if not base:
            rep.fail("omlx endpoint", "no candidate served /v1/models with our model")
            return
        rep.ok("omlx endpoint", base)

        try:
            r = await client.get(f"{base.removesuffix('/v1')}/metrics")
            rep.info("omlx /metrics",
                     "404 — no queue API; detect via sockets (as designed)"
                     if r.status_code == 404 else f"HTTP {r.status_code}")
        except Exception as exc:
            rep.info("omlx /metrics", f"{type(exc).__name__}")

        if not key:
            rep.fail("inference round-trip", "no api key")
            return
        try:
            t0 = time.perf_counter()
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": cfg.get("inference.model"),
                    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                    # Tiny and thinking-off on purpose: doctor must not stomp the
                    # box it is diagnosing, and this model spends its whole budget
                    # on reasoning when thinking is on.
                    "max_tokens": 64,
                    "temperature": 0.0,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            if r.status_code == 200:
                j = r.json()
                msg = j["choices"][0]["message"]
                txt = (msg.get("content") or "").strip()
                rc = len(msg.get("reasoning_content") or "")
                cpt = (j.get("usage") or {}).get("completion_tokens") or 1
                dt = max(time.perf_counter() - t0, 1e-6)
                if not txt:
                    rep.fail("inference round-trip",
                            f"empty content, finish={j['choices'][0].get('finish_reason')} "
                            f"— raise max_tokens_floor")
                else:
                    rep.ok("inference round-trip",
                           f"{txt[:20]!r} {cpt}tok {cpt / dt:.0f}tok/s reasoning={rc}c")
            else:
                rep.fail("inference round-trip", f"HTTP {r.status_code} {r.text[:120]}")
        except Exception as exc:
            rep.fail("inference round-trip", f"{type(exc).__name__}: {exc}")

        # SearXNG is container-only (host :8080 is Pi-hole), so both it and
        # autoresearch are reached through Traefik with the same step CA.
        try:
            t0 = time.perf_counter()
            r = await client.get(cfg.get("search.searxng_url"),
                                 params={"format": "json", "q": "mlx server"})
            dt = time.perf_counter() - t0
            if r.status_code == 200:
                n = len(r.json().get("results", []))
                rep.ok("searxng via Traefik", f"{n} results in {dt:.2f}s")
            else:
                rep.fail("searxng via Traefik", f"HTTP {r.status_code}")
        except Exception as exc:
            rep.fail("searxng via Traefik", f"{type(exc).__name__}: {exc}")

        try:
            r = await client.get(f"{cfg.get('delegate.autoresearch_url').rstrip('/')}/health")
            rep.ok("autoresearch reachable", f"HTTP {r.status_code}")
        except Exception as exc:
            rep.warn("autoresearch reachable", f"{type(exc).__name__}")
    finally:
        await res.aclose()


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    rep = Report()
    print(f"cooker {__version__} doctor\n" + "-" * 46)

    rep.info("python", sys.version.split()[0])
    rep.info("sqlite", sqlite3.sqlite_version)
    rep.info("platform", f"{platform.machine()} macOS {platform.mac_ver()[0]}")

    for tool in ("lsof", "ioreg", "ps", "git"):
        (rep.ok if shutil.which(tool) else rep.fail)(f"tool: {tool}",
                                                      shutil.which(tool) or "missing")

    # config sanity — the knobs that stop us being a noisy neighbour
    ceiling = cfg.get("inference.max_prefill_tokens_per_request")
    (rep.ok if ceiling and ceiling <= 1000 else rep.warn)(
        "prefill ceiling", f"{ceiling} tokens/request")
    floor = cfg.get("inference.max_tokens_floor")
    (rep.ok if floor and floor >= 512 else rep.warn)(
        "max_tokens floor", f"{floor} (this model streams reasoning first)")
    rep.info("concurrency", str(cfg.get("scheduler.max_concurrency")))
    rep.info("publish threshold", str(cfg.get("quality.publish_threshold")))

    # bind
    host, port = cfg.get("daemon.bind_host"), int(cfg.get("daemon.port"))
    if host not in ("127.0.0.1", "localhost"):
        rep.fail("bind host", f"{host} — must stay loopback")
    elif _free_port(host, port):
        rep.ok("bind port free", f"{host}:{port}")
    else:
        rep.warn("bind port free", f"{host}:{port} in use (daemon already running?)")

    # the trap we already fell into once: the map says tailnet-only, it is not
    peers = _established_peers(int(cfg.get("detect.omlx_port")))
    rep.info("omlx open sockets", f"{len(peers)} ({', '.join(peers) or 'none'})")

    # data dir
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-probe"
        probe.write_text("x")
        probe.unlink()
        rep.ok("writable", f"{cfg.data_dir} + {cfg.outputs_dir}")
    except OSError as exc:
        rep.fail("writable", str(exc))

    conn = db.connect(cfg.db_path)
    try:
        fresh = db.migrate(conn)
        rep.ok("database", f"{cfg.db_path} ({'created' if fresh else 'migrated'})")
        counts = db.queue_counts(conn)
        rep.info("tasks", json.dumps(counts) if counts else "empty")
    finally:
        conn.close()

    if not args.offline:
        asyncio.run(_net_checks(cfg, rep))

    print("-" * 46)
    print(f"{rep.failed} fail, {rep.warned} warn")
    return 1 if rep.failed else 0


# --- db -------------------------------------------------------------------


def cmd_db(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect(cfg.db_path)
    try:
        if args.db_cmd == "init":
            fresh = db.migrate(conn)
            print(f"{'created' if fresh else 'ok'} {cfg.db_path}")
            return 0
        if args.db_cmd == "selftest":
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                c = db.connect(Path(td) / "selftest.db")
                db.migrate(c)
                chain = db.new_id()
                root = db.create_task(c, chain_id=chain, kind="plan", generator="research",
                                      title="root")
                assert db.claim_next(c).id == root.id
                kids = [db.create_task(c, chain_id=chain, kind="extract", generator="research",
                                        title=f"k{i}", dependencies=[root.id]) for i in range(2)]
                assert db.claim_next(c) is None, "deps not satisfied yet"
                db.finish(c, root.id, db.SUCCEEDED)
                assert db.claim_next(c).id == kids[0].id
                print("dag selftest ok — gating, claim, finish")
                c.close()
            return 0
        if args.db_cmd == "status":
            db.migrate(conn)
            print(json.dumps({
                "db": str(cfg.db_path),
                "queue": db.queue_counts(conn),
                "next_runnable_in_s": (
                    round((db.next_runnable_eta(conn) or 0) - time.time(), 1) or None
                ),
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "artifacts": conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            }, indent=2))
            return 0
    finally:
        conn.close()
    return 2


# --- entrypoint -----------------------------------------------------------


def cmd_probe(cfg: Config, args: argparse.Namespace) -> int:
    """One full-size inference call, exactly as a real stage would make it.

    Separate from `doctor` on purpose: doctor stays cheap and polite; this one
    deliberately spends a real budget so you can see reasoning_content,
    finish_reason and tokens/sec before you trust the queue with it.
    """
    rep = Report()
    key = net.api_key(cfg)
    if not key:
        rep.fail("api key", "missing")
        return 1
    port = int(cfg.get("detect.omlx_port"))
    names = tuple(cfg.get("detect.omlx_proc_names", ["omlx", "omlx-server"]))
    max_tokens = int(args.max_tokens or cfg.get("inference.max_tokens_floor", 512))
    thinking = not args.no_thinking

    async def run() -> int:
        res = net.Resolver(cfg, cfg.get("inference.ca_file"), refresh_seconds=0)
        try:
            client = await res.start()
            base = None
            for cand in net.candidate_bases(
                port, cfg.get("inference.base_url"),
                cfg.get("detect.base_url_candidates", []),
            ):
                good, detail = await net.verify_base(
                    client, cand, key, cfg.get("inference.model"), tries=1)
                (rep.ok if good else rep.info)("probe", f"{cand} -> {detail}")
                if good and base is None:
                    base = cand
            if base is None:
                rep.fail("endpoint", net.route_report(port, names))
                return 1
            rep.ok("endpoint", base)

            body = {
                "model": cfg.get("inference.model"),
                "messages": [{"role": "user", "content": args.prompt}],
                "max_tokens": max_tokens,
                "temperature": float(cfg.get("inference.default_temperature", 0.7)),
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": thinking},
            }
            t0 = time.perf_counter()
            r = await client.post(f"{base}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}"}, json=body)
            dt = max(time.perf_counter() - t0, 1e-6)
            if r.status_code != 200:
                rep.fail("chat", f"HTTP {r.status_code} {r.text[:200]}")
                return 1
            j = r.json()
            ch = j["choices"][0]
            content = (ch["message"].get("content") or "").strip()
            reasoning = ch["message"].get("reasoning_content") or ""
            u = j.get("usage") or {}
            cpt = u.get("completion_tokens") or 0
            rep.info("thinking", str(thinking))
            rep.info("elapsed", f"{dt:.2f}s")
            rep.info("finish_reason", str(ch.get("finish_reason")))
            rep.info("reasoning_chars", str(len(reasoning)))
            rep.info("completion_tokens", f"{cpt} ({cpt / dt:.0f} tok/s)")
            rep.info("prompt_tokens", str(u.get("prompt_tokens")))
            print("\n--- content ---\n" + (content or "(empty)")[:1200])
            if not content:
                rep.fail("usable", "empty content — thinking ate the budget or parse is wrong")
                return 1
            rep.ok("usable", "content present")
            return 0
        finally:
            await res.aclose()

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cooker", description="polite background intelligence")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("-v", "--version", action="version", version=f"cooker {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check config, tools, ports, model, search")
    d.add_argument("--offline", action="store_true", help="skip network probes")
    d.set_defaults(func=cmd_doctor)

    b = sub.add_parser("db", help="database operations")
    b.add_argument("db_cmd", choices=["init", "selftest", "status"])
    b.set_defaults(func=cmd_db)

    s = sub.add_parser("status", help="one-line queue + state summary")
    s.set_defaults(func=lambda cfg, a: cmd_db(cfg, argparse.Namespace(db_cmd="status")))

    pr = sub.add_parser("probe", help="one real-size inference call, with accounting")
    pr.add_argument("-p", "--prompt", default="Give 3 concise ideas for a pixel-art kitchen UI.")
    pr.add_argument("--max-tokens", type=int, default=None)
    pr.add_argument("--no-thinking", action="store_true")
    pr.set_defaults(func=cmd_probe)

    sub.add_parser("run", help="start the daemon (M2)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        print("run: the daemon lands in M2 (detector + scheduler).")
        return 2
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"{FAIL} config: {exc}")
        return 1
    try:
        return int(args.func(cfg, args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
