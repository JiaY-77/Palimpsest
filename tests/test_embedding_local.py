"""provider=local 进程内词嵌入的单元测试。

全部 mock：不加载真 GGUF 模型、不下载、不连 Ollama；文件系统一律 tmp_path。
"""

import hashlib
import sys
import types
import urllib.parse
from pathlib import Path
from typing import ClassVar

import pytest
import requests as real_requests

import core.model_fetch as mf
import core.trivium_store as ts_module
from config import Config
from core.trivium_store import EmbeddingUnavailableError, TriviumStore

# conftest 的 session fake_embedder（autouse）会把 TriviumStore.embed_text 类属性
# 换成 n-gram fake；本文件要测真实分发链，collection 时（fixture 生效前）捕获原始
# 实现，测试里还原。
_ORIGINAL_EMBED_TEXT = TriviumStore.embed_text


@pytest.fixture(autouse=True)
def restore_real_dispatch(monkeypatch):
    """还原原始 embed_text，让本文件的分发/单例/归一化测试打到真代码。"""
    monkeypatch.setattr(TriviumStore, "embed_text", _ORIGINAL_EMBED_TEXT)


@pytest.fixture
def reset_local_llm():
    """_LOCAL_LLM 进程级单例的保存/清空/还原（防测试间串味）。"""
    old = ts_module._LOCAL_LLM
    ts_module._LOCAL_LLM = None
    yield
    ts_module._LOCAL_LLM = old


def make_store(dim: int = 1024) -> TriviumStore:
    """不走 __init__（避免碰库）：手工制造一份带 local provider 的切片实例。"""
    s = TriviumStore.__new__(TriviumStore)
    s.provider = "local"
    s.dim = dim
    return s


class FakeLlama:
    _result: ClassVar[list] = []
    ctor_calls: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        type(self).ctor_calls.append({"args": args, "kwargs": kwargs})

    def embed(self, text):
        return list(type(self)._result)


def install_fake_llama_cpp(monkeypatch, embed_result, ctor_calls):
    """向 sys.modules 注入假 llama_cpp 模块；返回 LLAMA_POOLING_TYPE_LAST 哨兵。"""
    sentinel = object()
    FakeLlama._result = embed_result
    FakeLlama.ctor_calls = ctor_calls
    mod = types.ModuleType("llama_cpp")
    mod.Llama = FakeLlama
    mod.LLAMA_POOLING_TYPE_LAST = sentinel
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)
    return sentinel


class FakeResponse:
    def __init__(self, data: bytes, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]


class FakeRequests:
    """替身 requests：按 URL 前缀决定成功/失败，记录每次调用的 headers。"""

    def __init__(self, fail_urls=()):
        self.fail_urls = fail_urls
        self.calls = []

    def get(self, url, stream=False, timeout=None, headers=None, allow_redirects=False):
        self.calls.append({"url": url, "headers": headers})
        if any(url.startswith(p) for p in self.fail_urls):
            raise real_requests.exceptions.ConnectionError(f"blocked: {url}")
        data = b"x" * 64
        return FakeResponse(data)


# ---------------------------------------------------------------------------
# 1. 分发：provider=local → _embed_local，且 Llama 构造参数含 pooling LAST
# ---------------------------------------------------------------------------


def test_dispatch_local_goes_to_embed_local(monkeypatch):
    store = make_store(dim=1024)
    seen = []
    monkeypatch.setattr(TriviumStore, "_embed_local", lambda self, text: seen.append(text) or [0.0] * 1024)
    assert store.embed_text("你好") == [0.0] * 1024
    assert seen == ["你好"]


