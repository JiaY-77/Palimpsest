"""`scripts/` 入口脚本的项目根注入（`_common` 约定）——护栏测试。

背景（2026-10-28 实际踩到）：`docs/INSTALL.md` 让人用 `python scripts/dashboard.py`
启动面板，但 `dashboard.py` 漏了 `from _common import ...`，于是在脚本模式下
（`sys.path[0]` 是 `scripts/` 而不是仓库根）直接崩在 `from config import Config`：

    ModuleNotFoundError: No module named 'config'

面板因此**从来没被正确启动过**，而文档看起来一切正常。这类问题只能靠「真的按脚本
模式 import 一遍」才测得出来——普通 pytest 里 `sys.path` 早就被 conftest 处理过，
怎么 import 都不会复现。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_like_a_script(script_name: str) -> subprocess.CompletedProcess:
    """按 `python scripts/<name>` 的 sys.path 形态 import 它，但不执行 `__main__`。

    关键：把仓库根从 `sys.path` 里摘掉、只放 `scripts/`——否则在仓库根跑任何东西
    都能 import 到 `config`，测试就成了永远绿的摆设。
    """
    code = textwrap.dedent(
        f"""
        import runpy
        import sys
        from pathlib import Path

        repo = Path(".").resolve()
        sys.path = [p for p in sys.path if Path(p or ".").resolve() != repo]
        sys.path.insert(0, str(repo / "scripts"))
        runpy.run_path(str(repo / "scripts" / {script_name!r}), run_name="not_main")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_dashboard_imports_in_script_mode():
    """`python scripts/dashboard.py` 是文档给的启动方式，import 阶段就不能崩。"""
    result = _import_like_a_script("dashboard.py")
    assert result.returncode == 0, (
        "dashboard.py 在脚本模式下 import 失败（很可能是漏了 `from _common import ...`，"
        "即项目根没注入 sys.path）：\n" + result.stderr[-2000:]
    )


def test_palimpsest_cli_imports_in_script_mode():
    """CLI 同样要能被 `python scripts/palimpsest_cli.py` 直接跑（回归保护）。"""
    result = _import_like_a_script("palimpsest_cli.py")
    assert result.returncode == 0, result.stderr[-2000:]
