"""根据文档中的层级关系验证真实源码导入，包括不允许的路径。"""

from __future__ import annotations

import importlib.util
from itertools import product
from pathlib import Path

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[1] / "architecture" / "test_boundaries.py"
RESTRICTED = ("core", "domain", "strategy", "data", "gateway", "infrastructure", "monitor", "engine")
TARGETS = (*RESTRICTED, "research", "analysis", "scripts", "unknown_layer")


@pytest.fixture
def checker(tmp_path):
    spec = importlib.util.spec_from_file_location("architecture_checker", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PACKAGE_ROOT = tmp_path / "qh_trader"
    module.PACKAGE_ROOT.mkdir()
    return module


def write_sources(checker, sources):
    for relative, source in sources.items():
        path = checker.PACKAGE_ROOT.parent / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(("source", "target"), tuple(product(RESTRICTED, TARGETS)))
def test_declared_layer_relationships(checker, source, target):
    target_module = "scripts.helper" if target == "scripts" else f"qh_trader.{target}.value"
    write_sources(
        checker,
        {
            f"qh_trader/{source}/consumer.py": f"import {target_module}\n",
            target_module.replace(".", "/") + ".py": "VALUE = 1\n",
        },
    )
    # 独立按文档约束判断，不读取被测检查器的允许矩阵。
    allowed = target == source or target == "core" or (source == "engine" and target == "domain")
    graph = checker.get_dependency_graph()
    if allowed:
        checker.assert_layer_dependencies(graph)
    else:
        with pytest.raises(AssertionError, match="架构违规"):
            checker.assert_layer_dependencies(graph)


@pytest.mark.parametrize("layer", (*RESTRICTED, "research", "analysis"))
def test_scripts_can_assemble_each_project_layer(checker, layer):
    write_sources(
        checker,
        {
            "scripts/run.py": f"from qh_trader.{layer} import value\n",
            f"qh_trader/{layer}/value.py": "VALUE = 1\n",
        },
    )
    checker.assert_layer_dependencies(checker.get_dependency_graph())


@pytest.mark.parametrize("layer", ("research", "analysis"))
def test_auxiliary_layers_cannot_import_cli(checker, layer):
    write_sources(
        checker,
        {f"qh_trader/{layer}/consumer.py": "from scripts import helper\n", "scripts/helper.py": ""},
    )
    with pytest.raises(AssertionError, match="架构违规"):
        checker.assert_layer_dependencies(checker.get_dependency_graph())


def test_root_reexport_cannot_hide_a_forbidden_dependency(checker):
    write_sources(
        checker,
        {
            "qh_trader/__init__.py": "from .domain.orders import Order\n",
            "qh_trader/core/value.py": "from qh_trader import Order\n",
            "qh_trader/domain/orders.py": "class Order: pass\n",
        },
    )
    with pytest.raises(AssertionError, match="qh_trader.domain.orders"):
        checker.test_core_layer_isolation()


def test_root_metadata_without_business_imports_is_allowed(checker):
    write_sources(
        checker,
        {"qh_trader/__init__.py": "VERSION = '0.1'\n", "qh_trader/core/value.py": "from qh_trader import VERSION\n"},
    )
    checker.test_core_layer_isolation()


def test_root_package_cannot_import_cli(checker):
    write_sources(checker, {"qh_trader/__init__.py": "from scripts import helper\n", "scripts/helper.py": ""})
    with pytest.raises(AssertionError, match="架构违规"):
        checker.assert_layer_dependencies(checker.get_dependency_graph())
