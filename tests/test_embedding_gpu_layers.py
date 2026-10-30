"""EMBEDDING_LOCAL_N_GPU_LAYERS 的回归测试。

钉住的核心契约：
1. **默认 0 = 行为不变**：``Llama(...)`` 的 kwargs 里**没有** ``n_gpu_layers``
   这个键——调用心甘情愿与改动前逐位一致，不信「传 0 等价于不传」；
2. `>0` 且 wheel 支持 GPU offload → 值原样传给 Llama()；
3. `>0` 但 wheel 是 CPU-only（``llama_supports_gpu_offload() == False``）→
   抛 ``EmbeddingUnavailableError``，文案含三条指引（CPU-only 事实 / 装 GPU
   wheel 或设回 0 / 模型文件），**绝不静默降级**；
4. ``startup_check`` 在同一种错配置下产出 error 级结果（raise RuntimeError）。

全程不加载真模型：向 ``sys.modules`` 注入一个假 ``llama_cpp``（可开关
``llama_supports_gpu_offload()`` 的替身），``Llama`` 只记录构造 kwargs。
CI 没装 llama-cpp-python 也能跑，不 skip。
"""

import sys
import types
from typing import ClassVar

import pytest

import core.startup_check as sc
import core.trivium_store as ts_module
from config import Config
from core.trivium_store import EmbeddingUnavailableError, TriviumStore

# ---------------------------------------------------------------------------
# 替身基础设施：假 llama_cpp 模块 + 记录式 Llama + 假模型定位
# ---------------------------------------------------------------------------


class _RecordingLlama:
    """替身 Llama：只把构造 kwargs 记到类属性上，绝不加载任何模型权重。"""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, **kwargs):
        _RecordingLlama.last_kwargs = dict(kwargs)


def _install_fake_llama_cpp(monkeypatch: pytest.MonkeyPatch) -> dict:
    """向 sys.modules 注入可开关 supports 的假 llama_cpp，返回可写开关 dict。

    monkeypatch 结束时自动还原真实模块（若本进程原本就有）。
    """
    switch = {"supports": True}
    mod = types.ModuleType("llama_cpp")
    mod.LLAMA_POOLING_TYPE_LAST = 0
    mod.llama_supports_gpu_offload = lambda: switch["supports"]
    mod.Llama = _RecordingLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)
    return switch


def _make_store() -> TriviumStore:
    """不走 __init__（避免碰库）：手工制造一份带 local provider 的切片实例。"""
    s = TriviumStore.__new__(TriviumStore)
    s.provider = "local"
    s.dim = Config.EMBEDDING_LOCAL_DIM
    return s


@pytest.fixture(name="gpu_env")
def _gpu_env(monkeypatch):
    """用例公共前置：清空单例 + 模型定位假实现 + 假 llama_cpp 模块 + 配置归零。"""
    old = ts_module._LOCAL_LLM
    ts_module._LOCAL_LLM = None
    monkeypatch.setattr("core.model_fetch.resolve_model_path", lambda: "fake-model.gguf")
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_GPU_LAYERS", 0)
    _RecordingLlama.last_kwargs = {}
    switch = _install_fake_llama_cpp(monkeypatch)
    yield {"monkeypatch": monkeypatch, "switch": switch}
    ts_module._LOCAL_LLM = old


# ---------------------------------------------------------------------------
# 1. 默认 0：不传 n_gpu_layers，调用与改动前逐位一致
# ---------------------------------------------------------------------------


def test_default_zero_omits_n_gpu_layers_kwarg(gpu_env):
    """=0 时 kwargs 里没有 n_gpu_layers 键，且其余参数与改动前完全一致。"""
    assert int(Config.EMBEDDING_LOCAL_N_GPU_LAYERS) == 0
    store = _make_store()

    store._ensure_local_llm()

    kwargs = _RecordingLlama.last_kwargs
    assert "n_gpu_layers" not in kwargs, "默认 0 与改动前逐位一致的契约：不许给 Llama() 注入 n_gpu_layers 默认值"
    assert kwargs == {
        "model_path": "fake-model.gguf",
        "embedding": True,
        "n_ctx": Config.EMBEDDING_LOCAL_N_CTX,
        "n_batch": Config.EMBEDDING_LOCAL_N_BATCH,
        "n_threads": Config.EMBEDDING_LOCAL_N_THREADS or None,
        "pooling_type": 0,
        "verbose": False,
    }


