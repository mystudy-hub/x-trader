"""验证架构检查能识别不同导入语法中的违规依赖和真实循环。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[1] / "architecture" / "test_boundaries.py"


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
        path = checker.PACKAGE_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(
    ("statement", "dependency"),
    [
        ("from qh_trader import domain", "qh_trader.domain"),
        ("from qh_trader import domain as trading_domain", "qh_trader.domain"),
        ("from .. import domain", "qh_trader.domain"),
        ("from .. import domain as trading_domain", "qh_trader.domain"),
        ("from qh_trader.domain import orders", "qh_trader.domain.orders"),
        ("from ..domain import orders as order_types", "qh_trader.domain.orders"),
        ("from qh_trader.domain.orders import Order", "qh_trader.domain.orders"),
        ("from ..domain.orders import Order", "qh_trader.domain.orders"),
        ("import qh_trader.domain.orders as order_types", "qh_trader.domain.orders"),
    ],
)
def test_core_rejects_forbidden_import_forms(checker, statement, dependency):
    write_sources(
        checker,
        {
            "core/constants.py": statement,
            "domain/__init__.py": "",
            "domain/orders.py": "class Order: pass\n",
        },
    )

    assert checker.get_dependency_graph()["qh_trader.core.constants"] == {dependency}
    with pytest.raises(AssertionError, match="Core 模块"):
        checker.test_core_layer_isolation()


@pytest.mark.parametrize(
    "statement",
    [
        "from . import {sibling}",
        "from . import {sibling} as sibling_module",
        "from qh_trader.core import {sibling}",
        "from qh_trader.core import {sibling} as sibling_module",
        "from .{sibling} import Value",
        "import qh_trader.core.{sibling}",
    ],
)
def test_cycle_detection_handles_import_forms(checker, statement):
    write_sources(
        checker,
        {
            "core/__init__.py": "",
            "core/clock.py": statement.format(sibling="event") + "\nclass Value: pass\n",
            "core/event.py": statement.format(sibling="clock") + "\nclass Value: pass\n",
        },
    )

    with pytest.raises(AssertionError, match="存在循环依赖链"):
        checker.test_no_circular_dependencies()


@pytest.mark.parametrize(
    "statement",
    [
        "from . import objects",
        "from . import objects as types",
        "from qh_trader.core import objects",
        "from .objects import Event",
    ],
)
def test_package_reexports_do_not_create_self_cycles(checker, statement):
    write_sources(
        checker,
        {"core/__init__.py": statement, "core/objects.py": "class Event: pass\n"},
    )

    assert checker.get_dependency_graph()["qh_trader.core"] == {"qh_trader.core.objects"}
    checker.test_no_circular_dependencies()


def test_reexported_module_can_import_a_sibling_without_a_parent_cycle(checker):
    write_sources(
        checker,
        {
            "__init__.py": "from . import core\n",
            "core/__init__.py": "from .clock import Clock\n",
            "core/clock.py": "from . import event\nclass Clock: pass\n",
            "core/event.py": "class Event: pass\n",
        },
    )

    checker.test_core_layer_isolation()
    checker.test_no_circular_dependencies()


def test_symbol_import_keeps_its_defining_package_dependency(checker):
    write_sources(
        checker,
        {
            "core/__init__.py": "from .objects import Event\n",
            "core/clock.py": "from . import Event\n",
            "core/objects.py": "class Event: pass\n",
        },
    )

    graph = checker.get_dependency_graph()
    assert graph["qh_trader.core.clock"] == {"qh_trader.core"}
    assert graph["qh_trader.core"] == {"qh_trader.core.objects"}
    checker.test_no_circular_dependencies()


def test_mixed_module_and_symbol_imports_keep_both_dependencies(checker):
    write_sources(
        checker,
        {
            "core/__init__.py": "VERSION = 1\n",
            "core/clock.py": "from . import objects as types, VERSION\n",
            "core/objects.py": "class Event: pass\n",
        },
    )

    assert checker.get_dependency_graph()["qh_trader.core.clock"] == {
        "qh_trader.core",
        "qh_trader.core.objects",
    }
    checker.test_no_circular_dependencies()


@pytest.mark.parametrize(
    ("relative", "statement"),
    [
        ("core/nested/consumer.py", "from .. import objects"),
        ("core/nested/__init__.py", "from .. import objects"),
        ("core/nested/deeper/consumer.py", "from ... import objects"),
    ],
)
def test_nested_relative_imports_resolve_from_the_containing_package(checker, relative, statement):
    write_sources(checker, {relative: statement, "core/objects.py": "class Event: pass\n"})

    assert checker.parse_internal_imports(checker.PACKAGE_ROOT / relative) == {"qh_trader.core.objects"}
    checker.test_no_circular_dependencies()


def test_relative_prefix_is_resolved_before_matching_the_project_name(checker):
    write_sources(
        checker,
        {
            "core/consumer.py": "from .qh_trader import domain\n",
            "core/qh_trader/domain.py": "VALUE = 1\n",
        },
    )

    assert checker.get_dependency_graph()["qh_trader.core.consumer"] == {"qh_trader.core.qh_trader.domain"}
    checker.test_core_layer_isolation()


def test_namespace_package_is_recognized_as_a_module_import(checker):
    write_sources(checker, {"core/clock.py": "from .. import domain\n", "domain/orders.py": ""})

    assert checker.get_dependency_graph()["qh_trader.core.clock"] == {"qh_trader.domain"}
    with pytest.raises(AssertionError, match="Core 模块"):
        checker.test_core_layer_isolation()


def test_star_import_keeps_the_source_module_dependency(checker):
    write_sources(checker, {"core/clock.py": "from .objects import *\n", "core/objects.py": "VALUE = 1\n"})

    assert checker.get_dependency_graph()["qh_trader.core.clock"] == {"qh_trader.core.objects"}
    checker.test_no_circular_dependencies()


def test_external_imports_do_not_become_project_dependencies(checker):
    write_sources(
        checker,
        {"core/clock.py": "import qh_trader_extra\nfrom pathlib import Path\nfrom collections.abc import Iterable\n"},
    )

    assert checker.get_dependency_graph()["qh_trader.core.clock"] == set()


def test_relative_import_beyond_package_root_fails_explicitly(checker):
    write_sources(checker, {"core/clock.py": "from ...domain import orders\n"})

    with pytest.raises(ImportError, match="beyond top-level package"):
        checker.get_dependency_graph()
