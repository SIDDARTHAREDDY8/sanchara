"""Sanchara robot-side agent.

Runs on each robot (here: one process per simulated robot). It:
  1. heartbeats robot state (battery, dock, mission, connectivity) to the
     control plane,
  2. polls for an assigned update task,
  3. runs ROS-aware preflight, applies the update, then verifies the live
     ROS graph against the target version's manifest,
  4. reports every transition; on verification failure it reports `failed`,
     which is what triggers the control plane's auto-rollback.

Failure injection (--inject-failure) simulates a bad release on this robot
so the demo can show detection + rollback.
"""
import argparse
import os
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from ros_backend import RclpyBackend
from ros_sim import MANIFESTS, SimulatedROSStack
from secure_update import ArtifactVerificationError, download_and_verify


def log(robot_id, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{robot_id}] {msg}", flush=True)


class Agent:
    def __init__(self, args):
        self.a = args
        if args.ros_backend == "rclpy":
            self.stack = RclpyBackend(version=args.version, manifests=MANIFESTS)
        else:
            self.stack = SimulatedROSStack(version=args.version,
                                           seed=hash(args.robot_id) % 10000)
        self.battery = args.battery
        self.docked = args.docked
        self.charging = args.charging
        self.on_mission = args.on_mission
        self.mission_ticks = args.mission_ticks
        self.session = requests.Session()

    def api(self, method, path, **kw):
        url = self.a.server.rstrip("/") + path
        headers = dict(kw.pop("headers", {}) or {})
        if self.a.token:
            headers["Authorization"] = f"Bearer {self.a.token}"
        r = self.session.request(method, url, headers=headers, timeout=10, **kw)
        r.raise_for_status()
        return r.json()

    def heartbeat(self):
        # simple physical simulation
        if self.charging:
            self.battery = min(100.0, self.battery + 1.5)
        elif self.on_mission:
            self.battery = max(0.0, self.battery - 0.8)
        else:
            self.battery = max(0.0, self.battery - 0.1)
        if self.on_mission and self.mission_ticks > 0:
            self.mission_ticks -= 1
            if self.mission_ticks == 0:
                self.on_mission = False
                self.docked = True
                self.charging = True
                log(self.a.robot_id, "mission complete; docked and charging")
        try:
            resp = self.api("POST", f"/api/robots/{self.a.robot_id}/heartbeat", json={
                "battery_pct": round(self.battery, 1),
                "docked": self.docked, "charging": self.charging,
                "on_mission": self.on_mission, "connectivity": "ok",
                "ros_distro": self.a.ros_distro, "hw_model": self.a.hw_model,
                "current_version": self.stack.version,
                "name": self.a.name, "fleet_id": self.a.fleet,
            })
            # bootstrap: first heartbeat for an unknown robot returns a
            # one-time bearer token; capture and reuse it from now on
            if isinstance(resp, dict) and resp.get("token") and not self.a.token:
                self.a.token = resp["token"]
                log(self.a.robot_id, "bootstrap: bearer token received")
        except Exception as e:
            log(self.a.robot_id, f"heartbeat failed: {e}")

    def set_status(self, task, status, detail=""):
        try:
            self.api("POST",
                     f"/api/robots/{self.a.robot_id}/task/{task['id']}/status",
                     json={"status": status, "detail": detail,
                           "current_version": self.stack.version})
        except Exception as e:
            log(self.a.robot_id, f"status update failed: {e}")
        log(self.a.robot_id, f"task {task['id'][:12]}… -> {status}"
            + (f" ({detail[:100]})" if detail else ""))

    def run_update(self, task):
        target = task["to_version"]
        is_rb = task.get("is_rollback")
        log(self.a.robot_id, f"{'ROLLBACK' if is_rb else 'UPDATE'} "
                             f"{task['from_version']} -> {target}")

        # preflight (ROS-aware)
        ok, checks = self.stack.preflight_check(target)
        if not ok:
            bad = "; ".join(c["detail"] for c in checks if not c["ok"])
            self.set_status(task, "failed", f"preflight failed: {bad}")
            return

        self.set_status(task, "downloading", f"fetching release {target}")
        if self.a.artifact_pubkey and not is_rb:
            # real artifact download + sha256/ed25519 verification
            os.makedirs(self.a.artifact_dir, exist_ok=True)
            release_id = task.get("release_id")
            if not release_id:
                self.set_status(task, "failed",
                                "no release_id on task; cannot download artifact")
                return
            last_pct = [0]

            def progress(done, total):
                pct = int(done * 100 / total) if total else 0
                if pct >= last_pct[0] + 25:
                    last_pct[0] = pct - (pct % 25)
                    log(self.a.robot_id,
                        f"  …downloading {target}: {pct}% ({done}/{total or '?'} bytes)")
                    self.set_status(task, "downloading",
                                    f"fetching release {target}: {pct}%")

            try:
                path = download_and_verify(
                    self.a.server, release_id, self.a.token,
                    self.a.artifact_pubkey, self.a.artifact_dir,
                    progress_cb=progress)
            except ArtifactVerificationError as e:
                self.set_status(task, "failed",
                                f"artifact verification failed: {e}")
                return
            except Exception as e:
                self.set_status(task, "failed", f"artifact download failed: {e}")
                return
            self.set_status(task, "downloading",
                             f"artifact verified: {os.path.basename(path)}")
        else:
            # legacy path: no pubkey configured (or rollback to a known-good
            # local version, which needs no new bytes) — simulated download
            time.sleep(2)

        self.set_status(task, "applying", "installing packages, restarting ROS stack")
        inject = None
        if self.a.inject_failure and not is_rb:
            inject = self.a.inject_failure
            log(self.a.robot_id, f"!! failure injection armed: {inject}")
        try:
            self.stack.apply_update(target, inject_failure=inject,
                                    progress_cb=lambda s: log(self.a.robot_id, f"  …{s}"))
        except Exception as e:
            self.set_status(task, "failed", f"apply failed: {e}")
            return

        # post-update ROS-aware health verification
        self.set_status(task, "verifying",
                        f"checking ROS graph against manifest for {target}")
        time.sleep(2)
        ok, checks = self.stack.health_check(target)
        if ok:
            self.set_status(task, "succeeded",
                             f"ROS graph healthy on {target} "
                             f"({len(checks)} checks passed)")
        else:
            bad = "; ".join(f"{c['name']} {c['detail']}"
                            for c in checks if not c["ok"])
            self.set_status(task, "failed",
                             f"post-update health check FAILED: {bad}")

    def run(self):
        log(self.a.robot_id,
            f"agent started (v{self.stack.version}, {self.a.ros_distro}, {self.a.hw_model}, "
            f"battery={self.battery:.0f}%, docked={self.docked}, mission={self.on_mission})")
        while True:
            self.heartbeat()
            try:
                resp = self.api("GET", f"/api/robots/{self.a.robot_id}/task")
                task = resp.get("task")
                if task and task["status"] == "assigned":
                    self.run_update(task)
            except Exception as e:
                log(self.a.robot_id, f"task poll failed: {e}")
            time.sleep(self.a.interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--robot-id", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--fleet", default="default")
    p.add_argument("--ros-distro", default="humble")
    p.add_argument("--hw-model", default="wbot-1")
    p.add_argument("--version", default="1.0.0")
    p.add_argument("--battery", type=float, default=80)
    p.add_argument("--docked", action="store_true")
    p.add_argument("--charging", action="store_true")
    p.add_argument("--on-mission", action="store_true")
    p.add_argument("--mission-ticks", type=int, default=0,
                   help="heartbeats until the current mission ends (0 = never)")
    p.add_argument("--inject-failure", default="",
                   help="crash_node | topic_stall (applies to forward updates only)")
    p.add_argument("--interval", type=float, default=3)
    p.add_argument("--ros-backend", choices=["sim", "rclpy"], default="sim",
                   help="ROS backend: sim (default, no ROS install needed) or "
                        "rclpy (real ROS 2)")
    p.add_argument("--token", default="",
                   help="bearer token for the robot API; if absent, the agent "
                        "bootstraps: the first heartbeat captures the one-time "
                        "token the server returns and reuses it")
    p.add_argument("--artifact-pubkey", default="",
                   help="base64 ed25519 public key; when set, the agent really "
                        "downloads the release tarball from GET /artifacts/{id} "
                        "and verifies sha256 + signature. When absent, the "
                        "legacy simulated download sleep is kept")
    p.add_argument("--artifact-dir", default="/tmp/sanchara-artifacts",
                   help="where verified release tarballs are stored")
    args = p.parse_args()
    Agent(args).run()


if __name__ == "__main__":
    main()