# ---------------------------------------------------------------------------
# 2. >0 且 wheel 支持：值原样传入
# ---------------------------------------------------------------------------


def test_positive_with_gpu_capable_wheel_passes_value(gpu_env):
    """=99 且 llama_supports_gpu_offload()==True → kwargs 里 n_gpu_layers=99。"""
    gpu_env["monkeypatch"].setattr(Config, "EMBEDDING_LOCAL_N_GPU_LAYERS", 99)
    store = _make_store()

    store._ensure_local_llm()

    assert _RecordingLlama.last_kwargs.get("n_gpu_layers") == 99, "支持 GPU 的 wheel 必须原样传值"


# ---------------------------------------------------------------------------
# 3. >0 且 wheel 是 CPU-only：报错 + 文案指引齐全，绝不静默降级
# ---------------------------------------------------------------------------


def test_positive_with_cpu_only_wheel_raises(gpu_env):
    """=2 且 supports==False → EmbeddingUnavailableError，文案含三条关键指引。"""
    gpu_env["monkeypatch"].setattr(Config, "EMBEDDING_LOCAL_N_GPU_LAYERS", 2)
    gpu_env["switch"]["supports"] = False
    store = _make_store()

    with pytest.raises(EmbeddingUnavailableError) as ei:
        store._ensure_local_llm()

    msg = str(ei.value)
    assert "EMBEDDING_LOCAL_N_GPU_LAYERS=2" in msg, "文案必须点名配置项与当前值"
    assert "CPU-only" in msg, "文案必须点明当前 wheel 是 CPU-only 版本"
    assert "wheel" in msg, "文案必须给出「装支持 GPU 的 wheel」这条出路"
    assert "设回 0" in msg, "文案必须给出「设回 0 保持 CPU」这条出路"
    assert Config.EMBEDDING_MODEL_FILE in msg, "文案必须带上模型文件名"
    assert _RecordingLlama.last_kwargs == {}, "报错路径绝不允许碰 Llama()（静默降级的反面就是根本不构造）"
    assert ts_module._LOCAL_LLM is None, "加载失败不缓存，下次调用可重试（既有语义）"


# ---------------------------------------------------------------------------
# 4. startup_check：>0 且不支持 → error 级（raise RuntimeError，由 _probe 标红）
# ---------------------------------------------------------------------------


def test_startup_check_error_when_configured_but_cpu_only(monkeypatch):
    """provider=local、layers=99、wheel CPU-only → RuntimeError（error 级结果）。"""
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_GPU_LAYERS", 99)
    switch = _install_fake_llama_cpp(monkeypatch)
    switch["supports"] = False

    with pytest.raises(RuntimeError) as ei:
        sc._check_embedding_gpu_layers()

    msg = str(ei.value)
    assert "EMBEDDING_LOCAL_N_GPU_LAYERS=99" in msg
    assert "CPU-only" in msg
    assert "wheel" in msg and "设回 0" in msg
    assert Config.EMBEDDING_MODEL_FILE in msg


def test_startup_check_ok_and_skip_states(monkeypatch):
    """配套状态位：支持时通过；=0 时明说未启用；非 local provider 不适用。"""
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_GPU_LAYERS", 99)
    switch = _install_fake_llama_cpp(monkeypatch)
    switch["supports"] = True
    assert "GPU 卸载配置自洽" in sc._check_embedding_gpu_layers()

    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_GPU_LAYERS", 0)
    detail_zero = sc._check_embedding_gpu_layers()
    assert "未启用" in detail_zero and "0" in detail_zero

    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", "ollama")
    detail_ollama = sc._check_embedding_gpu_layers()
    assert "不适用" in detail_ollama and "ollama" in detail_ollama
