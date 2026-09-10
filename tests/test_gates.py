"""Unit tests for Sanchara rollout gates and engine helpers."""
import sys
import time

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "../control-plane"))
from engine import check_gates, check_compat, _stage_target_count  # noqa: E402

REL = {"ros_distros": ["humble"], "hw_models": ["wbot-1"], "version": "2.0.0"}
GATES = {"min_battery": 40, "require_docked": True,
         "require_connectivity": "ok", "heartbeat_timeout_s": 30}


def robot(**kw):
    r = {"id": "t", "ros_distro": "humble", "hw_model": "wbot-1",
         "battery_pct": 80, "docked": 1, "charging": 1, "on_mission": 0,
         "connectivity": "ok", "last_heartbeat": time.time()}
    r.update(kw)
    return r


def test_gates_pass():
    ok, reasons = check_gates(robot(), REL, GATES)
    assert ok, reasons


def test_gate_blocks_mission():
    ok, reasons = check_gates(robot(on_mission=1), REL, GATES)
    assert not ok and any("mission" in x for x in reasons), reasons


def test_gate_blocks_low_battery():
    ok, reasons = check_gates(robot(battery_pct=10), REL, GATES)
    assert not ok and any("battery" in x for x in reasons), reasons


def test_gate_blocks_undocked():
    ok, reasons = check_gates(robot(docked=0, charging=0), REL, GATES)
    assert not ok and any("docked" in x for x in reasons), reasons


def test_gate_blocks_stale_heartbeat():
    ok, reasons = check_gates(robot(last_heartbeat=time.time() - 120), REL, GATES)
    assert not ok and any("heartbeat" in x for x in reasons), reasons


def test_compat_blocks_wrong_distro():
    ok, reasons = check_compat(robot(ros_distro="foxy"), REL)
    assert not ok and any("foxy" in x for x in reasons), reasons


def test_stage_target_counts():
    s = {"canary_count": 2, "stage_fractions": [0.5, 1.0]}
    assert _stage_target_count(s, 0, 8) == 2
    assert _stage_target_count(s, 1, 8) == 4
    assert _stage_target_count(s, 2, 8) == 8


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
