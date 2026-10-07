"""`cooker` command line.

`doctor` and `db` are M1. M2 adds the parts that watch and plan: `watch` (the
detector, live), `run` (detector + scheduler, dry-run unless you say `--live`),
and `add` / `pause` / `resume` so there is something in the queue to point at and
a way to stop it from another terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cooker import __version__, daemon, db, detect, net
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

    # nettop is not optional any more: bytes are how inference is detected, so
    # without it the detector can name the peers but not say whether any of them
    # is generating, and it will (correctly) decline to cook forever.
    for tool in ("lsof", "ioreg", "ps", "git", "nettop"):
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

    # A resident probe that never samples looks exactly like an idle machine from
    # the outside, so check it produces numbers rather than assuming it will.
    probe = detect.WireProbe(cfg)
    if not probe.start():
        rep.fail("wire probe", f"cannot start nettop: {probe.last_error}")
    else:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6.0 and not probe.snapshot().up:
            time.sleep(0.25)
        snap = probe.snapshot()
        probe.stop()
        if snap.up:
            rep.ok("wire probe", f"{snap.samples} samples in {snap.window_s:.0f}s "
                                 f"window, floor {cfg.get('detect.infer_min_bps')} B/s")
        else:
            rep.fail("wire probe", "nettop running but produced no samples — "
                                    "inference cannot be detected")

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
            counts = db.queue_counts(conn)
            eta = db.next_runnable_eta(conn)
            running = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status=?", (db.RUNNING,)
            ).fetchone()["n"]
            print(json.dumps({
                "db": str(cfg.db_path),
                "queue": counts,
                "running": running,
                "runnable_now": db.runnable_now(conn),
                "next_runnable_in_s": round(eta - time.time(), 1) if eta else None,
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "artifacts": conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            }, indent=2))
            return 0
    finally:
        conn.close()
    return 2


# --- watch and run ----------------------------------------------------


def _hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    """The detector, narrated. No scheduler, no queue, no inference: this is how
    you check that Cooker reads the room correctly before it ever cooks, and the
    exit summary prints the probe budget so 'how expensive is watching?' has a
    number attached to it."""
    async def go() -> int:
        conn = db.connect(cfg.db_path)
        db.migrate(conn)
        det = detect.Detector(cfg, conn)
        # Without the resident wire reader the detector can name the peers but
        # not say whether any of them is generating, so it reports blind. Start
        # it here for the same reason the daemon does: watching is the whole job.
        wire_up = bool(det.sampler.start())
        print(f"cooker {__version__} watching {args.seconds}s "
              f"(cadence {det.tick_seconds}s, ctrl-c for the summary)")
        print(f"wire reader: {'nettop up' if wire_up else 'NOT RUNNING '
              '(peers visible, traffic invisible => will report blind)'}")
        print("-" * 76)
        last: tuple[str, bool] | None = None
        beats = 0.0
        ticks = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.seconds:
            st = await det.tick()
            ticks += 1
            key = (st.state, st.ready)
            if key != last or time.monotonic() - beats > 10:
                beats = time.monotonic()
                last = key
                gate = "ready to cook" if st.ready else "; ".join(st.blockers)
                # `cooking` is who moves bytes, `clients` who holds a socket:
                # showing both is the whole lesson of bench #7 in one column.
                who = ",".join(st.cooking) if st.cooking else (
                    f"{len(st.clients)} connected, silent" if st.clients else "-")
                print(f"{_hhmmss(st.ts):>9}  {st.state:<12} {gate[:34]:<36} "
                      f"{st.reason[:30]:<32} {who[:18]}")
            await asyncio.sleep(det.tick_seconds)
        det.sampler.stop()
        print("-" * 76)
        print("probe cost on this box:")
        for name, ms in sorted((kv for kv in det.signals.cost_ms.items()
                                if kv[0] != "wire"), key=lambda kv: -kv[1]):
            cad = det.sampler.cadence.get(name, 1.0)
            print(f"  {name:<9} {ms:6.0f}ms per probe, every {cad:>4.1f}s "
                  f"= ~{ms / (cad * 1000) * 100:.1f}% of a core")
        # The wire's cost is paid in its own thread, so it never shows up in
        # `cost_ms` above. Printing 0ms for it would understate the one probe
        # that decides whether the kitchen lights.
        # The wire pays its cost in its own thread, so it never appears in the
        # table above. Totals rather than per-cycle: one cycle's CPU depends on
        # what else the box was doing at that instant, and quoting one of them as
        # "the" cost of the probe has already produced a number I could not repeat.
        probe = det.sampler.wire
        cad = det.sampler.cadence.get("wire", 1.0)
        if probe.cycles:
            per = probe.cpu_total_ms / probe.cycles
            print(f'  {"wire":<9} {probe.cpu_total_ms:6.0f}ms cpu total, {probe.cycles} '
                  f"runs = {per:.0f}ms/run here")
            if per > 400:
                print(f"            note: nettop run alone in a shell costs ~60ms. It "
                      f"costs {per:.0f}ms here because lsof, ioreg and ps are walking "
                      f"the same kernel tables at the same time. The honest total is "
                      f"the line below, not this one.")
        wall = time.monotonic() - t0
        # indices 0,1 are this process's own user/sys time; the children — lsof,
        # nettop, ioreg, ps, which is where all the work actually happens — are
        # indices 2,3. Reading only 0,1 reported 0.04 cores for a run that cost
        # ten times that, because the daemon itself is nearly idle and its
        # subprocesses are not.
        ut = os.times()
        cpu = ut[0] + ut[1] + ut[2] + ut[3]
        print(f"whole process over {wall:.1f}s: {cpu:.1f}s cpu "
              f"(of which {ut[2] + ut[3]:.1f}s in subprocesses) = {cpu / wall:.2f} cores, "
              f"{cpu / wall / (os.cpu_count() or 1) * 100:.1f}% of "
              f"{os.cpu_count()} cores")
        st = det.snapshot()
        print(f"{ticks} ticks. final {det.state}: "
              f"{'ready to cook' if st.ready else '; '.join(st.blockers)}")
        conn.close()
        return 0

    return asyncio.run(go())


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    """Detector + scheduler. Dry-run unless `--live`, and `--live` still has
    nothing to run until M3 hands us a cancellable llm.py — by design, because
    the traffic control has to be right before it ever touches the GPU."""
    async def go() -> int:
        conn = db.connect(cfg.db_path)
        db.migrate(conn)
        orphans = db.recover_orphans(conn)
        kitchen = daemon.Kitchen(cfg, conn, dry_run=not args.live)
        mode = "LIVE" if args.live else "dry-run"
        print(f"cooker {__version__} ({mode})  max_concurrency="
              f"{kitchen.scheduler.max_workers}  db={cfg.db_path}")
        print(f"  idle_confirm={kitchen.detector.idle_confirm:.0f}s "
              f"cooldown={kitchen.detector.cooldown:.0f}s "
              f"stage<= {kitchen.scheduler.max_stage_s:.0f}s")
        if orphans:
            print(f"{INFO} recovered {orphans} orphaned stage(s)")
        if args.live:
            print(f"{WARN} --live with no stage runner: stages will be claimed and"
                  f" requeued. llm.py lands in M3.")
        print("-" * 76)
        summary = await kitchen.run(seconds=args.seconds)
        print("-" * 76)
        print(f"stopped after {summary['uptime_s']:.0f}s  state={summary['state']}  "
              f"ready={summary['ready']}")
        print(f"  queue={json.dumps(summary['queue'])} "
              f"runnable_now={summary['runnable_now']}")
        if summary["seconds_in_state"]:
            spent = ", ".join(f"{k} {v:.0f}s"
                              for k, v in summary["seconds_in_state"].items())
            print(f"  time in: {spent}")
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 130


def cmd_add(cfg: Config, args: argparse.Namespace) -> int:
    """Put one stage in the queue. The generator chains land in M4; until then
    this is how you give the scheduler something real to point at."""
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    task = db.create_task(conn, chain_id=args.chain or db.new_id(), kind=args.kind,
                          generator=args.generator, title=args.title,
                          prompt=args.prompt, priority=args.priority)
    print(f"{OK} queued {task.generator}.{task.kind}  {task.id}")
    conn.close()
    return 0


def cmd_pause(cfg: Config, args: argparse.Namespace) -> int:
    """Stop starting new work, from any terminal. Stages in flight finish; the
    daemon reads this flag every tick."""
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    # nargs="*" hands us a list; SQLite cannot bind one, and silently str()ing
    # it would store "['sam', 'is', 'presenting']" as the reason you see in the UI.
    db.set_meta(conn, "paused", " ".join(args.note) or "paused from the CLI")
    print(f"{OK} paused. 'cooker resume' to let it cook again.")
    conn.close()
    return 0


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    db.set_meta(conn, "paused", "")
    print(f"{OK} resumed")
    conn.close()
    return 0


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

    w = sub.add_parser("watch", help="run the detector and narrate the bus state")
    w.add_argument("-s", "--seconds", type=float, default=30.0,
                   help="how long to watch (default 30)")
    w.set_defaults(func=lambda cfg, a: cmd_watch(cfg, a))

    r = sub.add_parser("run", help="detector + scheduler (dry-run by default)")
    group = r.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true",
                       help="really claim stages. Needs llm.py, which lands in M3")
    # Dry-run is already the default, but it is the word the design uses for
    # everything, so accepting it beats correcting the user every time.
    group.add_argument("--dry-run", action="store_true",
                       help="the default: decide and narrate, never generate")
    r.add_argument("-s", "--seconds", type=float, default=None,
                   help="stop after N seconds (default: run until interrupted)")
    r.set_defaults(func=lambda cfg, a: cmd_run(cfg, a))

    a = sub.add_parser("add", help="queue one stage (full chains land in M4)")
    a.add_argument("-g", "--generator", default="research")
    a.add_argument("-k", "--kind", default="plan")
    a.add_argument("-t", "--title", required=True)
    a.add_argument("-p", "--prompt", default=None)
    a.add_argument("--priority", type=int, default=500)
    a.add_argument("--chain", default=None)
    a.set_defaults(func=cmd_add)

    pa = sub.add_parser("pause", help="stop starting new stages")
    pa.add_argument("note", nargs="*", help="why, shown in the UI")
    pa.set_defaults(func=cmd_pause)

    sub.add_parser("resume", help="undo a pause").set_defaults(func=cmd_resume)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
