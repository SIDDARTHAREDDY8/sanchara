"""SQLite persistence for the Sanchara control plane.

Schema changes go through versioned migrations (see MIGRATIONS): on startup
init_db() creates the schema_migrations bookkeeping table, runs any pending
migrations in order inside a transaction, and records each applied version.
"""
import json
import sqlite3
import threading
import time
import uuid

from config import settings

DB_PATH = settings.db_path
LOCK = threading.RLock()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


# ---- schema migrations ----
def migrate_1_initial(c):
    """The original v1 schema: all 5 application tables + indexes."""
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS robots (
            id TEXT PRIMARY KEY,
            name TEXT,
            fleet_id TEXT,
            ros_distro TEXT,
            hw_model TEXT,
            current_version TEXT,
            battery_pct REAL DEFAULT 100,
            docked INTEGER DEFAULT 0,
            charging INTEGER DEFAULT 0,
            on_mission INTEGER DEFAULT 0,
            connectivity TEXT DEFAULT 'ok',
            last_heartbeat REAL DEFAULT 0,
            extra TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS releases (
            id TEXT PRIMARY KEY,
            version TEXT,
            ros_distros TEXT DEFAULT '[]',
            hw_models TEXT DEFAULT '[]',
            artifact_url TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS deployments (
            id TEXT PRIMARY KEY,
            release_id TEXT,
            fleet_id TEXT,
            strategy TEXT DEFAULT '{}',
            gates TEXT DEFAULT '{}',
            status TEXT DEFAULT 'running',
            current_stage INTEGER DEFAULT 0,
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            deployment_id TEXT,
            robot_id TEXT,
            stage INTEGER,
            status TEXT DEFAULT 'pending',
            from_version TEXT,
            to_version TEXT,
            is_rollback INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            detail TEXT DEFAULT '',
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            deployment_id TEXT,
            robot_id TEXT,
            kind TEXT,
            message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_dep ON tasks(deployment_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_robot ON tasks(robot_id);
        CREATE INDEX IF NOT EXISTS idx_events_dep ON events(deployment_id);
        """
    )


# Ordered list of (version, migration_fn). Add new migrations by appending.
def migrate_2_auth_and_artifacts(c):
    """Security columns (append-only, nullable, safe on existing rows):

    - robots.token_hash: sha256 of the robot's bearer token (only the hash
      is stored server-side).
    - releases.artifact_sha256: hex sha256 of the release tarball.
    - releases.artifact_sig: base64 ed25519 signature of the tarball bytes.
    """
    c.executescript(
        """
        ALTER TABLE robots ADD COLUMN token_hash TEXT;
        ALTER TABLE releases ADD COLUMN artifact_sha256 TEXT;
        ALTER TABLE releases ADD COLUMN artifact_sig TEXT;
        """
    )


MIGRATIONS = [
    (1, migrate_1_initial),
    (2, migrate_2_auth_and_artifacts),
]


def init_db():
    """Apply any pending migrations in order; idempotent and safe to call
    concurrently from multiple processes (migrations are IF NOT EXISTS and
    version rows have a unique constraint)."""
    with LOCK, conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (version INTEGER PRIMARY KEY, applied_at REAL)"
        )
        row = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        current = row[0] or 0
        for version, fn in sorted(MIGRATIONS):
            if version <= current:
                continue
            fn(c)  # runs inside this connection's transaction
            try:
                c.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
            except sqlite3.IntegrityError:
                pass  # another process applied it concurrently
            current = version


# ---- generic helpers ----
def _all(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _one(c, sql, args=()):
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def log_event(deployment_id, robot_id, kind, message):
    with LOCK, conn() as c:
        c.execute(
            "INSERT INTO events (ts, deployment_id, robot_id, kind, message) VALUES (?,?,?,?,?)",
            (time.time(), deployment_id, robot_id, kind, message),
        )


# ---- robots ----
def upsert_robot(robot):
    with LOCK, conn() as c:
        c.execute(
            """INSERT INTO robots (id, name, fleet_id, ros_distro, hw_model, current_version,
                   battery_pct, docked, charging, on_mission, connectivity, last_heartbeat, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, fleet_id=excluded.fleet_id, ros_distro=excluded.ros_distro,
                 hw_model=excluded.hw_model,
                 current_version=COALESCE(excluded.current_version, robots.current_version)""",
            (
                robot["id"],
                robot.get("name", robot["id"]),
                robot.get("fleet_id", "default"),
                robot.get("ros_distro", "humble"),
                robot.get("hw_model", "wbot-1"),
                robot.get("current_version"),
                robot.get("battery_pct", 100),
                int(robot.get("docked", 0)),
                int(robot.get("charging", 0)),
                int(robot.get("on_mission", 0)),
                robot.get("connectivity", "ok"),
                robot.get("last_heartbeat", time.time()),
                json.dumps(robot.get("extra", {})),
            ),
        )


def update_robot_state(robot_id, state):
    fields, vals = [], []
    for k in ("battery_pct", "docked", "charging", "on_mission", "connectivity"):
        if k in state:
            fields.append(f"{k}=?")
            v = state[k]
            vals.append(int(v) if k in ("docked", "charging", "on_mission") else v)
    fields.append("last_heartbeat=?")
    vals.append(time.time())
    vals.append(robot_id)
    with LOCK, conn() as c:
        c.execute(f"UPDATE robots SET {', '.join(fields)} WHERE id=?", vals)


def set_robot_version(robot_id, version):
    with LOCK, conn() as c:
        c.execute("UPDATE robots SET current_version=? WHERE id=?", (version, robot_id))


def get_robot(robot_id):
    with LOCK, conn() as c:
        return _one(c, "SELECT * FROM robots WHERE id=?", (robot_id,))


def fleet_robots(fleet_id):
    with LOCK, conn() as c:
        return _all(c, "SELECT * FROM robots WHERE fleet_id=? ORDER BY id", (fleet_id,))


# ---- auth token hashes (migration 2) ----
def set_robot_token_hash(robot_id, token_hash):
    """Store ONLY the sha256 hash of a robot's bearer token."""
    with LOCK, conn() as c:
        c.execute("UPDATE robots SET token_hash=? WHERE id=?",
                  (token_hash, robot_id))


def get_robot_token_hash(robot_id):
    with LOCK, conn() as c:
        r = _one(c, "SELECT token_hash FROM robots WHERE id=?", (robot_id,))
        return r["token_hash"] if r else None


def robot_id_for_token_hash(token_hash):
    """Reverse lookup: which robot owns this bearer-token hash (or None)."""
    with LOCK, conn() as c:
        r = _one(c, "SELECT id FROM robots WHERE token_hash=?", (token_hash,))
        return r["id"] if r else None


# ---- release artifacts (migration 2) ----
def set_release_artifact(release_id, sha256_hex, sig_b64):
    """Persist a release artifact's checksum and base64 ed25519 signature."""
    with LOCK, conn() as c:
        c.execute("UPDATE releases SET artifact_sha256=?, artifact_sig=? WHERE id=?",
                  (sha256_hex, sig_b64, release_id))


def get_release_artifact(release_id):
    """Return (sha256_hex, sig_b64) for a release, or (None, None)."""
    with LOCK, conn() as c:
        r = _one(c, "SELECT artifact_sha256, artifact_sig FROM releases WHERE id=?",
                 (release_id,))
        if not r:
            return None, None
        return r["artifact_sha256"], r["artifact_sig"]


# ---- releases ----
def create_release(data):
    rid = new_id("rel")
    with LOCK, conn() as c:
        c.execute(
            "INSERT INTO releases (id, version, ros_distros, hw_models, artifact_url, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                rid,
                data["version"],
                json.dumps(data.get("ros_distros", [])),
                json.dumps(data.get("hw_models", [])),
                data.get("artifact_url", ""),
                data.get("notes", ""),
                time.time(),
            ),
        )
    return get_release(rid)


def get_release(rid):
    with LOCK, conn() as c:
        return _one(c, "SELECT * FROM releases WHERE id=?", (rid,))


def list_releases():
    with LOCK, conn() as c:
        return _all(c, "SELECT * FROM releases ORDER BY created_at DESC")


# ---- deployments ----
DEFAULT_STRATEGY = {
    "canary_count": 1,
    "stage_fractions": [0.5, 1.0],
    "failure_threshold": 0,
    "auto_rollback": True,
}
DEFAULT_GATES = {
    "min_battery": 40,
    "require_docked": True,
    "require_connectivity": "ok",
    "heartbeat_timeout_s": 30,
}


def create_deployment(release_id, fleet_id, strategy=None, gates=None):
    strat = dict(DEFAULT_STRATEGY)
    strat.update(strategy or {})
    g = dict(DEFAULT_GATES)
    g.update(gates or {})
    did = new_id("dep")
    now = time.time()
    with LOCK, conn() as c:
        c.execute(
            "INSERT INTO deployments (id, release_id, fleet_id, strategy, gates, status,"
            " current_stage, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (did, release_id, fleet_id, json.dumps(strat), json.dumps(g), "running", 0, now, now),
        )
    log_event(did, None, "deployment_created",
              f"Deployment {did} created for release {release_id} on fleet {fleet_id}")
    return get_deployment(did)


def get_deployment(did):
    with LOCK, conn() as c:
        return _one(c, "SELECT * FROM deployments WHERE id=?", (did,))


def list_deployments():
    with LOCK, conn() as c:
        return _all(c, "SELECT * FROM deployments ORDER BY created_at DESC")


def set_deployment_status(did, status):
    with LOCK, conn() as c:
        c.execute("UPDATE deployments SET status=?, updated_at=? WHERE id=?",
                  (status, time.time(), did))


def set_deployment_stage(did, stage):
    with LOCK, conn() as c:
        c.execute("UPDATE deployments SET current_stage=?, updated_at=? WHERE id=?",
                  (stage, time.time(), did))


# ---- tasks ----
TERMINAL = ("succeeded", "failed", "rolled_back", "skipped")


def create_task(deployment_id, robot_id, stage, from_version, to_version,
                status="pending", is_rollback=0, detail=""):
    tid = new_id("task")
    with LOCK, conn() as c:
        c.execute(
            "INSERT INTO tasks (id, deployment_id, robot_id, stage, status, from_version,"
            " to_version, is_rollback, attempts, detail, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, deployment_id, robot_id, stage, status, from_version, to_version,
             is_rollback, 0, detail, time.time()),
        )
    return get_task(tid)


def get_task(tid):
    with LOCK, conn() as c:
        return _one(c, "SELECT * FROM tasks WHERE id=?", (tid,))


def deployment_tasks(did, stage=None, is_rollback=None):
    with LOCK, conn() as c:
        sql = "SELECT * FROM tasks WHERE deployment_id=?"
        args = [did]
        if stage is not None:
            sql += " AND stage=?"
            args.append(stage)
        if is_rollback is not None:
            sql += " AND is_rollback=?"
            args.append(is_rollback)
        return _all(c, sql + " ORDER BY robot_id", args)


def robot_task(deployment_id, robot_id):
    with LOCK, conn() as c:
        return _one(
            c,
            "SELECT * FROM tasks WHERE deployment_id=? AND robot_id=? ORDER BY updated_at DESC LIMIT 1",
            (deployment_id, robot_id),
        )


def active_task_for_robot(robot_id):
    """Newest non-terminal assigned task for a robot across running/rolling_back deployments."""
    with LOCK, conn() as c:
        return _one(
            c,
            """SELECT t.* FROM tasks t JOIN deployments d ON d.id=t.deployment_id
               WHERE t.robot_id=? AND d.status IN ('running','rolling_back')
               AND t.status NOT IN ('succeeded','failed','rolled_back','skipped')
               ORDER BY t.updated_at DESC LIMIT 1""",
            (robot_id,),
        )


def set_task_status(tid, status, detail="", current_version=None):
    with LOCK, conn() as c:
        c.execute("UPDATE tasks SET status=?, detail=?, updated_at=? WHERE id=?",
                  (status, detail, time.time(), tid))
    if current_version:
        set_robot_version(
            get_task(tid)["robot_id"], current_version)


def get_events(deployment_id, limit=200):
    with LOCK, conn() as c:
        return _all(
            c,
            "SELECT * FROM events WHERE deployment_id=? ORDER BY id DESC LIMIT ?",
            (deployment_id, limit),
        )
