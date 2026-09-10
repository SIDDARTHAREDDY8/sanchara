#!/usr/bin/env python3
"""End-to-end black-box tests for Sanchara.

Boots the REAL control plane (``uvicorn app:app``) as a subprocess with a
fresh temp SQLite DB and artifact dir, then drives six scenarios through the
real HTTP API: scripted robots over HTTP, plus one real ``agent.py``
subprocess proving the true agent path (signed-artifact download + verify).

Run from the repo root:  ``.venv/bin/python tests/test_e2e.py``
No pytest needed. Each scenario prints a PASS/FAIL line; the process exits
nonzero if any scenario fails.
"""
import io
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CP_DIR = os.path.join(ROOT, "control-plane")
AG_DIR = os.path.join(ROOT, "agent")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")

for _p in (CP_DIR, AG_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- scratch space + env for the in-process imports below.
# The server subprocess gets the same values explicitly (see boot_server).
TMPDIR = tempfile.mkdtemp(prefix="sanchara-e2e-")
DB_PATH = os.path.join(TMPDIR, "e2e.db")
ARTIFACT_DIR = os.path.join(TMPDIR, "artifacts")
os.environ["SANCHARA_DB"] = DB_PATH
os.environ["SANCHARA_ARTIFACT_DIR"] = ARTIFACT_DIR

from artifacts import generate_keypair, sign_bytes  # noqa: E402
from secure_update import (  # noqa: E402
    ArtifactVerificationError, download_and_verify,
)

PORT = 18001
SERVER = f"http://127.0.0.1:{PORT}"
OPERATOR_KEY = "e2e-test-key"


def build_release_tarball(version, path):
    """Build a real release tarball: a VERSION file + manifest notes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in {
            "VERSION": f"{version}\n",
            "NOTES.txt": (f"Sanchara e2e release {version}\n"
                          "ros_distros: humble\nhw_models: wbot-1\n"),
        }.items():
            data = text.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return path


# ---------------------------------------------------------------- helpers

class Ctx:
    """Test context: server handle, HTTP helpers, release/robot factories."""

    def __init__(self):
        self.priv_b64, self.pubkey_b64 = generate_keypair()
        self.srv = None
        self.srv_log = os.path.join(TMPDIR, "server.log")
        self.agent_procs = []  # (robot_id, Popen, log_path, log_file)

    # -- server lifecycle --
    def boot_server(self):
        env = dict(os.environ)
        env.update({
            "SANCHARA_PORT": str(PORT),
            "SANCHARA_DB": DB_PATH,
            "SANCHARA_ARTIFACT_DIR": ARTIFACT_DIR,
            "SANCHARA_OPERATOR_KEY": OPERATOR_KEY,
            "SANCHARA_ENGINE_TICK_S": "1",
            "SANCHARA_LOG_LEVEL": "WARNING",
        })
        logf = open(self.srv_log, "w")
        self.srv = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app:app", "--port", str(PORT),
             "--log-level", "warning"],
            cwd=CP_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=logf)
        t0 = time.time()
        while time.time() - t0 < 30:
            if self.srv.poll() is not None:
                logf.close()
                tail = "\n".join(open(self.srv_log).read().splitlines()[-25:])
                raise RuntimeError(f"server died during boot:\n{tail}")
            try:
                r = requests.get(SERVER + "/ready", timeout=3)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("server never became ready (/ready stayed non-200)")

    def shutdown(self):
        self.stop_real_agents()
        if self.srv is not None:
            try:
                self.srv.send_signal(signal.SIGTERM)
                self.srv.wait(timeout=5)
            except Exception:
                try:
                    self.srv.kill()
                except Exception:
                    pass
            self.srv = None

    # -- HTTP --
    def api(self, method, path, json=None, robot_token=None, operator=False,
            timeout=15):
        headers = {}
        if robot_token:
            headers["Authorization"] = f"Bearer {robot_token}"
        if operator:
            headers["X-API-Key"] = OPERATOR_KEY
        return requests.request(method, SERVER + path, json=json,
                                headers=headers, timeout=timeout)

    # -- factories --
    def create_release(self, version):
        tarball = os.path.join(TMPDIR, f"release-{version}.tar.gz")
        build_release_tarball(version, tarball)
        with open(tarball, "rb") as f:
            sig_b64 = sign_bytes(self.priv_b64, f.read())
        r = self.api("POST", "/api/releases", operator=True, json={
            "version": version, "ros_distros": ["humble"], "hw_models": ["wbot-1"],
            "artifact_path": tarball, "artifact_sig_b64": sig_b64,
            "notes": f"e2e test release {version}"})
        assert r.status_code == 200, \
            f"create release {version} -> {r.status_code}: {r.text[:300]}"
        rel = r.json()
        assert rel.get("artifact_sha256"), \
            f"release {version} has no ingested artifact sha256"
        return rel

    def register_robot(self, rid, fleet, ros_distro="humble", hw_model="wbot-1",
                       version="1.0.0"):
        r = self.api("POST", "/api/robots", operator=True, json={
            "id": rid, "name": rid, "fleet_id": fleet,
            "ros_distro": ros_distro, "hw_model": hw_model,
            "current_version": version})
        assert r.status_code == 200, \
            f"register {rid} -> {r.status_code}: {r.text[:300]}"
        token = r.json().get("token")
        assert token, f"server issued no token for {rid}"
        return SimRobot(self, rid, fleet, token, ros_distro, hw_model)

    def create_deployment(self, release_id, fleet, strategy, gates):
        r = self.api("POST", "/api/deployments", operator=True, json={
            "release_id": release_id, "fleet_id": fleet,
            "strategy": strategy, "gates": gates})
        assert r.status_code == 200, \
            f"create deployment -> {r.status_code}: {r.text[:300]}"
        return r.json()

    def pause(self, dep_id):
        r = self.api("POST", f"/api/deployments/{dep_id}/pause", operator=True)
        assert r.status_code == 200, f"pause -> {r.status_code}: {r.text[:200]}"

    def deployment(self, dep_id):
        r = self.api("GET", f"/api/deployments/{dep_id}", operator=True)
        assert r.status_code == 200, \
            f"get deployment -> {r.status_code}: {r.text[:200]}"
        return r.json()

    def fleet_robots(self, fleet):
        r = self.api("GET", f"/api/fleets/{fleet}/robots", operator=True)
        assert r.status_code == 200
        return r.json()["robots"]

    # -- scripted task driving --
    def drive_update(self, robot, task, target_version):
        """Drive a scripted robot through an assigned task to success."""
        assert task["status"] == "assigned", \
            f"{robot.id}: expected assigned task, got {task['status']}"
        for s in ("downloading", "applying", "verifying"):
            robot.set_status(task["id"], s, f"e2e scripted {s}")
        robot.set_status(task["id"], "succeeded", "e2e scripted success",
                         current_version=target_version)
        robot.state["current_version"] = target_version

    # -- real agent.py subprocess --
    def launch_real_agent(self, rid, fleet, token):
        logpath = os.path.join(TMPDIR, f"agent-{rid}.log")
        art_dir = os.path.join(TMPDIR, "agent-artifacts", rid)
        cmd = [VENV_PY, "agent/agent.py", "--server", SERVER,
               "--robot-id", rid, "--name", rid, "--fleet", fleet,
               "--version", "1.0.0", "--battery", "80",
               "--docked", "--charging",
               "--token", token, "--artifact-pubkey", self.pubkey_b64,
               "--artifact-dir", art_dir, "--interval", "1"]
        logf = open(logpath, "w")
        proc = subprocess.Popen(cmd, cwd=ROOT,
                                stdout=logf, stderr=subprocess.STDOUT)
        self.agent_procs.append((rid, proc, logpath, logf))
        return proc

    def stop_real_agents(self):
        for rid, proc, logpath, logf in self.agent_procs:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                logf.close()
            except Exception:
                pass
        self.agent_procs = []

    def dump_agent_logs(self):
        for rid, proc, logpath, _logf in self.agent_procs:
            try:
                lines = open(logpath).read().splitlines()
            except Exception:
                continue
            print(f"--- agent {rid} log (last 25 lines) ---")
            print("\n".join(lines[-25:]))


class SimRobot:
    """A scripted robot driving the real HTTP API (no agent.py process)."""

    def __init__(self, ctx, rid, fleet, token, ros_distro="humble",
                 hw_model="wbot-1"):
        self.ctx = ctx
        self.id = rid
        self.fleet = fleet
        self.token = token
        self.ros_distro = ros_distro
        self.hw_model = hw_model
        self.state = {"battery_pct": 80.0, "docked": True, "charging": True,
                      "on_mission": False, "connectivity": "ok",
                      "current_version": "1.0.0"}

    def set_state(self, **kw):
        self.state.update(kw)

    def heartbeat_body(self):
        return {"battery_pct": self.state["battery_pct"],
                "docked": self.state["docked"],
                "charging": self.state["charging"],
                "on_mission": self.state["on_mission"],
                "connectivity": self.state["connectivity"],
                "ros_distro": self.ros_distro, "hw_model": self.hw_model,
                "current_version": self.state["current_version"],
                "name": self.id, "fleet_id": self.fleet}

    def heartbeat(self):
        r = self.ctx.api("POST", f"/api/robots/{self.id}/heartbeat",
                         json=self.heartbeat_body(), robot_token=self.token)
        assert r.status_code == 200, \
            f"{self.id} heartbeat -> {r.status_code}: {r.text[:200]}"
        return r.json()

    def get_task(self):
        r = self.ctx.api("GET", f"/api/robots/{self.id}/task",
                         robot_token=self.token)
        assert r.status_code == 200, \
            f"{self.id} task poll -> {r.status_code}: {r.text[:200]}"
        return r.json()["task"]

    def set_status(self, task_id, status, detail="", current_version=None):
        r = self.ctx.api(
            "POST", f"/api/robots/{self.id}/task/{task_id}/status",
            json={"status": status, "detail": detail,
                  "current_version": current_version
                  or self.state["current_version"]},
            robot_token=self.token)
        assert r.status_code == 200, \
            f"{self.id} set_status {status} -> {r.status_code}: {r.text[:200]}"
        return r.json()


# -- polling helpers ----------------------------------------------------

def wait_for(desc, fn, heartbeats=(), timeout=30, interval=0.5):
    """Poll fn() until it returns truthy; heartbeat robots meanwhile.

    Raises AssertionError (loud) on timeout instead of hanging forever.
    """
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        for rb in heartbeats:
            try:
                rb.heartbeat()
            except Exception as e:
                last = f"heartbeat {rb.id} failed: {e}"
        try:
            got = fn()
        except Exception as e:  # transient: keep polling until timeout
            last = str(e)
            got = None
        if got:
            return got
        time.sleep(interval)
    raise AssertionError(
        f"TIMEOUT after {timeout}s waiting for: {desc}"
        + (f" (last: {last[:300]})" if last else ""))


def task_with_status(robot, status):
    t = robot.get_task()
    return t if (t and t["status"] == status) else None


def dep_task_with(ctx, dep_id, robot_id, status, is_rollback=0):
    for t in ctx.deployment(dep_id)["tasks"]:
        if (t["robot_id"] == robot_id and t["is_rollback"] == is_rollback
                and t["status"] == status):
            return t
    return None


def dep_status(ctx, dep_id):
    return ctx.deployment(dep_id)["status"]


def assert_stays_pending(robot, window_s=8, must_contain=None, extra_hb=()):
    """Negative gate assertion: task exists but must NOT leave 'pending'.

    Heartbeats keep flowing so a stale heartbeat can never be the reason;
    must_contain pins the assertion to the expected gate reason.
    """
    t = wait_for(f"{robot.id} task exists", robot.get_task,
                 heartbeats=[robot, *extra_hb], timeout=30)
    assert t["status"] == "pending", \
        f"{robot.id}: task should start pending, got {t['status']}"
    t0 = time.time()
    while time.time() - t0 < window_s:
        robot.heartbeat()
        for r in extra_hb:
            r.heartbeat()
        t = robot.get_task()
        assert t is not None, f"{robot.id}: task disappeared mid-scenario"
        assert t["status"] == "pending", \
            f"{robot.id}: gate leaked! task -> {t['status']} ({t['detail'][:120]})"
        if must_contain:
            assert must_contain in (t["detail"] or ""), \
                f"{robot.id}: expected {must_contain!r} in gate detail, " \
                f"got {t['detail']!r}"
        time.sleep(1)

# ---------------------------------------------------------------- scenarios

def scenario_happy_path(ctx):
    """Staged canary rollout completes: canary(1) -> 50%(2) -> 100%(4).

    e2e1-a runs the REAL agent.py (signed-artifact download + verify);
    b/c/d are scripted over HTTP. All reach `succeeded`, versions update,
    deployment completes.

    NOTE on fleet size: the strategy params are exactly canary_count=1 +
    stage_fractions [0.5, 1.0]. With only 2 robots the engine's stage target
    counts would be 1 -> 1 -> 2, so stage 1 would create no new task and the
    deployment could never leave stage 1 ("if stage_tasks" is falsy for an
    empty stage). 4 robots keeps the same strategy shape with strictly
    growing stages (1 -> 2 -> 4). This engine quirk is reported separately.
    """
    fleet = "e2e1"
    rel = ctx.create_release("2.0.0")  # real agent needs a MANIFESTS version
    robots = {rid: ctx.register_robot(rid, fleet)
              for rid in ("e2e1-a", "e2e1-b", "e2e1-c", "e2e1-d")}
    ctx.launch_real_agent("e2e1-a", fleet, robots["e2e1-a"].token)
    sims = [robots[r] for r in ("e2e1-b", "e2e1-c", "e2e1-d")]
    for r in sims:
        r.set_state(docked=True, charging=True, battery_pct=80, on_mission=False)
        r.heartbeat()

    dep = ctx.create_deployment(
        rel["id"], fleet,
        {"canary_count": 1, "stage_fractions": [0.5, 1.0],
         "failure_threshold": 0, "auto_rollback": True}, {})
    dep_id = dep["id"]

    # stage 0: canary via the real agent.py (download + ed25519 verify + ROS checks)
    wait_for("canary e2e1-a task succeeded",
             lambda: dep_task_with(ctx, dep_id, "e2e1-a", "succeeded"),
             heartbeats=sims, timeout=90)
    # stage 1 (50%): e2e1-b
    tb = wait_for("e2e1-b task assigned",
                  lambda: task_with_status(robots["e2e1-b"], "assigned"),
                  heartbeats=sims, timeout=30)
    ctx.drive_update(robots["e2e1-b"], tb, "2.0.0")
    # stage 2 (100%): e2e1-c, e2e1-d
    tc = wait_for("e2e1-c task assigned",
                  lambda: task_with_status(robots["e2e1-c"], "assigned"),
                  heartbeats=sims, timeout=30)
    td = wait_for("e2e1-d task assigned",
                  lambda: task_with_status(robots["e2e1-d"], "assigned"),
                  heartbeats=sims, timeout=30)
    ctx.drive_update(robots["e2e1-c"], tc, "2.0.0")
    ctx.drive_update(robots["e2e1-d"], td, "2.0.0")

    wait_for("deployment completed",
             lambda: dep_status(ctx, dep_id) == "completed",
             heartbeats=sims, timeout=60)
    ctx.stop_real_agents()

    final = ctx.deployment(dep_id)
    counts = {}
    for t in final["tasks"]:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    assert counts.get("succeeded") == 4, \
        f"expected 4 succeeded tasks, got {counts}"
    assert not any(t["is_rollback"] for t in final["tasks"]), \
        "no rollback tasks expected on the happy path"
    versions = {r["id"]: r["current_version"] for r in ctx.fleet_robots(fleet)}
    for rid in robots:
        assert versions[rid] == "2.0.0", \
            f"{rid} at {versions[rid]}, expected 2.0.0"
    evs = ctx.api("GET", f"/api/deployments/{dep_id}/events?limit=200",
                  operator=True).json()["events"]
    adv = sum(1 for e in evs if e["kind"] == "stage_advanced")
    assert adv >= 2, f"expected >=2 stage_advanced events, got {adv}"


def scenario_canary_rollback(ctx):
    """Canary failure halts the deployment and rolls back the bad release.

    A succeeds fully first (so it holds 2.0.0 when the halt fires), then B
    reports `failed` simulating the bad health check while staying on
    1.0.0. The engine must halt, issue A a rollback task (is_rollback=1),
    and A must return to 1.0.0; C (never touched) stays on 1.0.0.
    """
    fleet = "e2e2"
    rel = ctx.create_release("2.1.0")
    a = ctx.register_robot("e2e2-a", fleet)
    b = ctx.register_robot("e2e2-b", fleet)
    c = ctx.register_robot("e2e2-c", fleet)
    hb = [a, b, c]
    for r in hb:
        r.set_state(docked=True, charging=True, battery_pct=80, on_mission=False)
        r.heartbeat()

    dep = ctx.create_deployment(
        rel["id"], fleet,
        {"canary_count": 2, "stage_fractions": [1.0],
         "failure_threshold": 0, "auto_rollback": True}, {})
    dep_id = dep["id"]

    # A completes the update BEFORE B fails: rollback only covers robots
    # that actually hold the bad release at halt time.
    ta = wait_for("e2e2-a task assigned",
                  lambda: task_with_status(a, "assigned"),
                  heartbeats=hb, timeout=30)
    ctx.drive_update(a, ta, "2.1.0")
    wait_for("deployment still running after A succeeded",
             lambda: dep_status(ctx, dep_id) == "running",
             heartbeats=hb, timeout=15)

    # B fails its health check; it never took the release (stays 1.0.0).
    tb = wait_for("e2e2-b task assigned",
                  lambda: task_with_status(b, "assigned"),
                  heartbeats=hb, timeout=30)
    b.set_status(tb["id"], "failed",
                 "post-update health check FAILED: node:nav2_planner state=crashed",
                 current_version="1.0.0")

    wait_for("deployment halting",
             lambda: dep_status(ctx, dep_id) in ("rolling_back", "halted"),
             heartbeats=hb, timeout=30)

    # A gets a rollback task and drives it back to the old version.
    rbt = wait_for("e2e2-a rollback task assigned",
                   lambda: dep_task_with(ctx, dep_id, "e2e2-a", "assigned",
                                         is_rollback=1),
                   heartbeats=hb, timeout=30)
    assert rbt["is_rollback"] == 1, "rollback task must be flagged is_rollback=1"
    assert (rbt["from_version"], rbt["to_version"]) == ("2.1.0", "1.0.0"), \
        f"rollback should be 2.1.0 -> 1.0.0, got {rbt['from_version']} -> {rbt['to_version']}"
    ctx.drive_update(a, rbt, "1.0.0")

    wait_for("deployment halted",
             lambda: dep_status(ctx, dep_id) == "halted",
             heartbeats=hb, timeout=30)

    tasks = ctx.deployment(dep_id)["tasks"]
    by_robot = {}
    for t in tasks:
        by_robot.setdefault(t["robot_id"], []).append(t)
    a_tasks = {t["is_rollback"]: t["status"] for t in by_robot["e2e2-a"]}
    assert a_tasks.get(0) == "succeeded", f"A forward task: {a_tasks}"
    assert a_tasks.get(1) == "rolled_back", \
        f"A rollback task should be rolled_back, got {a_tasks}"
    b_tasks = by_robot["e2e2-b"]
    assert len(b_tasks) == 1 and b_tasks[0]["status"] == "failed", \
        f"B should have exactly its failed task, got {b_tasks}"
    assert "e2e2-c" not in by_robot, \
        f"C was never touched but has tasks: {by_robot.get('e2e2-c')}"
    versions = {r["id"]: r["current_version"] for r in ctx.fleet_robots(fleet)}
    assert versions["e2e2-a"] == "1.0.0", f"A at {versions['e2e2-a']}, expected 1.0.0"
    assert versions["e2e2-b"] == "1.0.0", f"B at {versions['e2e2-b']}, expected 1.0.0"
    assert versions["e2e2-c"] == "1.0.0", f"C at {versions['e2e2-c']}, expected 1.0.0"
    kinds = {e["kind"] for e in ctx.api(
        "GET", f"/api/deployments/{dep_id}/events?limit=200",
        operator=True).json()["events"]}
    assert "deployment_halted" in kinds, "missing deployment_halted event"
    assert "rollback_complete" in kinds, "missing rollback_complete event"


def scenario_gate_blocking(ctx):
    """Gates block, then unblock.

    e2e3-a-mission is the canary: while on_mission its task must stay
    `pending` (never `assigned`); once docked+charging+healthy it is
    assigned and proceeds. e2e3-b-lowbatt (10% battery) must then stay
    `pending` on its gate.
    """
    fleet = "e2e3"
    rel = ctx.create_release("2.2.0")
    m = ctx.register_robot("e2e3-a-mission", fleet)  # sorts first -> canary
    l = ctx.register_robot("e2e3-b-lowbatt", fleet)
    m.set_state(on_mission=True, docked=True, charging=True, battery_pct=80)
    l.set_state(on_mission=False, docked=True, charging=True, battery_pct=10)
    m.heartbeat()
    l.heartbeat()

    dep = ctx.create_deployment(
        rel["id"], fleet,
        {"canary_count": 1, "stage_fractions": [1.0],
         "failure_threshold": 0, "auto_rollback": True}, {})
    dep_id = dep["id"]

    # phase 1: mission gate blocks (task exists, never assigned)
    assert_stays_pending(m, window_s=8, must_contain="mission", extra_hb=[l])

    # phase 2: mission ends -> gates pass -> assigned -> succeeds
    m.set_state(on_mission=False, docked=True, charging=True)
    tm = wait_for("mission robot task assigned",
                  lambda: task_with_status(m, "assigned"),
                  heartbeats=[m, l], timeout=30)
    ctx.drive_update(m, tm, "2.2.0")
    wait_for("mission robot task succeeded",
             lambda: dep_task_with(ctx, dep_id, "e2e3-a-mission", "succeeded"),
             heartbeats=[m, l], timeout=30)

    # phase 3: low-battery gate blocks the stage-1 robot
    assert_stays_pending(l, window_s=8, must_contain="battery", extra_hb=[m])
    ctx.pause(dep_id)


def scenario_incompatible_distro(ctx):
    """A foxy robot on a humble-only release is skipped, not updated."""
    fleet = "e2e4"
    r = ctx.register_robot("e2e4-foxy", fleet, ros_distro="foxy")
    r.set_state(docked=True, charging=True, battery_pct=80, on_mission=False)
    r.heartbeat()
    rel = ctx.create_release("2.3.0")

    dep = ctx.create_deployment(
        rel["id"], fleet,
        {"canary_count": 1, "stage_fractions": [1.0],
         "failure_threshold": 0, "auto_rollback": True}, {})
    dep_id = dep["id"]

    t = wait_for("foxy robot task skipped",
                 lambda: dep_task_with(ctx, dep_id, "e2e4-foxy", "skipped"),
                 heartbeats=[r], timeout=30)
    assert "incompatible" in (t["detail"] or "").lower(), \
        f"expected 'incompatible' in skip detail, got {t['detail']!r}"
    assert "foxy" in (t["detail"] or ""), \
        f"expected distro name in skip detail, got {t['detail']!r}"
    versions = {x["id"]: x["current_version"] for x in ctx.fleet_robots(fleet)}
    assert versions["e2e4-foxy"] == "1.0.0", "skipped robot must keep its version"
    ctx.pause(dep_id)


def scenario_bad_token(ctx):
    """A known robot presenting a wrong bearer token gets 401."""
    r = ctx.register_robot("e2e5-r", "e2e5")
    assert r.token and len(r.token) > 10, "expected a real issued token"
    bad = {"Authorization": "Bearer this-is-the-wrong-token"}

    resp = requests.post(SERVER + f"/api/robots/{r.id}/heartbeat",
                         json=r.heartbeat_body(), headers=bad, timeout=10)
    assert resp.status_code == 401, \
        f"heartbeat with bad token -> {resp.status_code}, expected 401"
    resp = requests.get(SERVER + f"/api/robots/{r.id}/task",
                        headers=bad, timeout=10)
    assert resp.status_code == 401, \
        f"task poll with bad token -> {resp.status_code}, expected 401"
    resp = requests.post(SERVER + f"/api/robots/{r.id}/heartbeat",
                         json=r.heartbeat_body(), timeout=10)
    assert resp.status_code == 401, \
        f"heartbeat with no token -> {resp.status_code}, expected 401"
    # sanity: the real token still works
    r.heartbeat()


def scenario_tampered_artifact(ctx):
    """A tampered tarball fails download_and_verify; bad token -> 401."""
    rel = ctx.create_release("9.9.9")
    release_id = rel["id"]
    stored = os.path.join(ARTIFACT_DIR, release_id + ".tar.gz")
    assert os.path.exists(stored), f"stored artifact missing at {stored}"
    with open(stored, "ab") as f:
        f.write(b"tampered-by-e2e-test")

    r = ctx.register_robot("e2e6-r", "e2e6")
    dl_dir = os.path.join(TMPDIR, "tamper-dl")
    try:
        download_and_verify(SERVER, release_id, r.token,
                            ctx.pubkey_b64, dl_dir)
    except ArtifactVerificationError as e:
        assert "sha256" in str(e).lower() or "mismatch" in str(e).lower(), \
            f"expected a checksum-mismatch error, got: {e}"
    else:
        raise AssertionError(
            "download_and_verify accepted a tampered artifact!")

    resp = requests.get(SERVER + f"/artifacts/{release_id}",
                        headers={"Authorization": "Bearer wrong-token"},
                        timeout=10)
    assert resp.status_code == 401, \
        f"artifact GET with bad token -> {resp.status_code}, expected 401"


# ---------------------------------------------------------------- main

SCENARIOS = [
    ("happy_path_canary_staged", scenario_happy_path),
    ("canary_failure_halt_rollback", scenario_canary_rollback),
    ("gate_blocking_then_unblocking", scenario_gate_blocking),
    ("incompatible_distro_skipped", scenario_incompatible_distro),
    ("bad_bearer_token_rejected", scenario_bad_token),
    ("tampered_artifact_rejected", scenario_tampered_artifact),
]


def main():
    ctx = Ctx()
    t_all = time.time()
    try:
        ctx.boot_server()
        print(f"server up at {SERVER} (db={DB_PATH})")
        failures = []
        for name, fn in SCENARIOS:
            t0 = time.time()
            try:
                fn(ctx)
            except Exception:
                failures.append(name)
                print(f"FAIL {name} ({time.time() - t0:.1f}s)")
                traceback.print_exc()
                ctx.dump_agent_logs()
            else:
                print(f"PASS {name} ({time.time() - t0:.1f}s)")
        ok = len(SCENARIOS) - len(failures)
        print(f"\n{ok}/{len(SCENARIOS)} e2e scenarios passed "
              f"({time.time() - t_all:.0f}s total)")
        return 1 if failures else 0
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    sys.exit(main())
