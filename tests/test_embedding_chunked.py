"""分块池化 embedding（issue #232）的回归测试。

钉住的核心契约：
1. **单块路径与改动前逐位一致**——短文本（≤ chunk）的向量必须与
   「llama.embed(全文) + L2 归一」完全相同（谁优化这段算式都得先面对它）；
2. **长文本向量不再是前缀**——与「前 chunk_tokens 个 token 的向量」余弦
   必须显著低于 1.0（改动前 cos≈0.405 一线，分块池化实测 0.92+）；
3. audit 字段齐全：embedded_tokens == total_tokens、embedded_chunks ==
   ceil(total/chunk)——#232 的验收点就是「截断事实可读」；
4. CHUNK_TOKENS > N_BATCH 时 startup_check 报 error（配置自洽检查）;
5. reembed_long_nodes.py 幂等：第二次跑 0 变更，临时库 / 假 provider，绝不
   碰生产库（生产 --apply 由维护者执行）。

真模型断言在 GGUF 存在时才跑（CI 没有那个 639 MB 文件），否则 skip。模型
路径优先取 ``PALIMPSEST_TEST_EMBEDDING_GGUF``，其次项目根 ``models/``；绝不
走 ``resolve_model_path()``——那会在默认配置下触发自动下载。定位方式照抄
``tests/test_embedding_n_batch.py::_find_model_path``。
"""

import math
import os
from pathlib import Path

import pytest

import core.startup_check as sc
import core.trivium_store as ts_module
import scripts.reembed_long_nodes as reembed
from config import Config
from core.trivium_store import EmbeddingUnavailableError, TriviumStore

# 长输入压过 n_ctx 一小截没意义——压过 chunk（默认 512）且跨多块即可，
# 同时保持 token 化测试开销可控。
_LONG_TEXT = "".join(
    f"第{i}段：修行典故与势力纠葛的占位正文，内含门派恩怨、同门道法、师承谱系等细节，确保 token 数量与内容多样性足够。"
    for i in range(110)
)
_SHORT_TEXT = "网关端口配置在配置文件里，端口号是 8090"


# ---------------------------------------------------------------------------
# 真模型基础设施（照抄 test_embedding_n_batch.py）
# ---------------------------------------------------------------------------


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
    """与 TriviumStore._embed_local 同一尺度（L2 归一化）。"""
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _load_llama(model_path: str):
    """改动前基线的最小复刻：默认 n_batch（与生产同参）加载 + LAST pooling。"""
    from llama_cpp import LLAMA_POOLING_TYPE_LAST, Llama

    return Llama(
        model_path=model_path,
        embedding=True,
        n_ctx=Config.EMBEDDING_LOCAL_N_CTX,
        n_batch=Config.EMBEDDING_LOCAL_N_BATCH,
        n_threads=Config.EMBEDDING_LOCAL_N_THREADS or None,
        pooling_type=LLAMA_POOLING_TYPE_LAST,
        verbose=False,
    )


def _make_store(dim: int | None = None) -> TriviumStore:
    """不走 __init__（避免碰库）：手工制造一份带 local provider 的切片实例。"""
    s = TriviumStore.__new__(TriviumStore)
    s.provider = "local"
    s.dim = Config.EMBEDDING_LOCAL_DIM if dim is None else dim
    return s


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


@pytest.fixture(name="reset_local_llm")
def _reset_local_llm():
    """_LOCAL_LLM 进程级单例的保存/清空/还原（防测试间串味）。"""
    old = ts_module._LOCAL_LLM
    ts_module._LOCAL_LLM = None
    yield
    ts_module._LOCAL_LLM = old


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_single_short_path_is_bitwise_identical_to_baseline(embedding_model_path, reset_local_llm):
    """短文本：_embed_local_chunked 与「改动前 embed() + L2 归一」逐位一致。"""
    reference_llm = _load_llama(embedding_model_path)
    try:
        baseline = _l2_normalize(reference_llm.embed(_SHORT_TEXT))
    finally:
        del reference_llm  # 释放权重，再让单例加载，同段内存峰值

    store = _make_store()
    chunked_vec, audit = store._embed_local_chunked(_SHORT_TEXT)

    assert len(baseline) == len(chunked_vec) == Config.EMBEDDING_LOCAL_DIM
    assert chunked_vec == baseline, (
        "单块路径必须与改动前逐位一致（先 embed 全文再 L2 归一，运算顺序不能变）；"
        "这条失败说明有人改了单块算式或 normalize 算式，属于 #232 的回归"
    )
    assert audit["embedding_mode"] == "single"
    assert audit["embedded_tokens"] == audit["total_tokens"]
    assert audit["embedded_chunks"] == 1


