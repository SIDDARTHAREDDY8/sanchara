"""Unit tests for the ROS backend interface (agent/ros_backend.py, agent/ros_sim.py).

Covers:
  * SimBackend implements the ROSBackend ABC with behavior identical to the
    original SimulatedROSStack (kept as a thin alias).
  * RclpyBackend raises a clear RuntimeError when rclpy is unavailable.
  * RclpyBackend's graph-vs-manifest logic (nodes, topic-rate tolerance,
    lifecycle states) runs against a fake rclpy module injected into
    sys.modules — no ROS installation needed.

Run:  .venv/bin/python tests/test_ros_backend.py
"""
import os
import sys
import time
import types
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "../agent"))

from ros_backend import ROSBackend, RclpyBackend  # noqa: E402
from ros_sim import MANIFESTS, RATE_TOLERANCE, SimBackend, SimulatedROSStack  # noqa: E402

_MISSING = object()


def _save_modules(*names):
    return {n: sys.modules.get(n, _MISSING) for n in names}


def _restore_modules(saved):
    for n, v in saved.items():
        if v is _MISSING:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = v


def _without_sleeps():
    """Disable time.sleep; returns a restore function."""
    orig = time.sleep
    time.sleep = lambda s: None  # noqa: E731
    return lambda: setattr(time, "sleep", orig)


# ---------------------------------------------------------------- SimBackend

def test_sim_implements_abc():
    b = SimBackend(version="1.0.0", seed=1)
    assert isinstance(b, ROSBackend)
    for m in ("get_nodes", "get_topic_rates", "get_lifecycle_state",
              "preflight_check", "apply_update", "health_check"):
        assert callable(getattr(b, m)), m
    assert b.version == "1.0.0"


def test_sim_alias_preserved():
    assert SimulatedROSStack is SimBackend
    b = SimulatedROSStack(version="1.0.0", seed=1)
    assert isinstance(b, ROSBackend)


def test_sim_abstract_cannot_instantiate():
    try:
        ROSBackend()
    except TypeError:
        pass
    else:
        raise AssertionError("ROSBackend should not be directly instantiable")


def test_sim_health_check_passes_on_good_boot():
    b = SimBackend(version="1.0.0", seed=7)
    ok, checks = b.health_check("1.0.0")
    assert ok, [c for c in checks if not c["ok"]]
    assert all({"name", "ok", "detail"} <= set(c) for c in checks)


def test_sim_health_check_fails_on_crash_node():
    restore = _without_sleeps()
    try:
        b = SimBackend(version="1.0.0", seed=7)
        b.apply_update("2.0.0", inject_failure="crash_node")
        assert b.version == "2.0.0"
        ok, checks = b.health_check("2.0.0")
        assert not ok
        bad = [c for c in checks if not c["ok"]]
        assert any(c["name"] == "node:nav2_planner" for c in bad), bad
    finally:
        restore()


def test_sim_health_check_fails_on_topic_stall():
    restore = _without_sleeps()
    try:
        b = SimBackend(version="1.0.0", seed=7)
        b.apply_update("2.0.0", inject_failure="topic_stall")
        ok, checks = b.health_check("2.0.0")
        assert not ok
        bad = [c for c in checks if not c["ok"]]
        assert any(c["name"] == "topic:/scan" for c in bad), bad
    finally:
        restore()


def test_sim_preflight_rejects_unknown_version():
    b = SimBackend(version="1.0.0", seed=7)
    ok, checks = b.preflight_check("9.9.9")
    assert not ok
    assert checks[0]["name"] == "manifest" and not checks[0]["ok"]


def test_sim_rate_tolerance_matches_manifest():
    # jittered rates must stay inside RATE_TOLERANCE for a healthy boot
    b = SimBackend(version="2.0.0", seed=123)
    for _ in range(20):
        rates = b.get_topic_rates()
        for topic, hz in MANIFESTS["2.0.0"]["topics"].items():
            assert abs(rates[topic] - hz) / hz <= RATE_TOLERANCE, (topic, rates[topic])


# ------------------------------------------------- RclpyBackend: no rclpy