def test_llama_construction_params(monkeypatch, reset_local_llm, tmp_path):
    ctor_calls = []
    sentinel = install_fake_llama_cpp(monkeypatch, embed_result=[0.2] * 1024, ctor_calls=ctor_calls)
    monkeypatch.setattr(mf, "resolve_model_path", lambda: str(tmp_path / "m.gguf"))
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_CTX", 2048)
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_BATCH", 64)
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_THREADS", 7)
    store = make_store(dim=1024)
    store._embed_local("hi")
    kw = ctor_calls[-1]["kwargs"]
    assert kw["pooling_type"] is sentinel
    assert kw["embedding"] is True
    assert kw["n_ctx"] == 2048
    assert kw["n_batch"] == 64
    assert kw["n_threads"] == 7


def test_singleton_constructs_llama_once(monkeypatch, reset_local_llm):
    ctor_calls = []
    install_fake_llama_cpp(monkeypatch, embed_result=[0.5] * 1024, ctor_calls=ctor_calls)
    monkeypatch.setattr(mf, "resolve_model_path", lambda: "fake.gguf")
    store = make_store()
    store._embed_local("first")
    store._embed_local("second")
    assert len(ts_module._LOCAL_LLM.__class__.ctor_calls) == 1


# ---------------------------------------------------------------------------
# 2. 归一化：[3, 4] → [0.6, 0.8]（模长 1.0）
# ---------------------------------------------------------------------------


def test_l2_normalization(monkeypatch, reset_local_llm):
    ctor_calls = []
    install_fake_llama_cpp(monkeypatch, embed_result=[3.0, 4.0], ctor_calls=ctor_calls)
    monkeypatch.setattr(mf, "resolve_model_path", lambda: "fake.gguf")
    store = make_store(dim=2)
    out = store._embed_local("x")
    assert out == pytest.approx([0.6, 0.8])
    assert sum(v * v for v in out) == pytest.approx(1.0)


def test_dimension_mismatch_raises(monkeypatch, reset_local_llm):
    ctor_calls = []
    install_fake_llama_cpp(monkeypatch, embed_result=[1.0, 2.0, 3.0], ctor_calls=ctor_calls)
    monkeypatch.setattr(mf, "resolve_model_path", lambda: "fake.gguf")
    store = make_store(dim=1024)
    with pytest.raises(EmbeddingUnavailableError) as ei:
        store._embed_local("x")
    msg = str(ei.value)
    assert "3" in msg and "1024" in msg


def test_missing_llama_cpp_dependency(monkeypatch, reset_local_llm):
    # sys.modules 注入 None → from llama_cpp import ... 抛 ImportError
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    store = make_store()
    with pytest.raises(EmbeddingUnavailableError) as ei:
        store._embed_local("x")
    assert "local-embed" in str(ei.value)


# ---------------------------------------------------------------------------
# 3. resolve_model_path：模型缺失 + 不自动下载 → 报错含两条可操作路径
# ---------------------------------------------------------------------------


