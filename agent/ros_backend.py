"""ROS backend interface + real ROS 2 implementation for the Sanchara agent.

The agent programs against the small ``ROSBackend`` interface defined here:

  * observe the live ROS graph: ``get_nodes`` / ``get_topic_rates`` /
    ``get_lifecycle_state``
  * gate an update before touching anything: ``preflight_check``
  * perform the update: ``apply_update``
  * verify the update landed healthy: ``health_check``

Two implementations exist:

  * ``SimBackend`` (in ``ros_sim.py``) — in-process simulation of the ROS 2
    graph; needs no ROS installation. Used by the demo and the default agent
    path (``--ros-backend sim``).
  * ``RclpyBackend`` (below) — talks to a real ROS 2 system through ``rclpy``
    (``--ros-backend rclpy``).

``rclpy`` is imported lazily (inside ``RclpyBackend.__init__``) so merely
importing this module never fails on machines without ROS 2 installed.
"""
import abc
import importlib
import os
import shutil
import subprocess
import time


def _within_tolerance(actual, expected, tol):
    """True if a measured value is within ``tol`` fraction of ``expected``."""
    if expected:
        return abs(actual - expected) / expected <= tol
    return actual == 0


class ROSBackend(abc.ABC):
    """Abstract ROS 2 backend: the contract ``agent.py`` programs against.

    Implementations must be cheap to construct and must never raise at import
    time (``rclpy`` may not be installed). All ``*_check`` methods return
    ``(ok, checks)`` where ``checks`` is a list of
    ``{"name": str, "ok": bool, "detail": str}`` dicts — the agent logs the
    details and reports ``failed`` to the control plane when ``ok`` is False.
    """

    @property
    @abc.abstractmethod
    def version(self):
        """Currently installed software version (str). Set by ``apply_update``."""

    @version.setter
    @abc.abstractmethod
    def version(self, value):
        """Update the recorded installed version after a successful apply."""

    @abc.abstractmethod
    def get_nodes(self):
        """Return the names of currently alive ROS nodes (list of str).

        Mirrors ``rclpy``'s ``node.get_node_names()`` / ``ros2 node list``.
        """

    @abc.abstractmethod
    def get_topic_rates(self):
        """Return measured publish rates ``{topic: hz}`` for the topics in the
        current version's manifest.

        Topics that are not advertised, or that cannot be sampled, report
        ``0.0`` — the health check treats that as a failure against any
        positive expected rate.
        """

    @abc.abstractmethod
    def get_lifecycle_state(self, node):
        """Return the lowercase lifecycle state of ``node``
        (``"active"``, ``"inactive"``, …) or ``"unknown"`` when the state
        cannot be determined. Non-lifecycle nodes report ``"unknown"``.
        """

    @abc.abstractmethod
    def preflight_check(self, target_version):
        """ROS-side preflight before touching anything.

        Returns ``(ok, checks)``. When ``ok`` is False the agent aborts the
        update and reports ``failed`` without changing the robot.
        """

    @abc.abstractmethod
    def apply_update(self, target_version, inject_failure=None, progress_cb=None):
        """Apply the update to ``target_version`` and restart the ROS stack.

        On success ``self.version == target_version``. Raises ``RuntimeError``
        (with a human-readable message) when the update cannot be applied —
        the agent catches it and reports ``failed``.

        ``inject_failure`` is a sim-only hint (``"crash_node"`` /
        ``"topic_stall"``); real backends ignore it. ``progress_cb``, when
        given, is called with short step-name strings (``"installing
        packages"``, …) for the agent's log.
        """

    @abc.abstractmethod
    def health_check(self, expected_version):
        """Verify the live ROS graph matches the version manifest.

        Returns ``(ok, checks)``. When ``ok`` is False the agent reports
        ``failed``, which is what triggers the control plane's auto-rollback.
        """


