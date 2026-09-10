# Sanchara architecture

Sanchara is update management for ROS 2 robot fleets. Two processes, one
contract between them:

```
┌──────────────────────┐   heartbeat (state)        ┌─────────────────────────┐
│  robot agent          │ ────────────────────────►  │  control plane           │
│  agent/agent.py       │                            │  control-plane/app.py    │
│  agent/ros_backend.py │ ◄──────────────────────── │  (FastAPI)               │
│  agent/secure_update.py│  task poll / transitions  │                          │
└──────────────────────┘                            │  control-plane/engine.py │
        │                                            │  (rollout state machine) │
        ▼                                            │  control-plane/store.py  │
  ROS 2 (or simulated)                               │  (SQLite + migrations)   │
  via ROSBackend ABC                                 │  control-plane/auth.py   │
                                                     │  control-plane/artifacts.py
                                                     └─────────────────────────┘
```

## Control plane

**`app.py`** — FastAPI REST API + dashboard static serving. Thin: validates
input, enforces auth, delegates to `store`/`engine`. An engine thread ticks
every `SANCHARA_ENGINE_TICK_S` (default 2s). Request logging uses key=value
structured lines with a per-request id; secrets are redacted at startup.

**`engine.py`** — the rollout state machine. Robots *pull*; the engine never
pushes bytes. Each tick, for every `running` deployment:

1. `_ensure_stage_tasks` — create `pending` tasks for the current stage's
   target robots (prefix slice of the fleet ordered by robot id). Robots whose
   ROS distro / hw model is incompatible with the release get `skipped`.
2. `_promote_pending` — evaluate `check_gates` (mission, dock, battery,
   connectivity, heartbeat freshness) against the robot's latest heartbeat;
   passing tasks move `pending → assigned`, failing ones stay `pending` with
   the blocking reasons in `detail`.
3. On failure past `strategy.failure_threshold`: halt and, if
   `strategy.auto_rollback`, create `is_rollback` tasks for every robot that
   applied the release.
4. When all non-rollback tasks in the stage are terminal, advance the stage
   (or mark the deployment `completed`). An empty stage counts as complete.

Agents report transitions (`downloading → applying → verifying → succeeded /
failed`) via the task-status endpoint; `failed` feeds step 3. The audit trail
is the `events` table — every assignment, gate outcome, halt, and rollback is
logged with a timestamp.

**`store.py`** — SQLite persistence with versioned migrations (`MIGRATIONS`).
`init_db()` applies pending migrations in order, idempotently. Tables:
`robots`, `releases`, `deployments`, `tasks`, `events`, `schema_migrations`.
Single-node SQLite is a deliberate MVP choice (see "What production still
needs").

**`auth.py`** — per-robot bearer tokens (only SHA-256 hashes stored) and an
operator API key. See `docs/SECURITY.md`.

**`artifacts.py`** — filesystem artifact store (`SANCHARA_ARTIFACT_DIR`):
ingest tarballs on release creation, serve them at
`GET /artifacts/{release_id}`, ed25519 sign/verify helpers. The agent verifies
SHA-256 + signature before applying.

**`config.py`** — all runtime config from environment variables with defaults.
See `docs/DEPLOYMENT.md`.

## Robot agent

**`agent.py`** — one process per robot. Loop: heartbeat → poll task → if
`assigned`, run the update pipeline:

1. `preflight_check` (manifest present, disk space, backend available)
2. `download_and_verify` (only when `--artifact-pubkey` is set; skipped for
   rollbacks — known-good local version, no new bytes)
3. `backend.apply_update` (install + restart; `--inject-failure` simulates a
   bad release)
4. `backend.health_check` against the target version's manifest → `succeeded`
   or `failed` (the `failed` report is what triggers fleet-wide halt/rollback)

**`ros_backend.py`** — the `ROSBackend` ABC: `version`, `get_nodes()`,
`get_topic_rates()`, `get_lifecycle_state(node)`, `preflight_check`,
`apply_update`, `health_check`. The agent's decision logic is written only
against this interface.

- `SimBackend` (`ros_sim.py`) — deterministic simulation: node graph, topic
  Hz with jitter, lifecycle states, per-version manifests, failure injection.
  Used by the demo, the e2e suite, and CI.
- `RclpyBackend` — real ROS 2: node names via `rclpy`, topic rates by
  subscribing + sampling (timeout-bounded), lifecycle state via
  `lifecycle_msgs/srv/GetState`, update via a configurable install hook +
  respawn command. `rclpy` is imported lazily; without ROS 2 installed it
  raises a clear error. Selected with `--ros-backend rclpy`.

**`secure_update.py`** — `download_and_verify`: streams
`GET /artifacts/{release_id}` to a temp file, checks the SHA-256 against the
`X-Sanchara-SHA256` header and the ed25519 signature against
`X-Sanchara-Signature`, raises `ArtifactVerificationError` on any mismatch.
Nothing unverified ever lands on disk.

## Key design decisions

- **Pull, not push.** Robots poll for tasks; the server never opens connections
  to robots. Works behind NAT and over flaky warehouse Wi-Fi.
- **Gates are evaluated server-side from heartbeats**, so policy lives in one
  place and applies uniformly even to agents that lie about their state.
- **Rollback is a first-class task**, not an undo button: it goes through the
  same gates, assignment, and health verification as a forward update.
- **Additive API evolution.** Existing request/response shapes are frozen;
  new data arrives as new fields (e.g. `token`, `release_id` on tasks).
- **Boring technology.** SQLite, FastAPI, bearer tokens, ed25519, tarballs.
  No message bus, no service mesh, no custom crypto.
