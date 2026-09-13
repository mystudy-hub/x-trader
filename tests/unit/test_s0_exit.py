"""S0 出口检查的真实文件与反例回归；测试证据只存在于临时目录。"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location("s0_checks", ROOT / "scripts/check_s0_exit.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def ready_case(tmp_path, module):
    def write(relative, data):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        elif path.suffix == ".json":
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return {"path": relative, "sha256": module.sha256(path)}

    registry = json.loads((ROOT / "docs/requirements.json").read_bytes())
    for relative in (
        "docs/requirements.json",
        registry["baseline"]["source_file"],
        "tests/fixtures/ledger_examples.json",
        "config/gaps.yaml",
        "config/a25_applicability.yaml",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    config = yaml.safe_load((ROOT / "config/settings.yaml.example").read_text(encoding="utf-8"))
    config["broker"]["profile"] = "simnow_v6"
    write("config/settings.yaml", config)
    witness = write("evidence/review.txt", "Independent verification record used only by this unit test.\n")
    verified = {
        "verification_status": "已核验",
        "verified_by": "unit-test reviewer",
        "verified_at": "2026-09-13",
        "evidence": witness,
    }
    gap = {"verification_status": "登记缺口", "gap_ids": ["GAP-S0-05"], "value": None}
    profile = {
        "schema_version": 1,
        "profile_name": "simnow_v6",
        "ctp_version": None,
        "effective_from": None,
        "gap_ids": ["GAP-S0-01"],
        "scope": {"contracts": ["SHFE.rb2410"]},
        "capabilities": {name: dict(gap) for name in module.CAPABILITIES},
    }
    write("config/broker_profiles/simnow_v6.yaml", profile)
    period = {"start_date": "2024-09-09", "end_date": "2024-09-11"}
    rules = {}
    for kind in module.REQUIRED_RULE_TYPES:
        source = write(f"evidence/{kind}.txt", "Synthetic source for validator unit tests.\n")
        rule = {
            "schema_version": 1,
            "rule_id": kind,
            "rule_type": kind,
            "source_url": "https://example.invalid/source",
            "source_document": source,
            "effective_basis": "timestamp",
            "effective_from": "2024-01-01T00:00:00+08:00",
            "effective_to": None,
            "published_at": "2023-12-01T00:00:00+08:00",
            "known_at": "2023-12-01T00:00:00+08:00",
            "applies_to": {"exchanges": ["SHFE"], "products": ["rb"], "contracts": []},
            **verified,
        }
        relative = f"config/rule_sources/exchanges/SHFE/{kind}.yaml"
        rules[relative] = write(relative, rule)["sha256"]
    artifacts = {
        role: write(f"data_storage/sample/{role}.txt", f"unit-test {role}\n") for role in module.REQUIRED_SAMPLE_ROLES
    }
    acceptance = write(
        "evidence/sample.json",
        {
            "schema_version": 1,
            "contract": "SHFE.rb2410",
            "time_range": period,
            "checks": dict.fromkeys(
                ("fields_complete", "execution_price_covered", "time_range_covered", "rules_covered"), True
            ),
            "artifact_hashes": {role: ref["sha256"] for role, ref in artifacts.items()},
            "rule_source_hashes": rules,
        },
    )
    dataset = {
        "dataset_id": "hourly",
        "period": "1h",
        "time_range": period,
        "available_fields": ["open", "high", "low", "close", "volume"],
        "update_delay": "after bar close",
        "historical_revision": False,
        "license": {"scope": "synthetic tests", "local_storage": True},
        "open_semantic": "next bar open",
        "auction_inclusion": False,
        **verified,
    }
    coverage = {
        "schema_version": 1,
        "data_sources": [{"source_id": "unit", "source_name": "Unit fixture source", "datasets": [dataset]}],
        "execution_price_coverage": [
            {
                "strategy": "NEXT_BAR_OPEN",
                "data_granularity": "1h",
                "data_source": {"source_id": "unit", "dataset_id": "hourly"},
                **verified,
            }
        ],
        "engineering_sample": {
            "contract": "SHFE.rb2410",
            "time_range": period,
            "artifacts": artifacts,
            **verified,
            "evidence": acceptance,
        },
        "research_dataset": {
            "requirements": {"min_years": 8, "min_products": 20, "cycles": "two full cycles"},
            "status": "采购中",
            "procurement_started": "2026-09-13",
            "data_source": "Unit fixture source",
            "procurement_evidence": witness,
        },
    }
    write("config/data_coverage.yaml", coverage)
    inputs = {
        name: write(name, (ROOT / name).read_text(encoding="utf-8"))
        for name in ("pyproject.toml", "uv.lock", ".python-version")
    }
    locked_versions = {
        package["name"]: package["version"]
        for package in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    }
    environment = {
        "schema_version": 1,
        "kind": "s0_environment",
        "generated_at": "2026-09-13T12:00:00Z",
        "inputs": inputs,
        "python": {"supported": True, "version": "3.12.11"},
        "required_dependencies_ok": True,
        "dependencies": {
            name: {"status": "OK", "version": locked_versions[name]}
            for name in ("polars", "pandas", "pyarrow", "numpy", "pydantic", "pyyaml", "structlog", "duckdb")
        },
        "sqlite": {
            "passed": True,
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "busy_timeout": 5000,
            "foreign_key_enforced": True,
        },
        "ctp_candidates": {"openctp_ctp": {"status": "NOT_INSTALLED"}},
        "sdk_archives": [{**witness, "status": "INVENTORIED"}],
    }
    write("runs/s0/environment.json", environment)
    rows = []
    for section in (2, 3):
        for number in range(1, 13):
            status = "登记缺口" if section == 3 or number in (1, 3, 4) else "已核验"
            evidence = "GAP-S0-01" if status == "登记缺口" else "unit-test verification record"
            rows.append(f"| {section}.{number} | item | requirement | {status} | {evidence} | 2026-09-13 | tester |")
    write("docs/09_前期准备与规则核验清单.md", "\n".join(rows))
    return module.S0Checker(tmp_path), write, coverage, config, environment


def test_complete_evidence_can_pass_without_treating_ctp_gaps_as_verified(ready_case):
    checker, *_ = ready_case
    results = checker.run()
    assert all(result.status in {"verified", "gap"} for result in results), results
    assert next(result for result in results if result.check_id == "9.4").status == "gap"


@pytest.mark.parametrize("field", ("category", "signal_frequency", "execution_strategy", "data_granularity"))
def test_invalid_strategy_fields_do_not_pass(ready_case, field):
    checker, write, _, config, _ = ready_case
    config["strategy"][field] = None
    write("config/settings.yaml", config)
    assert checker.run()[0].status == "invalid"


def test_example_is_not_an_actual_configuration(ready_case):
    checker, write, _, config, _ = ready_case
    write("config/settings.yaml.example", config)
    checker.config_path = "config/settings.yaml.example"
    assert checker.run()[0].status == "invalid"


def test_empty_sources_and_unstarted_procurement_remain_pending(ready_case):
    checker, write, coverage, _, _ = ready_case
    coverage["data_sources"] = []
    coverage["research_dataset"]["status"] = "未启动"
    write("config/data_coverage.yaml", coverage)
    results = {result.check_id: result for result in checker.run()}
    assert results["9.3"].status == "pending"
    assert results["9.9"].status == "pending"


def test_environment_rechecks_reported_versions_instead_of_trusting_ok_label(ready_case):
    checker, write, _, _, environment = ready_case
    environment["dependencies"]["pyarrow"]["version"] = "unverified version"
    write("runs/s0/environment.json", environment)
    with pytest.raises(ValueError, match="依赖版本与锁文件不一致"):
        checker.environment()


@pytest.mark.parametrize("contract", ("SHFE.rb2410", "SHFE.rb2501", "FAKE.NO_DATA"))
def test_a_contract_name_cannot_replace_sample_files(ready_case, contract):
    checker, write, coverage, config, _ = ready_case
    config["data"]["engineering_sample"]["contract"] = contract
    coverage["engineering_sample"]["contract"] = contract
    coverage["engineering_sample"]["artifacts"] = {}
    write("config/settings.yaml", config)
    write("config/data_coverage.yaml", coverage)
    with pytest.raises(ValueError, match="文件类型"):
        checker.engineering_sample()


def test_changed_sample_bytes_invalidate_the_evidence(ready_case):
    checker, _, coverage, _, _ = ready_case
    path = checker.path(coverage["engineering_sample"]["artifacts"]["bars"]["path"])
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="哈希不符"):
        checker.engineering_sample()


def test_sample_report_must_bind_rule_versions(ready_case):
    checker, write, coverage, _, _ = ready_case
    reference = coverage["engineering_sample"]["evidence"]
    report = checker.load(reference["path"])
    report["rule_source_hashes"] = {}
    coverage["engineering_sample"]["evidence"] = write(reference["path"], report)
    write("config/data_coverage.yaml", coverage)
    with pytest.raises(ValueError, match="规则来源"):
        checker.engineering_sample()


def test_example_rules_cannot_satisfy_rule_registration(ready_case):
    checker, write, _, _, _ = ready_case
    for path in (checker.root / "config/rule_sources/exchanges").rglob("*.yaml"):
        rule = checker.load(path)
        rule["is_example"] = True
        write(path.relative_to(checker.root).as_posix(), rule)
    with pytest.raises(ValueError, match="尚缺"):
        checker.rules()


def test_duplicate_a25_ids_are_rejected(ready_case):
    checker, write, _, _, _ = ready_case
    applicability = checker.load("config/a25_applicability.yaml")
    applicability["cases"][0]["id"] = "A25-02"
    write("config/a25_applicability.yaml", applicability)
    with pytest.raises(ValueError, match="A25 编号"):
        checker.a25()


@pytest.mark.parametrize("corruption", ("shape", "arithmetic", "source"))
def test_ledger_requires_independent_arithmetic_and_traceability(ready_case, corruption):
    checker, write, _, _, _ = ready_case
    ledger = checker.load("tests/fixtures/ledger_examples.json")
    if corruption == "shape":
        ledger = {"not_a_ledger": True}
    elif corruption == "arithmetic":
        ledger["cases"][0]["expected"]["final_cash"] = "75233.92"
    else:
        ledger["source_refs"] = []
    write("tests/fixtures/ledger_examples.json", ledger)
    with pytest.raises(ValueError):
        checker.ledger()


def test_stale_environment_inputs_are_rejected(ready_case):
    checker, write, _, _, _ = ready_case
    write("uv.lock", "changed dependency lock")
    with pytest.raises(ValueError, match="哈希不符"):
        checker.environment()


def test_import_only_report_does_not_prove_ctp_runtime_checks(ready_case):
    checker, _, _, _, _ = ready_case
    path = checker.path("docs/09_前期准备与规则核验清单.md")
    value = path.read_text(encoding="utf-8").replace(
        "| 2.1 | item | requirement | 登记缺口 | GAP-S0-01 |",
        "| 2.1 | item | requirement | 已核验 | import only |",
    )
    path.write_text(value, encoding="utf-8")
    with pytest.raises(ValueError, match="脱敏运行验证"):
        checker.environment()


def test_missing_gap_reference_is_not_accepted(ready_case):
    checker, write, _, _, _ = ready_case
    profile = checker.load("config/broker_profiles/simnow_v6.yaml")
    profile["capabilities"]["margin_rates"]["gap_ids"] = ["GAP-NOT-REGISTERED"]
    write("config/broker_profiles/simnow_v6.yaml", profile)
    with pytest.raises(ValueError, match="无效"):
        checker.broker_capabilities()


def test_evidence_path_cannot_escape_repository(ready_case):
    checker, *_ = ready_case
    with pytest.raises(ValueError, match="项目目录"):
        checker.file_ref({"path": "../outside.txt", "sha256": "0" * 64}, "outside")


def test_checker_uses_repository_root_when_called_from_elsewhere(ready_case, tmp_path, monkeypatch):
    checker, *_ = ready_case
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert checker.strategy()[0] == "verified"
