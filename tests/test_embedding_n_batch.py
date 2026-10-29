"""EMBEDDING_LOCAL_N_BATCH（issue #228）的回归测试。

这个配置项**同时是 embedding 的截断长度**：llama-cpp-python 的 embed() 内部有
``if truncate: tokens = tokens[:n_batch]``，超出部分静默丢弃。所以本文件钉的不是
「降它不改变向量」（那是错的），而是：

1. 默认值 = 512，保持历史行为；
2. **输入超过 n_batch 时向量会变**——把这个语义变成被测事实，谁想降它都得先
   面对这条测试，而不是以为它只省内存。

真模型断言在 GGUF 存在时才跑（CI 没有那个 639 MB 文件），否则 skip。模型路径
优先取 ``PALIMPSEST_TEST_EMBEDDING_GGUF``，其次项目根 ``models/``；绝不走
``resolve_model_path()``——那会在默认配置下触发自动下载。
"""

import os
from pathlib import Path

import pytest

from config import Config

# 长输入要覆盖生产里最长的切片（novel_chunk 最长 3118 字符，issue #228），
# 所以这条不是「恰好 ≥1000」，而是压过最大档。
_LONG_TEXT = "".join(
    f"第{i}段：修行典故与势力纠葛的占位正文，内入门派恩怨、同门道法、师承谱系等细节，确保 token 数量与内容多样性足够。"
    for i in range(110)
)


def _find_model_path() -> str:
    """真模型定位：显式环境变量 → 项目根 models/；非会话内不落库不下载。"""
    env_path = os.getenv("PALIMPSEST_TEST_EMBEDDING_GGUF", "").strip()
    if env_path:
        return env_path if Path(env_path).is_file() else ""
    root = Path(__file__).resolve().parent.parent
    if Config.EMBEDDING_MODELS_DIR:
        candidate = Path(Config.EMBEDDING_MODELS_DIR) / Config.EMBEDDING_MODEL_FILE
        return str(candidate) if candidate.is_file() else ""
    candidate = root / "models" / Config.EMBEDDING_MODEL_FILE
    return str(candidate) if candidate.is_file() else ""


def _l2_normalize(vec: list[float]) -> list[float]:
    """与 TriviumStore._embed_local 同一尺度（L2 归一化，零向量防除）。"""
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def test_default_n_batch_is_512_and_positive():
    """默认 512 = 历史行为不变；且必须是小于 n_ctx 的正整数。"""
    from config import Config as fresh_config  # 防上游猴补过本进程 Config

    value = fresh_config.EMBEDDING_LOCAL_N_BATCH
    assert isinstance(value, int)
    assert value == 512, "默认值改动会悄悄改变所有超长节点的向量，必须显式 review"
    assert value > 0
    assert value < fresh_config.EMBEDDING_LOCAL_N_CTX


def _load_llama(n_batch: int, model_path: str):
    from llama_cpp import LLAMA_POOLING_TYPE_LAST, Llama

    return Llama(
        model_path=model_path,
        embedding=True,
        n_ctx=Config.EMBEDDING_LOCAL_N_CTX,
        n_batch=n_batch,
        pooling_type=LLAMA_POOLING_TYPE_LAST,
        verbose=False,
    )


@pytest.fixture(name="embedding_model_path")
def _embedding_model_path() -> str:
    """缺模型 / 缺 llama-cpp-python → skip；绝不自动下载。"""
    import importlib.util

    if importlib.util.find_spec("llama_cpp") is None:
        pytest.skip("llama-cpp-python 未安装（CI 无 local-embed 依赖），跳过真模型断言")
    model_path = _find_model_path()
    if not model_path:
        pytest.skip(
            "真 GGUF 模型不可得（设 PALIMPSEST_TEST_EMBEDDING_GGUF 指向模型文件可启用本测试"
            f"，或把模型放到项目根 models/{Config.EMBEDDING_MODEL_FILE}），跳过向量一致性断言"
        )
    return model_path


def test_lowering_n_batch_truncates_long_input(embedding_model_path):
    """钉死语义：n_batch 小于输入 token 数时向量会变（只嵌前缀）。

    早期结论「降 n_batch 不改变向量」是错的——那次只比了短文本（单批内）。
    本测试用超过两批的长文本，证明同一模型换 n_batch 会给出不同向量，
    所以把它当纯内存旋钮是危险的。
    """
    first = _load_llama(n_batch=2048, model_path=embedding_model_path)
    try:
        reference = _l2_normalize(first.embed(_LONG_TEXT))
    finally:
        del first  # 释放权重再加载第二份，峰值控制在一段模型内存

    second = _load_llama(n_batch=64, model_path=embedding_model_path)
    try:
        truncated = _l2_normalize(second.embed(_LONG_TEXT))
    finally:
        del second

    assert len(reference) == len(truncated) == Config.EMBEDDING_LOCAL_DIM
    max_delta = max(abs(a - b) for a, b in zip(reference, truncated, strict=True))
    assert max_delta > 1e-3, (
        "长输入在不同 n_batch 下应产生不同向量（截断语义）；"
        "这条开始失败说明 llama-cpp-python 改了 embed() 的截断行为，需重新评估本配置项"
    )


def test_short_input_is_batch_size_independent(embedding_model_path):
    """反向保险：输入短到能装进最小一批时，n_batch 不该影响结果。"""
    short = "网关端口配置在配置文件里，端口号是 8090"
    first = _load_llama(n_batch=2048, model_path=embedding_model_path)
    try:
        reference = _l2_normalize(first.embed(short))
    finally:
        del first
    second = _load_llama(n_batch=64, model_path=embedding_model_path)
    try:
        candidate = _l2_normalize(second.embed(short))
    finally:
        del second
    max_delta = max(abs(a - b) for a, b in zip(reference, candidate, strict=True))
    assert max_delta < 1e-6


def test_long_input_is_actually_long():
    """文档口径的长输入 ≥1000 字符；常量必须真的达到，别让测试静默变短。"""
    assert len(_LONG_TEXT) >= 3118  # 压过 novel_chunk 生产最长切片
