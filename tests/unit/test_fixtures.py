"""验收规格夹具完整性与测试 Oracles 接入准备测试.

验证 tests/fixtures/ 中的 15 个独立预期规格场景结构完整，
作为后续业务模块 (ledger, orders, calendar, data schemas) 实现时的黄金预期.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = ROOT / "tests" / "fixtures"


def test_fixture_cases_integrity():
    """验证 4 个夹具文件、15 个规格用例结构完整性."""
    expected_fixtures = {
        "ledger_examples.json": 2,
        "order_event_examples.json": 4,
        "session_boundary_examples.json": 2,
        "price_domain_examples.json": 7,
    }

    total_cases = 0
    for file_name, case_count in expected_fixtures.items():
        fixture_path = FIXTURES_DIR / file_name
        assert fixture_path.is_file(), f"缺失夹具文件: {file_name}"

        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert data.get("schema_version") == 1
        assert data.get("execution_status") == "not_executed"
        assert data.get("oracle", {}).get("method") == "independent_specification"

        cases = data.get("cases", [])
        assert len(cases) == case_count, f"{file_name} 期望 {case_count} 个用例，实际得到 {len(cases)}"

        for case in cases:
            assert "id" in case
            assert "inputs" in case
            assert "expected" in case
            assert len(case.get("acceptance_ids", [])) > 0

        total_cases += len(cases)

    assert total_cases == 15, f"规格夹具总数应为 15，实际为 {total_cases}"
