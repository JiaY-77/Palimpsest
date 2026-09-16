"""readme_check.py 的失败路径测试。

「检查器全绿」本身不构成证据——必须证明注入漂移后它确实报错，否则它和没有一样。
覆盖：配置项默认值漂移、空值语义漂移、`.env.example` 死键、`--strict` 退出码、
以及「当前仓库自身一致」这条回归线。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 代码侧：含 str(50_000) 这类嵌套括号默认值——正则截断会直接让下面的比对失败
CONFIG_PY = """\
import os

REST_PORT = int(os.getenv("REST_PORT", "8090"))
SOFT_RERANK_EPS = float(os.getenv("SOFT_RERANK_EPS", "0.02"))
PALIMPSEST_API_KEY = os.getenv("PALIMPSEST_API_KEY", "")
MEM_INGEST_MAX_LENGTH = int(os.getenv("MEM_INGEST_MAX_LENGTH", str(50_000)))
"""

BASE_CELLS = {
    "REST_PORT": "`8090`",
    "SOFT_RERANK_EPS": "`0.02`",
    "MEM_INGEST_MAX_LENGTH": "`50000`",
}
EMPTY_CELL_ZH = "*（空 = 关闭）*"
EMPTY_CELL_EN = "*(empty = off)*"

ENV_EXAMPLE = "REST_PORT=8090\nSOFT_RERANK_EPS=0.02\nPALIMPSEST_API_KEY=\nMEM_INGEST_MAX_LENGTH=50000\n"


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    """按路径加载 scripts/readme_check.py（scripts/ 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("readme_check", REPO_ROOT / "scripts" / "readme_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(cells: dict[str, str]) -> str:
    body = "\n".join(f"| `{key}` | {cell} | 说明 |" for key, cell in cells.items())
    return f"| 变量 | 默认值 | 说明 |\n|---|---|---|\n{body}\n"


def _build_repo(tmp_path: Path, *, zh_overrides: dict[str, str] | None = None, en_overrides: dict[str, str] | None = None,
                env_extra: str = "", config_py: str = CONFIG_PY, main_py: str = "") -> Path:
    """造一个最小可检查仓库：config.py / .env.example / 两份 README / main.py。

    `main()` 会跑全部检查项，因此其余检查项要读的文件也建好空桩，避免
    FileNotFoundError 混进来掩盖真正被测的退出码。
    """
    zh = {**BASE_CELLS, "PALIMPSEST_API_KEY": EMPTY_CELL_ZH, **(zh_overrides or {})}
    en = {**BASE_CELLS, "PALIMPSEST_API_KEY": EMPTY_CELL_EN, **(en_overrides or {})}
    (tmp_path / "config.py").write_text(config_py, encoding="utf-8")
    (tmp_path / ".env.example").write_text(ENV_EXAMPLE + env_extra, encoding="utf-8")
    (tmp_path / "README.md").write_text(f"## 配置\n\n{_table(zh)}\n## 更换向量模型\n", encoding="utf-8")
    (tmp_path / "README_EN.md").write_text(f"## Configuration\n\n{_table(en)}\n## Swapping models\n", encoding="utf-8")
    # main() 会用 main.py 判断仓库根是否可用
    (tmp_path / "main.py").write_text(main_py, encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("", encoding="utf-8")
    (tmp_path / "mcp_tools").mkdir(exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "palimpsest_cli.py").write_text("", encoding="utf-8")
    return tmp_path


def test_matching_docs_produce_no_findings(checker: ModuleType, tmp_path: Path) -> None:
    report = checker.check_config_keys(_build_repo(tmp_path))
    assert report.errors == []
    assert report.warnings == []
    assert "默认值比对 4" in report.info


def test_zh_default_drift_is_an_error(checker: ModuleType, tmp_path: Path) -> None:
    root = _build_repo(tmp_path, zh_overrides={"SOFT_RERANK_EPS": "`0.5`"})
    errors = checker.check_config_keys(root).errors
    assert any("README.md" in e and "SOFT_RERANK_EPS" in e and "默认值不一致" in e for e in errors), errors


def test_en_default_drift_is_an_error(checker: ModuleType, tmp_path: Path) -> None:
    root = _build_repo(tmp_path, en_overrides={"REST_PORT": "`8000`"})
    errors = checker.check_config_keys(root).errors
    assert any("README_EN.md" in e and "REST_PORT" in e for e in errors), errors


def test_nested_paren_default_is_parsed(checker: ModuleType, tmp_path: Path) -> None:
    """str(50_000) 若被正则截断，会被读成「str(50_000」而与文档 50000 不符。"""
    root = _build_repo(tmp_path)
    assert checker.check_config_keys(root).errors == []
    assert checker._canon_code_default('str(50_000)') == "50000"


def test_empty_default_must_be_documented_as_empty(checker: ModuleType, tmp_path: Path) -> None:
    root = _build_repo(tmp_path, zh_overrides={"PALIMPSEST_API_KEY": "`enabled`"})
    errors = checker.check_config_keys(root).errors
    assert any("PALIMPSEST_API_KEY" in e for e in errors), errors


def test_env_example_dead_key_warns(checker: ModuleType, tmp_path: Path) -> None:
    root = _build_repo(tmp_path, env_extra="DEAD_KNOB=1\n")
    warnings = checker.check_config_keys(root).warnings
    assert any("DEAD_KNOB" in w for w in warnings), warnings


def test_strict_turns_warning_into_failure(checker: ModuleType, tmp_path: Path) -> None:
    """CI 依赖这条：只有警告时普通模式放行、--strict 必须非零退出。"""
    root = _build_repo(tmp_path, env_extra="DEAD_KNOB=1\n")
    assert checker.main(["--root", str(root)]) == 0
    assert checker.main(["--root", str(root), "--strict"]) == 1


def test_error_always_fails_regardless_of_strict(checker: ModuleType, tmp_path: Path) -> None:
    root = _build_repo(tmp_path, zh_overrides={"SOFT_RERANK_EPS": "`0.5`"})
    assert checker.main(["--root", str(root)]) == 1


def test_repo_itself_is_consistent(checker: ModuleType) -> None:
    """回归线：仓库当前的文档与代码必须一致（含本次新增的默认值比对）。"""
    errors = [item for report in checker.run_checks(REPO_ROOT) for item in report.errors]
    assert errors == []


# ---- 1a：config.py 的 os.getenv 键必须可归类 ----

def test_config_non_literal_default_unregistered_warns(checker: ModuleType, tmp_path: Path) -> None:
    """os.getenv 无默认值 / 非字面量默认值且未登记 → warning，不能静默拿去字面量比对。"""
    drift = CONFIG_PY + 'FOO_DERIVED = os.getenv("FOO_DERIVED")\n'
    root = _build_repo(tmp_path, config_py=drift)
    warnings = checker.check_config_keys(root).warnings
    assert any("键 FOO_DERIVED 的默认值不是字面量且未登记" in w for w in warnings), warnings


def test_config_non_literal_default_registered_is_allowed(checker: ModuleType, tmp_path: Path, monkeypatch) -> None:
    """登记进 CONFIG_DERIVED_DEFAULTS 的非字面量默认值 → 不再报未登记 warning。"""
    drift = CONFIG_PY + 'FOO_DERIVED = os.getenv("FOO_DERIVED")\n'
    monkeypatch.setitem(checker.CONFIG_DERIVED_DEFAULTS, "FOO_DERIVED", "占位：由外部解析")
    root = _build_repo(tmp_path, config_py=drift, env_extra="FOO_DERIVED=\n")
    warnings = checker.check_config_keys(root).warnings
    assert not any("默认值不是字面量且未登记" in w and "FOO_DERIVED" in w for w in warnings), warnings


def test_config_derived_key_outside_config_is_allowed(checker: ModuleType, tmp_path: Path, monkeypatch) -> None:
    """config.py 之外读取的键登记进 CONFIG_READ_OUTSIDE_CONFIG_PY → 不报未登记 warning。"""
    drift = CONFIG_PY + 'os.getenv("OUTSIDE_KEY")\n'
    monkeypatch.setitem(checker.CONFIG_READ_OUTSIDE_CONFIG_PY, "OUTSIDE_KEY", "core/xxx.py")
    root = _build_repo(tmp_path, config_py=drift, env_extra="OUTSIDE_KEY=\n")
    warnings = checker.check_config_keys(root).warnings
    assert not any("默认值不是字面量且未登记" in w and "OUTSIDE_KEY" in w for w in warnings), warnings


# ---- 1b：root() endpoints 列表与真实路由一致 ----

def _main_py(endpoints: list[str], routes: str = "") -> str:
    """拼一个带 root() endpoints 列表 + 额外路由的 main.py 文本桩。"""
    payload = ", ".join(f'"{p}"' for p in endpoints)
    return (
        '@app.get("/")\n'
        "async def root():\n"
        f'    return {{"endpoints": [{payload}]}}\n'
        + routes
    )


def test_route_index_missing_route_is_error(checker: ModuleType, tmp_path: Path) -> None:
    """代码注册了、endpoints 索引没列 → 报 error（代码有、索引缺）。"""
    routes = (
        '@app.get("/export")\n'
        "async def export():\n"
        "    pass\n"
        '@app.get("/summary")\n'
        "async def summary():\n"
        "    pass\n"
    )
    root = _build_repo(tmp_path, main_py=_main_py(["/export"], routes))
    errors = checker.check_route_index(root).errors
    assert any("代码有、端点索引缺" in e and "/summary" in e for e in errors), errors


def test_route_index_phantom_path_is_error(checker: ModuleType, tmp_path: Path) -> None:
    """endpoints 索引列了、代码没注册 → 报 error（索引有、代码没有）。"""
    routes = (
        '@app.get("/export")\n'
        "async def export():\n"
        "    pass\n"
    )
    root = _build_repo(tmp_path, main_py=_main_py(["/export", "/ghost"], routes))
    errors = checker.check_route_index(root).errors
    assert any("端点索引有、代码没有" in e and "/ghost" in e for e in errors), errors


def test_route_index_normalizes_placeholders(checker: ModuleType, tmp_path: Path) -> None:
    """索引里的 {id} 与代码里的 {node_id} 经 normalize_route 归一后可比。"""
    routes = (
        '@app.get("/memory/{node_id}")\n'
        "async def get_memory(node_id: int):\n"
        "    pass\n"
        '@app.patch("/memory/{node_id}/vector")\n'
        "async def upd_vector(node_id: int):\n"
        "    pass\n"
    )
    root = _build_repo(tmp_path, main_py=_main_py(["/", "/memory/{id}", "/memory/{id}/vector"], routes))
    errors = checker.check_route_index(root).errors
    assert errors == [], errors


# ---- 1c：section() 空串时不得静默假绿 ----

def test_rest_routes_missing_section_warns(checker: ModuleType, tmp_path: Path) -> None:
    """README 没有「### REST API」标记时，check_rest_routes 必须报 warning 而非静默放行。"""
    root = _build_repo(tmp_path)
    warnings = checker.check_rest_routes(root).warnings
    assert any("未找到区块" in w and "### REST API" in w for w in warnings), warnings


def test_cli_commands_missing_section_warns(checker: ModuleType, tmp_path: Path) -> None:
    """README 没有「### CLI 命令」标记时，check_cli_commands 必须报 warning 而非静默放行。"""
    root = _build_repo(tmp_path)
    warnings = checker.check_cli_commands(root).warnings
    assert any("未找到区块" in w and "### CLI 命令" in w for w in warnings), warnings
