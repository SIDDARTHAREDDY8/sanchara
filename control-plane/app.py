"""Sanchara control plane: REST API + rollout engine + dashboard."""
import logging
import os
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from config import settings
import artifacts
import auth
import store
from store import log_event
import engine


# ---------- structured logging ----------
logging.basicConfig(level=settings.log_level, format="%(message)s")
log = logging.getLogger("sanchara.control-plane")


def _kv(**fields):
    return " ".join(f"{k}={v}" for k, v in fields.items())


store.init_db()
log.info(_kv(event="startup", service="sanchara-control-plane",
             **settings.redacted()))

app = FastAPI(title="Sanchara Control Plane")


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    start = time.time()
    try:
        response = await call_next(request)
    except Exception:
        log.error(_kv(event="request_error", request_id=request_id,
                       method=request.method, path=request.url.path))
        raise
    response.headers["x-request-id"] = request_id
    log.info(_kv(event="request", request_id=request_id, method=request.method,
                 path=request.url.path, status=response.status_code,
                 ms=int((time.time() - start) * 1000)))
    return response


# ---------- engine loop ----------
# Note: only `Exception` is caught here, so KeyboardInterrupt/SystemExit still
# propagate and uvicorn can shut down gracefully on SIGTERM.
def _engine_loop():
    while True:
        try:
            engine.tick()
        except Exception:
            log.exception(_kv(event="engine_tick_error"))
        time.sleep(settings.engine_tick_s)


engine_thread = threading.Thread(target=_engine_loop, daemon=True,
                                 name="sanchara-engine")
engine_thread.start()


# ---------- models ----------
class RobotIn(BaseModel):
    id: str
    name: str = ""
    fleet_id: str = "default"
    ros_distro: str = "humble"
    hw_model: str = "wbot-1"
    current_version: str = "1.0.0"


class Heartbeat(BaseModel):
    battery_pct: float = 100
    docked: bool = False
    charging: bool = False
    on_mission: bool = False
    connectivity: str = "ok"
    ros_distro: str = "humble"
    hw_model: str = "wbot-1"
    current_version: str | None = None
    name: str = ""
    fleet_id: str = "default"


class ReleaseIn(BaseModel):
    version: str
    ros_distros: list[str] = Field(default_factory=list)
    hw_models: list[str] = Field(default_factory=list)
    artifact_url: str = ""
    notes: str = ""
    # optional additive fields: ingest a server-local tarball as the
    # release artifact on creation (path on the control-plane machine,
    # plus its base64 ed25519 signature)
    artifact_path: str | None = None
    artifact_sig_b64: str | None = None


class DeploymentIn(BaseModel):
    release_id: str
    fleet_id: str = "default"
    strategy: dict = Field(default_factory=dict)
    gates: dict = Field(default_factory=dict)


class TaskStatusIn(BaseModel):
    status: str
    detail: str = ""
    current_version: str | None = None


# ---------- robot / agent API ----------
def _public_robot(r):
    """Robot dict safe for API responses: token hashes never leave the server."""
    if r is None:
        return None
    r = dict(r)
    r.pop("token_hash", None)
    return r


@app.post("/api/robots/{robot_id}/heartbeat")
def heartbeat(robot_id: str, hb: Heartbeat,
              _robot: str = Depends(auth.require_robot)):
    robot = store.get_robot(robot_id)
    token = None
    if not robot or store.get_robot_token_hash(robot_id) is None:
        # bootstrap: auto-register and issue a one-time bearer token
        store.upsert_robot({
            "id": robot_id, "name": hb.name or robot_id, "fleet_id": hb.fleet_id,
            "ros_distro": hb.ros_distro, "hw_model": hb.hw_model,
            "current_version": hb.current_version or "1.0.0",
        })
        token, token_hash = auth.issue_robot_token()
        store.set_robot_token_hash(robot_id, token_hash)
        log.info(_kv(event="robot_bootstrapped", robot_id=robot_id))
    store.update_robot_state(robot_id, hb.model_dump())
    if hb.current_version:
        store.set_robot_version(robot_id, hb.current_version)
    resp = {"ok": True}
    if token is not None:
        resp["token"] = token  # additive, only for newly bootstrapped robots
    return resp


@app.get("/api/robots/{robot_id}/task")
def get_task(robot_id: str, _robot: str = Depends(auth.require_robot)):
    t = store.active_task_for_robot(robot_id)
    if t:
        dep = store.get_deployment(t["deployment_id"])
        t = {**t, "release_id": dep["release_id"] if dep else None}
    return {"task": t}


@app.post("/api/robots/{robot_id}/task/{task_id}/status")
def task_status(robot_id: str, task_id: str, s: TaskStatusIn,
                _robot: str = Depends(auth.require_robot)):
    t = store.get_task(task_id)
    if not t or t["robot_id"] != robot_id:
        raise HTTPException(404, "task not found")
    store.set_task_status(task_id, s.status, s.detail, s.current_version)
    dep = store.get_deployment(t["deployment_id"])
    kind = {"downloading": "task_downloading", "applying": "task_applying",
            "verifying": "task_verifying", "succeeded": "task_succeeded",
            "failed": "task_failed"}.get(s.status, "task_update")
    log_event(t["deployment_id"], robot_id, kind,
              f"{robot_id}: {s.status} ({t['from_version']} -> {t['to_version']})"
              + (f" — {s.detail}" if s.detail else ""))
    return {"ok": True, "deployment_status": dep["status"] if dep else None}


