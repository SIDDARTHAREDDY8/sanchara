"""End-to-end Sanchara demo.

Spins up the control plane + 8 simulated ROS 2 robots and runs a canary
deployment of release 2.0.0 that goes wrong on purpose:

  wbot-01  docked/charging, healthy        -> canary, succeeds
  wbot-02  docked/charging, BAD release    -> canary, health check FAILS
  wbot-03  on a mission                    -> gated: never touched mid-mission
  wbot-04  docked, 25% battery             -> gated: battery too low
  wbot-05  idle, not docked                -> gated: not at dock
  wbot-06  docked/charging, healthy        -> would be stage 1
  wbot-07  docked/charging, healthy        -> would be stage 1
  wbot-08  docked/charging, ROS "foxy"     -> skipped: incompatible distro

Expected outcome: canary catches the bad release -> deployment halts ->
wbot-01 auto-rolls back to 1.0.0 -> the rest of the fleet never sees 2.0.0.

Usage:  .venv/bin/python demo/run_demo.py [--keep]
"""
import argparse
import base64
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
SERVER = "http://127.0.0.1:8000"

sys.path.insert(0, os.path.join(ROOT, "control-plane"))
from artifacts import generate_keypair, sign_bytes  # noqa: E402

import urllib.request  # noqa: E402

OPERATOR_KEY = secrets.token_urlsafe(32)


