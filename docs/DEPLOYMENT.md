# Sanchara deployment guide

## Configuration (all via environment)

| Variable | Default | Purpose |
|---|---|---|
| `SANCHARA_PORT` | `8000` | Control-plane HTTP port |
| `SANCHARA_DB` | `<control-plane dir>/sanchara.db` | SQLite path (legacy `SANCHRA_DB` honored) |
| `SANCHARA_ARTIFACT_DIR` | `<control-plane dir>/artifacts` | Release tarball store |
| `SANCHARA_OPERATOR_KEY` | `""` (auth disabled, loud warning) | Operator API key |
| `SANCHARA_ENGINE_TICK_S` | `2` | Rollout engine tick interval |
| `SANCHARA_LOG_LEVEL` | `INFO` | Python logging level |

See `control-plane/config.py` — `settings.redacted()` logs config at startup
without secrets.

## Option A — local processes (dev / demo)

```bash
cd ~/workspace/sanchara
.venv/bin/python demo/run_demo.py            # headline proof (~40s)
.venv/bin/python demo/run_demo.py --keep     # leave the stack running
# dashboard: http://127.0.0.1:8000/
```

Manual:
```bash
# terminal 1
cd control-plane && SANCHARA_OPERATOR_KEY=secret ../.venv/bin/python -m uvicorn app:app --port 8000
# terminal 2 — register, then run an agent with its token
curl -s -H 'X-API-Key: secret' -X POST localhost:8000/api/robots \
  -H 'Content-Type: application/json' \
  -d '{"id":"wbot-01","fleet_id":"warehouse-a","ros_distro":"humble"}'
../.venv/bin/python agent/agent.py --server http://127.0.0.1:8000 \
  --robot-id wbot-01 --fleet warehouse-a --docked --charging \
  --token <token-from-registration>
```

## Option B — docker compose (control plane + 3 sim agents)

```bash
cd ~/workspace/sanchara
docker compose up --build
# dashboard: http://127.0.0.1:8000/
```

The control plane gets a `/ready` healthcheck; agents start only after it is
healthy (`depends_on: service_healthy`) and bootstrap their tokens via first
heartbeat. The DB lives on the `sanchara-db` named volume. For a keyed setup,
add `SANCHARA_OPERATOR_KEY` to the control-plane environment and register
agents out-of-band (Option A pattern) instead of relying on bootstrap.

> Note: `docker` is not installed in the build VM, so `compose up` was
> validated by structural inspection only (YAML parse, target names, port
> mapping, healthcheck path, healthy-dependency). Run it once on a machine
> with Docker before relying on it.

## Production checklist

1. **Reverse proxy with TLS** in front of the control plane (Caddy is the
   boring choice); never expose plain HTTP to the internet.
2. **Set `SANCHARA_OPERATOR_KEY`** to a long random value; distribute robot
   tokens at provisioning time; start agents with `--artifact-pubkey`.
3. **Persistent volume + backups** for `SANCHARA_DB` and
   `SANCHARA_ARTIFACT_DIR`. SQLite WAL mode is fine for a single node.
4. **Log shipping**: logs are key=value lines on stdout — point them at
   whatever you already use.
5. **Monitoring**: alert on `/ready` != 200, on `deployment_halted` events
   (query `/api/deployments/{id}/events`), and on heartbeat staleness per
   fleet.
6. **Backups**: the DB file plus the artifact dir are the entire state. Copy
   both; restore = put the files back and restart.

## Scaling beyond the MVP

- **Postgres**: `store.py` uses versioned migrations and a single `conn()`
  seam — the cutover is "new migration target + replace the connection
  factory", not a rewrite. Not done here because single-node SQLite is the
  boring, correct choice until fleet size or HA demands otherwise.
- **Multi-replica control plane**: needs a leader for the engine tick (DB
  advisory lock or an external lock) — the engine is the only writer that
  must be single.
- **Real ROS 2 agents**: `--ros-backend rclpy` with ROS 2 installed and
  sourced; configure the install/respawn hooks for your packaging (deb /
  containers); keep using signed artifacts.
- **mTLS**: replace bearer tokens with client certificates when the fleet
  PKI story is ready (see `docs/SECURITY.md`).
