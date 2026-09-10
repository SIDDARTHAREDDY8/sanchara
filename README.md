# Sanchara — ROS-aware fleet update management

**Sanchara** (संचार — *transmission, movement, communication*) is update
management software for ROS 2 robot fleets. Generic OTA tools (Mender, balena,
AWS IoT Jobs) push bytes; Sanchara understands robots: it never updates a robot
mid-mission, gates rollouts on battery / dock / connectivity / ROS-distro and
hardware compatibility, serves **signed release artifacts** that agents verify
before applying, checks the live ROS graph after every update, and
automatically rolls back when the expected nodes, topics, or lifecycle states
don't recover.

## What it does

- **State-gated rollouts** — a robot only becomes eligible when it is
  docked/charging, off-mission, above the battery threshold, reachable, and
  running a compatible ROS distro + hardware model. Stale heartbeats
  disqualify the robot.
- **Canary + staged deployments** — e.g. 2 canaries → 50% → 100%. A stage only
  advances when every robot in it is verified healthy.
- **Signed artifacts** — releases are real tarballs; agents verify SHA-256 +
  ed25519 signature before applying. Tampered bytes never touch disk.
- **ROS-aware health verification** — after applying an update the agent checks
  the live node graph, topic publish rates, and lifecycle states against the
  release's version manifest before declaring success. Works against real ROS 2
  (`--ros-backend rclpy`) or the built-in simulator.
- **Failure detection + auto-rollback** — if canary failures exceed the
  threshold, the deployment halts and every robot that took the bad release is
  rolled back through the same gated, verified path.
- **Auth** — per-robot bearer tokens (hash-only server-side) and an operator
  API key; see `docs/SECURITY.md`.
- **Audit trail** — every task transition, gate decision, halt, and rollback is
  logged per deployment.
- **Dashboard** — live fleet table, rollout progress, and event feed.

## Quickstart

```bash
cd ~/workspace/sanchara
.venv/bin/python demo/run_demo.py        # headline proof: full scenario (~40s)
.venv/bin/python demo/run_demo.py --keep # leave everything running
# dashboard: http://127.0.0.1:8000/
bash scripts/test_all.sh                 # unit + e2e + demo, all green
```

Manual run:

```bash
# terminal 1 — control plane
cd control-plane && SANCHARA_OPERATOR_KEY=secret ../.venv/bin/python -m uvicorn app:app --port 8000
# terminal 2 — register a robot, then run its agent with the issued token
curl -s -H 'X-API-Key: secret' -X POST localhost:8000/api/robots \
  -H 'Content-Type: application/json' -d '{"id":"wbot-01","fleet_id":"warehouse-a"}'
../.venv/bin/python agent/agent.py --server http://127.0.0.1:8000 \
  --robot-id wbot-01 --fleet warehouse-a --docked --charging --token <token>
```

Docker:

```bash
docker compose up --build   # control plane + 3 sim agents
```

## The demo scenario

8 simulated robots, release `2.0.0` (humble, wbot-1) as a **real signed
tarball**, canary strategy with `failure_threshold: 0`, all traffic
authenticated:

| robot | state | outcome |
|---|---|---|
| wbot-01 | docked/charging, healthy | canary ✅ (artifact verified), then rolled back |
| wbot-02 | docked/charging, **bad release injected** | canary ❌ — `nav2_planner` crashes, health check fails |
| wbot-03 | on a mission | gated — never touched mid-mission |
| wbot-04 | docked, 25% battery | gated — battery too low |
| wbot-05 | idle, not docked | gated — not at dock |
| wbot-06/07 | docked/charging, healthy | never reached (halted at canary) |
| wbot-08 | ROS `foxy` | skipped — incompatible distro |

Result: the canary catches the bad release → deployment halts → both canaries
auto-roll back to `1.0.0` → the rest of the fleet never sees `2.0.0`.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, engine state
  machine, design decisions
- [`docs/API.md`](docs/API.md) — full endpoint reference with auth requirements
- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model, auth model, and what
  the MVP deliberately does not do (no TLS, no mTLS — yet)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — env config, compose, production
  checklist, Postgres path

## Project layout

```
control-plane/   app.py (API) · engine.py (rollout state machine)
                 store.py (SQLite + versioned migrations) · auth.py
                 artifacts.py (signed tarball store) · config.py (env config)
agent/           agent.py · ros_backend.py (ROSBackend ABC: SimBackend, RclpyBackend)
                 ros_sim.py · secure_update.py (download + verify)
dashboard/       index.html — live fleet / rollout / event feed
demo/            run_demo.py — the headline proof
tests/           test_gates.py · test_ros_backend.py · test_security.py · test_e2e.py
scripts/         test_all.sh — full suite, exit green or it failed
Dockerfile  docker-compose.yml  requirements.txt  .dockerignore
```

## What's real vs. simulated

Real: rollout engine, gate evaluation, canary/stage state machine, halt and
rollback orchestration, signed artifact serving + verification, auth, audit
log, dashboard, agent lifecycle, migrations, container images.
Simulated (unless `--ros-backend rclpy` with real ROS 2): the ROS layer, robot
physics, artifact *contents*. The agent's decision points are written against
the `ROSBackend` interface so a real ROS 2 backend drops in behind it.

## Roadmap

1. TLS termination story baked into compose (Caddy) instead of documented.
2. Provisioning claim flow to replace first-heartbeat bootstrap.
3. Real packaging hooks for `RclpyBackend` (deb/containers) per robot model.
4. Postgres + single-leader engine when fleet size demands it.
5. mTLS robot identity; artifact encryption; rate limiting.
