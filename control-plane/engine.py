"""Rollout engine: gates, staged progression, failure detection, auto-rollback.

The engine is ticked every few seconds. It never pushes bytes to robots; it
promotes tasks through states and the robot-side agent pulls its assignment.
"""
import json
import math
import time

from store import (
    create_task, deployment_tasks, fleet_robots, get_deployment, get_release,
    list_deployments, log_event, robot_task, set_deployment_stage,
    set_deployment_status, set_task_status, get_robot,
)


def _load(obj, key):
    v = obj.get(key)
    return json.loads(v) if isinstance(v, str) else (v or {})


def check_compat(robot, release):
    """Static compatibility: ROS distro + hardware model. Decides skip vs. rollout."""
    reasons = []
    distros = _load(release, "ros_distros")
    hw = _load(release, "hw_models")
    if distros and robot.get("ros_distro") not in distros:
        reasons.append(f"ros distro {robot.get('ros_distro')} not in {distros}")
    if hw and robot.get("hw_model") not in hw:
        reasons.append(f"hw model {robot.get('hw_model')} not in {hw}")
    return (len(reasons) == 0, reasons)


def check_gates(robot, release, gates):
    """Dynamic pre-update gates evaluated from the latest heartbeat."""
    reasons = []
    now = time.time()
    last = robot.get("last_heartbeat") or 0
    if now - last > gates.get("heartbeat_timeout_s", 30):
        reasons.append("heartbeat stale (>%ss)" % gates.get("heartbeat_timeout_s", 30))
    if robot.get("connectivity") != gates.get("require_connectivity", "ok"):
        reasons.append(f"connectivity {robot.get('connectivity')!r} != 'ok'")
    if robot.get("on_mission"):
        reasons.append("robot is on an active mission")
    if gates.get("require_docked") and not (robot.get("docked") or robot.get("charging")):
        reasons.append("robot is not docked/charging")
    try:
        batt = float(robot.get("battery_pct") or 0)
    except (TypeError, ValueError):
        batt = 0
    if batt < gates.get("min_battery", 40):
        reasons.append(f"battery {batt:.0f}% < {gates.get('min_battery', 40)}% minimum")
    ok, compat_reasons = check_compat(robot, release)
    reasons.extend(compat_reasons)
    return (len(reasons) == 0, reasons)


def _stage_target_count(strategy, stage, fleet_size):
    if stage == 0:
        return min(strategy.get("canary_count", 1), fleet_size)
    fracs = strategy.get("stage_fractions", [1.0])
    idx = min(stage - 1, len(fracs) - 1)
    return min(fleet_size, max(1, math.ceil(fracs[idx] * fleet_size)))


def _max_stage(strategy):
    return len(strategy.get("stage_fractions", [1.0]))


def _promote_pending(dep, release, gates, only_rollback=False):
    """Move pending tasks to assigned when gates pass."""
    for t in deployment_tasks(dep["id"], stage=dep["current_stage"]):
        if t["status"] != "pending":
            continue
        if only_rollback and not t["is_rollback"]:
            continue
        if not only_rollback and t["is_rollback"]:
            continue
        robot = get_robot(t["robot_id"])
        if not robot:
            continue
        ok, reasons = check_gates(robot, release, gates)
        if ok:
            set_task_status(t["id"], "assigned",
                            "gates passed; assigned to agent")
            log_event(dep["id"], robot["id"], "task_assigned",
                      f"{'Rollback' if t['is_rollback'] else 'Update'} task assigned: "
                      f"{t['from_version']} -> {t['to_version']}")
        else:
            set_task_status(t["id"], "pending", "waiting on gates: " + "; ".join(reasons))


def _ensure_stage_tasks(dep, release):
    """Create pending tasks for this stage's target robots if missing."""
    robots = fleet_robots(dep["fleet_id"])
    strategy = _load(dep, "strategy")
    n = _stage_target_count(strategy, dep["current_stage"], len(robots))
    targets = robots[:n]
    for r in targets:
        if robot_task(dep["id"], r["id"]):
            continue
        ok, reasons = check_compat(r, release)
        if not ok:
            create_task(dep["id"], r["id"], dep["current_stage"],
                        r.get("current_version"), release["version"],
                        status="skipped", detail="incompatible: " + "; ".join(reasons))
            log_event(dep["id"], r["id"], "task_skipped",
                      f"Skipped {r['id']}: " + "; ".join(reasons))
        else:
            create_task(dep["id"], r["id"], dep["current_stage"],
                        r.get("current_version"), release["version"])
            log_event(dep["id"], r["id"], "task_created",
                      f"Update task created: {r.get('current_version')} -> {release['version']} "
                      f"(stage {dep['current_stage']})")


