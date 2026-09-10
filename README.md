# Sanchara

**Sanchara** (संचार — *transmission, movement, communication*) is over-the-air
update management for ROS 2 robot fleets.

Generic OTA tools (Mender, balena, AWS IoT Jobs) push bytes to devices.
Sanchara understands *robots*: it never updates a robot mid-mission, rolls out
in canary stages gated on dock, battery, connectivity and hardware
compatibility, verifies the live ROS graph after every update, and
automatically rolls the fleet back when a release misbehaves. If you run more
than a handful of ROS 2 robots, this is the boring infrastructure that keeps a
bad release from bricking your warehouse at 2 AM.

## How it works

Two processes and a pull-based contract between them. Robots phone home; the
server never opens connections to robots, so it works behind NAT and over
flaky warehouse Wi-Fi.

```
┌─────────────────┐  heartbeat (battery, dock, mission, …)  ┌──────────────────┐
│  robot agent    │ ───────────────────────────────────────► │  control plane   │
│  (one per robot)│                                          │  (FastAPI)       │
│                 │ ◄────────────────────────────────────── │                  │
└─────────────────┘  poll task · report transitions         │  rollout engine  │
        │                                                    └──────────────────┘
        ▼ picks up the assigned update
  ┌─────────────┐
  │ ROS 2 stack │  preflight → download+verify → apply → health check
  └─────────────┘
```

A deployment runs as a state machine on the control plane, ticked every few
seconds:

1. **Target.** You register a release (version + compatible ROS distros and
   hardware models + a signed tarball) and start a deployment against a fleet
   with a strategy: `{canary_count, stage_fractions, failure_threshold,
   auto_rollback}`.
2. **Stage.** The engine creates one task per robot in the current stage
   (canaries first, e.g. 2 → 50% → 100%). Robots whose distro or hardware
   doesn't match the release are `skipped` immediately.
3. **Gate.** A `pending` task becomes `assigned` only when the robot's latest
   heartbeat proves it is safe to touch: not on a mission, docked/charging,
   battery above the minimum, connectivity healthy, heartbeat fresh. A robot
   that fails a gate simply waits — the reason is recorded on the task.
4. **Update.** The agent pulls its assignment and walks the pipeline:
   `downloading → applying → verifying`. It streams the release tarball,
   verifies SHA-256 **and** the ed25519 signature before anything touches
   disk, applies the update, then checks the *live* ROS graph — node
   liveness, topic publish rates, lifecycle states — against the release's
   version manifest.
5. **Verify or roll back.** If the health check fails, the agent reports
   `failed`. Once failures pass the threshold, the deployment halts and every
   robot that took the bad release gets a **rollback task** — a first-class
   task that goes through the same gates, download and verification to
   restore the previous version. The rest of the fleet never sees the bad
   release.
6. **Advance.** A stage completes only when every task in it is terminal
   (`succeeded`, `skipped`, `rolled_back`). Then the next stage opens. Every
   transition — assignments, gate waits, halts, rollbacks — lands in a
   per-deployment audit log.

The dashboard (`http://127.0.0.1:8000/`) shows the live fleet table, rollout
progress and the event feed.

## Quickstart

**The 40-second proof** — 8 simulated robots, one bad release, canary catches
it, fleet protected:

```bash
.venv/bin/python demo/run_demo.py
# dashboard: http://127.0.0.1:8000/
```

**Manual run:**

```bash
# terminal 1 — control plane
cd control-plane && SANCHARA_OPERATOR_KEY=secret ../.venv/bin/python -m uvicorn app:app --port 8000

# terminal 2 — register a robot (returns its one-time token), create a release, deploy
curl -s -H 'X-API-Key: secret' -X POST localhost:8000/api/robots \
  -H 'Content-Type: application/json' -d '{"id":"wbot-01","fleet_id":"warehouse-a"}'

curl -s -H 'X-API-Key: secret' -X POST localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"2.0.0","ros_distros":["humble"],"hw_models":["wbot-1"]}'

curl -s -H 'X-API-Key: secret' -X POST localhost:8000/api/deployments \
  -H 'Content-Type: application/json' \
  -d '{"release_id":"<id>","fleet_id":"warehouse-a",
       "strategy":{"canary_count":1,"stage_fractions":[0.5,1.0],"failure_threshold":0}}'

# terminal 3 — the robot's agent
.venv/bin/python agent/agent.py --server http://127.0.0.1:8000 \
  --robot-id wbot-01 --fleet warehouse-a --docked --charging --token <token>
```

**Docker:**

```bash
docker compose up --build   # control plane + 3 sim agents
```

