"""M7 — the kitchen window: FastAPI on `127.0.0.1:8256`.

Two rules govern this whole file.

1. **Everything is a projection of the database.** No in-memory second source of
   truth. The queue, the events log, the artifacts table and `daily_stats` are
   what the daemon already writes; the API reads those, so a restarted server
   shows the true state and a page loaded two minutes ago can replay what it
   missed. SSE tails `events` by rowid exactly as `db.tail_events` was designed
   for — no broker, and nothing to keep in sync.
2. **The bus panel must never lie.** The live gate numbers (quiet seconds,
   cooldown remaining, blockers) only exist inside a running detector, so the
   server distinguishes *mounted* (`cooker serve` runs the kitchen in-process
   and reads `detector.snapshot()`) from *detached* (the last `bus.state` event,
   marked `stale: true`). 0 and unknown are different answers, and a UI that
   cannot tell them apart will report a cold kitchen as a dead one forever.

Bind stays loopback. Traefik reaches this through `host.docker.internal`; the
tailnet is the auth boundary, so there is no login here by design (OVERVIEW:
tailnet-trusted). `doctor` already guards that the port is free.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cooker import chains, db
from cooker import digest as digest_mod
from cooker import eval as eval_mod
from cooker.cli import collect_status
from cooker.config import Config

# 500ms poll on the events tail: cheap (one indexed range scan, LIMIT 200) and
# it is the *display* latency budget, not a detection one — detection lives in
# the detector and runs on its own cadence regardless of who is watching.
SSE_POLL_S = 0.5
# A comment frame every ~15s keeps Traefik's idle timeout from cutting a kitchen
# that has been quietly simmering through an empty quarter-hour.
SSE_HEARTBEAT_S = 15.0

# The six burners, in burner order (left to right, front counter).
BURNERS = ("research", "deep-dive", "project-review", "homelab-audit",
           "brainstorm", "creation")

# Stage kind -> how hard that stage boils. This is presentation, not policy: the
# mapping says "a `plan` stage is ingredients dropping in" because that is what
# it does, and `synthesize` is a rolling boil because it is the longest burn.
HEAT = {
    "plan": "filling", "search": "filling", "fetch": "filling",
    "scan": "filling", "collect": "filling", "seed": "filling",
    "extract": "simmer", "consolidate": "simmer", "analyze": "simmer",
    "evaluate": "simmer",  # the judge tastes, it does not boil
    "synthesize": "boiling", "critique": "boiling", "generate": "boiling",
    "write": "boiling", "publish": "plating",
}

# Events that mean "this pot just got shoved off the flame".
PREEMPT_EVENTS = ("llm.abort", "runner.cancelled", "task.paused")


def _day_bounds(day: date) -> tuple[float, float]:
    lo = time.mktime(day.timetuple())
    return lo, lo + 86400.0


def collect_pots(conn: sqlite3.Connection, cfg: Config,
                 now: float | None = None) -> dict[str, Any]:
    """One pot per generator, and the reasons the pot is doing what it does.

    The state machine here is deliberately derived, not stored: cold/waiting/
    filling/simmer/boiling come from the RUNNING row's *kind*, preempted comes
    from the last abort-style event still being recent, plated and swept come
    from today's artifact statuses, and taped comes from the same park list the
    scheduler enforces (`eval.state`). If the UI and the scheduler ever
    disagreed, this is where it would show up, so it reads the scheduler's own
    predicates rather than a parallel opinion.
    """
    now = now if now is not None else time.time()
    lo, hi = _day_bounds(date.today())
    state = eval_mod.state(conn, cfg)
    parked = state.get("parked") or {}
    allowed = state.get("allowed")
    cfg_gens = cfg.get("generators", {}) or {}

    running = {str(r["generator"]): r for r in conn.execute(
        "SELECT generator, id, kind, title, started_at, chain_id FROM tasks"
        f" WHERE status = '{db.RUNNING}'").fetchall()}
    per_gen = {str(r["generator"]): r for r in conn.execute(
        "SELECT generator,"
        " SUM(status = 'QUEUED') AS queued,"
        " MIN(CASE WHEN status = 'QUEUED' AND not_before > 0"
        "     THEN not_before ELSE NULL END) AS nb"
        " FROM tasks WHERE status IN ('QUEUED','RUNNING')"
        " GROUP BY generator").fetchall()}
    today = {str(r["generator"]): r for r in conn.execute(
        "SELECT generator,"
        " COUNT(*) AS stages,"
        " SUM(status = 'SUCCEEDED') AS done,"
        " SUM(status = 'FAILED') AS failed,"
        " COALESCE(SUM(preemptions),0) AS preemptions,"
        " COALESCE(SUM(output_tokens),0) AS tok_out"
        " FROM tasks"
        " WHERE COALESCE(started_at, created_at) >= ?"
        " AND COALESCE(started_at, created_at) < ? GROUP BY generator",
        (lo, hi)).fetchall()}
    plated = {str(r["generator"]): int(r["n"]) for r in conn.execute(
        "SELECT generator, COUNT(*) n FROM artifacts"
        " WHERE status = ? AND created_at >= ? GROUP BY generator",
        (db.PUBLISHED, lo)).fetchall()}
    swept = {str(r["generator"]): int(r["n"]) for r in conn.execute(
        "SELECT generator, COUNT(*) n FROM artifacts"
        " WHERE status = ? AND created_at >= ? GROUP BY generator",
        (db.REJECTED, lo)).fetchall()}
    # "recent" is a short window: smoke clears. An abort from two hours ago is
    # history, not the reason the burner is off *now*.
    recent_abort: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
            f"SELECT generator, type, message, ts FROM events"
            f" WHERE type IN {_in_sql(PREEMPT_EVENTS)} AND ts > ?"
            " ORDER BY id DESC", (now - 90.0,)).fetchall():
        if str(r["generator"] or "") not in recent_abort:
            recent_abort[str(r["generator"] or "")] = dict(r)

    pots: list[dict[str, Any]] = []
    for i, gen in enumerate(BURNERS):
        run = running.get(gen)
        q = per_gen.get(gen)
        t = today.get(gen)
        queued = int(q["queued"] or 0) if q else 0
        backoff_s = (round(float(q["nb"]) - now, 1)
                     if (q and q["nb"]) else None)
        abort = recent_abort.get(gen)

        if gen in parked or cfg_gens.get(gen, {}).get("enabled") is False:
            pot_state, note = "taped", str(parked.get(gen, "parked"))
        elif run is not None:
            pot_state = HEAT.get(str(run["kind"]), "boiling")
            note = str(run["title"] or run["kind"])
        elif abort is not None:
            pot_state = "preempted"
            note = str(abort["message"] or "preempted")
        elif queued:
            pot_state, note = "waiting", f"{queued} stage(s) on the pass"
        elif plated.get(gen):
            pot_state, note = "plated", "dinner is served"
        elif swept.get(gen):
            pot_state, note = "swept", "not good enough — swept"
        else:
            pot_state, note = "cold", ""

        # Chain progress, honestly: done/seen counts only the stages this chain
        # has *created* so far, because later stages of a research DAG do not
        # exist yet. 4/4 of a chain that is four stages deep mid-run would be
        # a lie while 2/7 is the truth with a question mark on the rest.
        progress: dict[str, Any] | None = None
        if run is not None:
            row = conn.execute(
                "SELECT COUNT(*) seen, SUM(status = 'SUCCEEDED') done"
                " FROM tasks WHERE chain_id = ?",
                (str(run["chain_id"]),)).fetchone()
            progress = {"done": int(row["done"] or 0), "seen": int(row["seen"] or 1)}

        pots.append({
            "burner": i,
            "generator": gen,
            "state": pot_state,
            "queued": queued,
            "backoff_s": backoff_s if pot_state == "preempted" or (
                backoff_s is not None and backoff_s > 0) else None,
            "running": None if run is None else {
                "id": eval_mod.short_id(str(run["id"])),
                "kind": str(run["kind"]),
                "title": str(run["title"] or "")[:90],
                "elapsed_s": round(now - float(run["started_at"] or now), 1),
                "max_s": float(cfg.get("detect.max_stage_seconds", 180)),
            },
            "progress": progress,
            "today": {
                "stages": int(t["stages"] or 0) if t else 0,
                "succeeded": int(t["done"] or 0) if t else 0,
                "failed": int(t["failed"] or 0) if t else 0,
                "preemptions": int(t["preemptions"] or 0) if t else 0,
                "tokens_out": int(t["tok_out"] or 0) if t else 0,
                "plated": int(plated.get(gen, 0)),
                "swept": int(swept.get(gen, 0)),
            },
            "note": note,
            "enabled": cfg_gens.get(gen, {}).get("enabled", True),
            # `allowed=None` means unfiltered; render that as "allowed" and
            # never as "blocked", which is the IN () freeze lesson again.
            "allowed_by_loop": allowed is None or gen in (allowed or []),
        })

    return {
        "pots": pots,
        # The digest is not a burner — it is the morning paper. The scene draws
        # it under the pass as a plated stack with its own little flag.
        "digest": {
            "path": str(digest_mod.digest_path(cfg, date.today())),
            "written": digest_mod.digest_path(cfg, date.today()).exists(),
        },
    }


def collect_state(cfg: Config, conn: sqlite3.Connection, kitchen: Any = None
                  ) -> dict[str, Any]:
    """The one payload the whole UI renders from: status + pots + live gate.

    `cooker status --json` is the tested shape and it is embedded verbatim
    under `status`, so anything the CLI can say the kitchen can show. The
    extras are the things only a live view needs: the gate countdowns, worker
    slots with elapsed time, and the recent artifacts the plates are drawn from.
    """
    now = time.time()
    out = collect_status(cfg, conn)
    out.update(collect_pots(conn, cfg, now))

    if kitchen is not None and hasattr(kitchen, "detector"):
        snap = kitchen.detector.snapshot().as_dict()
        snap.update({"stale": False, "ts": round(now, 1)})
        out["gate"] = snap
        sched = kitchen.scheduler
        out["workers"] = {
            "max": sched.max_workers,
            "running": len(sched.slots),
            "braked": sched.braked,
            "dry_run": sched.dry_run,
            "slots": [s.as_dict(now) for s in sched.sim.values()],
            "p95_ttft_s": (round(v, 2) if (v := sched._p95_ttft()) else None),
        }
        out["uptime_s"] = round(now - kitchen.started_at, 1)
    else:
        # Detached: the last known bus state, stamped and flagged. A viewer
        # must be able to see that nobody is home rather than infer it from a
        # kitchen that looks politely cold.
        last = conn.execute(
            "SELECT ts, data_json, message FROM events WHERE type = 'bus.state'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        gate: dict[str, Any] = {"stale": True}
        if last is not None:
            data = json.loads(last["data_json"] or "{}")
            gate.update({
                "state": data.get("to", "?"),
                "reason": str(last["message"] or "")[:120],
                "ts": round(float(last["ts"]), 1),
                "ready": False, "ramped": False, "blockers": [],
            })
        out["gate"] = gate
        out["workers"] = {"max": int(cfg.get("scheduler.max_concurrency", 1)),
                          "running": 0, "dry_run": None, "slots": []}
        out["uptime_s"] = None

    out["artifacts"] = [{
        "id": eval_mod.short_id(str(r["id"])),
        "full_id": str(r["id"]),
        "generator": str(r["generator"]),
        "title": str(r["title"])[:90],
        "score": (round(float(r["score"]), 2) if r["score"] is not None else None),
        "status": str(r["status"]),
        "age_s": round(now - float(r["created_at"]), 0),
    } for r in conn.execute(
        "SELECT id, generator, title, score, status, created_at FROM artifacts"
        " ORDER BY created_at DESC LIMIT 40").fetchall()]
    topics = chains.read_topics(cfg)
    out["topics"] = {"count": len(topics),
                     "list": [str(t)[:80] for t in topics[:10]]}
    out["ts"] = round(now, 1)
    return out


def _sse(row: dict[str, Any]) -> str:
    data = {
        "id": row["id"], "ts": row["ts"], "type": row["type"],
        "generator": row["generator"], "task_id": row["task_id"],
        "chain_id": row["chain_id"], "severity": row["severity"],
        "message": row["message"],
        "data": json.loads(row["data_json"]) if row["data_json"] else {},
    }
    return (f"id: {row['id']}\nevent: {row['type']}\n"
            f"data: {json.dumps(data, default=str)}\n\n")


def _read_conn(cfg: Config) -> sqlite3.Connection:
    """A detached server reads the same WAL the daemon writes. Busy-timeout
    because a checkpoint mid-read is normal SQLite weather, not an error."""
    conn = db.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _json(raw: Any) -> dict[str, Any]:
    """payload column to dict, forgivingly: a corrupt payload must not take
    the counter down with it."""
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def create_app(cfg: Config, kitchen: Any = None) -> FastAPI:
    """`kitchen=None` is a honest second-class mode, not a bug: the UI still
    renders the queue and plates it, but the gate panel says *stale*."""
    app = FastAPI(title="cooker", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.kitchen = kitchen

    def conn_for() -> tuple[sqlite3.Connection, bool]:
        """The mounted kitchen's connection when we share its loop (same thread,
        safe), else a fresh WAL reader for this request."""
        k = getattr(app.state, "kitchen", None)
        if k is not None:
            return k.conn, False
        return _read_conn(cfg), True

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        conn, own = conn_for()
        try:
            return JSONResponse(collect_state(cfg, conn, app.state.kitchen))
        finally:
            if own:
                conn.close()

    @app.get("/api/events")
    async def api_events(request: Request,
                         max_frames: int | None = None) -> StreamingResponse:
        # Replayable by design: the rowid is the cursor, the browser keeps it
        # across reconnects via Last-Event-ID, and the row never moves. A page
        # that slept through a preemption can still show the smoke when it wakes.
        # `max_frames` bounds the stream for curl and the tests — an infinite
        # generator on TestClient hangs until timeout, which masquerades as a
        # dead server (landmine: it looks like the upstream died; it is the
        # stream doing exactly what a live tail is supposed to do).
        header = request.headers.get("last-event-id")
        try:
            after = int(request.query_params.get("after")
                       or header or 0)
        except ValueError:
            after = 0

        async def stream():
            cursor = after
            sent = 0
            if cursor == 0:
                # A fresh page tails, it does not re-read history: with a full
                # events table, replaying from row 1 would page the whole log
                # down every phone that opens the kitchen.
                conn0, own0 = conn_for()
                try:
                    row = conn0.execute("SELECT MAX(id) m FROM events").fetchone()
                finally:
                    if own0:
                        conn0.close()
                cursor = max(0, int(row["m"] or 0) - 100)
            yield f"retry: {int(SSE_POLL_S * 1000)}\n\n"
            last_beat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                conn, own = conn_for()
                try:
                    rows = db.tail_events(conn, cursor, 200)
                finally:
                    if own:
                        conn.close()
                for row in rows:
                    cursor = int(row["id"])
                    sent += 1
                    yield _sse(row)
                    if max_frames is not None and sent >= max_frames:
                        yield "event: end\ndata: {}\n\n"
                        return
                if time.monotonic() - last_beat > SSE_HEARTBEAT_S:
                    last_beat = time.monotonic()
                    yield ": still cooking\n\n"
                await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/artifacts/{ref}")
    async def api_artifact(ref: str) -> PlainTextResponse:
        conn, own = conn_for()
        try:
            try:
                row = eval_mod.resolve_artifact(conn, ref)
            except ValueError as exc:  # ambiguous tail: refuse, don't guess
                return PlainTextResponse(str(exc), status_code=409)
            if row is None:
                return PlainTextResponse(f"no artifact {ref!r}", status_code=404)
            # The paths come from our own writer, but this is a GET on a
            # hostname anyone on the tailnet can spell: keep reads inside the
            # outputs tree no matter what a future row contains. All of it runs
            # in a thread — resolve() and read_text() are syscalls, and the loop
            # that serves this page is the same one the SSE tail runs on.
            def read_artifact(raw: str) -> tuple[str | None, int]:
                root = cfg.outputs_dir.resolve()
                path = Path(raw).resolve()
                if not path.is_relative_to(root):
                    return "artifact lives outside outputs_dir", 403
                if not path.is_file():
                    return f"file for {ref!r} is gone: {path}", 404
                return path.read_text("utf-8"), 200

            text, code = await asyncio.to_thread(read_artifact, str(row["path"]))
            if code != 200:
                return PlainTextResponse(text or "", status_code=code)
            return PlainTextResponse(
                text, media_type="text/markdown; charset=utf-8",
                headers={"X-Artifact-Status": str(row["status"])})
        finally:
            if own:
                conn.close()

    @app.get("/api/health")
    async def api_health() -> JSONResponse:
        k = getattr(app.state, "kitchen", None)
        return JSONResponse({
            "ok": True, "version": __import__("cooker").__version__,
            "dry_run": (bool(k.scheduler.dry_run) if k is not None else None),
            "ts": round(time.time(), 1),
        })

    @app.get("/api/metrics")
    async def api_metrics() -> JSONResponse:
        conn, own = conn_for()
        try:
            day = digest_mod.day_cost(conn, date.today())
            row = conn.execute(
                "SELECT requests, preemptions, published, rejected,"
                " ttft_sum_ms, ttft_count FROM daily_stats WHERE day = ?",
                (date.today().isoformat(),)).fetchone()
            metrics = {
                "day": date.today().isoformat(),
                "today": {**day,
                         "published": int(conn.execute(
                             "SELECT COUNT(*) n FROM artifacts WHERE status = ?"
                             " AND created_at >= ?",
                             (db.PUBLISHED, _day_bounds(date.today())[0])
                         ).fetchone()["n"])},
                "ttft_avg_ms": (round(float(row["ttft_sum_ms"]) / row["ttft_count"], 1)
                                if row and row["ttft_count"] else None),
                "rollup": None if row is None else dict(row),
            }
            return JSONResponse(metrics)
        finally:
            if own:
                conn.close()

    @app.get("/api/products")
    async def api_products() -> JSONResponse:
        """The pantry: every plate the kitchen has actually produced.

        A projection of the artifacts table joined against the stage that made
        it, newest first, file sizes stat'd in a thread so the SSE loop never
        blocks on a disk. Titles keep their `kind: ` prefix from the store —
        the pantry strips it for display and keeps `kind` as its own field,
        because a shelf of jars all starting with the same word tells you
        nothing about which one is the article.
        """
        conn, own = conn_for()
        try:
            rows = conn.execute(
                "SELECT a.id, a.generator, a.title, a.status, a.score,"
                " a.path, a.created_at, t.kind"
                " FROM artifacts a LEFT JOIN tasks t ON t.id = a.task_id"
                " ORDER BY a.created_at DESC LIMIT 300").fetchall()

            def build() -> list[dict[str, Any]]:
                out = []
                for r in rows:
                    try:
                        size: int | None = Path(str(r["path"])).stat().st_size
                    except OSError:
                        size = None
                    out.append({
                        "id": eval_mod.short_id(str(r["id"])),
                        "generator": r["generator"],
                        "kind": r["kind"] or Path(str(r["path"])).stem,
                        "title": str(r["title"]),
                        "status": r["status"],
                        "score": r["score"],
                        "bytes": size,
                        "day": time.strftime("%Y-%m-%d %H:%M",
                                            time.localtime(float(r["created_at"]))),
                    })
                return out

            products = await asyncio.to_thread(build)
            counts = {"published": sum(1 for p in products
                                        if p["status"] == db.PUBLISHED),
                     "candidate": sum(1 for p in products
                                      if p["status"] == db.CANDIDATE)}
            return JSONResponse({"products": products, "counts": counts})
        finally:
            if own:
                conn.close()

    @app.get("/api/stats")
    async def api_stats() -> JSONResponse:
        """The cook's books: per-day cost and per-generator spend.

        Days come from days that actually have stages — a chart with a bar of
        zeros every day the daemon was off would be the kind of honest that
        reads as a lie about the quiet. Published/rejected ride in from the
        daily rollup when it has them."""
        conn, own = conn_for()
        try:
            days = [str(r["d"]) for r in conn.execute(
                "SELECT DISTINCT strftime('%Y-%m-%d',"
                " COALESCE(started_at, finished_at), 'unixepoch','localtime') AS d"
                " FROM tasks WHERE COALESCE(started_at, finished_at) IS NOT NULL"
                " ORDER BY d DESC LIMIT 14").fetchall()]
            series = []
            for ds in reversed(days):
                cost = digest_mod.day_cost(conn, date.fromisoformat(ds))
                roll = conn.execute(
                    "SELECT requests, rejected, ttft_sum_ms, ttft_count"
                    " FROM daily_stats WHERE day = ?", (ds,)).fetchone()
                # published counts straight from artifacts — that table is the
                # truth of what was plated; the rollup is a cache of it.
                pub = int(conn.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE status = ?"
                    " AND date(created_at, 'unixepoch', 'localtime') = ?",
                    (db.PUBLISHED, ds)).fetchone()[0])
                series.append({
                    "day": ds, **cost,
                    "published": pub,
                    "rejected": int(roll["rejected"]) if roll else 0,
                    "ttft_avg_ms": (round(float(roll["ttft_sum_ms"])
                                           / int(roll["ttft_count"]), 1)
                                    if roll and roll["ttft_count"] else None),
                })
            gens = [
                {"generator": str(g["generator"]), "stages": int(g["stages"]),
                 "input_tokens": int(g["tin"]), "output_tokens": int(g["tout"]),
                 "gpu_seconds": round(float(g["ms"]) / 1000.0, 1),
                 "preemptions": int(g["pre"])}
                for g in conn.execute(
                    "SELECT generator, COUNT(*) AS stages,"
                    " COALESCE(SUM(input_tokens),0) AS tin,"
                    " COALESCE(SUM(output_tokens),0) AS tout,"
                    " COALESCE(SUM(duration_ms),0) AS ms,"
                    " COALESCE(SUM(preemptions),0) AS pre"
                    " FROM tasks"
                    " WHERE COALESCE(started_at, finished_at) IS NOT NULL"
                    " GROUP BY generator ORDER BY tout DESC").fetchall()]
            return JSONResponse({"days": series, "generators": gens})
        finally:
            if own:
                conn.close()

    @app.get("/api/digest")
    async def api_digest(
        date_str: str | None = Query(default=None, alias="date"),
    ) -> PlainTextResponse:
        """Today's menu — the digest markdown, by date, from our own writer."""
        d = date.today()
        if date_str:
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                return PlainTextResponse("date must be YYYY-MM-DD", status_code=400)
        path = digest_mod.digest_path(cfg, d)

        def read() -> tuple[str | None, int]:
            root = cfg.outputs_dir.resolve()
            p = path.resolve()
            if not p.is_relative_to(root):
                return "digest lives outside outputs_dir", 403
            if not p.is_file():
                return f"no digest for {d.isoformat()} yet", 404
            return p.read_text("utf-8"), 200

        text, code = await asyncio.to_thread(read)
        return PlainTextResponse(text or "", status_code=code,
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/api/orders")
    async def api_orders() -> JSONResponse:
        """Tickets at the counter: everything Sam has ordered, with the
        stage each order has reached. A projection of tasks whose payload
        says origin=sam — the queue is the order book, and this reads it."""
        conn, own = conn_for()
        try:
            rows = conn.execute(
                "SELECT id, chain_id, status, title, payload_json, score,"
                " created_at FROM tasks"
                " WHERE json_extract(payload_json, '$.origin') = 'sam'"
                " ORDER BY created_at DESC LIMIT 50").fetchall()
            orders = []
            for r in rows:
                prog = conn.execute(
                    "SELECT COUNT(*) AS n,"
                    " SUM(status IN ('SUCCEEDED','FAILED','REJECTED')) AS done"
                    " FROM tasks WHERE chain_id=?", (r["chain_id"],)).fetchone()
                orders.append({
                    "id": str(r["id"]),
                    "kind": str(_json(r["payload_json"]).get("order_kind")
                              or "research"),
                    "status": r["status"],
                    "topic": str(_json(r["payload_json"]).get("topic")
                                or r["title"]),
                    "score": r["score"],
                    "stages": int(prog["n"] or 0),
                    "done": int(prog["done"] or 0),
                    "when": time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(float(r["created_at"]))),
                })
            return JSONResponse({"orders": orders})
        finally:
            if own:
                conn.close()

    @app.post("/api/orders")
    async def api_orders_submit(request: Request) -> JSONResponse:
        """Write an order. research chains the five-stage self-sourced way;
        deep-dive delegates one opencode session. Both then face the same
        judge — ordering a task buys it attention, never a pass.

        Rate-limited by pending orders (429 with the count, not silence),
        and the master pause is honoured here at the counter: an ordered
        job does not unlock a closed kitchen."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "body must be json"},
                                status_code=400)
        kind = str(body.get("kind") or "research")
        topic = str(body.get("topic") or "").strip()
        if kind not in ("research", "deep-dive"):
            return JSONResponse({"ok": False,
                                 "error": "kind must be research or deep-dive"},
                                status_code=400)
        if not (8 <= len(topic) <= 200):
            return JSONResponse({"ok": False,
                                 "error": "topic must be 8-200 characters"},
                                status_code=400)
        conn, own = conn_for()
        try:
            note = db.is_paused(conn)
            if note is not None:
                return JSONResponse({"ok": False,
                                     "error": f"kitchen paused: {note}"},
                                    status_code=409)
            cap = int(cfg.get("orders.max_pending", 3))
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE"
                " json_extract(payload_json, '$.origin') = 'sam' AND status IN"
                " ('QUEUED','RUNNING','PAUSED')").fetchone()[0])
            if pending >= cap:
                return JSONResponse({"ok": False,
                                     "error": f"{pending} orders already pending "
                                            f"(cap {cap}); some must finish first"},
                                    status_code=429)
            if kind == "research":
                chain = chains.seed(cfg, conn, topic, source="order")
                # stamp the head so the counter can find its own tickets:
                # the research seed writes its own payload shape, and the
                # order book reads one field — origin=sam — off the head.
                head = conn.execute(
                    "SELECT id FROM tasks WHERE chain_id=?"
                    " AND parent_task_id IS NULL", (chain,)).fetchone()
                if head:
                    db.merge_payload(conn, str(head["id"]),
                                    {"origin": "sam", "order_kind": "research"})
            else:
                chain = chains.seed_deepdive(cfg, conn, topic)
                head = conn.execute(
                    "SELECT id FROM tasks WHERE chain_id=?"
                    " AND parent_task_id IS NULL", (chain,)).fetchone()
                if head:
                    db.merge_payload(conn, str(head["id"]),
                                    {"order_kind": "deep-dive"})
            db.emit(conn, "order.taken", message=f"{kind}: {topic[:90]}",
                    data={"chain_id": chain, "kind": kind})
            return JSONResponse({"ok": True, "chain_id": chain, "kind": kind})
        finally:
            if own:
                conn.close()

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    else:
        @app.get("/")
        async def no_ui() -> PlainTextResponse:
            return PlainTextResponse(
                "cooker api only — the kitchen is not built yet:\n"
                "  cd web && npm install && npm run build\n")

    return app


def _in_sql(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"
