"""Storage: SQLite in WAL mode, and the atomic DAG claim.

Two invariants matter more than anything else in this file:

1. **One worker, one task.** `claim_next()` is a single UPDATE ... RETURNING
   inside BEGIN IMMEDIATE. Two workers can never take the same row, so we can
   grow `max_concurrency` later without inventing a lock manager.

2. **Work survives restarts.** Nothing is ever silently lost: preemptions and
   failures re-queue with a backoff, rejects are kept on disk, and orphans left
   RUNNING by a crash are recovered to QUEUED at startup.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# --- statuses (exactly as specced in OVERVIEW.md) ------------------------
QUEUED = "QUEUED"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
PARKED = "PARKED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"

TASK_STATUSES = (QUEUED, RUNNING, PAUSED, SUCCEEDED, FAILED, PARKED, CANCELLED, REJECTED)
# Statuses that mean "this dependency is done for good, unblock whoever waits on it".
DEPS_SATISFIED = (SUCCEEDED,)

TERMINAL = (SUCCEEDED, FAILED, PARKED, CANCELLED, REJECTED)


def _in_sql(values: tuple[str, ...]) -> str:
    """Render a SQL literal list. Never use `%s % tuple` for this: a 1-tuple
    renders as ('X',) and is a syntax error."""
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


_CHECK_STATUS = f"CHECK (status IN {_in_sql(TASK_STATUSES)})"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per *stage instance*. The DAG lives here: dependencies is a JSON
-- array of task ids that must all be SUCCEEDED before this row is claimable.
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    chain_id       TEXT NOT NULL,
    job_id         TEXT,
    kind           TEXT NOT NULL,
    generator      TEXT NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL {_CHECK_STATUS},
    priority       INTEGER NOT NULL DEFAULT 500,
    prompt         TEXT,
    payload_json   TEXT NOT NULL DEFAULT '{{}}',
    dependencies   TEXT NOT NULL DEFAULT '[]',
    result_path    TEXT,
    result_hash    TEXT,
    parent_task_id TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    preemptions    INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    ttft_ms        REAL,
    duration_ms    REAL,
    score          REAL,
    scores_json    TEXT,
    error          TEXT,
    not_before     REAL NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    started_at     REAL,
    finished_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks (status, not_before, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_chain ON tasks (chain_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_gen_status ON tasks (generator, status);

-- Append-only log. The UI is a projection of this: SSE tails it by rowid, so
-- the animation can replay across a daemon restart without a broker.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    type       TEXT NOT NULL,
    generator  TEXT,
    task_id    TEXT,
    chain_id   TEXT,
    severity   TEXT NOT NULL DEFAULT 'info',
    message    TEXT,
    data_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_id ON events (id);

-- Published (and rejected) artifacts, indexed for the UI and for dedup.
CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    generator  TEXT NOT NULL,
    title      TEXT NOT NULL,
    path       TEXT NOT NULL,
    hash       TEXT NOT NULL,
    score      REAL,
    scores_json TEXT,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts (created_at DESC);

-- User feedback -> per-generator EMA. The scheduler stops cooking what you skip.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    rating     TEXT NOT NULL CHECK (rating IN ('good','meh','bad')),
    note       TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    overall     REAL NOT NULL,
    verdict     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Topic/url fingerprints, for the 14-day "don't cook this twice" rule.
CREATE TABLE IF NOT EXISTS seen (
    hash       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    ref        TEXT,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1
);

-- Rolled-up daily counters so `cooker status` and the HUD do not scan tasks.
CREATE TABLE IF NOT EXISTS daily_stats (
    day             TEXT PRIMARY KEY,
    requests        INTEGER NOT NULL DEFAULT 0,
    preemptions     INTEGER NOT NULL DEFAULT 0,
    published       INTEGER NOT NULL DEFAULT 0,
    rejected        INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    ttft_sum_ms     REAL NOT NULL DEFAULT 0,
    ttft_count      INTEGER NOT NULL DEFAULT 0
);
"""


def now() -> float:
    return time.time()


