"""startup_check._check_embedding 的分支测试。

回归重点（2.16.1 的修复）：``local`` 曾被当成 ``ollama`` 走 else 兜底，
于是内置引擎也在探测 11434 端口——Ollama 一关，自检就误报「Embedding 不可用」，
把「零外部依赖」的承诺打成笑话。下面第一条用例就是锁这个行为的。
"""

import pytest
import requests

import core.startup_check as sc
from config import Config
from core.trivium_store import EmbeddingUnavailableError, TriviumStore


def _set_provider(monkeypatch, provider):
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", provider)


def _fake_embed(monkeypatch, vec=None, exc=None):
    def _embed(self, text):
        if exc is not None:
            raise exc
        return vec

    monkeypatch.setattr(TriviumStore, "embed_text", _embed)


def _forbid_http(monkeypatch):
    """任何 HTTP 探测都算失败——local / openai 都不该走网络。"""

    def _boom(*args, **kwargs):
        raise AssertionError("该 provider 不应发起 HTTP 探测")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr(requests, "post", _boom)


# ---- local：不探 Ollama，直接实算 ----


def test_local_provider_never_probes_ollama(monkeypatch):
    """local 只实算向量，绝不碰 11434（这是 2.16.1 修的 bug）。"""
    _set_provider(monkeypatch, "local")
    _fake_embed(monkeypatch, [0.1, 0.2, 0.3])
    _forbid_http(monkeypatch)

    detail = sc._check_embedding()

    assert "provider=local" in detail
    assert "dim=3" in detail


def test_local_provider_failure_mentions_install_command(monkeypatch):
    """依赖没装时，报错要给出可直接复制的安装命令，而不是笼统的『不可用』。"""
    _set_provider(monkeypatch, "local")
    _fake_embed(monkeypatch, exc=EmbeddingUnavailableError("llama-cpp-python 未安装"))
    _forbid_http(monkeypatch)

    with pytest.raises(RuntimeError) as ei:
        sc._check_embedding()

    msg = str(ei.value)
    assert "llama-cpp-python" in msg
    assert "whl/cpu" in msg
    assert "实际错误" in msg


def test_local_provider_all_zero_vector_is_failure(monkeypatch):
    """模型在但算出全零向量 = 不可用（不能因为『没抛异常』就算通过）。"""
    _set_provider(monkeypatch, "local")
    _fake_embed(monkeypatch, [0.0, 0.0])
    _forbid_http(monkeypatch)

    with pytest.raises(RuntimeError) as ei:
        sc._check_embedding()
    assert "全零" in str(ei.value)


# ---- ollama：保持原有「先探端口再实算」----


def test_ollama_provider_probes_api_tags_then_embeds(monkeypatch):
    _set_provider(monkeypatch, "ollama")
    _fake_embed(monkeypatch, [0.5, 0.5])

    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

    def _get(url, timeout=None, **kwargs):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)

    detail = sc._check_embedding()

    assert "/api/tags" in seen["url"]
    assert "provider=ollama" in detail


def test_ollama_provider_probe_failure_reports_probe_error(monkeypatch):
    _set_provider(monkeypatch, "ollama")
    _fake_embed(monkeypatch, [0.5, 0.5])

    def _get(url, timeout=None, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr(requests, "get", _get)

    with pytest.raises(RuntimeError) as ei:
        sc._check_embedding()
    assert "探测失败" in str(ei.value)


# ---- openai：不探测，直接实算 ----


def test_openai_provider_does_not_probe(monkeypatch):
    _set_provider(monkeypatch, "openai")
    _fake_embed(monkeypatch, [0.4, 0.4, 0.4])
    _forbid_http(monkeypatch)

    detail = sc._check_embedding()

    assert "provider=openai" in detail