def test_resolve_missing_model_no_download(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_PATH", "")
    monkeypatch.setattr(Config, "EMBEDDING_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "EMBEDDING_AUTO_DOWNLOAD", False)
    (tmp_path / Config.EMBEDDING_MODEL_FILE).write_bytes(b"")
    with pytest.raises(EmbeddingUnavailableError) as ei:
        mf.resolve_model_path()
    msg = str(ei.value)
    assert "EMBEDDING_MODEL_PATH" in msg and "EMBEDDING_AUTO_DOWNLOAD" in msg


def test_resolve_explicit_path_missing(monkeypatch, tmp_path):
    missing = str(tmp_path / "nope.gguf")
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_PATH", missing)
    with pytest.raises(EmbeddingUnavailableError) as ei:
        mf.resolve_model_path()
    assert missing in str(ei.value)


def test_resolve_existing_cache_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_PATH", "")
    monkeypatch.setattr(Config, "EMBEDDING_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_SIZE", 5)
    (tmp_path / Config.EMBEDDING_MODEL_FILE).write_bytes(b"12345")
    got = mf.resolve_model_path()
    assert got == str(tmp_path / Config.EMBEDDING_MODEL_FILE)


# ---------------------------------------------------------------------------
# 4. download_model：sha 不匹配删 .part；Range 续传；ModelScope→HF 回退
# ---------------------------------------------------------------------------


def _patch_download_env(monkeypatch, tmp_path, data: bytes):
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_SIZE", len(data))
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_SHA256", hashlib.sha256(data).hexdigest())
    dest = str(tmp_path / "m.gguf")
    return dest


def test_download_hash_mismatch_deletes_part(monkeypatch, tmp_path):
    data = b"corrupted!"
    dest = _patch_download_env(monkeypatch, tmp_path, data)
    # 故意改成错误 sha256，制造校验失败
    monkeypatch.setattr(Config, "EMBEDDING_MODEL_SHA256", "0" * 64)

    # 下载本身成功，但 sha256 配置故意与之不符 → 校验失败删 .part 后抛错
    class FakeReq:
        RequestException = real_requests.exceptions.RequestException

        @staticmethod
        def get(url, **kw):
            return FakeResponse(data, status_code=206)

    monkeypatch.setattr(mf, "requests", FakeReq)
    import os

    with pytest.raises(EmbeddingUnavailableError) as ei:
        mf.download_model(dest, source="modelscope")
    assert "sha256" in str(ei.value)
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")


def test_download_resumes_with_range_header(monkeypatch, tmp_path):
    data = b"y" * 1000
    dest = _patch_download_env(monkeypatch, tmp_path, data)

    part = dest + ".part"
    Path(part).write_bytes(b"12345")  # 已有 5 字节残件
    fake = FakeRequests()

    class FakeReq:
        RequestException = real_requests.exceptions.RequestException

        @staticmethod
        def get(url, **kw):
            fake.calls.append({"url": url, "headers": kw.get("headers")})
            # 服务端不支持 Range，回 200 → 从头写，最后仍应校验通过
            return FakeResponse(data, status_code=200)

    monkeypatch.setattr(mf, "requests", FakeReq)
    monkeypatch.setattr(Config, "EMBEDDING_DOWNLOAD_SOURCE", "modelscope")
    got = mf.download_model(dest, source="modelscope")
    assert got == dest
    assert fake.calls[0]["headers"] == {"Range": "bytes=5-"}
    assert Path(dest).read_bytes() == data


def test_download_falls_back_to_huggingface(monkeypatch, tmp_path):
    data = b"z" * 512
    dest = _patch_download_env(monkeypatch, tmp_path, data)
    fake = FakeRequests()

    class FakeReq:
        RequestException = real_requests.exceptions.RequestException

        @staticmethod
        def get(url, **kw):
            fake.calls.append({"url": url, "headers": kw.get("headers")})
            if url.startswith("https://modelscope.cn/"):
                raise real_requests.exceptions.ConnectionError("down")
            return FakeResponse(data, status_code=206)

    monkeypatch.setattr(mf, "requests", FakeReq)
    monkeypatch.setattr(Config, "EMBEDDING_DOWNLOAD_SOURCE", "auto")
    got = mf.download_model(dest, source="auto")
    assert got == dest
    urls = [c["url"] for c in fake.calls]
    assert any(u.startswith("https://modelscope.cn/") for u in urls)
    assert any(u.startswith("https://huggingface.co/") for u in urls)
    ms_first = next(u for u in urls if "modelscope" in u)
    hf_first = next(u for u in urls if "huggingface" in u)
    assert urls.index(ms_first) < urls.index(hf_first)
    assert Path(dest).read_bytes() == data


def test_url_template_contains_repo_and_file(monkeypatch):
    urls = mf._download_urls("auto")
    repo = urllib.parse.quote(Config.EMBEDDING_MODEL_REPO, safe="/")
    file = urllib.parse.quote(Config.EMBEDDING_MODEL_FILE)
    assert urls[0].startswith(f"https://modelscope.cn/models/{repo}/resolve/master/")
    assert urls[0].endswith(file)
    assert urls[1].startswith(f"https://huggingface.co/{repo}/resolve/main/")
    assert not any("hf-mirror" in u for u in urls)