def _failures(dep):
    return [t for t in deployment_tasks(dep["id"])
            if t["status"] == "failed" and not t["is_rollback"]]


def _halt_and_rollback(dep, release, failed):
    strategy = _load(dep, "strategy")
    set_deployment_status(dep["id"], "rolling_back")
    log_event(dep["id"], None, "deployment_halted",
              f"Halted: {len(failed)} failure(s) exceeded threshold "
              f"({strategy.get('failure_threshold', 0)}). Auto-rollback starting.")
    rolled = 0
    for r in fleet_robots(dep["fleet_id"]):
        if r.get("current_version") != release["version"]:
            continue
        orig = robot_task(dep["id"], r["id"])
        prev = (orig or {}).get("from_version") or "unknown"
        # don't create a duplicate rollback task
        existing = [t for t in deployment_tasks(dep["id"]) if t["robot_id"] == r["id"]
                    and t["is_rollback"]]
        if existing:
            continue
        create_task(dep["id"], r["id"], dep["current_stage"], release["version"], prev,
                    is_rollback=1)
        rolled += 1
        log_event(dep["id"], r["id"], "rollback_started",
                  f"Rolling back {r['id']}: {release['version']} -> {prev}")
    if rolled == 0:
        set_deployment_status(dep["id"], "halted")
        log_event(dep["id"], None, "deployment_halted",
                  "Halted with nothing to roll back (no robot had applied the release).")


def _process_running(dep):
    release = get_release(dep["release_id"])
    strategy = _load(dep, "strategy")
    gates = _load(dep, "gates")
    _ensure_stage_tasks(dep, release)
    _promote_pending(dep, release, gates)

    failed = _failures(dep)
    if len(failed) > strategy.get("failure_threshold", 0):
        if strategy.get("auto_rollback", True):
            _halt_and_rollback(dep, release, failed)
        else:
            set_deployment_status(dep["id"], "halted")
            log_event(dep["id"], None, "deployment_halted",
                      f"Halted: {len(failed)} failure(s); auto-rollback disabled.")
        return

    stage_tasks = [t for t in deployment_tasks(dep["id"], stage=dep["current_stage"])
                   if not t["is_rollback"]]
    # NOTE: an empty stage counts as complete (vacuously true). This can happen
    # when a stage's target count does not grow over the previous stage (e.g.
    # canary_count=1 with stage_fractions=[0.5, 1.0] on a 2-robot fleet) — the
    # target prefix slice is already covered by earlier stages, so no new
    # tasks exist and there is nothing to wait for.
    if all(t["status"] in ("succeeded", "failed", "rolled_back", "skipped")
           for t in stage_tasks):
        if dep["current_stage"] < _max_stage(strategy):
            set_deployment_stage(dep["id"], dep["current_stage"] + 1)
            log_event(dep["id"], None, "stage_advanced",
                      f"Stage {dep['current_stage']} complete; advancing to stage "
                      f"{dep['current_stage'] + 1}.")
        else:
            set_deployment_status(dep["id"], "completed")
            log_event(dep["id"], None, "deployment_completed",
                      f"Deployment {dep['id']} completed: release {release['version']} "
                      f"across fleet {dep['fleet_id']}.")


def _process_rolling_back(dep):
    release = get_release(dep["release_id"])
    gates = _load(dep, "gates")
    _promote_pending(dep, release, gates, only_rollback=True)
    rbs = [t for t in deployment_tasks(dep["id"]) if t["is_rollback"]]
    # agent reports 'succeeded' on a rollback task; normalize to 'rolled_back'
    for t in rbs:
        if t["status"] == "succeeded":
            set_task_status(t["id"], "rolled_back", t["detail"])
            log_event(dep["id"], t["robot_id"], "rolled_back",
                      f"{t['robot_id']} rolled back to {t['to_version']}")
    rbs = [t for t in deployment_tasks(dep["id"]) if t["is_rollback"]]
    if rbs and all(t["status"] in ("rolled_back", "failed", "skipped") for t in rbs):
        set_deployment_status(dep["id"], "halted")
        ok = sum(1 for t in rbs if t["status"] == "rolled_back")
        log_event(dep["id"], None, "rollback_complete",
                  f"Rollback complete: {ok}/{len(rbs)} robot(s) restored. "
                  f"Deployment {dep['id']} halted.")


def tick():
    for dep in list_deployments():
        try:
            dep = get_deployment(dep["id"])
            if dep["status"] == "running":
                _process_running(dep)
            elif dep["status"] == "rolling_back":
                _process_rolling_back(dep)
        except Exception as e:  # never let one bad deployment kill the loop
            log_event(dep["id"], None, "engine_error", f"engine error: {e}")