def api(method, path, data=None, headers=None):
    h = {"Content-Type": "application/json", "X-API-Key": OPERATOR_KEY}
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        SERVER + path, data=json.dumps(data).encode() if data is not None else None,
        method=method, headers=h)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def wait_for_server(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            api("GET", "/api/health")
            return True
        except Exception:
            time.sleep(0.5)
    return False


def build_release_tarball(version, path):
    """Build a real release tarball: a VERSION file + manifest notes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in {
            "VERSION": f"{version}\n",
            "NOTES.txt": (f"Sanchara release {version}\n"
                          "adds perception_v2 node; /scan 10->15 Hz\n"
                          "ros_distros: humble\nhw_models: wbot-1\n"),
        }.items():
            data = text.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return path


ROBOTS = [
    dict(id="wbot-01", docked=True, charging=True, battery=80),
    dict(id="wbot-02", docked=True, charging=True, battery=75, inject="crash_node"),
    dict(id="wbot-03", on_mission=True, battery=60, mission_ticks=40),
    dict(id="wbot-04", docked=True, battery=25),
    dict(id="wbot-05", battery=70),
    dict(id="wbot-06", docked=True, charging=True, battery=90),
    dict(id="wbot-07", docked=True, charging=True, battery=65),
    dict(id="wbot-08", docked=True, charging=True, battery=85, ros_distro="foxy"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="leave server + agents running after the demo")
    args = ap.parse_args()

    procs = []

    def cleanup():
        for p in procs:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass

    # fresh database for a clean demo
    db = os.path.join(ROOT, "control-plane", "sanchara.db")
    if os.path.exists(db):
        os.remove(db)

    print("== Sanchara demo: ROS-aware fleet update with canary + auto-rollback ==\n")

    print("[1/5] starting control plane (operator auth enabled)…")
    env = dict(os.environ, SANCHARA_OPERATOR_KEY=OPERATOR_KEY)
    srv = subprocess.Popen(
        [VENV_PY, "-m", "uvicorn", "app:app", "--port", "8000", "--log-level", "warning"],
        cwd=os.path.join(ROOT, "control-plane"), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(srv)
    if not wait_for_server():
        print("server failed to start"); cleanup(); sys.exit(1)
    print(f"      control plane up · dashboard: {SERVER}/")

    print("[2/5] building + signing release 2.0.0 artifact…")
    priv_b64, pub_b64 = generate_keypair()
    tarball = os.path.join("/tmp", "sanchara-demo-2.0.0.tar.gz")
    build_release_tarball("2.0.0", tarball)
    with open(tarball, "rb") as f:
        sig_b64 = sign_bytes(priv_b64, f.read())
    print("      tarball signed with ed25519")
    rel = api("POST", "/api/releases", {
        "version": "2.0.0", "ros_distros": ["humble"], "hw_models": ["wbot-1"],
        "artifact_url": "s3://sanchara-releases/wbot/2.0.0",
        "artifact_path": tarball, "artifact_sig_b64": sig_b64,
        "notes": "adds perception_v2 node; /scan 10→15 Hz"})
    print(f"      release {rel['id']} ({rel['version']}), "
          f"sha256={rel['artifact_sha256'][:16]}…")

    print("[3/5] registering robots (operator key) + launching 8 agents…")
    for r in ROBOTS:
        reg = api("POST", "/api/robots", {
            "id": r["id"], "name": r["id"], "fleet_id": "warehouse-a",
            "ros_distro": r.get("ros_distro", "humble"), "hw_model": "wbot-1",
            "current_version": "1.0.0"})
        token = reg["token"]  # one-time plaintext bearer token
        cmd = [VENV_PY, "agent/agent.py", "--server", SERVER,
               "--robot-id", r["id"], "--name", r["id"], "--fleet", "warehouse-a",
               "--ros-distro", r.get("ros_distro", "humble"), "--hw-model", "wbot-1",
               "--version", "1.0.0", "--battery", str(r.get("battery", 80)),
               "--token", token, "--artifact-pubkey", pub_b64,
               "--interval", "2"]
        if r.get("docked"): cmd.append("--docked")
        if r.get("charging"): cmd.append("--charging")
        if r.get("on_mission"): cmd.append("--on-mission")
        if r.get("mission_ticks"): cmd += ["--mission-ticks", str(r["mission_ticks"])]
        if r.get("inject"): cmd += ["--inject-failure", r["inject"]]
        p = subprocess.Popen(cmd, cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
    time.sleep(6)  # let heartbeats register
    robots = api("GET", "/api/fleets/warehouse-a/robots")["robots"]
    print(f"      {len(robots)} robots heartbeating")

    print("[4/5] starting canary deployment (2 canaries → 50% → 100%)…")
    dep = api("POST", "/api/deployments", {
        "release_id": rel["id"], "fleet_id": "warehouse-a",
        "strategy": {"canary_count": 2, "stage_fractions": [0.5, 1.0],
                     "failure_threshold": 0, "auto_rollback": True},
        "gates": {"min_battery": 40, "require_docked": True}})
    dep_id = dep["id"]
    print(f"      deployment {dep_id}\n")
    print("--- live event log (Ctrl-C keeps everything running with --keep) ---")

    seen = 0
    t0 = time.time()
    final = None
    try:
        while time.time() - t0 < 180:
            st = api("GET", f"/api/deployments/{dep_id}")
            evs = api("GET", f"/api/deployments/{dep_id}/events?limit=200")["events"]
            new = [e for e in evs if e["id"] > seen]
            for e in sorted(new, key=lambda x: x["id"]):
                ts = datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
                print(f"  [{ts}] {e['robot_id'] or 'fleet':8} {e['message']}")
            if new:
                seen = max(e["id"] for e in new)
            if st["status"] in ("halted", "completed"):
                final = st
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass

    print("\n[5/5] final fleet state:")
    robots = api("GET", "/api/fleets/warehouse-a/robots")["robots"]
    tasks = {t["robot_id"]: t for t in api("GET", f"/api/deployments/{dep_id}")["tasks"]}
    print(f"  {'robot':9} {'version':9} {'task':14} detail")
    for r in robots:
        t = tasks.get(r["id"])
        tstr = t["status"] if t else "—"
        if t and t["is_rollback"]:
            tstr += " (rollback)"
        print(f"  {r['id']:9} {r['current_version']:9} {tstr:14} "
              f"{(t['detail'][:70] if t else '')}")

    if final:
        print(f"\ndeployment {dep_id}: {final['status'].upper()}")
        c = final.get("task_counts", {})
        print("task counts:", ", ".join(f"{k}={v}" for k, v in sorted(c.items())))

    print(f"\ndashboard: {SERVER}/")
    if args.keep:
        print("leaving server + agents running (--keep). Kill them manually when done.")
    else:
        cleanup()
        print("demo processes stopped.")


if __name__ == "__main__":
    main()
