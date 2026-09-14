"""core/ 分层约束与「维度探测只有一份实现」的架构测试。

审计批次 2 的回归守卫：
  - core/doctor.py 曾经 `from scripts.reindex import _get_db_dim`，即 core 反向
    依赖 scripts。core 是被 scripts / mcp_tools / REST 共用的底层，反向依赖会
    让「单独使用 core」时必须先有 scripts 包，也让 mypy 顺着 core 把 scripts
    一起拖进检查范围。
  - 维度探测（读 DB storage_info 的 dim）原先只有 scripts/reindex.py 一份实现，
    doctor 复用它。现在实现下沉到 core/dims.py，两边必须是同一个对象，避免又
    长出第二份。
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "core"


def _imported_modules(path: Path) -> list[str]:
    """返回模块里所有 import 的模块名（含 `from x import y` 的 x）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_core_does_not_import_scripts():
    """core/ 下任何模块都不许 import scripts.*（反向依赖）。"""
    offenders: list[str] = []
    for py in sorted(CORE_DIR.glob("*.py")):
        for module in _imported_modules(py):
            if module == "scripts" or module.startswith("scripts."):
                offenders.append(f"{py.name} -> {module}")
    assert offenders == [], f"core 反向依赖 scripts: {offenders}"


def test_dimension_probe_has_a_single_implementation():
    """维度探测只有一份实现：core.dims.get_db_dim，scripts.reindex 复用同一对象。"""
    from core.dims import get_db_dim
    from scripts import reindex

    assert reindex._get_db_dim is get_db_dim
