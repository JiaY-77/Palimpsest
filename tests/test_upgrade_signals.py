"""升级可观测性（issue #192 / #193）：provider 来源回显 + 存量库升级提示。

2.16.0 把 `EMBEDDING_PROVIDER` 默认值从 `ollama` 改成 `local`，老 `.env` 通常没写
这一项 → 升级后静默换后端，而配置面上毫无变化。这里锁三件事：

1. `config.embedding_provider_source()` 能区分 `env` / `auto:api-key` / `auto:fallback`；
2. 启动自检的 detail 里回显来源；
3. 「老库 + 靠默认值落到 local」这个最高风险组合会额外追加一句 WARN 级提示。
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. embedding_provider_source()
# ---------------------------------------------------------------------------


def test_source_is_env_when_provider_explicitly_set(monkeypatch):
    from config import embedding_provider_source

    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    assert embedding_provider_source() == "env"


def test_source_is_auto_api_key_when_only_key_present(monkeypatch):
    from config import embedding_provider_source

    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-real-looking-key-1234567890")
    assert embedding_provider_source() == "auto:api-key"


def test_source_ignores_placeholder_keys(monkeypatch):
    """占位符 key（YOUR_API_KEY 之类）不算「可用云端」——与 _detect 同口径。"""
    from config import embedding_provider_source

    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY", "YOUR_API_KEY")
    assert embedding_provider_source() == "auto:fallback"


def test_source_is_fallback_without_anything(monkeypatch):
    from config import embedding_provider_source

    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    assert embedding_provider_source() == "auto:fallback"


def test_provider_source_is_not_a_config_attribute():
    """刻意做成模块级函数：落进 Config 会被配置项一致性检查当成漏登记的旋钮。"""
    from config import Config

    assert not hasattr(Config, "EMBEDDING_PROVIDER_SOURCE")


# ---------------------------------------------------------------------------
# 2/3. 启动自检回显与升级提示
# ---------------------------------------------------------------------------


class _StoreStub:
    def __init__(self, has_nodes: bool):
        self._has_nodes = has_nodes

    def iter_payloads(self):
        if self._has_nodes:
            yield (1, {"type": "memory", "content": "x"})
        return


def _run_embedding_check(monkeypatch, *, has_nodes: bool, source: str):
    """跑 `_check_embedding()`，把无关依赖（真 embedding / 真库）换掉。"""
    import core.startup_check as sc

    monkeypatch.setattr(sc, "_has_existing_nodes", lambda store: has_nodes)
    monkeypatch.setattr("config.embedding_provider_source", lambda: source)
    # provider=local 分支下不探端口；embed_text 换成固定向量，避免真跑模型
    monkeypatch.setattr(
        "core.trivium_store.TriviumStore.embed_text",
        lambda self, text: [0.1] * 8,
    )
    return sc._check_embedding()


def test_detail_shows_source_when_auto_detected(monkeypatch):
    detail = _run_embedding_check(monkeypatch, has_nodes=False, source="auto:fallback")
    assert "自动探测" in detail
    assert "provider=local" in detail


def test_detail_omits_source_note_when_explicit(monkeypatch):
    detail = _run_embedding_check(monkeypatch, has_nodes=False, source="env")
    assert "自动探测" not in detail


def test_old_store_on_default_local_gets_migration_hint(monkeypatch):
    """老库 + 靠默认值落到 local = 升级风险最高的组合，必须点名。"""
    detail = _run_embedding_check(monkeypatch, has_nodes=True, source="auto:fallback")
    assert "EMBEDDING_PROVIDER" in detail
    assert "2.16.0" in detail


def test_empty_store_gets_no_migration_hint(monkeypatch):
    """新装的空库不该被升级提示打扰。"""
    detail = _run_embedding_check(monkeypatch, has_nodes=False, source="auto:fallback")
    assert "2.16.0" not in detail


def test_no_hint_when_provider_explicit_even_with_nodes(monkeypatch):
    detail = _run_embedding_check(monkeypatch, has_nodes=True, source="env")
    assert "2.16.0" not in detail


@pytest.mark.parametrize("broken", [RuntimeError("db gone"), ValueError("nope")])
def test_has_existing_nodes_never_raises(monkeypatch, broken):
    """库遍历异常一律当作「没有节点」，绝不影响自检本身。"""
    import core.startup_check as sc

    class Boom:
        def iter_payloads(self):
            raise broken

    assert sc._has_existing_nodes(Boom()) is False