def test_rclpy_backend_raises_clear_error_without_rclpy():
    saved = _save_modules("rclpy", "lifecycle_msgs", "lifecycle_msgs.srv")
    sys.modules["rclpy"] = None  # any `import rclpy` now raises ImportError
    try:
        try:
            RclpyBackend(version="1.0.0", manifests=MANIFESTS)
        except RuntimeError as e:
            assert "rclpy not available" in str(e), str(e)
        else:
            raise AssertionError("expected RuntimeError when rclpy is missing")
    finally:
        _restore_modules(saved)


# ------------------------------------------------- RclpyBackend: fake rclpy

class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FakeMsg:
    pass


class _FakeFuture:
    def __init__(self, label):
        self._label = label

    def done(self):
        return True

    def result(self):
        resp = types.SimpleNamespace()
        resp.current_state = types.SimpleNamespace(label=self._label, id=3)
        return resp


class _FakeLifecycleClient:
    def __init__(self, states, node_name):
        self._states = states
        self._node_name = node_name

    def wait_for_service(self, timeout_sec=None):
        return True

    def call_async(self, req):
        return _FakeFuture(self._states.get(self._node_name, "unknown"))


class _FakeNode:
    """Pretends to be an rclpy node. Pumps messages at the configured Hz on a
    virtual clock so topic-rate sampling is fully deterministic."""

    def __init__(self, node_names, topics, lifecycle):
        # topics: {topic: {"type": str, "hz": float}}
        self._node_names = list(node_names)
        self._topics = dict(topics)
        self._lifecycle = dict(lifecycle)
        self._subs = []

    def get_node_names(self):
        return list(self._node_names)

    def get_topic_names_and_types(self):
        return [(t, [info["type"]]) for t, info in self._topics.items()]

    def create_subscription(self, msg_cls, topic, cb, qos):
        sub = {"topic": topic, "cb": cb, "acc": 0.0}
        self._subs.append(sub)
        return sub

    def destroy_subscription(self, sub):
        self._subs.remove(sub)

    def create_client(self, srv_cls, srv_name):
        node_name = srv_name.rsplit("/", 1)[0]
        return _FakeLifecycleClient(self._lifecycle, node_name)

    def destroy_client(self, client):
        pass

    def _pump(self, dt=0.1):
        for sub in list(self._subs):
            hz = self._topics[sub["topic"]]["hz"]
            sub["acc"] += hz * dt
            n = int(sub["acc"])
            sub["acc"] -= n
            for _ in range(n):
                sub["cb"](_FakeMsg())


def _make_fake_rclpy(node, clock):
    mod = types.ModuleType("rclpy")
    mod.init = lambda *a, **k: None
    mod.shutdown = lambda *a, **k: None
    mod.ok = lambda: True
    mod.create_node = lambda name: node

    def spin_once(node, timeout_sec=0.1):
        node._pump()
        clock.advance(timeout_sec)

    mod.spin_once = spin_once
    mod.spin_until_future_complete = lambda node, fut, timeout_sec=None: None
    return mod


@contextmanager
def fake_ros(version, node_names=None, topics=None, lifecycle=None, **kw):
    """Build an RclpyBackend wired to a fake rclpy in sys.modules."""
    man = MANIFESTS[version]
    if node_names is None:
        node_names = list(man["nodes"])
    if topics is None:
        topics = {t: {"type": "fake_msgs/msg/FakeScan", "hz": hz}
                  for t, hz in man["topics"].items()}
    if lifecycle is None:
        lifecycle = dict(man["lifecycle_nodes"])
    saved = _save_modules("rclpy", "lifecycle_msgs", "lifecycle_msgs.srv",
                          "fake_msgs.msg")
    clock = _FakeClock()
    node = _FakeNode(node_names, topics, lifecycle)
    sys.modules["rclpy"] = _make_fake_rclpy(node, clock)

    lc = types.ModuleType("lifecycle_msgs")
    srv = types.ModuleType("lifecycle_msgs.srv")

    class GetState:
        class Request:
            pass

    srv.GetState = GetState
    lc.srv = srv
    sys.modules["lifecycle_msgs"] = lc
    sys.modules["lifecycle_msgs.srv"] = srv

    pkg = types.ModuleType("fake_msgs.msg")

    class FakeScan:
        pass

    pkg.FakeScan = FakeScan
    sys.modules["fake_msgs.msg"] = pkg
    try:
        b = RclpyBackend(version=version, manifests=MANIFESTS,
                         sample_time=3.0, **kw)
        b._clock = clock  # deterministic virtual clock
        yield b
    finally:
        _restore_modules(saved)


