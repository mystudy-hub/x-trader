"""用独立 Git 仓库验证 hook 触发、暂存区隔离与失败阻断。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installer():
    spec = importlib.util.spec_from_file_location("hook_installer", ROOT / "scripts/install_hooks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args, check=True):
    env = os.environ.copy()
    env["QH_TRADER_PYTHON"] = sys.executable
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--quiet", "-b", "main")
    for relative in (".githooks/pre-commit", ".gitattributes", ".gitignore", "scripts/pre_commit.py"):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    (tmp_path / "scripts/check_ci.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.exit(int(Path('status.txt').read_text()))\n",
        encoding="utf-8",
    )
    (tmp_path / "status.txt").write_text("0", encoding="utf-8")
    git(tmp_path, "add", "--all")
    git(tmp_path, "update-index", "--chmod=+x", ".githooks/pre-commit")
    return tmp_path


def commit(repo):
    return git(
        repo,
        "-c",
        "user.name=Day0 Tests",
        "-c",
        "user.email=day0@example.invalid",
        "commit",
        "-m",
        "test: validate commit hook",
        check=False,
    )


def test_hook_installs_idempotently_and_is_triggered_by_git(repository, installer):
    installer.install_hooks(repository)
    installer.install_hooks(repository)
    assert installer.hooks_are_installed(repository)
    result = commit(repository)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Checking staged snapshot" in result.stdout + result.stderr


def test_unstaged_fix_does_not_hide_a_staged_failure(repository, installer):
    installer.install_hooks(repository)
    (repository / "status.txt").write_text("5", encoding="utf-8")
    git(repository, "add", "status.txt")
    (repository / "status.txt").write_text("0", encoding="utf-8")
    result = commit(repository)
    assert result.returncode != 0
    assert git(repository, "show", ":status.txt").stdout == "5"
    assert (repository / "status.txt").read_text() == "0"
    assert git(repository, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    git(repository, "add", "status.txt")
    assert commit(repository).returncode == 0


def test_unstaged_failure_does_not_replace_the_content_being_committed(repository, installer):
    installer.install_hooks(repository)
    (repository / "status.txt").write_text("5", encoding="utf-8")
    result = commit(repository)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repository, "show", "HEAD:status.txt").stdout == "0"
    assert (repository / "status.txt").read_text() == "5"


@pytest.mark.parametrize("filename", (".env", "config/settings.yaml", "config/rule_sources/rules.db"))
def test_force_staged_runtime_or_secret_paths_are_blocked(repository, installer, filename):
    installer.install_hooks(repository)
    path = repository / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("dummy test content", encoding="utf-8")
    git(repository, "add", "--force", "--", filename)
    result = commit(repository)
    assert result.returncode != 0
    assert "match ignore rules" in result.stderr
    assert filename in result.stderr


def test_installer_preserves_an_existing_hooks_path(repository, installer):
    git(repository, "config", "core.hooksPath", ".existing-hooks")
    with pytest.raises(RuntimeError, match="preserved"):
        installer.install_hooks(repository)
    assert installer.configured_hooks(repository) == ".existing-hooks"


def test_installer_preserves_existing_default_hooks(repository, installer):
    (repository / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="preserved"):
        installer.install_hooks(repository)
    assert installer.configured_hooks(repository) is None
