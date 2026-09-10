# Sanchara API reference

Base URL: `http://<host>:8000` (default port 8000, see `SANCHARA_PORT`).
Dashboard: `GET /`. Health: `GET /api/health`, `GET /ready` (no auth).

## Authentication

- **Operator endpoints** — header `X-API-Key: <SANCHARA_OPERATOR_KEY>` (or
  `Authorization: Bearer <SANCHARA_OPERATOR_KEY>`). Missing/wrong → `401`.
  If `SANCHARA_OPERATOR_KEY` is empty, endpoints are open and the server logs
  a loud warning on every request (dev mode — do not use in production).
- **Robot endpoints** — header `Authorization: Bearer <robot-token>`.
  Tokens are issued once at registration (`POST /api/robots`) or on first
  heartbeat for unknown robots (auto-bootstrap). Known robot + wrong token →
  `401`. `GET /artifacts/{release_id}` accepts any valid robot token.

## Robot / agent API

### `POST /api/robots/{robot_id}/heartbeat` 🔒 robot
Upsert robot state. Unknown robots are auto-registered and the response
includes a one-time `"token"` (bootstrap mode).

Request body:
```json
{"battery_pct": 82.5, "docked": true, "charging": true, "on_mission": false,
 "connectivity": "ok", "ros_distro": "humble", "hw_model": "wbot-1",
 "current_version": "1.0.0", "name": "wbot-01", "fleet_id": "warehouse-a"}
```
Response: `{"ok": true}` (+ `"token"` for newly bootstrapped robots).

### `GET /api/robots/{robot_id}/task` 🔒 robot
Returns the robot's newest non-terminal task across running/rolling_back
deployments: `{"task": {...} | null}`. The task dict includes an additive
`release_id` field so the agent can download the artifact.

### `POST /api/robots/{robot_id}/task/{task_id}/status` 🔒 robot
Body: `{"status": "downloading|applying|verifying|succeeded|failed",
"detail": "...", "current_version": "2.0.0"}`.
Response: `{"ok": true, "deployment_status": "running"}`. Every transition is
written to the event log.

### `GET /artifacts/{release_id}` 🔒 robot (any valid token)
Serves the release tarball (`application/gzip`) with headers
`X-Sanchara-SHA256` and `X-Sanchara-Signature` (base64 ed25519).

## Operator API

### `POST /api/robots` 🔒 operator
Register a robot. Response: `{"ok": true, "robot": {...}, "token": "<one-time>"}`.
The plaintext token is shown once; the server stores only its SHA-256 hash.

### `GET /api/fleets/{fleet_id}/robots` 🔒 operator
`{"robots": [...]}` — latest heartbeat state per robot. Token hashes are
stripped from responses.

### `POST /api/releases` 🔒 operator
```json
{"version": "2.0.0", "ros_distros": ["humble"], "hw_models": ["wbot-1"],
 "artifact_url": "s3://…", "notes": "…",
 "artifact_path": "/srv/artifacts/2.0.0.tar.gz",      // optional, server-local
 "artifact_sig_b64": "<base64 ed25519 signature>"}    // optional
```
When `artifact_path` is given, the tarball is ingested into the artifact
store and served at `GET /artifacts/{release_id}`.

### `GET /api/releases` 🔒 operator — `{"releases": [...]}`

### `POST /api/deployments` 🔒 operator
```json
{"release_id": "rel_abc123", "fleet_id": "warehouse-a",
 "strategy": {"canary_count": 2, "stage_fractions": [0.5, 1.0],
              "failure_threshold": 0, "auto_rollback": true},
 "gates": {"min_battery": 40, "require_docked": true,
           "require_connectivity": "ok", "heartbeat_timeout_s": 30}}
```
Strategy defaults: canary 1, one 100% stage, threshold 0, auto-rollback on.
Stage targets are a prefix slice of the fleet ordered by robot id: stage 0 →
`canary_count` robots, stage N → `ceil(stage_fractions[N-1] * fleet_size)`.

### `GET /api/deployments` 🔒 operator
`{"deployments": [...]}` — each with `task_counts` and `release_version`.

### `GET /api/deployments/{id}` 🔒 operator
Full deployment with per-robot `tasks` and `task_counts`.

### `GET /api/deployments/{id}/events?limit=200` 🔒 operator
`{"events": [...]}` — newest first; the audit trail.

### `POST /api/deployments/{id}/pause|resume` 🔒 operator — `{"ok": true}`

## Task lifecycle

`pending → assigned → downloading → applying → verifying → succeeded`
(or `failed`), plus `skipped` (incompatible) and `rolled_back` (rollback
tasks; reported as `succeeded` by the agent and normalized server-side).

## Deployment lifecycle

`running → completed`, `running → rolling_back → halted`,
`running → halted` (auto-rollback disabled), `running ⇄ paused`.