def new_id() -> str:
    return str(uuid.uuid7())


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a WAL connection. SQLite allows only one writer at a time, which
    is what makes the claim statement safe; busy_timeout covers the waits."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-16000")  # 16MB, plenty for a sidecar
    return conn


def migrate(conn: sqlite3.Connection) -> bool:
    """Create tables if absent. Returns True if this run created the schema."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    fresh = existing is None
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return fresh


# --- events ---------------------------------------------------------------


def emit(
    conn: sqlite3.Connection,
    type_: str,
    *,
    message: str | None = None,
    generator: str | None = None,
    task_id: str | None = None,
    chain_id: str | None = None,
    severity: str = "info",
    data: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (ts, type, generator, task_id, chain_id, severity, message, data_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            now(),
            type_,
            generator,
            task_id,
            chain_id,
            severity,
            message,
            json.dumps(data, separators=(",", ":")) if data else None,
        ),
    )
    return int(cur.lastrowid or 0)


def tail_events(conn: sqlite3.Connection, after: int = 0, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (after, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# --- the DAG ------------------------------------------------------------


@dataclass
class Task:
    id: str
    chain_id: str
    kind: str
    generator: str
    title: str
    status: str
    prompt: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    parent_task_id: str | None = None
    priority: int = 500
    attempts: int = 0
    preemptions: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            chain_id=row["chain_id"],
            kind=row["kind"],
            generator=row["generator"],
            title=row["title"],
            status=row["status"],
            prompt=row["prompt"],
            payload=json.loads(row["payload_json"] or "{}"),
            dependencies=json.loads(row["dependencies"] or "[]"),
            parent_task_id=row["parent_task_id"],
            priority=row["priority"],
            attempts=row["attempts"],
            preemptions=row["preemptions"],
        )


def create_task(
    conn: sqlite3.Connection,
    *,
    chain_id: str,
    kind: str,
    generator: str,
    title: str,
    prompt: str | None = None,
    payload: dict[str, Any] | None = None,
    dependencies: list[str] | None = None,
    parent_task_id: str | None = None,
    priority: int = 500,
    not_before: float = 0.0,
    job_id: str | None = None,
    task_id: str | None = None,
) -> Task:
    """Insert a QUEUED stage. `dependencies` is what makes this a DAG rather
    than a queue: the stage is unclaimable until every id listed SUCCEEDs."""
    tid = task_id or new_id()
    deps = list(dependencies or [])
    conn.execute(
        "INSERT INTO tasks (id, chain_id, job_id, kind, generator, title, status, priority,"
        " prompt, payload_json, dependencies, parent_task_id, not_before, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tid,
            chain_id,
            job_id,
            kind,
            generator,
            title,
            QUEUED,
            priority,
            prompt,
            json.dumps(payload or {}, separators=(",", ":")),
            json.dumps(deps, separators=(",", ":")),
            parent_task_id,
            not_before,
            now(),
        ),
    )
    emit(conn, "task.queued", task_id=tid, chain_id=chain_id, generator=generator,
         message=title, data={"kind": kind, "deps": len(deps)})
    return Task(
        id=tid, chain_id=chain_id, kind=kind, generator=generator, title=title,
        status=QUEUED, prompt=prompt, payload=payload or {}, dependencies=deps,
        parent_task_id=parent_task_id, priority=priority,
    )


# --- the runnable predicate, defined once -------------------------------
# claim_next, peek_runnable, next_runnable_eta and runnable_now all need to
# answer "is this stage claimable if the clock says so?". Four hand-copied
# versions of that predicate is a bug waiting to happen: "peek says runnable but
# claim says no" is a race that only appears under load and reads as a phantom
# stall. All of them require `tasks` aliased as t.
_DEPS_OK = (
    "NOT EXISTS ("
    "  SELECT 1 FROM json_each(t.dependencies) d"
    "  LEFT JOIN tasks p ON p.id = d.value"
    f"  WHERE p.id IS NULL OR p.status NOT IN {_in_sql(DEPS_SATISFIED)})"
)

_ORDER = "ORDER BY t.priority ASC, t.not_before ASC, t.created_at ASC"


def _claim_sql(generators: list[str] | None) -> tuple[str, dict[str, Any]]:
    """Build the claim statement. Built rather than `.replace()`d on purpose:
    str.replace edits *every* occurrence, so a second QUEUED predicate anywhere in
    the statement would get the generator filter bolted onto it too."""
    extra, params = "", {}
    if generators:
        ph = ",".join(f":g{i}" for i in range(len(generators)))
        extra = f" AND t.generator IN ({ph})"
        params = {f"g{i}": g for i, g in enumerate(generators)}
    params["now"] = now()
    sql = (
        f"UPDATE tasks SET status = '{RUNNING}', started_at = :now,"
        " attempts = attempts + 1 WHERE id = ("
        f"  SELECT t.id FROM tasks t WHERE t.status = '{QUEUED}'"
        f"    AND t.not_before <= :now{extra} AND {_DEPS_OK} {_ORDER} LIMIT 1)"
        " RETURNING *"
    )
    return sql, params


def claim_next(conn: sqlite3.Connection, *, generators: list[str] | None = None) -> Task | None:
    """Atomically take the highest-priority runnable stage, or return None.

    BEGIN IMMEDIATE grabs the write lock up front, so a second worker calling
    this concurrently blocks on busy_timeout and then sees the row already
    RUNNING — it cannot claim the same stage twice.
    """
    for attempt in range(2):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                sql, params = _claim_sql(generators)
                row = conn.execute(sql, params).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return Task.from_row(row) if row else None
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt == 0:
                time.sleep(0.05)
                continue
            raise
    return None


def peek_runnable(conn: sqlite3.Connection, limit: int = 8) -> list[Task]:
    """The rows claim_next would take, without taking them. Lets the dry-run
    scheduler point at real queued work instead of inventing any."""
    rows = conn.execute(
        f"SELECT t.* FROM tasks t WHERE t.status = '{QUEUED}'"
        f" AND t.not_before <= ? AND {_DEPS_OK} {_ORDER} LIMIT ?",
        (now(), limit),
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def finish(
    conn: sqlite3.Connection,
    task_id: str,
    status: str,
    *,
    result_path: str | None = None,
    result_hash: str | None = None,
    error: str | None = None,
    score: float | None = None,
    scores: dict[str, Any] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    ttft_ms: float | None = None,
    duration_ms: float | None = None,
) -> None:
    """Close out a stage. Rejected/failed work keeps its row — never deleted."""
    if status not in TERMINAL and status != PAUSED:
        raise ValueError(f"bad terminal status: {status}")
    conn.execute(
        "UPDATE tasks SET status=?, result_path=COALESCE(?, result_path),"
        " result_hash=COALESCE(?, result_hash), error=?, score=COALESCE(?, score),"
        " scores_json=COALESCE(?, scores_json),"
        " input_tokens=COALESCE(?, input_tokens), output_tokens=COALESCE(?, output_tokens),"
        " ttft_ms=COALESCE(?, ttft_ms), duration_ms=COALESCE(?, duration_ms),"
        " finished_at=? WHERE id=?",
        (
            status, result_path, result_hash, error, score,
            json.dumps(scores, separators=(",", ":")) if scores else None,
            input_tokens, output_tokens, ttft_ms, duration_ms, now(), task_id,
        ),
    )


def requeue(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    backoff_seconds: float,
    preempted: bool = False,
) -> None:
    """Put a stage back in the queue with a delay. This is the preemption path:
    the stage is the unit of resumption, so we lose one stage, not one job."""
    conn.execute(
        "UPDATE tasks SET status=?, not_before=?, preemptions=preemptions+?,"
        " error=?, finished_at=NULL WHERE id=?",
        (QUEUED, now() + backoff_seconds, 1 if preempted else 0, reason, task_id),
    )
    emit(conn, "task.paused", task_id=task_id, severity="warn",
         message=f"{reason} (backoff {int(backoff_seconds)}s)")


def park(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?",
        (PARKED, reason, now(), task_id),
    )
    emit(conn, "task.parked", task_id=task_id, severity="warn", message=reason)


def recover_orphans(conn: sqlite3.Connection) -> int:
    """A daemon that died mid-stage must not leave rows stuck in RUNNING
    forever — nothing would ever claim them again."""
    rows = conn.execute("SELECT id, chain_id, generator, title FROM tasks WHERE status=?",
                        (RUNNING,)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE tasks SET status=?, finished_at=NULL, error='recovered from restart'"
            " WHERE id=?",
            (QUEUED, r["id"]),
        )
        emit(conn, "task.recovered", task_id=r["id"], chain_id=r["chain_id"],
             generator=r["generator"], message=r["title"])
    propagate_blocked(conn)
    return len(rows)


def propagate_blocked(conn: sqlite3.Connection) -> int:
    """Park QUEUED stages whose dependencies can never succeed. Without this a
    failed `fetch` leaves its `synthesize` retrying forever."""
    cur = conn.execute(
        "UPDATE tasks SET status=?, error='dependency failed or cancelled',"
        " finished_at=? WHERE status=? AND EXISTS ("
        "  SELECT 1 FROM json_each(tasks.dependencies) d"
        "  LEFT JOIN tasks p ON p.id=d.value"
        f"  WHERE p.id IS NULL OR p.status IN ('{FAILED}','{CANCELLED}','{REJECTED}'))",
        (PARKED, now(), QUEUED),
    )
    return cur.rowcount or 0


# --- meta (daemon-to-CLI handoff) ---------------------------------------


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """A durable note the CLI can leave for the running daemon. `cooker pause`
    writes one; the scheduler reads it every tick. A file in the DB rather than
    a signal, so it survives a restart and a closed terminal."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def merge_payload(conn: sqlite3.Connection, task_id: str,
                  patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch into a stage's payload and return the result.

    Merge, not replace. A stage's payload is written by whoever queued it (the
    topic, the candidates) and by whatever the stage discovered (the sources it
    kept); a blind overwrite loses the topic and the chain forgets what it was
    researching, which is the sort of bug that shows up three stages late as a
    nonsense artifact rather than here as an error.
    """
    row = conn.execute("SELECT payload_json FROM tasks WHERE id=?",
                       (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such task: {task_id}")
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(patch)
    conn.execute("UPDATE tasks SET payload_json=? WHERE id=?",
                 (json.dumps(payload, separators=(",", ":")), task_id))
    return payload


# --- reporting ----------------------------------------------------------


def queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts by status, always the full shape.

    `GROUP BY` returns no rows for an empty queue, so a caller who writes
    `counts[QUEUED]` to ask "is there work?" gets a KeyError at exactly the
    moment there is no work — which is the state a self-refilling queue spends
    most of its life in. Every status is present, zeroed.
    """
    counts = dict.fromkeys(TASK_STATUSES, 0)
    for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall():
        counts[r["status"]] = r["n"]
    return counts


def next_runnable_eta(conn: sqlite3.Connection) -> float | None:
    """When the next *claimable-if-time-passed* stage stops waiting, or None if
    nothing is claimable at all. Ignores stages blocked on dependencies: waiting
    for a dependency is not a timer, and inventing an ETA for one is how a HUD
    starts lying."""
    row = conn.execute(
        f"SELECT MIN(t.not_before) AS nb FROM tasks t"
        f" WHERE t.status = '{QUEUED}' AND {_DEPS_OK}",
    ).fetchone()
    return float(row["nb"]) if row is not None and row["nb"] is not None else None


def runnable_now(conn: sqlite3.Connection) -> int:
    """Queued stages whose dependencies are met and whose backoff has expired:
    'what could be claimed this instant', a different question from the ETA."""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks t WHERE t.status = '{QUEUED}'"
        f" AND t.not_before <= ? AND {_DEPS_OK}",
        (now(),),
    ).fetchone()
    return int(row["n"])
