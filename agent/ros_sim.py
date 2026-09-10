"""Simulated ROS 2 stack for the Sanchara agent.

Stands in for a real ROS 2 installation (rclpy node graph, topics, lifecycle).
The agent treats this exactly like it would treat the real ROS APIs: it asks
'what nodes are alive?', 'what is each topic's publish rate?', 'are lifecycle
nodes in the right state?' — and gates the update on the answers.

Failure injection simulates a bad release (e.g. a node that crashes on boot
after the update), which is what the post-update health check must catch.

``SimBackend`` implements the ``ROSBackend`` interface from ``ros_backend.py``;
``SimulatedROSStack`` is kept as a thin alias for backward compatibility.
"""
import random
import time

from ros_backend import ROSBackend, _within_tolerance

# Expected ROS graph per software version: the "version manifest".
# A real implementation would derive this from the release artifact
# (package list + launch description + expected topic rates).
MANIFESTS = {
    "1.0.0": {
        "nodes": ["lidar_driver", "nav2_planner", "mission_executor", "battery_monitor"],
        "topics": {"/scan": 10.0, "/cmd_vel": 20.0, "/odom": 30.0, "/battery_state": 1.0},
        "lifecycle_nodes": {"mission_executor": "active"},
    },
    "2.0.0": {
        "nodes": ["lidar_driver", "nav2_planner", "mission_executor", "battery_monitor",
                  "perception_v2"],
        "topics": {"/scan": 15.0, "/cmd_vel": 20.0, "/odom": 30.0, "/battery_state": 1.0,
                   "/detections": 5.0},
        "lifecycle_nodes": {"mission_executor": "active"},
    },
}

RATE_TOLERANCE = 0.35  # topic hz may deviate this fraction and still pass


class SimBackend(ROSBackend):
    def __init__(self, version="1.0.0", seed=None):
        self._version = version
        self.rng = random.Random(seed)
        self._boot()

    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, value):
        self._version = value

    def _boot(self):
        manifest = MANIFESTS[self.version]
        self.nodes = {n: "active" for n in manifest["nodes"]}
        self.lifecycle = dict(manifest["lifecycle_nodes"])
        self.topics = dict(manifest["topics"])

    # -- what the agent observes (mirrors rclpy / ros2cli queries) --
    def get_nodes(self):
        return [n for n, s in self.nodes.items() if s == "active"]

    def get_node_names(self):
        # backward-compat alias for the pre-interface name
        return self.get_nodes()

    def get_topic_rates(self):
        return {t: hz * self.rng.uniform(0.92, 1.08) for t, hz in self.topics.items()}

    def get_lifecycle_state(self, node):
        return self.lifecycle.get(node, "unknown")

    # -- update application --
    def apply_update(self, target_version, inject_failure=None, progress_cb=None):
        """Simulate download/extract/install/restart. Returns after 'reboot'."""
        steps = [("stopping nodes", 0.6), ("installing packages", 1.2),
                 ("restarting ROS stack", 0.8)]
        for name, dur in steps:
            if progress_cb:
                progress_cb(name)
            time.sleep(dur)
        if target_version not in MANIFESTS:
            raise RuntimeError(f"unknown target version {target_version}")
        self.version = target_version
        self._boot()
        if inject_failure == "crash_node":
            # bad release: a required node crashes on boot
            victim = "nav2_planner"
            self.nodes[victim] = "crashed"
        elif inject_failure == "topic_stall":
            self.topics["/scan"] = 0.0

    # -- ROS-aware health verification --
    def health_check(self, expected_version):
        """Verify the live graph matches the version manifest."""
        manifest = MANIFESTS[expected_version]
        checks = []
        ok = True

        for node in manifest["nodes"]:
            alive = self.nodes.get(node) == "active"
            checks.append({"name": f"node:{node}", "ok": alive,
                           "detail": "active" if alive else f"state={self.nodes.get(node)}"})
            ok = ok and alive

        rates = self.get_topic_rates()
        for topic, hz in manifest["topics"].items():
            actual = rates.get(topic, 0.0)
            good = _within_tolerance(actual, hz, RATE_TOLERANCE)
            checks.append({"name": f"topic:{topic}", "ok": good,
                           "detail": f"{actual:.1f} Hz (expected {hz:.1f})"})
            ok = ok and good

        for node, want in manifest["lifecycle_nodes"].items():
            got = self.get_lifecycle_state(node)
            good = got == want
            checks.append({"name": f"lifecycle:{node}", "ok": good,
                           "detail": f"{got} (expected {want})"})
            ok = ok and good

        return ok, checks

    def preflight_check(self, target_version):
        """ROS-side preflight before touching anything."""
        checks = []
        if target_version not in MANIFESTS:
            return False, [{"name": "manifest", "ok": False,
                            "detail": f"no manifest for {target_version}"}]
        checks.append({"name": "manifest", "ok": True,
                       "detail": f"manifest for {target_version} present"})
        # simulated disk-space check
        free_gb = self.rng.uniform(4, 12)
        ok_disk = free_gb > 2.0
        checks.append({"name": "disk", "ok": ok_disk, "detail": f"{free_gb:.1f} GB free"})
        return ok_disk, checks


# Backward compatibility: the original class name.
SimulatedROSStack = SimBackend
