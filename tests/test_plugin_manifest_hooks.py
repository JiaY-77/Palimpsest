"""``plugin.yaml`` 的 ``provides_hooks`` 必须与 ``register()`` 真实注册的钩子一致。

宿主 ``hermes plugins doctor`` / ``plugins validate`` 拿这个字段比对「声明 == 注册」。
它被写错过两轮：

- #178：``on_pre_compress`` 被当成事件钩子声明 —— 它其实是 ``MemoryProvider`` 的方法，
  宿主通过 provider 接口直接调用，于是报 ``unknown hook``；
- #183：同样的错还留在 ``on_session_end`` 上，报
  ``declares hook 'on_session_end' but registration did not add it``。

两轮都是**声明层错误**（功能本身正常），但 WARN 常驻之后真问题就淹在噪声里，
``plugins doctor`` 再也回不到全绿。这里把这条契约钉成测试。

用**静态解析**（ast + yaml）而不是真的加载插件：``register()`` 依赖宿主注入的 ``ctx``
与 ``agent.memory_provider`` 模块，端到端加载要造一整套假宿主（见
``tests/test_hermes_plugin_adapter.py``），而这条契约只关心两串名字是否相等。

约束：``register()`` 必须用**字符串字面量**调用 ``ctx.register_hook(...)``。将来若改成
变量 / 循环注册，本测试会 fail 并提示改用运行时校验 —— 这是有意的，宁可报错也不要
静默漏检。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "hermes-plugin"
MANIFEST = PLUGIN_DIR / "plugin.yaml"
INIT_PY = PLUGIN_DIR / "__init__.py"

# MemoryProvider 接口方法：宿主通过 provider 接口调用，不是事件钩子。
# 声明进 provides_hooks 只会换来一条 WARN。
PROVIDER_METHODS = ("on_pre_compress", "on_session_end")


def _declared_hooks() -> list[str]:
    """读到 ``provides_hooks`` 声明的钩子名。"""
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{MANIFEST} 解析结果不是映射"
    hooks = data.get("provides_hooks")
    if hooks is None:
        return []
    assert isinstance(hooks, list), "provides_hooks 必须是列表"
    return [str(h) for h in hooks]


def _registered_hooks() -> set[str]:
    """静态提取 ``register()`` 里所有 ``ctx.register_hook(<字面量>, ...)`` 的第一个参数。"""
    tree = ast.parse(INIT_PY.read_text(encoding="utf-8"))
    register_fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register"),
        None,
    )
    assert register_fn is not None, f"{INIT_PY} 里找不到顶层 register()"

    hooks: set[str] = set()
    for node in ast.walk(register_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "register_hook"):
            continue
        first = node.args[0] if node.args else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            pytest.fail(
                "register() 里的 ctx.register_hook(...) 第一个参数不是字符串字面量，"
                "静态校验无法判定声明的钩子名。请改回字面量，或把本测试改成"
                "「构造 fake ctx 真正调用 register() 并记录注册」的运行时校验。"
            )
        hooks.add(first.value)
    return hooks


def test_manifest_declares_at_least_one_hook() -> None:
    """防假绿：两边都空时下面的相等断言毫无意义。"""
    assert _declared_hooks(), "provides_hooks 为空 —— 解析失败还是真没钩子？"
    assert _registered_hooks(), "静态解析没找到任何 ctx.register_hook(...) —— 解析逻辑该更新了"


def test_declared_hooks_match_registrations() -> None:
    """声明 == 注册（双向）：多声明 → doctor WARN；少声明 → 未声明的注册。"""
    declared = set(_declared_hooks())
    registered = _registered_hooks()

    only_declared = declared - registered
    only_registered = registered - declared
    assert not only_declared, (
        f"provides_hooks 声明了未注册的钩子: {sorted(only_declared)}。"
        f"注册的是 {sorted(registered)} —— 多声明会让 `plugins doctor` 常驻 WARN。"
    )
    assert not only_registered, (
        f"注册了但没声明: {sorted(only_registered)}。声明的是 {sorted(declared)} —— 少声明同样会被校验器点名。"
    )


@pytest.mark.parametrize("method", PROVIDER_METHODS)
def test_provider_methods_are_not_declared_as_hooks(method: str) -> None:
    """MemoryProvider 方法不得出现在 provides_hooks（#178 / #183 的共同根因）。"""
    assert method not in _declared_hooks(), (
        f"{method} 是 MemoryProvider 的方法，不是事件钩子；写进 provides_hooks 只会换来一条"
        f"永久 WARN。写在 plugin.yaml 的注释里说明它由 provider 接口调用即可。"
    )
    assert method not in _registered_hooks(), (
        f"{method} 不该通过 ctx.register_hook() 注册 —— 宿主是通过 provider 接口调它的。"
    )
