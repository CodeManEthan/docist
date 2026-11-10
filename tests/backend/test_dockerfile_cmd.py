"""The container's start command: gunicorn must be PID 1 so a stop's SIGTERM
reaches it, and Railway must give it time to shut down gracefully.

A shell-form CMD runs as `/bin/sh -c "..."` with sh as PID 1, which ignores
SIGTERM, so every stop waits out the timeout and ends in SIGKILL.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def last_cmd():
    lines = (ROOT / 'Dockerfile').read_text().splitlines()
    cmds = [line for line in lines if line.startswith('CMD')]
    assert cmds, 'Dockerfile has no CMD'
    return cmds[-1][len('CMD'):].strip()


def test_cmd_is_exec_form_and_execs_gunicorn():
    cmd = json.loads(last_cmd())  # shell form is not JSON and fails here
    assert isinstance(cmd, list)
    assert cmd[:2] == ['sh', '-c']
    script = cmd[2]
    assert script.startswith('exec gunicorn ')
    assert '--bind "0.0.0.0:${PORT}"' in script
    assert '--preload' in script
    assert script.endswith(' app:app')


def test_railway_draining_outlasts_graceful_timeout():
    config = json.loads((ROOT / 'railway.json').read_text())
    assert config['deploy']['drainingSeconds'] == 35
