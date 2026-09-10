#!/usr/bin/env bash
# Sanchara full test suite: unit tests -> e2e (real server) -> demo.
# Run from the repo root:  bash scripts/test_all.sh
set -euo pipefail

cd "$(dirname "$0")/.."

banner() {
    echo
    echo "================================================================"
    echo " $1"
    echo "================================================================"
}

banner "tests/test_gates.py"
.venv/bin/python tests/test_gates.py

banner "tests/test_ros_backend.py"
.venv/bin/python tests/test_ros_backend.py

banner "tests/test_security.py"
.venv/bin/python tests/test_security.py

banner "tests/test_e2e.py"
.venv/bin/python tests/test_e2e.py

banner "demo/run_demo.py"
.venv/bin/python demo/run_demo.py

echo
echo "ALL GREEN"
