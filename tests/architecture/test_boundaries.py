"""静态架构依赖边界与循环引用检查 (Architecture Boundary Tests).

对应需求: NFR-03, 规划 §3.4 分层与模块依赖约束, docs/04 §3。
本测试通过 Python AST 静态分析语法树，提取依赖图，不以"模块可导入"为通过条件。
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "qh_trader"
ALLOWED_LAYERS = {
    "core": {"core"},
    "domain": {"core", "domain"},
    "strategy": {"core", "strategy"},
    "data": {"core", "data"},
    "gateway": {"core", "gateway"},
    "infrastructure": {"core", "infrastructure"},
    "monitor": {"core", "monitor"},
    "engine": {"core", "domain", "engine"},
}


def get_all_py_files() -> List[Path]:
    roots = (PACKAGE_ROOT, PACKAGE_ROOT.parent / "scripts")
    return sorted(p for root in roots for p in root.rglob("*.py") if p.is_file())


def get_module_name(file_path: Path) -> str:
    """将源码路径转换为模块名，包的 __init__.py 使用包名。"""
    parts = file_path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def get_known_modules(files: List[Path]) -> Set[str]:
    """收集模块及其父包，包含没有 __init__.py 的命名空间包。"""
    modules: Set[str] = set()
    for file_path in files:
        parts = get_module_name(file_path).split(".")
        modules.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return modules


def parse_internal_imports(file_path: Path, known_modules: Set[str] | None = None) -> Set[str]:
    """解析单个 Python 文件中对 qh_trader 内部包的依赖集合."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    imports: Set[str] = set()
    if known_modules is None:
        known_modules = get_known_modules(get_all_py_files())
    current_module = get_module_name(file_path)
    package_parts = file_path.relative_to(PACKAGE_ROOT.parent).parts[:-1]
    current_package = ".".join(package_parts)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in {"qh_trader", "scripts"}:
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                target = resolve_name("." * node.level + target, current_package)
            if target.split(".", 1)[0] not in {"qh_trader", "scripts"}:
                continue

            for alias in node.names:
                submodule = target + "." + alias.name
                if alias.name != "*" and submodule in known_modules:
                    # 子模块是实际依赖；额外连向父包会给合法的包内导出制造循环。
                    imports.add(submodule)
                elif target != current_module:
                    # 类、函数、常量及星号导入依赖提供该对象的模块。
                    imports.add(target)
    return imports


def get_dependency_graph() -> Dict[str, Set[str]]:
    """构建模块级依赖图: module_name -> set of imported module_names."""
    files = get_all_py_files()
    known_modules = get_known_modules(files)
    return {get_module_name(path): parse_internal_imports(path, known_modules) for path in files}


def get_layer(module_name: str) -> str:
    """提取模块所属子层 (如 core, domain, gateway, data, engine)."""
    parts = module_name.split(".")
    if parts[0] == "scripts":
        return "scripts"
    if len(parts) >= 2:
        return parts[1]
    return "root"


# ==============================================================================
# 架构分层规则断言
# ==============================================================================


def assert_layer_dependencies(graph: Dict[str, Set[str]], source_layers: Set[str] | None = None):
    """按允许矩阵检查依赖链，包根导出不能隐藏跨层依赖。"""
    for source in sorted(graph):
        layer = get_layer(source)
        if source_layers is not None and layer not in source_layers:
            continue
        allowed = ALLOWED_LAYERS.get(layer)
        visited = {source}
        pending = [(target, [source, target]) for target in sorted(graph[source])]
        while pending:
            target, chain = pending.pop()
            target_layer = get_layer(target)
            label = "Core" if layer == "core" else layer
            detail = f"架构违规: {label} 模块的禁止依赖链: {' -> '.join(chain)}"
            assert layer == "scripts" or target_layer != "scripts", detail
            # 包根自身可承载元信息，但必须继续核对它导出的实际模块。
            if allowed is not None and target_layer != "root":
                assert target_layer in allowed, detail
            if target in visited:
                continue
            visited.add(target)
            pending.extend((child, [*chain, child]) for child in sorted(graph.get(target, set())))


def test_core_layer_isolation():
    """Core 仅允许本层无环依赖。"""
    assert_layer_dependencies(get_dependency_graph(), {"core"})


def test_domain_layer_isolation():
    """断言 Domain 领域内核仅依赖 Core 与自身内部模块，禁止依赖适配器与引擎层 (ADR-01, NFR-03)."""
    assert_layer_dependencies(get_dependency_graph(), {"domain"})


def test_adapter_layer_isolation():
    """每种适配器仅允许 core 及本层依赖。"""
    assert_layer_dependencies(get_dependency_graph(), {"gateway", "data", "infrastructure", "monitor"})


def test_strategy_layer_isolation():
    """断言策略层仅依赖 Core 协议，禁止直接依赖具体的 Engine 或 Gateway 适配器."""
    assert_layer_dependencies(get_dependency_graph(), {"strategy"})


def test_engine_layer_isolation():
    """Engine 接收注入端口，仅依赖 core、domain 及本层。"""
    assert_layer_dependencies(get_dependency_graph(), {"engine"})


def test_project_layers_never_import_entrypoints():
    """全部项目层（含根包、research、analysis）不得反向导入脚本。"""
    assert_layer_dependencies(get_dependency_graph())


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