# ---------- operator API ----------
@app.post("/api/robots")
def register_robot(r: RobotIn, _op: None = Depends(auth.require_operator)):
    store.upsert_robot(r.model_dump())
    token, token_hash = auth.issue_robot_token()
    store.set_robot_token_hash(r.id, token_hash)
    log.info(_kv(event="robot_registered", robot_id=r.id))
    return {"ok": True, "robot": _public_robot(store.get_robot(r.id)),
            "token": token}  # one-time plaintext; store only the hash


@app.get("/api/fleets/{fleet_id}/robots")
def list_robots(fleet_id: str, _op: None = Depends(auth.require_operator)):
    return {"robots": [_public_robot(r) for r in store.fleet_robots(fleet_id)]}


@app.post("/api/releases")
def create_release(r: ReleaseIn, _op: None = Depends(auth.require_operator)):
    rel = store.create_release(r.model_dump())
    if r.artifact_path:
        sha, size = artifacts.ingest_artifact(rel["id"], r.artifact_path,
                                              r.artifact_sig_b64)
        log.info(_kv(event="artifact_ingested", release_id=rel["id"],
                     sha256=sha[:16] + "…", size=size))
        rel = store.get_release(rel["id"])
    return rel


@app.get("/api/releases")
def list_releases(_op: None = Depends(auth.require_operator)):
    return {"releases": store.list_releases()}


@app.post("/api/deployments")
def create_deployment(d: DeploymentIn, _op: None = Depends(auth.require_operator)):
    rel = store.get_release(d.release_id)
    if not rel:
        raise HTTPException(404, "release not found")
    return store.create_deployment(d.release_id, d.fleet_id, d.strategy, d.gates)


@app.get("/api/deployments")
def list_deployments(_op: None = Depends(auth.require_operator)):
    deps = []
    for dep in store.list_deployments():
        tasks = store.deployment_tasks(dep["id"])
        counts = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        release = store.get_release(dep["release_id"])
        deps.append({**dep, "task_counts": counts,
                     "release_version": release["version"] if release else "?"})
    return {"deployments": deps}


@app.get("/api/deployments/{dep_id}")
def get_deployment(dep_id: str, _op: None = Depends(auth.require_operator)):
    dep = store.get_deployment(dep_id)
    if not dep:
        raise HTTPException(404, "deployment not found")
    tasks = store.deployment_tasks(dep_id)
    counts = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {**dep, "tasks": tasks, "task_counts": counts}


@app.get("/api/deployments/{dep_id}/events")
def get_events(dep_id: str, limit: int = 200,
               _op: None = Depends(auth.require_operator)):
    return {"events": store.get_events(dep_id, limit)}


@app.post("/api/deployments/{dep_id}/pause")
def pause(dep_id: str, _op: None = Depends(auth.require_operator)):
    store.set_deployment_status(dep_id, "paused")
    log_event(dep_id, None, "deployment_paused", f"Deployment {dep_id} paused by operator.")
    return {"ok": True}


@app.post("/api/deployments/{dep_id}/resume")
def resume(dep_id: str, _op: None = Depends(auth.require_operator)):
    store.set_deployment_status(dep_id, "running")
    log_event(dep_id, None, "deployment_resumed", f"Deployment {dep_id} resumed by operator.")
    return {"ok": True}


# ---------- artifact download (robot API) ----------
@app.get("/artifacts/{release_id}")
def download_artifact(release_id: str,
                      _robot: str = Depends(auth.require_any_robot)):
    sha, sig = store.get_release_artifact(release_id)
    path = artifacts.artifact_path(release_id)
    if not sha or not os.path.exists(path):
        raise HTTPException(404, "artifact not found")
    return FileResponse(
        path, filename=f"{release_id}.tar.gz",
        media_type="application/gzip",
        headers={"X-Sanchara-SHA256": sha,
                 "X-Sanchara-Signature": sig or ""})


# ---------- ops endpoints ----------
@app.get("/api/health")
def health():
    return {"ok": True, "service": "sanchara-control-plane"}


@app.get("/ready")
def ready():
    """Readiness probe: 200 only when the DB is reachable AND the engine
    thread is alive; 503 otherwise."""
    db_ok = False
    try:
        with store.LOCK, store.conn() as c:
            c.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        log.exception(_kv(event="ready_db_check_failed"))
    engine_ok = engine_thread is not None and engine_thread.is_alive()
    if db_ok and engine_ok:
        return {"ready": True}
    return JSONResponse({"ready": False, "db": db_ok, "engine": engine_ok},
                        status_code=503)


# ---------- dashboard ----------
DASH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard", "index.html")


@app.get("/")
def index():
    if os.path.exists(DASH):
        return FileResponse(DASH)
    return JSONResponse({"service": "sanchara-control-plane", "dashboard": "not built yet"})