def test_long_text_is_not_prefix_vector_and_audit_complete(embedding_model_path, reset_local_llm):
    """长文本：向量 ≠「前 chunk 个 token 的向量」，audit 齐全且全文覆盖。"""
    store = _make_store()
    chunked_vec, audit = store._embed_local_chunked(_LONG_TEXT)  # 本次调用顺带加载单例

    assert audit["embedding_mode"] == "chunked"
    assert audit["chunk_tokens"] == Config.EMBEDDING_LOCAL_CHUNK_TOKENS
    assert audit["embedded_tokens"] == audit["total_tokens"], "分块模式必须全文覆盖（#232 验收点）"
    assert audit["embedded_chunks"] == math.ceil(audit["total_tokens"] / Config.EMBEDDING_LOCAL_CHUNK_TOKENS)
    assert len(chunked_vec) == Config.EMBEDDING_LOCAL_DIM
    assert max(abs(v) for v in chunked_vec) > 0.0

    # 前缀向量：用同一把分词器切前 chunk 个 token → detokenize → 单块 embed
    llm = ts_module._LOCAL_LLM
    tokens = llm.tokenize(_LONG_TEXT.encode("utf-8"))
    assert len(tokens) > Config.EMBEDDING_LOCAL_CHUNK_TOKENS
    prefix_text = llm.detokenize(tokens[: Config.EMBEDDING_LOCAL_CHUNK_TOKENS]).decode("utf-8", errors="replace")
    prefix_vec = store._embed_local(prefix_text)

    sim = _cos(chunked_vec, prefix_vec)
    assert sim < 0.999, (
        f"长文本分块池化向量与前缀向量余弦={sim:.6f} ≥ 0.999——向量退化成了前缀，#232 的消除静默截断目标失守"
    )
    # 控制方向：分块池化与完整前缀应该语义相关但不相同（远小于 1、不会反号）
    assert sim > 0.0


# ---------------------------------------------------------------------------
# 纯 mock：chunk 切块加权数学 + audit（CI 无模型也要全绿）
# ---------------------------------------------------------------------------


class FakeLlamaTokenizer:
    """tokenize = 逐字节位置编号；detokenize = 按编号取回原字节；embed = 确定性 n-gram。

    位置编号让「token 边界切块 → detokenize 回原字节」精确可验证，
    embed 结果可手算期望，适合钉加权平均算式。
    """

    dim: int = 4

    def __init__(self) -> None:
        self._bytes: bytes = b""

    def tokenize(self, data: bytes, add_bos: bool = True, special: bool = False) -> list[int]:
        self._bytes = data
        return list(range(len(data)))

    def detokenize(self, tokens: list[int], prev_tokens=None, special: bool = False) -> bytes:
        return bytes(self._bytes[t] for t in tokens if 0 <= t < len(self._bytes))

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, b in enumerate(text.encode("utf-8")):
            vec[(b + i) % self.dim] += 1.0
        return vec


def _fake_expected(llm: FakeLlamaTokenizer, text: str, chunk_limit: int) -> tuple[list[float], dict]:
    """手算期望：与 _embed_chunked_locked 同算式的逐块加权平均。"""
    data = text.encode("utf-8")
    total = len(data)
    acc = [0.0] * llm.dim
    for c in range(math.ceil(total / chunk_limit)):
        chunk_text = data[c * chunk_limit : (c + 1) * chunk_limit].decode("utf-8", errors="replace")
        vec = llm.embed(chunk_text)
        norm = sum(v * v for v in vec) ** 0.5
        unit = [v / norm for v in vec]
        w = min(chunk_limit, total - c * chunk_limit)
        for i in range(llm.dim):
            acc[i] += unit[i] * w
    norm = sum(v * v for v in acc) ** 0.5
    expected = [v / norm for v in acc]
    audit = {
        "embedding_mode": "chunked",
        "total_tokens": total,
        "embedded_tokens": total,
        "embedded_chunks": math.ceil(total / chunk_limit),
        "chunk_tokens": chunk_limit,
    }
    return expected, audit