class RclpyBackend(ROSBackend):
    """Real ROS 2 backend, driven by ``rclpy``.

    ``manifests`` maps version strings to manifests of the form::

        {"nodes": [...], "topics": {topic: hz, ...},
         "lifecycle_nodes": {node: "active", ...}}

    (same shape as ``ros_sim.MANIFESTS``).

    Update mechanics are deliberately simple and explicit:

    * ``install_hook`` — optional shell command run to install the release,
      formatted with ``{version}`` (e.g.
      ``"sudo apt-get install -y sanchara-robot={version}"``). When unset, the
      install step is skipped and only the restart hook runs.
    * ``respawn_cmd`` — optional shell command that restarts the robot's ROS
      stack (e.g. re-running its launch description).

    Real package-install mechanics (apt/dpkg vs. containers, artifact
    download, signature verification, atomic switch + verified relaunch) are
    intentionally left for later — this class proves the interface against
    live ROS 2, it is not a production installer.
    """

    def __init__(self, version="1.0.0", manifests=None, install_hook=None,
                 respawn_cmd=None, sample_time=1.0, rate_tolerance=0.35,
                 min_disk_gb=2.0, srv_timeout=5.0):
        try:
            import rclpy
        except ImportError as e:
            raise RuntimeError(
                "rclpy not available; install ROS 2 (e.g. Humble), source its "
                "setup.bash, and rerun — or use --ros-backend sim"
            ) from e
        self._rclpy = rclpy
        self._clock = time.monotonic  # replaceable in tests for determinism
        self._version = version
        self.manifests = manifests or {}
        self.install_hook = install_hook
        self.respawn_cmd = respawn_cmd
        self.sample_time = sample_time
        self.rate_tolerance = rate_tolerance
        self.min_disk_gb = min_disk_gb
        self.srv_timeout = srv_timeout
        if not rclpy.ok():
            rclpy.init(args=[])
        # pid suffix avoids node-name clashes when several agents run on one host
        self._node = rclpy.create_node(f"sanchara_agent_{os.getpid()}")

    # -- ROSBackend interface --
    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, value):
        self._version = value

    def get_nodes(self):
        return list(self._node.get_node_names())

    def get_topic_rates(self):
        manifest = self.manifests.get(self._version, {})
        return self._sample_rates(manifest)

    def get_lifecycle_state(self, node):
        try:
            from lifecycle_msgs.srv import GetState
        except ImportError:
            return "unknown"
        client = self._node.create_client(GetState, f"{node}/get_state")
        try:
            if not client.wait_for_service(timeout_sec=self.srv_timeout):
                return "unknown"
            fut = client.call_async(GetState.Request())
            self._rclpy.spin_until_future_complete(
                self._node, fut, timeout_sec=self.srv_timeout)
            if not fut.done():
                return "unknown"
            return str(fut.result().current_state.label).lower()
        except Exception:
            return "unknown"
        finally:
            try:
                self._node.destroy_client(client)
            except Exception:
                pass

    def preflight_check(self, target_version):
        checks = []
        if target_version not in self.manifests:
            return False, [{"name": "manifest", "ok": False,
                            "detail": f"no manifest for {target_version}"}]
        checks.append({"name": "manifest", "ok": True,
                       "detail": f"manifest for {target_version} present"})
        try:
            import rclpy  # noqa: F401
            checks.append({"name": "rclpy", "ok": True,
                           "detail": "rclpy importable"})
        except ImportError:
            checks.append({"name": "rclpy", "ok": False,
                           "detail": "rclpy not importable"})
            return False, checks
        free_gb = shutil.disk_usage("/").free / 1e9
        ok_disk = free_gb > self.min_disk_gb
        checks.append({"name": "disk", "ok": ok_disk,
                       "detail": f"{free_gb:.1f} GB free "
                                 f"(need >{self.min_disk_gb:g} GB)"})
        return ok_disk, checks

    def apply_update(self, target_version, inject_failure=None, progress_cb=None):
        self._require_manifest(target_version)
        # inject_failure is a sim-only chaos hint; a real backend ignores it.
        if progress_cb:
            progress_cb("installing packages")
        if self.install_hook:
            cmd = self.install_hook.format(version=target_version)
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(
                    f"install hook failed (rc={r.returncode}): "
                    f"{(r.stderr or r.stdout)[-500:]}")
        elif progress_cb:
            progress_cb("skipping package install (no install_hook configured)")
        if progress_cb:
            progress_cb("restarting ROS stack")
        if self.respawn_cmd:
            r = subprocess.run(self.respawn_cmd, shell=True, capture_output=True,
                               text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(
                    f"respawn command failed (rc={r.returncode}): "
                    f"{(r.stderr or r.stdout)[-500:]}")
        # NOTE: a production backend would relaunch the robot's bringup here
        # and block until the graph recovers; this backend records the version
        # and lets health_check verify the live graph.
        self._version = target_version

    def health_check(self, expected_version):
        manifest = self._require_manifest(expected_version)
        checks = []
        ok = True

        live_nodes = set(self.get_nodes())
        for node in manifest.get("nodes", []):
            alive = node in live_nodes
            checks.append({"name": f"node:{node}", "ok": alive,
                           "detail": "active" if alive else "not in ROS graph"})
            ok = ok and alive

        rates = self._sample_rates(manifest)
        for topic, hz in manifest.get("topics", {}).items():
            actual = rates.get(topic, 0.0)
            good = _within_tolerance(actual, hz, self.rate_tolerance)
            checks.append({"name": f"topic:{topic}", "ok": good,
                           "detail": f"{actual:.1f} Hz (expected {hz:.1f})"})
            ok = ok and good

        for node, want in manifest.get("lifecycle_nodes", {}).items():
            got = self.get_lifecycle_state(node)
            good = got == want
            checks.append({"name": f"lifecycle:{node}", "ok": good,
                           "detail": f"{got} (expected {want})"})
            ok = ok and good

        return ok, checks

    def close(self):
        """Tear down the rclpy node. The long-lived agent never calls this;
        it exists for tests and clean shutdowns."""
        try:
            self._node.destroy_node()
        except Exception:
            pass
        try:
            self._rclpy.shutdown()
        except Exception:
            pass

    # -- internals --
    def _require_manifest(self, version):
        try:
            return self.manifests[version]
        except KeyError:
            raise RuntimeError(f"no manifest for version {version}")

    def _sample_rates(self, manifest):
        """Sample {topic: hz} for the topics in ``manifest``."""
        expected = manifest.get("topics", {})
        live = dict(self._node.get_topic_names_and_types())
        rates = {}
        for topic in expected:
            types = live.get(topic)
            if not types:
                rates[topic] = 0.0
            else:
                rates[topic] = self._sample_topic_rate(
                    topic, types[0], self.sample_time)
        return rates

    @staticmethod
    def _resolve_msg_class(type_str):
        """``"pkg/msg/Name"`` -> message class, resolved via importlib."""
        parts = type_str.split("/")
        if len(parts) != 3 or parts[1] != "msg":
            raise RuntimeError(f"cannot resolve ROS message type {type_str!r}")
        try:
            mod = importlib.import_module(".".join(parts[:2]))
            return getattr(mod, parts[2])
        except (ImportError, AttributeError) as e:
            raise RuntimeError(
                f"cannot resolve ROS message type {type_str!r}: {e}")

    def _sample_topic_rate(self, topic, type_str, duration):
        """Subscribe to ``topic`` for ``duration`` seconds and measure Hz.

        Timeout-bounded: never blocks longer than ``duration``. Returns 0.0
        when the type cannot be resolved or no messages arrive.
        """
        try:
            msg_cls = self._resolve_msg_class(type_str)
        except RuntimeError:
            return 0.0
        clock = self._clock
        stamps = []
        sub = self._node.create_subscription(
            msg_cls, topic, lambda _m: stamps.append(clock()), 10)
        try:
            end = clock() + duration
            # 1e-9 epsilon keeps the loop count exact on coarse clocks
            while clock() < end - 1e-9:
                self._rclpy.spin_once(
                    self._node,
                    timeout_sec=min(0.1, max(1e-3, end - clock())))
        finally:
            self._node.destroy_subscription(sub)
        if len(stamps) < 2:
            return 0.0
        return (len(stamps) - 1) / (stamps[-1] - stamps[0])
