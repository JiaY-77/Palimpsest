"""语义重排（rerank）：opt-in，默认关闭。

三条红线（每条都有离线实测依据，见 issue #272 / #274）：

1. **只排序、不筛选** —— 返回列表长度与传参一致，只改顺序，绝不置空；``llm``
   后端还要求模型「输出前 N 个、禁止空列表」，排出半截就判不可信。
2. **fail-open** —— 任何异常 / 超时 / 非 JSON / 空结果 / 模型文件缺失 / 输出不可
   解析都返回**原顺序**，并把状态标成 ``"degraded"``，绝不抛、绝不置空。
3. **默认关闭时零成本** —— 调用方在 ``RERANK_ENABLED`` 为假时连本模块都不导入。

两个后端（``RERANK_BACKEND``，``RERANK_ENABLED=1`` 时分派）：

- ``local_ce``（默认）：本地 cross-encoder，llama-cpp-python 打
  bge-reranker-v2-m3 GGUF，逐条打 query-doc 对、取返回向量第 0 维（其余维是
  llama.cpp 填充位）。零 API 额度、模型不出本机；实测见 docs/RETRIEVAL_QUALITY.md。
- ``llm``：远端 listwise（issue #272 原路径），行为保持不变。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)

STATUS_DISABLED = "disabled"
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"

# ---------------------------------------------------------------
# local_ce 后端：本地 cross-encoder（issue #274）
# ---------------------------------------------------------------

_CE_MODEL_CTX = 4096  # 一次只送一条 query-doc 对（snippet 默认 240 字），4096 足够
_CE_MODEL_UBATCH = 4096  # n_ubatch token 上限；仍逐条调用，这里只是放开物理上限

# llama.cpp 实例不是线程安全的（REST 层可能多线程进入）：懒加载与打分全程持锁。
_CE_LOCK = threading.Lock()
_CE_MODEL: Any = None  # llama_cpp.Llama 单例，首次调用才加载（mmap ≈ 0.7 秒）
_CE_BLOCKED = ""  # 非空 = 加载被永久挡住的原因（缺依赖/缺文件/坏文件），不再重试
_BACKEND_WARNED = False  # RERANK_BACKEND 非法值只 warn 一次


def _effective_backend() -> str:
    """解析 RERANK_BACKEND；非法值（含空串）视同 ``local_ce`` 并 warn 一次，不抛。"""
    global _BACKEND_WARNED
    raw = str(getattr(Config, "RERANK_BACKEND", "") or "").strip().lower()
    if raw not in ("local_ce", "llm"):
        raw = "local_ce"
        if not _BACKEND_WARNED:
            _BACKEND_WARNED = True
            logger.warning("RERANK_BACKEND 配置值未识别（可选 local_ce / llm），视同 local_ce 继续")
    return raw


def _resolve_local_model_path() -> Path:
    """local_ce 模型路径：绝对路径直接用，相对路径基于项目根解析（core/ 的上一级）。"""
    raw = str(getattr(Config, "RERANK_LOCAL_MODEL_PATH", "") or "").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / raw
    return path


def _model_file_exists(path: Path) -> bool:
    """模型文件存在性检查（单独立出来：测试用它观察「是否重复走磁盘」）。"""
    return path.is_file()


def _get_ce_model() -> Any | None:
    """懒加载 local_ce 单例（必须在 ``_CE_LOCK`` 内调用）。

    返回 ``None`` = 后端不可用。原因记入 ``_CE_BLOCKED``，之后不再碰磁盘：
    缺文件 / 缺依赖重试只会把同样的失败在每条查询上重复一遍。
    """
    global _CE_MODEL, _CE_BLOCKED
    if _CE_BLOCKED:
        return None
    if _CE_MODEL is not None:
        return _CE_MODEL
    model_path = _resolve_local_model_path()
    if not _model_file_exists(model_path):
        _CE_BLOCKED = f"模型文件不存在：{model_path}"
        logger.warning(
            "rerank(local_ce) 降级：本后端不可用（%s）。请下载 bge-reranker-v2-m3 的 Q8_0 "
            "GGUF 放到该路径，或用 RERANK_LOCAL_MODEL_PATH 指向已有模型，并安装 Python "
            "依赖 llama-cpp-python（见 docs/CONFIGURATION.md）；本次检索 fail-open 回原顺序",
            _CE_BLOCKED,
        )
        return None
    try:
        # llama-cpp-python 为可选依赖（extras: local-embed），上游无类型桩（mypy 已豁免本模块）；
        # wheel 安装方式见 docs/CONFIGURATION.md 的本地重排模型一节。
        from llama_cpp import LLAMA_POOLING_TYPE_RANK, Llama
    except Exception as e:  # noqa: BLE001 —— 缺依赖也是 fail-open，不是异常上抛
        _CE_BLOCKED = f"缺少 llama-cpp-python 依赖：{e}"
        logger.warning("rerank(local_ce) 降级（%s）；fail-open 回原顺序", _CE_BLOCKED)
        return None
    try:
        _CE_MODEL = Llama(
            model_path=str(model_path),
            embedding=True,
            pooling_type=LLAMA_POOLING_TYPE_RANK,
            n_ctx=_CE_MODEL_CTX,
            n_batch=_CE_MODEL_UBATCH,
            n_threads=int(getattr(Config, "RERANK_LOCAL_N_THREADS", 0) or 0),
            verbose=False,
        )
    except Exception as e:  # noqa: BLE001 —— 坏文件 / 加载失败同样归为后端不可用
        _CE_BLOCKED = f"模型加载失败：{e}"
        logger.warning("rerank(local_ce) 降级（%s）；fail-open 回原顺序", _CE_BLOCKED)
        return None
    logger.info("rerank(local_ce)：模型已加载 %s", model_path)
    return _CE_MODEL


def _ce_score_all(model: Any, query: str, docs: list[str]) -> list[float]:
    """给每个 (query, doc) 对打相关分。**必须逐条调用，禁止批量**：llama.cpp 的
    encoder 路径要求 n_ubatch >= n_tokens，一次塞多条候选会在 C 层 GGML_ASSERT
    直接 abort——Python 捕不到这个异常，进程直接死；实测批量也更慢（4 条
    395 ms/对 vs 单条 263 ms/对）。
    """
    import numpy as np  # 运行时跟随 triviumdb 一并就位；缺了走外层 fail-open

    scores: list[float] = []
    for doc in docs:
        out = model.create_embedding([f"{query}</s>{doc}"])
        # 返回 1024 维向量，**只有第 0 维是有效分数**，其余维度是 llama.cpp 的填充位
        # （实测 NaN/0）——对整向量做任何聚合都得到垃圾，这里只读第 0 维。
        scores.append(float(np.asarray(out["data"][0]["embedding"], dtype=np.float32).ravel()[0]))
    return scores


def _ce_document_text(item: dict, snippet_chars: int) -> str:
    """候选正文：沿用 _item_text（content 优先、summary 兜底），截断后把换行压成空格
    （同 build_prompt 的清洗，chr(10)/chr(13) = LF/CR）。"""
    view = _item_text(item)
    text = str(view.get("content", "") or "")[:snippet_chars]
    return text.replace(chr(10), " ").replace(chr(13), " ")


def _rerank_local_ce(query: str, items: list[dict], top_k: int) -> tuple[list[dict], str]:
    """local_ce 后端：本地 cross-encoder 重排。三条红线与 llm 路径一致。"""
    try:
        top_k_int = max(1, int(top_k or 1))
        n = len(items)
        if top_k_int >= n:
            # 整池都要：重排无意义，原样返回（与 llm 路径语义一致），也不加载模型
            return items, STATUS_OK
        snippet_chars = int(getattr(Config, "RERANK_SNIPPET_CHARS", 240))
        docs = [_ce_document_text(it, snippet_chars) for it in items]
        # 全程持锁：llama.cpp 实例非线程安全，并发查询在这里串行打分
        with _CE_LOCK:
            model = _get_ce_model()
            if model is None:  # 首次检测时已给过下载指引，这里不重复刷日志
                return items, STATUS_DEGRADED
            scores = _ce_score_all(model, query, docs)
    except Exception as e:  # noqa: BLE001 —— 任何异常都 fail-open 回原顺序（红线 2）
        logger.warning("rerank(local_ce) fail-open（异常）: %s", e)
        return items, STATUS_DEGRADED
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)  # sorted 稳定：同分保原序
    top = order[:top_k_int]
    top_set = set(top)
    # 前 top_k 按分数降序，其余按**原顺序**补在后面——全长返回，只改顺序（红线 1）
    ordered = [items[i] for i in top] + [items[i] for i in range(n) if i not in top_set]
    return ordered, STATUS_OK


_PROMPT_TEMPLATE = """你是检索结果的复排器。给定一个问题和一组候选资料片段，请把候选按「对回答这个问题的帮助程度」从高到低排序，输出最靠前的 N 个。

