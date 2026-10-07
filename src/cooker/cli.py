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
from datetime import date
from pathlib import Path
from typing import Any

from cooker import __version__, chains, daemon, db, detect, digest, net
from cooker import eval as eval_mod
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
        # WAL is not a performance detail here: the queue's atomic claim relies on
        # it, so a database that silently reverted to rollback journal mode means
        # two workers can take the same stage. Ask, don't assume.
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        (rep.ok if mode == "wal" else rep.fail)(
            "journal mode", mode + ("" if mode == "wal" else " — claim is not safe"))
        busy = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        (rep.ok if busy > 0 else rep.warn)("busy_timeout", f"{busy}ms")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        (rep.ok if integrity == "ok" else rep.fail)("integrity check", integrity)
    finally:
        conn.close()

    used = dir_bytes(cfg.outputs_dir)
    cap_gb = float(cfg.get("safety.max_disk_gb", 2.0))
    near = used > cap_gb * 1024**3 * 0.8
    (rep.warn if near else rep.ok)(
        "outputs disk", f"{used / 1024**2:.1f} MB of {cap_gb:.1f} GB")

    if not args.offline:
        asyncio.run(_net_checks(cfg, rep))

    # launchd. The plist does not exist until M8 installs it, so this is a warn
    # with the reason rather than a fail: the daemon running in a terminal today
    # and never restarting tomorrow is the difference this check exists to name.
    label = str(cfg.get("daemon.launchd_label", "com.homelab.cooker"))
    plist = Path(f"~/Library/LaunchAgents/{label}.plist").expanduser()
    if not plist.exists():
        rep.warn("launchd", f"{label} not installed — M8 (cooker will not survive "
                            "a reboot or a logout)")
    elif args.offline:
        rep.info("launchd", f"{plist.name} present; state not checked (--offline)")
    else:
        uid = os.getuid()
        out = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                             capture_output=True, text=True, timeout=10)
        loaded = out.returncode == 0
        state = "unknown"
        for line in (out.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith(("state =", "path =")):
                state = stripped.split("=", 1)[1].strip()
                break
        (rep.ok if loaded else rep.fail)(
            "launchd", f"{state}" if loaded else f"{label} not loaded")

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
    """Detector + scheduler. Dry-run unless `--live`.

    In dry-run the Kitchen never constructs an LLM object at all, so there is
    nothing in the process holding a connection to omlx to misuse. `--live`
    constructs it, and prints the guardrails as it does so, because a run that
    spends GPU time should say out loud what it is bounded by.
    """
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
            assert kitchen.llm is not None and kitchen.runner is not None
            print(f"  {INFO} spending GPU: model={kitchen.llm.model} "
                  f"prefill<= {kitchen.llm.ceiling}tok "
                  f"max_tokens>={kitchen.llm.floor} "
                  f"endpoint={kitchen.llm.resolver.base or 'resolving…'}")
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
    """`cooker add "a topic"` queues a whole research chain.

    The one-stage form (`--kind/--generator/--title`) stays, because poking a
    specific stage of an existing chain is a different job from asking for new
    research, and the daemon's own stages are worth being able to add by hand.

    A topic inside the dedup window is queued anyway, with a warning. The 14-day
    rule exists to stop the *daemon* repeating itself; a human naming the topic a
    second time is not the daemon repeating itself, and silently dropping the
    request would be the machine overruling the person who just typed into it.
    """
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        if args.topic:
            topic = " ".join(args.topic).strip()
            if not topic:
                print(f"{FAIL} add needs a topic: cooker add \"why prefill blocks\"")
                return 2
            if args.kind or args.title:
                print(f"{WARN} a topic makes the whole chain; --kind/--title ignored")
            days = float(cfg.get("quality.dedup_window_days", 14))
            if chains.seen_recently(conn, chains.topic_hash(topic), days=days):
                print(f"{WARN} \"{topic[:60]}\" was researched in the last {days:.0f}"
                      " days — queueing it anyway, you asked")
            chain = chains.seed(cfg, conn, topic, priority=args.priority)
            stages = conn.execute(
                "SELECT kind, id FROM tasks WHERE chain_id=? ORDER BY id",
                (chain,)).fetchall()
            print(f"{OK} queued a research chain: {chain[:8]}")
            # The chain is not all queued at once — each stage is created when its
            # parent succeeds — so listing "plan" alone is the truth, and saying
            # why stops it reading as a bug in the seeding.
            print(f"      now: {' → '.join(s['kind'] for s in stages)};"
                  " the rest appear as each stage finishes")
            return 0
        if not args.title:
            print(f"{FAIL} add needs a topic, or --title with --kind")
            return 2
        task = db.create_task(conn, chain_id=args.chain or db.new_id(),
                              kind=args.kind or "plan", generator=args.generator,
                              title=args.title, prompt=args.prompt,
                              priority=args.priority)
        print(f"{OK} queued {task.generator}.{task.kind}  {task.id}")
        return 0
    finally:
        conn.close()


# --- queue inspection (M6) -----------------------------------------------


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    """What is in here. Artifacts first, then the live half of the queue.

    ids print in the tail form `cooker rate` accepts, because the alternative is
    printing a uuid and then making you go and find the eight characters that
    actually resolve — and `[:8]` of a uuid7 is a timestamp, so the eight you would
    have copied identify nothing.
    """
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        limit = int(args.limit)
        what = args.what
        if what in ("artifacts", "all"):
            rows = conn.execute(
                "SELECT a.id, a.generator, a.title, a.status, a.path, a.created_at,"
                " t.chain_id FROM artifacts a LEFT JOIN tasks t ON t.id = a.task_id"
                " ORDER BY a.created_at DESC LIMIT ?", (limit,)).fetchall()
            print(f"artifacts ({len(rows)} shown)")
            if not rows:
                print("  none yet")
            for r in rows:
                ev = digest.score_for_chain(conn, str(r["chain_id"] or ""))
                score = (f"{ev['overall']:>4.2f}" if ev["verdict"] != "UNEVALUATED"
                         else "  —  ")
                age = _age(time.time() - float(r["created_at"]))
                print(f"  {eval_mod.short_id(str(r['id']))}  {score}  {r['status']:<10}"
                      f"  {age:>7}  {digest.strip_stage_prefix(str(r['title']))[:52]}")
        if what in ("queue", "all"):
            live = (db.QUEUED, db.RUNNING, db.PAUSED, db.PARKED)
            rows = conn.execute(
                "SELECT id, kind, generator, title, status, not_before, attempts"
                " FROM tasks WHERE status IN ({}) ORDER BY priority ASC, created_at"
                " DESC LIMIT ?".format(",".join("?" * len(live))),
                (*live, limit)).fetchall()
            print(f"\nlive queue ({len(rows)} shown)")
            if not rows:
                print("  nothing queued — the queue refills itself from topics.md")
            for r in rows:
                wait = float(r["not_before"] or 0) - time.time()
                held = f" backoff {wait:.0f}s" if wait > 0 else ""
                print(f"  {eval_mod.short_id(str(r['id']))}  {r['status']:<8}"
                      f" {r['generator']}.{r['kind']:<12}{held}"
                      f"  {r['title'][:40]}")
        if what in ("rejected", "failed"):
            rows = conn.execute(
                "SELECT id, kind, generator, title, status, error, finished_at"
                " FROM tasks WHERE status IN (?,?) ORDER BY finished_at DESC"
                " LIMIT ?", (db.REJECTED, db.FAILED, limit)).fetchall()
            print(f"\nnot published ({len(rows)} shown)")
            for r in rows:
                print(f"  {eval_mod.short_id(str(r['id']))}  {r['status']:<9}"
                      f" {r['generator']}.{r['kind']:<12}"
                      f" {str(r['error'] or '')[:56]}")
        return 0
    finally:
        conn.close()


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    """One thing, everything the database knows about it.

    The point of `show` is that you get the score, the evaluator's sentence, the
    path and the rate command without opening three tools, so all four are here and
    none of them is re-derived from the model.
    """
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        row = eval_mod.resolve_artifact(conn, args.id)
        t_row = None
        if row is not None:
            t_row = conn.execute("SELECT * FROM tasks WHERE id=?",
                                 (row["task_id"],)).fetchone() if row["task_id"] else None
        else:
            # A stage rather than an artifact: resolved the same way, because the
            # id came from the same list.
            t_row = eval_mod.resolve_task(conn, args.id)
            if t_row is None:
                print(f"{FAIL} nothing matches {args.id!r};"
                      " 'cooker list' shows what's here")
                return 1

        if row is not None:
            print(f"artifact {row['id']}")
            print(f"  {row['status']}  {row['generator']}  {row['title']}")
            print(f"  path   {row['path']}")
            p = Path(str(row["path"]))
            size = p.stat().st_size if p.exists() else None
            print(f"  file   {'exists' if size is not None else 'MISSING'}"
                  + (f"  ({size:,} bytes)" if size is not None else ""))
            print(f"  hash   {str(row['hash'])[:16]}…")
            chain_id = str(t_row["chain_id"]) if t_row else ""
            ev = digest.score_for_chain(conn, chain_id)
            if ev["verdict"] == "UNEVALUATED":
                print("  score  never evaluated — the gate did not run on this one")
            else:
                print(f"  score  {ev['overall']:.2f}  {ev['verdict']}"
                      + (f"  ({ev['axes']})" if ev["axes"] else ""))
                if ev["partial"]:
                    print(f"         *{ev['partial']}*")
                reason = digest.rationale_for_chain(conn, chain_id)
                if reason:
                    print(f"  judge  {reason}")
            print(f"  rate   cooker rate {eval_mod.short_id(str(row['id']))}"
                  " good|meh|bad")
            if chain_id:
                _print_chain(conn, chain_id)
            return 0

        assert t_row is not None
        print(f"stage {t_row['id']}")
        print(f"  {t_row['status']}  {t_row['generator']}.{t_row['kind']}"
              f"  {t_row['title']}")
        print(f"  chain  {t_row['chain_id']}")
        deps = json.loads(str(t_row["dependencies"] or "[]"))
        if deps:
            print(f"  deps   {', '.join(str(d)[:8] for d in deps)}")
        if t_row["error"]:
            print(f"  error  {t_row['error']}")
        if t_row["result_path"]:
            print(f"  file   {t_row['result_path']}")
        tin, tout = int(t_row["input_tokens"] or 0), int(t_row["output_tokens"] or 0)
        if tin or tout:
            print(f"  tokens {tin:,} in / {tout:,} out"
                  f"  ttft {float(t_row['ttft_ms'] or 0):.0f}ms"
                  f"  {float(t_row['duration_ms'] or 0):.0f}ms"
                  f"  preemptions {int(t_row['preemptions'] or 0)}")
        if float(t_row["not_before"] or 0) > time.time():
            print(f"  held   backoff until "
                  f"{_hhmmss(float(t_row['not_before']))}"
                  f" ({float(t_row['not_before']) - time.time():.0f}s)")
        _print_chain(conn, str(t_row["chain_id"]))
        evs = [e for e in db.tail_events(conn, limit=400)
               if e.get("task_id") == t_row["id"]][-8:]
        if evs:
            print("  events")
            for e in evs:
                print(f"    {_hhmmss(float(e['ts']))} {e['type']:<20}"
                      f" {str(e['message'] or '')[:60]}")
        return 0
    except ValueError as exc:
        print(f"{FAIL} {exc}")
        return 1
    finally:
        conn.close()


def _print_chain(conn: sqlite3.Connection, chain_id: str) -> None:
    st = chains.chain_state(conn, chain_id)
    if not st or not st.get("stages"):
        return
    print(f"  state  {st['state']}  ({st['done']}/{st['total']} stages)")
    for s in st["stages"]:
        print(f"           {s['kind']:<12} {s['status']}")


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


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


# --- the feedback loop (M5) ----------------------------------------------


def cmd_rate(cfg: Config, args: argparse.Namespace) -> int:
    """`cooker rate <id> good|meh|bad` — the entire RLHF input in one keystroke.

    Refuses an id it cannot resolve. A typo that silently records feedback is worse
    than a typo that errors, because the thing you read afterwards is the EMA, and
    it will have absorbed a rating attached to nothing.
    """
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        out = eval_mod.add_feedback(conn, args.id, args.rating,
                                     note=" ".join(args.note) or None)
    except ValueError as exc:
        print(f"{FAIL} {exc}")
        conn.close()
        return 1
    gen = out["generator"]
    floor = cfg.get("quality.feedback_ema_floor", 2.5)
    after = int(cfg.get("quality.feedback_park_after", 5))
    line = f"{OK} rated {gen} {args.rating}"
    if out["ema"] is not None:
        line += f"  ema={out['ema']:.2f}"
    n = eval_mod.rated_count(conn, gen) if gen else 0
    line += f"  ({n} rated; parks under {floor} after {after})"
    print(line)
    parked = eval_mod.parked_generators(cfg, conn)
    if gen in parked:
        print(f"{WARN} {gen} is parked — the scheduler will not claim from it.")
        print(f"      reason: {parked[gen]}")
        print(f"      'cooker unpark {gen}' to override, or let the EMA recover.")
    conn.close()
    return 0


def cmd_digest(cfg: Config, args: argparse.Namespace) -> int:
    """Write today's digest and print the path. `--day` for a specific date."""
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    day = None
    if getattr(args, "day", None):
        try:
            day = date.fromisoformat(args.day)
        except ValueError:
            print(f"{FAIL} --day wants YYYY-MM-DD, got {args.day!r}")
            conn.close()
            return 1
    path, published = digest.write(cfg, conn, day=day)
    print(f"{OK} wrote {path}  ({published} item(s) above threshold)")
    conn.close()
    return 0


def cmd_park(cfg: Config, args: argparse.Namespace) -> int:
    """Stop a generator by hand. The reason is stored, because future-you will ask."""
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    parked = eval_mod.park(conn, args.generator,
                           reason=" ".join(args.note) or "parked by hand")
    print(f"{OK} parked {args.generator}: {parked[args.generator]}")
    print(f"      allowed now: {', '.join(eval_mod.allowed_generators(cfg, conn) or [])}")
    conn.close()
    return 0


def cmd_unpark(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    eval_mod.unpark(conn, args.generator)
    allowed = eval_mod.allowed_generators(cfg, conn)
    print(f"{OK} unparked {args.generator}")
    if args.generator not in (allowed or []):
        print(f"{WARN} still not claimed: its feedback EMA is below the floor"
              f" ({eval_mod.generator_ema(conn, args.generator)})")
    conn.close()
    return 0


def cmd_feedback(cfg: Config, args: argparse.Namespace) -> int:
    """The loop, printed: what is allowed, what is parked, and the EMA of each."""
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    st = eval_mod.state(conn, cfg)
    print(f"threshold {st['threshold']}   "
          f"allowed {st['allowed'] if st['allowed'] != 'unfiltered' else 'unfiltered'}")
    for gen, ema in sorted(st["ema"].items()):
        n = eval_mod.rated_count(conn, gen)
        mark = "PARKED" if gen in st["parked"] else "ok"
        print(f"  {gen:<18} ema={'—' if ema is None else f'{ema:.2f}'}"
              f"  rated={n}  {mark}")
    for gen, reason in st["parked"].items():
        print(f"  {gen}: {reason}")
    conn.close()
    return 0


# --- status (M6) ------------------------------------------------------


def dir_bytes(path: Path) -> int:
    """Total size of a directory tree, 0 if it is not there.

    Not `du`: shelling out to measure our own output directory means the answer
    depends on a binary we have to find first, and `du`'s block rounding reports
    4 KB for a 200-byte file, which matters when the warning threshold is a
    fraction of the cap.
    """
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue  # a file that vanished mid-walk is not an error worth raising
    return total


def collect_status(cfg: Config, conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything `cooker status` says, as data, so the test can read the same
    structure the terminal reads rather than scraping stdout.

    Backoff is the part the spec asks for and the easy version omits: a queue
    count of 3 says nothing about whether those 3 are waiting for the GPU to free
    up or waiting on a parent stage that already failed. Those are different
    situations with different fixes, and only the first one resolves itself.
    """
    now = time.time()
    counts = db.queue_counts(conn)
    waiting = conn.execute(
        "SELECT id, kind, generator, not_before, preemptions FROM tasks"
        f" WHERE status = '{db.QUEUED}' AND not_before > ? ORDER BY not_before"
        " LIMIT 10", (now,)).fetchall()
    blocked = conn.execute(
        "SELECT id, kind, generator, error FROM tasks WHERE status = ?"
        " ORDER BY finished_at DESC LIMIT 10", (db.PARKED,)).fetchall()
    running = conn.execute(
        "SELECT id, kind, generator, started_at FROM tasks WHERE status = ?",
        (db.RUNNING,)).fetchall()

    def last(types: tuple[str, ...]) -> dict[str, Any] | None:
        ph = ",".join("?" * len(types))
        row = conn.execute(
            f"SELECT ts, type, message FROM events WHERE type IN ({ph})"
            " ORDER BY id DESC LIMIT 1", types).fetchone()
        return dict(row) if row else None

    state = last(("bus.state",))
    today_cost = digest.day_cost(conn, date.today())
    published = int(conn.execute(
        "SELECT COUNT(*) n FROM artifacts WHERE status = ?",
        (db.PUBLISHED,)).fetchone()["n"])
    # the same directory the writer writes into, not a second definition of it
    used = dir_bytes(cfg.outputs_dir)
    cap_gb = float(cfg.get("safety.max_disk_gb", 2.0))
    topics = chains.read_topics(cfg)
    st = eval_mod.state(conn, cfg)
    return {
        "paused": db.get_meta(conn, "paused") or "",
        "running": [dict(r) for r in running],
        "counts": counts,
        "backoff": [{
            "id": str(r["id"]), "kind": str(r["kind"]),
            "generator": str(r["generator"]),
            "remaining_s": round(float(r["not_before"]) - now, 1),
            "preemptions": int(r["preemptions"] or 0),
        } for r in waiting],
        "parked_stages": [{"id": str(r["id"]), "kind": str(r["kind"]),
                           "error": str(r["error"] or "")[:80]} for r in blocked],
        "state": state,
        # None means "nothing is waiting on a clock": an empty queue, or work that
        # is runnable right now. It is not zero, and printing zero here would be
        # inventing an ETA for dependency-blocked chains — the mistake a previous
        # commit had to go back and undo.
        "next_runnable_s": (round(eta - time.time(), 1)
                            if (eta := db.next_runnable_eta(conn)) else None),
        "generators": {"allowed": st["allowed"], "parked": st["parked"],
                       "ema": st["ema"]},
        "today": {**today_cost, "published": published},
        "digest": str(digest.digest_path(cfg, date.today())),
        "disk": {"bytes": used, "cap_bytes": int(cap_gb * 1024**3),
                 "near": used > cap_gb * 1024**3 * 0.8},
        "topics": {"count": len(topics), "next": topics[0] if topics else None},
    }


def _clip(text: str, width: int) -> str:
    """Cut to width at the last whole word.

    Mid-word truncation is how the panel came to read `keepalive: OrbStack He`,
    which looks like a bug in the detector rather than a bug in the formatter — and
    a reader who cannot tell which of the two it is stops trusting the panel.
    """
    t = str(text or "")
    if len(t) <= width:
        return t
    cut = t[:width]
    space = cut.rfind(" ")
    return (cut[:space] if space > width // 2 else cut).rstrip() + "…"


def _row(marker: str, label: str, value: str) -> str:
    """One aligned line. Padded here rather than in each caller because the
    markers carry ANSI escapes, so counting spaces in the source does not tell you
    where the column lands on screen — which is exactly how `disk` came out one
    character wide last time this was written by hand."""
    return f"{marker} {label:<9} {value}"


def render_status(cfg: Config, d: dict[str, Any]) -> str:
    threshold = float(cfg.get("quality.publish_threshold", 3.5))
    lines = [f"cooker {__version__}  queue", "-" * 58]
    if d["paused"]:
        lines.append(f"{WARN} PAUSED: {d['paused']}   ('cooker resume' to cook)")
    state = d["state"]
    if state:
        lines.append(f"{INFO} detector {_hhmmss(float(state['ts']))}"
                     f" {_clip(state['message'], 46)}")
    else:
        lines.append(f"{INFO} detector  no reading yet — run 'cooker watch'")

    live = {k: v for k, v in d["counts"].items()
            if k in (db.QUEUED, db.RUNNING, db.PAUSED, db.PARKED) and v}
    lines.append(f"{INFO} queue     " + (", ".join(f"{v} {k.lower()}"
                                                    for k, v in live.items())
                                           or "empty"))
    if d["running"]:
        for r in d["running"]:
            age = time.time() - float(r["started_at"] or time.time())
            lines.append(f"            running {r['generator']}.{r['kind']}"
                         f"  {age:.0f}s")

    # backoff, named. "3 queued" and "3 waiting on a dead parent" look identical
    # in a count and mean completely different things — only the first one resolves
    # itself while you are not looking at it.
    if d["backoff"]:
        lines.append(_row(INFO, "backoff", f"{len(d['backoff'])} stage(s) waiting:"))
        for b in d["backoff"]:
            why = (f"{b['preemptions']} preemption(s)" if b["preemptions"]
                   else "waiting")
            lines.append(f"            {eval_mod.short_id(b['id'])}"
                         f" {b['generator']}.{b['kind']:<12} {why}:"
                         f" {b['remaining_s']:.0f}s")
    if d["next_runnable_s"] is not None:
        lines.append(f"            next runnable in {d['next_runnable_s']:.0f}s")
    elif not live:
        lines.append("            nothing to run until a topic arrives or a parent"
                     " stage finishes")
    if d["parked_stages"]:
        lines.append(_row(WARN, "parked",
                          f"{len(d['parked_stages'])} stage(s) will not run as"
                          " things stand:"))
        for b in d["parked_stages"]:
            lines.append(f"            {eval_mod.short_id(b['id'])} {b['kind']:<12}"
                         f" {b['error'][:52]}")

    g = d["generators"]
    allowed = g["allowed"]
    # `None` and `[]` are different answers — unfiltered versus everything switched
    # off — and collapsing them is how a config edit that disabled every generator
    # reads as a healthy queue.
    allowed_text = (", ".join(allowed) if allowed
                    else "none" if isinstance(allowed, list) else str(allowed))
    lines.append(_row(INFO, "loop",
                      f"threshold {threshold}; allowed {allowed_text}"))
    for gen, ema in sorted((g["ema"] or {}).items()):
        if gen in (g["parked"] or {}):
            ema_text = "—" if ema is None else f"{ema:.2f}"
            lines.append(f"            {gen:<16} PARKED (ema {ema_text}):"
                         f" {str(g['parked'][gen])[:40]}")

    t = d["today"]
    lines.append(_row(INFO, "today",
                      f"{t['stages']} stages, {t['gpu_seconds']}s GPU,"
                      f" {t['input_tokens']:,} in / {t['output_tokens']:,} out,"
                      f" {t['published']} published"))
    lines.append(_row(INFO, "digest", str(d["digest"])))
    disk = d["disk"]
    lines.append(_row(WARN if disk["near"] else INFO, "disk",
                      f"{disk['bytes'] / 1024**2:.1f} MB of"
                      f" {disk['cap_bytes'] / 1024**3:.1f} GB"
                      + ("  (near the cap)" if disk["near"] else "")))
    if d["topics"]["next"]:
        lines.append(_row(INFO, "topics",
                          f"{d['topics']['count']} in the backlog;"
                          f" next: {str(d['topics']['next'])[:44]}"))
    else:
        lines.append(_row(WARN, "topics",
                          "topics.md is empty — the queue will not refill itself"))
    return "\n".join(lines)


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        d = collect_status(cfg, conn)
        if getattr(args, "json", False):
            print(json.dumps(d, indent=2, default=str))
        else:
            print(render_status(cfg, d))
        return 0
    finally:
        conn.close()


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

    s = sub.add_parser("status", help="queue, backoff, loop and cost in one panel")
    s.add_argument("--json", action="store_true",
                   help="machine-shaped output (this is also the SSE payload shape)")
    s.set_defaults(func=cmd_status)

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

    a = sub.add_parser("add", help="queue a topic (a whole chain) or one stage")
    a.add_argument("topic", nargs="*", help="the subject; queues a research chain")
    a.add_argument("-g", "--generator", default="research")
    a.add_argument("-k", "--kind", default=None)
    a.add_argument("-t", "--title", default=None)
    a.add_argument("-p", "--prompt", default=None)
    a.add_argument("--priority", type=int, default=500)
    a.add_argument("--chain", default=None)
    a.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="what is queued and what has been cooked")
    ls.add_argument("what", nargs="?", default="all",
                    choices=["all", "artifacts", "queue", "rejected"])
    ls.add_argument("-n", "--limit", type=int, default=20)
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="one artifact or stage, with its judgement")
    sh.add_argument("id", help="id, or the 8-char tail that list and digest print")
    sh.set_defaults(func=cmd_show)

    pa = sub.add_parser("pause", help="stop starting new stages")
    pa.add_argument("note", nargs="*", help="why, shown in the UI")
    pa.set_defaults(func=cmd_pause)

    sub.add_parser("resume", help="undo a pause").set_defaults(func=cmd_resume)

    rt = sub.add_parser("rate", help="rate a published artifact (the RLHF input)")
    rt.add_argument("id", help="artifact id, task id, or the 8-char prefix the digest prints")
    rt.add_argument("rating", choices=["good", "meh", "bad"])
    rt.add_argument("note", nargs="*", help="optional, why")
    rt.set_defaults(func=cmd_rate)

    dg = sub.add_parser("digest", help="write today's digest now")
    dg.add_argument("--day", default=None, help="YYYY-MM-DD (default: today)")
    dg.set_defaults(func=cmd_digest)

    fb = sub.add_parser("feedback", help="the loop: EMAs, what is allowed, what is parked")
    fb.set_defaults(func=cmd_feedback)

    pk = sub.add_parser("park", help="stop a generator by hand")
    pk.add_argument("generator")
    pk.add_argument("note", nargs="*", help="why, stored with the park")
    pk.set_defaults(func=cmd_park)

    up = sub.add_parser("unpark", help="re-enable a parked generator")
    up.add_argument("generator")
    up.set_defaults(func=cmd_unpark)
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