def test_rclpy_implements_abc():
    with fake_ros("1.0.0") as b:
        assert isinstance(b, ROSBackend)
        assert b.version == "1.0.0"


def test_rclpy_health_check_passes():
    with fake_ros("1.0.0") as b:
        ok, checks = b.health_check("1.0.0")
        assert ok, [c for c in checks if not c["ok"]]
        assert all({"name", "ok", "detail"} <= set(c) for c in checks)


def test_rclpy_health_check_fails_on_missing_node():
    man = MANIFESTS["1.0.0"]
    names = [n for n in man["nodes"] if n != "nav2_planner"]
    with fake_ros("1.0.0", node_names=names) as b:
        ok, checks = b.health_check("1.0.0")
        assert not ok
        bad = [c for c in checks if not c["ok"]]
        assert any(c["name"] == "node:nav2_planner" for c in bad), bad


def test_rclpy_health_check_fails_on_stalled_topic():
    man = MANIFESTS["1.0.0"]
    topics = {t: {"type": "fake_msgs/msg/FakeScan", "hz": (0.0 if t == "/scan" else hz)}
              for t, hz in man["topics"].items()}
    with fake_ros("1.0.0", topics=topics) as b:
        ok, checks = b.health_check("1.0.0")
        assert not ok
        bad = [c for c in checks if not c["ok"]]
        assert any(c["name"] == "topic:/scan" for c in bad), bad


def test_rclpy_health_check_fails_on_bad_rate():
    man = MANIFESTS["1.0.0"]
    topics = {t: {"type": "fake_msgs/msg/FakeScan", "hz": (1.0 if t == "/scan" else hz)}
              for t, hz in man["topics"].items()}
    with fake_ros("1.0.0", topics=topics) as b:
        ok, checks = b.health_check("1.0.0")
        assert not ok
        bad = [c for c in checks if not c["ok"]]
        assert any(c["name"] == "topic:/scan" for c in bad), bad


def test_rclpy_health_check_fails_on_lifecycle_mismatch():
    with fake_ros("1.0.0", lifecycle={"mission_executor": "inactive"}) as b:
        ok, checks = b.health_check("1.0.0")
        assert not ok
        bad = [c for c in checks if not c["ok"]]
        assert any(c["name"] == "lifecycle:mission_executor" for c in bad), bad


def test_rclpy_get_nodes_and_topic_rates():
    with fake_ros("1.0.0") as b:
        assert set(b.get_nodes()) == set(MANIFESTS["1.0.0"]["nodes"])
        rates = b.get_topic_rates()
        for topic, hz in MANIFESTS["1.0.0"]["topics"].items():
            assert abs(rates[topic] - hz) / hz <= RATE_TOLERANCE, (topic, rates[topic])
        assert b.get_lifecycle_state("mission_executor") == "active"
        assert b.get_lifecycle_state("nope") == "unknown"


def test_rclpy_preflight():
    with fake_ros("1.0.0") as b:
        ok, checks = b.preflight_check("2.0.0")
        assert ok, [c for c in checks if not c["ok"]]
        assert {c["name"] for c in checks} >= {"manifest", "rclpy", "disk"}
        ok, checks = b.preflight_check("9.9.9")
        assert not ok and not checks[0]["ok"]


def test_rclpy_apply_update_runs_hook_and_sets_version():
    with fake_ros("1.0.0", install_hook="echo installing {version}") as b:
        seen = []
        b.apply_update("2.0.0", progress_cb=seen.append)
        assert b.version == "2.0.0"
        assert seen  # progress callbacks fired


def test_rclpy_apply_update_rejects_unknown_version():
    with fake_ros("1.0.0") as b:
        try:
            b.apply_update("9.9.9")
        except RuntimeError as e:
            assert "no manifest" in str(e)
        else:
            raise AssertionError("expected RuntimeError for unknown version")


def test_rclpy_apply_update_surfaces_hook_failure():
    with fake_ros("1.0.0", install_hook="exit 3") as b:
        try:
            b.apply_update("2.0.0")
        except RuntimeError as e:
            assert "install hook failed" in str(e)
        else:
            raise AssertionError("expected RuntimeError for failing hook")
        assert b.version == "1.0.0"  # version untouched on failure


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