规则：
- 必须输出 N 个编号（候选不足 N 个时输出全部候选），不要输出空列表，也不要解释。
- 判断依据是片段内容能否提供回答问题所需的事实；同主题但答不上来的排后面。

只输出 JSON，不要任何其它文字：
{{"ranking": ["C3", "C11", "C1", "C7", "C2"]}}

问题：{query}

候选片段：
{cands}
"""


def build_prompt(query: str, items: list[dict], snippet_chars: int) -> str:
    """把 query + 候选正文拼成 listwise 重排 prompt。候选按传入顺序标号 C1..Cn。"""
    lines = []
    for i, item in enumerate(items, start=1):
        text = str(item.get("content", "") or "")
        text = text[:snippet_chars].replace("\n", " ").replace("\r", " ")
        lines.append(f"[C{i}] {text}")
    cands = "\n".join(lines)
    return _PROMPT_TEMPLATE.format(query=query, cands=cands)


# "C3" / "c3" 形式的候选编号（严格 token，防止从长正文里误抓 C 开头词）
_C_LABEL_RE = re.compile(r"^C(\d+)$", re.IGNORECASE)


def parse_ranking(raw: str, ids: list[int]) -> list[int]:
    """从模型输出里解析出 node id 顺序；解析失败返回 []（调用方视为 degraded）。

    raw 里应有一个形如 {"ranking": ["C3", ...]} 的 JSON 对象（可能裹在其它文字
    或 ```json 围栏里）。编号只接受 C1..Cn（n = len(ids)）；重复编号取首次出现；
    解析出空列表或整体失败都返回 []。
    """
    if not raw:
        return []
    # 抽出第一段形似 JSON 对象的文本（容忍模型把 JSON 裹在围栏/说明文字里）
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        obj = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    ranking = obj.get("ranking") if isinstance(obj, dict) else None
    if not isinstance(ranking, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for token in ranking:
        if not isinstance(token, str):
            continue
        m = _C_LABEL_RE.match(token.strip())
        if not m:
            continue
        idx = int(m.group(1))  # 1-based 候选序号
        if not 1 <= idx <= len(ids):
            continue
        nid = ids[idx - 1]
        if nid in seen:
            continue
        seen.add(nid)
        out.append(nid)
    return out


def rerank(query: str, items: list[dict], top_k: int) -> tuple[list[dict], str]:
    """返回 (重排后的 items, status)，status ∈ {"disabled","ok","degraded"}。

    非 ok 一律返回原 items（原顺序、原长度语义），由调用方决定怎么用。
    """
    # 所有配置现场读取（getattr），保证测试里 monkeypatch 改 Config 即生效。
    if not bool(getattr(Config, "RERANK_ENABLED", False)):
        return items, STATUS_DISABLED
    if len(items) <= 1:
        # 没什么可排的：直接 ok 返回，语义仍是「重排后的原序」
        return items, STATUS_OK
    if _effective_backend() == "local_ce":
        # 本地 cross-encoder 后端（默认；issue #274）：不需要任何 API key
        return _rerank_local_ce(query, items, top_k)

    # ---- llm 后端：远端 listwise（issue #272 原路径，行为不动）----
    api_key = str(getattr(Config, "DEEPSEEK_API_KEY", "") or "")
    if not api_key:
        logger.warning("rerank 跳过：DEEPSEEK_API_KEY 为空，fail-open 回原顺序")
        return items, STATUS_DEGRADED

    try:
        top_k_int = max(1, int(top_k or 1))
        n = len(items)
        need = min(top_k_int, n)
        if need >= n:
            # 候选不足 top_k 时全部都要，无需重排（与 prompt 规则「候选不足 N 个时
            # 输出全部候选」一致）——仍发起调用没有意义，直接原样返回。
            return items, STATUS_OK

        # 条目正文取 content；混合检索条目的正文在 "summary"——由 _item_text 兜底
        prompt = build_prompt(
            query, [_item_text(it) for it in items], int(getattr(Config, "RERANK_SNIPPET_CHARS", 240))
        )

        model = str(getattr(Config, "RERANK_MODEL", "") or "") or str(getattr(Config, "DEEPSEEK_MODEL", ""))
        body: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": int(getattr(Config, "RERANK_MAX_TOKENS", 8000)),
        }
        effort = str(getattr(Config, "RERANK_REASONING_EFFORT", "") or "")
        if effort:
            body["reasoning_effort"] = effort

        base_url = str(getattr(Config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=float(getattr(Config, "RERANK_TIMEOUT", 20)),
        )
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") != "stop":
            logger.warning("rerank fail-open：finish_reason=%r（非 stop）", choice.get("finish_reason"))
            return items, STATUS_DEGRADED
        content = str(choice.get("message", {}).get("content", "") or "")

        node_ids = []
        for it in items:
            node_ids.append(int(it["id"]))  # id 缺失 / None → TypeError → 外层 fail-open

        ordered_ids = parse_ranking(content, node_ids)
        if len(ordered_ids) < need:
            # 只排序不筛选：模型给的编号不足 N 个时，整个结果不可信 → fail-open
            # （prompt 已禁止空列表，缺号说明输出被截断或格式跑了，别拿半截排）。
            logger.warning("rerank fail-open：模型只排出 %d/%d 个候选", len(ordered_ids), need)
            return items, STATUS_DEGRADED
        by_id = {it.get("id"): it for it in items}
        # 幂余条目保持原顺序补在已排序列后面（不丢、不重复、长度不变）
        ordered = []
        seen: set = set()
        for nid in ordered_ids:
            if nid in seen:
                continue
            seen.add(nid)
            ordered.append(by_id[nid])
        for it in items:
            if it.get("id") not in seen:
                ordered.append(it)
        return ordered[:top_k_int], STATUS_OK
    except Exception as e:  # noqa: BLE001 —— 任何异常都 fail-open 回原顺序（红线 2）
        logger.warning("rerank fail-open（异常）: %s", e)
        return items, STATUS_DEGRADED


def _item_text(item: dict) -> dict:
    """给 build_prompt 用的候选视图：content 优先，缺了用 summary 兜底。"""
    out = dict(item)
    if not str(out.get("content", "") or "").strip():
        out["content"] = str(out.get("summary", "") or "")
    return out
