"""验证统一检查入口传播子检查失败，并完成所有规定检查。"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ci_runner():
    spec = importlib.util.spec_from_file_location("ci_runner", ROOT / "scripts" / "check_ci.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_success_runs_all_required_checks_with_the_current_interpreter(ci_runner, monkeypatch, capsys):
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(ci_runner.subprocess, "run", run)

    assert ci_runner.main() == 0
    commands = [call.args[0] for call in run.call_args_list]
    assert commands == [
        [ci_runner.sys.executable, "-m", "pytest", "tests/architecture"],
        [ci_runner.sys.executable, "-m", "pytest", "tests/unit"],
        [ci_runner.sys.executable, "scripts/smoke.py"],
        [ci_runner.sys.executable, "scripts/check_docs.py", "--check"],
    ]
    assert all(call.kwargs["cwd"] == ROOT for call in run.call_args_list)
    assert "PASS: All CI checks passed." in capsys.readouterr().out


@pytest.mark.parametrize("failed_check", range(4))
def test_any_failed_check_fails_the_run_without_skipping_other_checks(ci_runner, monkeypatch, capsys, failed_check):
    statuses = [0, 0, 0, 0]
    statuses[failed_check] = 5
    run = Mock(side_effect=[subprocess.CompletedProcess([], status) for status in statuses])
    monkeypatch.setattr(ci_runner.subprocess, "run", run)

    assert ci_runner.main() == 1
    assert run.call_count == 4
    output = capsys.readouterr()
    assert "PASS: All CI checks passed." not in output.out
    assert f"FAIL: {ci_runner.CHECKS[failed_check][0]} (exit 5)" in output.err
