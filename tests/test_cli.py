"""Smoke-test the real CLI entry point for every subcommand that needs no browser or network."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def root(tmp_path):
    for d in ("config", "resumes"):
        shutil.copytree(ROOT / d, tmp_path / d)
    return tmp_path


def run(root, *args):
    env = {**os.environ, "JOBPILOT_ROOT": str(root), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "jobpilot", *args], cwd=root, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("args", [["status"], ["triage"], ["plan"], ["report"], ["outreach", "status"], ["outreach", "followups"],
                                  ["review"], ["--help"], ["outreach", "--help"]])
def test_cli_commands_run(root, args):
    r = run(root, *args)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "Traceback" not in r.stderr


def test_cli_status_json(root):
    out = json.loads(run(root, "status").stdout)
    assert out["mode"] == "shadow" and out["applications"] == {}
