"""守护测试：记忆 type 分层登记完备性（issue #151）。

缺陷史：``thought``（思维底片）既不在 ``TIER_FACTS`` 也不在 ``TIER_LOGS``，
走了「未登记保守归 facts」的兜底——与设计意图（logs 层、按需显式查）相反：
它混进默认检索与注入池，而 ``tier=logs`` 反过来一条都查不到。

本测试是防复发闸门：``Config.KNOWN_TYPES``（代码内已知会写入的 type 全集）
必须被 ``TIER_FACTS ∪ TIER_LOGS`` 覆盖，任何新 type 落库前必须登记进其中一层
（或显式加入 KNOWN_TYPES 的「例外」豁免清单并同步更新本测试语义）。

「类默认值语义」：不直接 ``import Config`` 取运行时值——CI 若预置了 ``TIER_*``
环境变量，import 出来的是被覆盖后的值，测不到源码默认值。这里直接从
``config.py`` 源码用 ast 解析 ``os.getenv("TIER_...")`` 的字面量默认值。
"""

from __future__ import annotations

import ast
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.py"


def _getenv_literal_default(source: str, key: str) -> list[str]:
    """从源码里抠出 ``os.getenv("KEY", "a,b,...")`` 的默认值列表（逗号分隔）。

    只处理「TIER_* / KNOWN_TYPES」这类字面量默认值的节点的赋值语句：
    在整个赋值表达式内找 ``os.getenv`` 调用，取第 2 个实参（字符串字面量）。
    抠不到抛 AssertionError——静默放过会让守护测试假绿（本测试的禁区）。
    """
    tree = ast.parse(source)
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id != key:
            continue
        for node in ast.walk(stmt.value):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            if is_getenv and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == key:
                if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                    raise AssertionError(f"config.py: {key} 的默认值不是字符串字面量，守护测试需同步更新解析逻辑")
                return [t.strip() for t in node.args[1].value.split(",") if t.strip()]
    raise AssertionError(f"config.py: 未找到 {key} 的 os.getenv 赋值（名称或结构被改？守护测试需同步，防假绿）")


def test_known_types_covered_by_tier_registry() -> None:
    """KNOWN_TYPES ⊆ TIER_FACTS ∪ TIER_LOGS：新写入 type 必须登记进其中一层。"""
    source = CONFIG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id != "KNOWN_TYPES":
            continue
        # KNOWN_TYPES 是 frozenset({...}) 形式（配置源码注释见 config.py）
        if not isinstance(stmt.value, ast.Call) or not stmt.value.args:
            raise AssertionError("config.py: KNOWN_TYPES 结构变了，请同步本测试")
        inner = stmt.value.args[0]
        if not isinstance(inner, ast.Set):
            raise AssertionError("config.py: KNOWN_TYPES 不是 frozenset(字面量集合)，请同步本测试")
        known = {el.value for el in inner.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)}
        break
    else:
        raise AssertionError("config.py: 未找到 KNOWN_TYPES 定义（名称被改？守护测试需同步，防假绿）")

    facts = set(_getenv_literal_default(source, "TIER_FACTS"))
    logs = set(_getenv_literal_default(source, "TIER_LOGS"))

    diff = known - (facts | logs)
    assert not diff, (
        f"未登记 tier 分层（会错误走「未登记归 facts」兜底，见 #151）："
        f"KNOWN_TYPES 里这些 type 既不在 TIER_FACTS 也不在 TIER_LOGS —— {sorted(diff)}"
    )


def test_thought_registered_in_tier_logs() -> None:
    """回归主断言：thought（思维底片，#151 所述缺陷）必须登记进 logs 层。"""
    logs = set(_getenv_literal_default(CONFIG_PATH.read_text(encoding="utf-8"), "TIER_LOGS"))
    assert "thought" in logs, "thought 应登记进 TIER_LOGS 默认值（#151），当前缺失"


def test_thought_not_in_tier_facts() -> None:
    """thought 是过程记录，不得回流事实层默认注入池（设计意图见 core/strategy.py 注释）。"""
    facts = _getenv_literal_default(CONFIG_PATH.read_text(encoding="utf-8"), "TIER_FACTS")
    assert "thought" not in facts, "thought 属过程记录，不应登记进 TIER_FACTS（会混进默认注入池）"
