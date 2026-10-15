"""写入判定观测（write_judgment）——mem_ingest 写入时的判定痕迹累积与窗口汇总

用途：mem_ingest 的写入口每次成功写入都会产出三个判定数字——related_ids（相似
但未取代的话题相关提示）、replaces（显式取代）、outdated_ids（被标取代的旧记忆）。
这些数字此前一次性返回后即丢弃，无法回看「写入方到底有没有做写入前判定」。本模块
把它们追加到**库旁的观测文件**（judgment_metrics.jsonl，一行一个 JSON 对象），再由
aggregate() 按时间窗汇总成 write_judgment 分节（core.stats.compute_stats 输出），供
mem_stats 三入口（CLI / REST / MCP）共用。

**观测数据不进记忆库**（脱敏硬要求）：
  - 观测只落在独立的 jsonl 追加文件，不建节点、不进向量索引、不进检索池——否则
    观测记录会被 mem_search 召回、被 mem_stats 当成记忆盘点，污染检索与统计口径；
  - 只记 ts / node_id / type / domain 与三个计数，**绝不记录 content 或任何记忆正文**。

失败语义：record_ingest 整体 try/except 静默降级（只 logger.warning），观测写失败
绝不允许影响 mem_ingest 主流程；aggregate 对文件缺失 / 坏行同样不抛异常。
"""

import json
import logging
import os
import time
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

# 类型白名单：与 core.conflict 的自动相似判定白名单一致（仅这些类型计入 writes）
_WHITELIST_TYPES = ("memory", "task", "plan")

_FILENAME = "judgment_metrics.jsonl"


def _metrics_file(db_path: str | None = None) -> str:
    """观测文件路径：与库同目录、文件名固定（db_path 缺省取 Config.DB_PATH）。"""
    return os.path.join(os.path.dirname(db_path or Config.DB_PATH), _FILENAME)


def _as_int(value) -> int:
    """计数字段安全转 int（缺失 / 非法 → 0，绝不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def record_ingest(
    node_id: int | None,
    node_type: str,
    domain: str,
    *,
    related_count: int,
    replaced_count: int,
    outdated_count: int,
    db_path: str | None = None,
) -> None:
    """往观测文件追加一行写入判定记录（一行一个 JSON 对象，ensure_ascii=False）。

    字段固定 7 个：ts / node_id / type / domain / related_count / replaced_count /
    outdated_count——**绝不记录 content 或任何记忆正文**（脱敏硬要求）。

    **写失败必须静默降级**：整个函数体包 try/except，出错只 logger.warning，
    绝不允许异常冒泡影响 mem_ingest 主流程（本模块最关键的约束）。
    """
    try:
        row = {
            "ts": time.time(),
            "node_id": node_id,
            "type": node_type,
            "domain": domain,
            "related_count": int(related_count),
            "replaced_count": int(replaced_count),
            "outdated_count": int(outdated_count),
        }
        with open(_metrics_file(db_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 —— 观测写失败静默降级，绝不影响写入主流程
        logger.warning("写入判定观测记录失败（静默降级，不影响写入）: %s", e)


def aggregate(days: int = 30, db_path: str | None = None) -> dict:
    """读观测文件，汇总时间窗（ts >= now - days*86400）内的写入判定指标。

    只统计类型白名单（memory / task / plan）内的行；文件不存在返回 file_exists=False
    + 全 0 结构，坏行（JSON 解析失败 / 缺 ts / ts 非数值）跳过——**均不抛异常**；
    所有除法防零（writes==0 时各 rate 为 0.0）。无阻塞式网络调用。
    """
    path = os.path.abspath(_metrics_file(db_path))
    result: dict[str, Any] = {
        "window_days": days,
        "writes": 0,
        "explicit_replace_rate": 0.0,
        "related_hit_rate": 0.0,
        "high_similar_unreplaced_rate": 0.0,
        "outdated_in_window": 0,
        "by_type": {
            t: {"writes": 0, "related_hit_rate": 0.0, "high_similar_unreplaced_rate": 0.0} for t in _WHITELIST_TYPES
        },
        "file": path,
        "file_exists": False,
    }
    try:
        if not os.path.isfile(path):
            return result
        result["file_exists"] = True
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001 —— 观测文件读取失败按缺失降级
        logger.warning("写入判定观测文件读取失败（按缺失降级）: %s", e)
        return result

    cutoff = time.time() - days * 86400
    writes = 0
    replace_hits = 0
    related_hits = 0
    high_similar_unreplaced = 0
    outdated_in_window = 0
    per = {t: {"writes": 0, "related_hits": 0, "high_similar_unreplaced": 0} for t in _WHITELIST_TYPES}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001 —— 坏行（非 JSON）跳过继续
            continue
        if not isinstance(row, dict):
            continue
        ts = row.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue  # 缺 ts / ts 非数值 → 坏行跳过
        if ts < cutoff:
            continue  # 窗口外
        t = row.get("type")
        if t not in _WHITELIST_TYPES:
            continue  # 类型白名单：非 memory/task/plan 不计入
        related = _as_int(row.get("related_count"))
        replaced = _as_int(row.get("replaced_count"))
        outdated = _as_int(row.get("outdated_count"))

        writes += 1
        outdated_in_window += outdated
        bucket = per[t]
        bucket["writes"] += 1
        if related > 0:
            related_hits += 1
            bucket["related_hits"] += 1
        if replaced > 0:
            replace_hits += 1
        if related > 0 and replaced == 0:
            high_similar_unreplaced += 1
            bucket["high_similar_unreplaced"] += 1

    result["writes"] = writes
    result["outdated_in_window"] = outdated_in_window
    if writes:
        result["explicit_replace_rate"] = replace_hits / writes
        result["related_hit_rate"] = related_hits / writes
        result["high_similar_unreplaced_rate"] = high_similar_unreplaced / writes
    for t, bucket in per.items():
        n = bucket["writes"]
        entry = result["by_type"][t]
        entry["writes"] = n
        if n:
            entry["related_hit_rate"] = bucket["related_hits"] / n
            entry["high_similar_unreplaced_rate"] = bucket["high_similar_unreplaced"] / n
    return result
