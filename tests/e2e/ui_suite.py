"""Run the UI drivers in sequence against an already running port-8091 server.

Requires Chromium, Playwright and a running worker. Creates configurations,
procedures, a pipeline run and checks (including a deliberate technical error).
Evidence is written to /tmp/ui-verify. The final driver fails on the known
checks-only revision regression until the service owner fixes it.
"""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
for name in ('probe', 'inspect', 'interact', 'create', 'workspace', 'acceptance', 'final'):
    print(f'Running ui_{name}.py', flush=True)
    subprocess.run([sys.executable, '-u', str(root / f'ui_{name}.py')], check=True)
