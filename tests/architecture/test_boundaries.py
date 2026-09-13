"""静态架构依赖边界与循环引用检查 (Architecture Boundary Tests).

对应需求: NFR-03, 规划 §3.4 分层与模块依赖约束, docs/04 §3。
本测试通过 Python AST 静态分析语法树，提取依赖图，不以"模块可导入"为通过条件。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "qh_trader"


def get_all_py_files() -> List[Path]:
    return [p for p in PACKAGE_ROOT.rglob("*.py") if p.is_file()]


def parse_internal_imports(file_path: Path) -> Set[str]:
    """解析单个 Python 文件中对 qh_trader 内部包的依赖集合."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    imports: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("qh_trader."):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "qh_trader" or node.module.startswith("qh_trader.")):
                imports.add(node.module)
            elif node.level and node.level > 0:
                # 相对导入解析
                rel_parts = file_path.relative_to(PACKAGE_ROOT).parts[:-1]
                if node.level <= len(rel_parts) + 1:
                    base_parts = rel_parts[: len(rel_parts) - (node.level - 1)]
                    if node.module:
                        target = "qh_trader." + ".".join((*base_parts, node.module))
                    else:
                        target = "qh_trader." + ".".join(base_parts)
                    imports.add(target.rstrip("."))
    return imports


def get_dependency_graph() -> Dict[str, Set[str]]:
    """构建模块级依赖图: module_name -> set of imported module_names."""
    graph: Dict[str, Set[str]] = {}
    for p in get_all_py_files():
        rel = p.relative_to(PACKAGE_ROOT).with_suffix("")
        mod_name = "qh_trader." + ".".join(rel.parts)
        if mod_name.endswith(".__init__"):
            mod_name = mod_name[:-9]
        graph[mod_name] = parse_internal_imports(p)
    return graph


def get_layer(module_name: str) -> str:
    """提取模块所属子层 (如 core, domain, gateway, data, engine)."""
    parts = module_name.split(".")
    if len(parts) >= 2:
        return parts[1]
    return "root"


# ==============================================================================
# 架构分层规则断言
# ==============================================================================


def test_core_layer_isolation():
    """断言 Core 层为纯基础类型与协议，禁止依赖任何其他业务层或适配器层 (NFR-03)."""
    graph = get_dependency_graph()
    forbidden_layers = {
        "domain",
        "gateway",
        "data",
        "infrastructure",
        "monitor",
        "engine",
        "research",
        "strategy",
        "analysis",
    }

    for mod, imported_mods in graph.items():
        if get_layer(mod) == "core":
            for imp in imported_mods:
                imp_layer = get_layer(imp)
                assert (
                    imp_layer not in forbidden_layers
                ), f"架构违规: Core 模块 {mod} 逆向依赖了 {imp_layer} 层的 {imp}"


def test_domain_layer_isolation():
    """断言 Domain 领域内核仅依赖 Core 与自身内部模块，禁止依赖适配器与引擎层 (ADR-01, NFR-03)."""
    graph = get_dependency_graph()
    forbidden_layers = {"gateway", "data", "infrastructure", "monitor", "engine", "research", "analysis"}

    for mod, imported_mods in graph.items():
        if get_layer(mod) == "domain":
            for imp in imported_mods:
                imp_layer = get_layer(imp)
                assert (
                    imp_layer not in forbidden_layers
                ), f"架构违规: Domain 领域内核 {mod} 违规直接依赖了适配器/外部层 {imp_layer} 的 {imp} (领域必须通过 Port 注入)"


def test_adapters_do_not_depend_on_engine():
    """断言适配器层 (gateway, data, infrastructure, monitor) 不得反向依赖 engine 装配层."""
    graph = get_dependency_graph()
    adapter_layers = {"gateway", "data", "infrastructure", "monitor"}

    for mod, imported_mods in graph.items():
        if get_layer(mod) in adapter_layers:
            for imp in imported_mods:
                assert (
                    get_layer(imp) != "engine"
                ), f"架构违规: 适配器模块 {mod} 反向依赖了引擎层 {imp}"


def test_strategy_layer_isolation():
    """断言策略层仅依赖 Core 协议，禁止直接依赖具体的 Engine 或 Gateway 适配器."""
    graph = get_dependency_graph()
    forbidden_layers = {"gateway", "engine", "infrastructure", "monitor"}

    for mod, imported_mods in graph.items():
        if get_layer(mod) == "strategy":
            for imp in imported_mods:
                imp_layer = get_layer(imp)
                assert (
                    imp_layer not in forbidden_layers
                ), f"架构违规: 策略模块 {mod} 违规依赖了 {imp_layer} 的 {imp}"


def test_no_circular_dependencies():
    """使用 DFS 检测整个项目中是否存在循环依赖边 (Cycles) (NFR-03)."""
    graph = get_dependency_graph()
    visited: Dict[str, int] = {}  # 0: 未访问, 1: 访问中, 2: 已完成
    path: List[str] = []

    def dfs(node: str):
        visited[node] = 1
        path.append(node)
        for neighbor in graph.get(node, set()):
            # 仅检查图内节点
            if neighbor not in graph:
                continue
            state = visited.get(neighbor, 0)
            if state == 1:
                cycle_idx = path.index(neighbor)
                cycle_chain = " -> ".join(path[cycle_idx:] + [neighbor])
                raise AssertionError(f"架构违规: 存在循环依赖链: {cycle_chain}")
            elif state == 0:
                dfs(neighbor)
        path.pop()
        visited[node] = 2

    for n in graph:
        if visited.get(n, 0) == 0:
            dfs(n)


def test_package_structure_completeness():
    """检查 docs/04 §10 工程目录规划中声明的核心目录与包是否存在."""
    required_packages = [
        "core",
        "domain",
        "gateway",
        "monitor",
        "data",
        "infrastructure",
        "engine",
        "research",
        "strategy",
        "analysis",
    ]
    for pkg in required_packages:
        dir_path = PACKAGE_ROOT / pkg
        assert dir_path.is_dir(), f"缺失核心架构分层目录: qh_trader/{pkg}"
        assert (dir_path / "__init__.py").is_file(), f"目录缺失 __init__.py: qh_trader/{pkg}"
