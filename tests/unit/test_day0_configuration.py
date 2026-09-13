"""验证 Day 0 模板的执行语义与运行文件隔离。"""

from __future__ import annotations

import copy
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_checks", ROOT / "scripts/smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def template():
    return yaml.safe_load((ROOT / "config/settings.yaml.example").read_text(encoding="utf-8"))


def test_template_declarations_are_valid(smoke, template):
    smoke.validate_config_template(template)


@pytest.mark.parametrize("policy", ("THIS_CLOSE", "TWAP", "VWAP", "NEXT_OPEN"))
def test_unsupported_execution_choices_fail(smoke, template, policy):
    template["strategy"]["execution_strategy"] = policy
    with pytest.raises(ValueError, match="FR-EXEC-02"):
        smoke.validate_config_template(template)


@pytest.mark.parametrize(
    "policy", ("NEXT_SESSION_OPEN", "NEXT_DAY_SESSION_OPEN", "NEXT_DAY_FIXED_TIME", "NEXT_BAR_OPEN")
)
def test_all_declared_execution_choices_are_supported(smoke, template, policy):
    template["strategy"]["execution_strategy"] = policy
    template["strategy"]["fixed_time_before_close_minutes"] = 5
    smoke.validate_config_template(template)


@pytest.mark.parametrize("offset", (None, 0, -1, True, float("nan"), float("inf")))
def test_fixed_time_requires_a_usable_offset(smoke, template, offset):
    template["strategy"]["execution_strategy"] = "NEXT_DAY_FIXED_TIME"
    template["strategy"]["fixed_time_before_close_minutes"] = offset
    with pytest.raises(ValueError, match="分钟偏移"):
        smoke.validate_config_template(template)


def test_strategy_style_does_not_replace_the_capability_category(smoke, template):
    template["strategy"]["category"] = "trend_following"
    with pytest.raises(ValueError, match="能力类别"):
        smoke.validate_config_template(template)


@pytest.mark.parametrize("path", ("config/rule_sources/rules.db", "../rules.db", "data_storage/../rules.db"))
def test_runtime_database_cannot_leave_the_ignored_data_directory(smoke, template, path):
    template["storage"]["rules_db_path"] = path
    with pytest.raises(ValueError, match="运行数据目录"):
        smoke.validate_config_template(template)


def test_invalid_yaml_fails_the_smoke_check(smoke, tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml.example").write_text("system: [broken", encoding="utf-8")
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    assert smoke.check_config_template() is False


def test_runtime_files_are_ignored_but_templates_and_specs_remain_trackable(tmp_path, template):
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    protected = [
        "config/settings.yaml", ".env", "config/account.credentials.yaml", "config/secrets.local.yaml",
        "runs/result.json", "data_storage/raw/bars.parquet", "live/trading.db",
        "config/rule_sources/rules.db", "terminal_payload.json", "terminal_system_info.bin",
        "terminal_payloads/capture.txt",
    ]
    for path in template["storage"].values():
        protected.extend((path, path + "-wal", path + "-shm", path + "-journal"))
    for relative in protected:
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", relative], cwd=tmp_path)
        assert result.returncode == 0, relative
    for relative in ("config/settings.yaml.example", "config/rule_sources/README.md", "tests/fixtures/example.json"):
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", relative], cwd=tmp_path)
        assert result.returncode == 1, relative


def test_nonfinite_capital_is_rejected(smoke, template):
    invalid = copy.deepcopy(template)
    invalid["risk"]["initial_capital"] = "NaN"
    with pytest.raises(ValueError, match="有限金额"):
        smoke.validate_config_template(invalid)