@pytest.fixture(name="fake_llama_chunked")
def _fake_llama_chunked(monkeypatch, reset_local_llm):
    """注入 FakeLlamaTokenizer 单例 + 小 chunk 配置，全程不碰库。"""
    llm = FakeLlamaTokenizer()
    monkeypatch.setattr(ts_module, "_LOCAL_LLM", llm)
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_CHUNK_TOKENS", 8)
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_BATCH", 16)
    return llm


def test_chunked_weighted_math_and_audit_complete(fake_llama_chunked):
    """分块路径的加权平均与 audit：逐块单位向量 × 块长权重 → 再归一。"""
    llm = fake_llama_chunked
    store = _make_store(dim=FakeLlamaTokenizer.dim)
    text = "甲乙丙丁戊己庚辛壬癸。修行典故与势力纠葛的正文继续往后写，凑出超过一个 chunk。"

    vec, audit = store._embed_local_chunked(text)

    expected, expected_audit = _fake_expected(llm, text, chunk_limit=8)
    assert audit == expected_audit
    assert audit["chunk_tokens"] == 8
    assert audit["embedded_chunks"] > 1
    assert len(vec) == llm.dim
    max_delta = max(abs(a - b) for a, b in zip(vec, expected, strict=True))
    assert max_delta < 1e-12, f"分块加权平均手算不符（max_delta={max_delta}）——算式或权重口径被改"
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 1e-12, "输出必须保持模长 1.0（与单块路径/既有向量同尺度）"


def test_single_short_path_with_fake_llama_matches_direct_call(fake_llama_chunked):
    """假模型下单块路径：tokenize 计数 ≤ chunk → fake embed 全文 + audit single。"""
    llm = fake_llama_chunked
    store = _make_store(dim=FakeLlamaTokenizer.dim)
    text = "很短"
    data = text.encode("utf-8")
    assert len(data) <= 8

    vec, audit = store._embed_local_chunked(text)

    assert audit == {
        "embedding_mode": "single",
        "total_tokens": len(data),
        "embedded_tokens": len(data),
        "embedded_chunks": 1,
        "chunk_tokens": 8,
    }
    raw = llm.embed(text)
    norm = sum(v * v for v in raw) ** 0.5
    assert vec == [v / norm for v in raw]


def test_chunk_exceeds_n_batch_fails_fast(fake_llama_chunked, monkeypatch):
    """运行时兜底：chunk > n_batch 直接报错，不悄悄把每块截断。"""
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_CHUNK_TOKENS", 64)
    store = _make_store(dim=FakeLlamaTokenizer.dim)
    with pytest.raises(EmbeddingUnavailableError) as ei:
        store._embed_local_chunked("哪怕很短也先拦")
    msg = str(ei.value)
    assert "64" in msg and "16" in msg
    assert "n_batch" in msg
    # 确认没碰模型：报错发生在 tokenize 之前
    assert ts_module._LOCAL_LLM._bytes == b""


# ---------------------------------------------------------------------------
# startup_check：chunk > n_batch → error（照 test_startup_check_embedding 写法）
# ---------------------------------------------------------------------------


def _set_embedding_config(monkeypatch, *, chunk, n_batch, provider="local"):
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_CHUNK_TOKENS", chunk)
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_N_BATCH", n_batch)
    monkeypatch.setattr(Config, "EMBEDDING_PROVIDER", provider)


def test_startup_check_chunk_exceeds_n_batch_is_error(monkeypatch):
    _set_embedding_config(monkeypatch, chunk=1024, n_batch=512)
    with pytest.raises(RuntimeError) as ei:
        sc._check_embedding_chunk_config()
    msg = str(ei.value)
    assert "1024" in msg and "512" in msg, "错误消息必须给出两个配置值"
    assert "n_batch" in msg and "截断" in msg


