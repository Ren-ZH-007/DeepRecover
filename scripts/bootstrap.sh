#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bootstrap_python="${DEEPRECOVER_BUILD_PYTHON:-python3}"
"$bootstrap_python" -c 'import sys; assert sys.version_info[:2]==(3,12), "Python 3.12 is required"'
if [[ ! -x .venv/bin/python ]]; then "$bootstrap_python" -m venv .venv; fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
exec .venv/bin/python main.py
