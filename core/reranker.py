"""语义重排（rerank）：opt-in，默认关闭。

三条红线（每条都有离线实测依据，见 issue #272）：

1. **只排序、不筛选** —— prompt 要求「输出前 N 个、禁止空列表」。允许模型输出空
   列表时，它会因为「某候选与问题不直接对应」把整条查询的候选全部丢掉。
2. **fail-open** —— 任何异常 / 超时 / 非 JSON / 空结果 / finish_reason != "stop"
   都返回**原顺序**，并把状态标成 ``"degraded"``，绝不抛、绝不置空。
3. **默认关闭时零成本** —— 调用方在 ``RERANK_ENABLED`` 为假时连本模块都不导入。
"""

from __future__ import annotations

import json
import logging
import re

import requests

from config import Config

logger = logging.getLogger(__name__)

STATUS_DISABLED = "disabled"
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"

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
        # 没什么可排的：直接 ok 返回（不花一次 LLM 调用），语义仍是「重排后的原序」
        return items, STATUS_OK
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