def test_startup_check_chunk_within_n_batch_ok(monkeypatch):
    _set_embedding_config(monkeypatch, chunk=512, n_batch=512)
    detail = sc._check_embedding_chunk_config()
    assert "自洽" in detail and "512" in detail


def test_startup_check_chunk_not_applicable_for_ollama(monkeypatch):
    _set_embedding_config(monkeypatch, chunk=1024, n_batch=512, provider="ollama")
    detail = sc._check_embedding_chunk_config()
    assert "不适用" in detail and "ollama" in detail


# ---------------------------------------------------------------------------
# reembed_long_nodes.py：幂等（第二次 0 变更），假库假 token 计数
# ---------------------------------------------------------------------------


class FakeReembedStore:
    """iter_payloads + reembed_node 的最小替身（写入仅打 chunked 审计标记）."""

    def __init__(self, payloads: dict[int, dict]):
        self.payloads = payloads
        self.reembed_calls: list[int] = []

    def iter_payloads(self):
        return iter([(nid, dict(p)) for nid, p in self.payloads.items()])

    def reembed_node(self, nid: int) -> bool:
        if nid not in self.payloads:
            return False
        self.reembed_calls.append(nid)
        # 真实实现的写入侧效果：payload["embedding"].embedding_mode = "chunked"
        content = self.payloads[nid].get("content", "")
        self.payloads[nid]["embedding"] = {
            "embedding_mode": "chunked",
            "total_tokens": len(content),
            "embedded_tokens": len(content),
        }
        return True


def _fake_token_count(text: str) -> int:
    return len(text)  # 测试口径：1 字符 = 1 token（token_count 是注入的，不必真）


def test_reembed_long_nodes_is_idempotent(monkeypatch):
    # 测试口径：chunk 阈值设 10，让「长 * 40 / 长 * 25」落进待重嵌、其他不挑
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_CHUNK_TOKENS", 10)
    payloads = {
        1: {"type": "novel_chunk", "content": "长" * 40},  # > 10 → 待重嵌
        2: {"type": "record", "content": "短"},  # ≤ 10 → 不挑
        3: {"type": "task", "content": "长" * 25},
        4: {"type": "record", "content": "长" * 15, "embedding": {"embedding_mode": "chunked"}},  # 已分块 → 跳过
    }
    store = FakeReembedStore(payloads)

    # 第 1 次 dry-run：只挑不写
    r1 = reembed.reembed_long_nodes(store, _fake_token_count, dry_run=True)
    assert r1["dry_run"] is True
    assert r1["already_chunked"] == 1
    assert r1["candidates_total"] == 2
    assert [c["id"] for c in r1["candidates"]] == [1, 3]
    assert store.reembed_calls == []  # dry-run 绝不写

    # 第 2 次 apply：真写
    r2 = reembed.reembed_long_nodes(store, _fake_token_count, dry_run=False)
    assert r2["applied"] == 2
    assert store.reembed_calls == [1, 3]
    assert r2["total_changed"] == 2

    # 第 3 次：幂等——全部已带 chunked 审计，挑不出任何节点、0 变更
    r3 = reembed.reembed_long_nodes(store, _fake_token_count, dry_run=False)
    assert r3["candidates_total"] == 0
    assert r3["total_changed"] == 0
    assert r3["already_chunked"] == 3

    # dry-run 复跑同样 0 变更（读到的审计已经就位）
    r4 = reembed.reembed_long_nodes(store, _fake_token_count, dry_run=True)
    assert r4["candidates_total"] == 0
    assert r4["already_chunked"] == 3


def test_reembed_long_nodes_report_fields(monkeypatch):
    monkeypatch.setattr(Config, "EMBEDDING_LOCAL_CHUNK_TOKENS", 10)
    store = FakeReembedStore({9: {"type": "kb_chunk", "content": "x" * 12}})
    r = reembed.reembed_long_nodes(store, _fake_token_count, dry_run=True)
    assert r["scanned"] == 1
    assert r["empty_content"] == 0
    assert r["candidates_by_type"] == {"kb_chunk": 1}
    assert r["candidates"][0]["tokens"] == 12
    assert r["candidates"][0]["chars"] == 12
    assert r["failed"] == []