**Full test suite** (unit + 6 end-to-end scenarios against a live server + the
demo — must exit green):

```bash
bash scripts/test_all.sh
```

### The demo scenario

Release `2.0.0` (humble, wbot-1) shipped as a real signed tarball, canary
strategy, `failure_threshold: 0`:

| robot | state | outcome |
|---|---|---|
| wbot-01 | docked/charging, healthy | canary ✅ (artifact verified), then rolled back |
| wbot-02 | docked/charging, **bad release injected** | canary ❌ — `nav2_planner` crashes on boot, health check fails |
| wbot-03 | on a mission | gated — never touched mid-mission |
| wbot-04 | docked, 25% battery | gated — below the battery minimum |
| wbot-05 | idle, not docked | gated — not at the dock |
| wbot-06 / wbot-07 | docked/charging, healthy | never reached (deployment halted at canary) |
| wbot-08 | ROS `foxy` | skipped — incompatible distro |

## Configuration

Everything is environment variables on the control plane (`SANCHARA_PORT`,
`SANCHARA_DB`, `SANCHARA_ARTIFACT_DIR`, `SANCHARA_OPERATOR_KEY`,
`SANCHARA_ENGINE_TICK_S`, `SANCHARA_LOG_LEVEL`).

Per-deployment knobs:

- **Strategy** — `canary_count` (default 1), `stage_fractions` (default
  `[0.5, 1.0]`), `failure_threshold` (default 0 — any canary failure halts),
  `auto_rollback` (default true).
- **Gates** — `min_battery` (default 40%), `require_docked` (default true),
  `require_connectivity` (default `"ok"`), `heartbeat_timeout_s` (default 30).

## Security

- Per-robot bearer tokens (only hashes stored server-side), operator API key
  on all management endpoints.
- Releases are ed25519-signed tarballs; agents verify SHA-256 + signature
  before applying. Tampered bytes never touch disk.
- Known MVP limits, stated plainly: **no TLS** (terminate at a reverse
  proxy), no mTLS robot identity yet, signed-but-unencrypted artifacts.

Full threat model and the production checklist: [`docs/SECURITY.md`](docs/SECURITY.md).

## API

`POST /api/releases` · `POST /api/deployments` · `GET /api/deployments/{id}`
· `GET /api/deployments/{id}/events` · `POST /api/deployments/{id}/pause|resume`
· robot endpoints: heartbeat, task poll, task status · `GET /artifacts/{release_id}`

Full reference with auth requirements: [`docs/API.md`](docs/API.md).

## Project layout

```
control-plane/   app.py (API) · engine.py (rollout state machine)
                 store.py (SQLite + versioned migrations) · auth.py
                 artifacts.py (signed tarball store) · config.py (env config)
agent/           agent.py · ros_backend.py (ROSBackend ABC)
                 ros_sim.py (SimBackend) · secure_update.py (download + verify)
dashboard/       index.html — live fleet / rollout / event feed
demo/            run_demo.py — the headline proof
tests/           unit (gates, ROS backends, security) + test_e2e.py (6 scenarios)
scripts/         test_all.sh — the whole suite, green or it failed
docs/            ARCHITECTURE.md · API.md · SECURITY.md · DEPLOYMENT.md
Dockerfile  docker-compose.yml  requirements.txt
```

## How the ROS layer works

The agent never calls ROS APIs directly — it programs against the
`ROSBackend` interface (`get_nodes`, `get_topic_rates`, `get_lifecycle_state`,
`preflight_check`, `apply_update`, `health_check`). Two implementations ship:

- **`SimBackend`** — deterministic simulation used by the demo, e2e suite and
  CI. Also the `--inject-failure` chaos hooks (`crash_node`, `topic_stall`).
- **`RclpyBackend`** — real ROS 2 (`--ros-backend rclpy`): node discovery via
  rclpy, topic rates by subscribing and sampling, lifecycle state via
  `lifecycle_msgs`, updates through a configurable install hook.

## Production notes

This is a real, tested MVP — not a prototype — but go in with eyes open:
single-node SQLite (migrations + a single connection seam make Postgres a
future cutover, not a rewrite), plain HTTP (put Caddy/nginx in front), bearer
tokens rather than mTLS. [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) has the
full production checklist; [`docs/SECURITY.md`](docs/SECURITY.md) lists every
deliberate tradeoff.

## Roadmap

TLS termination in compose · provisioning claim flow to replace bootstrap
registration · real packaging hooks per robot model · Postgres + single-leader
engine at scale · mTLS robot identity.

## License

TBD — a license will be added before any distribution.
