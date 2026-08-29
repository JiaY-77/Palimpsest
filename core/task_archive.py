# -*- coding: utf-8 -*-
"""已完成任务节点自动归档：扫描 task 域已完成节点 → 写入知识库归档目录 → 删除节点。

归档目标：KNOWLEDGE_DIR/05_任务归档/{YYYYMMDD}_{title}.md（Obsidian 知识库）。
判定「已完成」两种都算：
  1. payload.status ∈ {completed, done}；
  2. payload.content 含完成标记（「已完成」关键词 / 结尾「完成】」/ 开头「【…完成」），
     且不含「未完成 / 等发布」等挂起提示（防误判「等发布」这种含完成字样的未完成任务）。
"""

import logging
import os
import re
from datetime import datetime
from typing import Any

from core.fts_index import remove_node
from core.trivium_store import TriviumStore, node_domain
from core.utils import _to_float

logger = logging.getLogger(__name__)

# 任务类节点 type 取值（Palimpsest 库里任务节点的 type）
TASK_TYPES = ("task", "plan", "record")

# 显式完成状态（并入「含完成标记」的判定）
COMPLETED_STATUSES = ("completed", "done")

# 内容级完成标记（宽松正则，三选一命中即视为完成）：
#   1. 「已完成」关键词（负向断言排除「未完成」，如「✅ 已完成」）；
#   2. 结尾「完成】」（如「…已全部完成】」）；
#   3. 开头「【…完成」（如「【全身优化任务 TASK-XXX 完成】」）。
_COMPLETED_MARKERS = re.compile(r"(?<!未)已完成|完成】\s*$|^\s*【[^【\n]*完成")

# 挂起/未完成提示：内容命中任一（且无显式完成状态）时不算已完成，
# 防误判「等发布」这类含完成字样的未完成任务（如「预热已完成，待生产发布 → [等发布]」）。
_PENDING_HINTS = ("未完成", "等发布", "待发布", "等待发布", "待启动", "进行中", "计划中", "未启动")

# 行首序号/前缀（如「T055：」「W009:」），归档标题清洗时剔除
_LEADING_PREFIX_RE = re.compile(r"^[A-Za-z]*\d+[：:]\s*")
_LEADING_TRIM_CHARS = " \t-–—•·。：:,.，"


def _sanitize_filename(name: str) -> str:
    """文件名安全化：剔除 Windows 非法字符（反斜杠、/、:、*、?、"、<、>、|）与空白，并去尾部点/空格。"""
    name = re.sub(r'[\\/:*?"<>|\r\n]', "", name or "")
    name = re.sub(r"\s+", "", name)
    return name.strip(" .")


def _extract_title(content: str) -> str:
    """从任务内容提取归档标题：取第一行，去行首序号/前缀，清洗为文件名安全形式，最长 50 字符。"""
    first_line = (content or "").splitlines()
    name = first_line[0].strip() if first_line else ""
    name = _LEADING_PREFIX_RE.sub("", name)
    name = name.strip(_LEADING_TRIM_CHARS)
    name = name[:50]
    return _sanitize_filename(name)


def _is_completed(payload: dict) -> bool:
    """判定节点是否为已完成任务（status 显式完成 or 内容含完成标记且无挂起提示）。"""
    status = (payload.get("status") or "").strip().lower()
    if status in COMPLETED_STATUSES:
        return True
    content = payload.get("content") or ""
    if not content:
        return False
    if any(hint in content for hint in _PENDING_HINTS):
        return False
    return bool(_COMPLETED_MARKERS.search(content))


def _item_from_node(nid: int, payload: dict) -> dict:
    """节点 payload → 归档条目字典（含标题）。"""
    content = payload.get("content") or ""
    return {
        "id": nid,
        "type": payload.get("type", ""),
        "content": content,
        "importance": round(_to_float(payload.get("importance"), 0.5), 2),
        "status": payload.get("status", ""),
        "created_at": payload.get("created_at"),
        "title": _extract_title(content),
    }


def _scan(store: TriviumStore) -> tuple[list[dict], int]:
    """扫描 task 域节点（node_domain(payload) == task，type ∈ TASK_TYPES）。

    返回 (completed, skipped)：completed 为已完成条目列表，skipped 为未完成任务数。
    """
    completed: list[dict] = []
    skipped = 0
    for nid, payload in store.iter_payloads():
        if node_domain(payload) != "task":
            continue
        if payload.get("type") not in TASK_TYPES:
            continue
        if not _is_completed(payload):
            skipped += 1
            continue
        completed.append(_item_from_node(nid, payload))
    return completed, skipped


def find_completed_tasks(store: TriviumStore) -> list[dict]:
    """扫描 task 域节点，返回已完成任务条目列表。

    条目：[{id, type, content, importance, status, created_at, title}]。
    """
    completed, _ = _scan(store)
    return completed


def build_archive_md(item: dict) -> str:
    """生成归档 Markdown 内容（含标题、元数据表、任务正文原文）。"""
    title = item.get("title") or str(item.get("id", ""))
    now = datetime.now().isoformat(timespec="seconds")
    content = (item.get("content") or "").rstrip()
    return "\n".join([
        f"# {title}",
        "",
        "> 归档自 Palimpsest（自动归档）",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| 节点 ID | {item.get('id', '')} |",
        f"| 类型 | {item.get('type', '')} |",
        f"| 状态 | {item.get('status', '')} |",
        f"| 重要度 | {item.get('importance', '')} |",
        f"| 归档时间 | {now} |",
        "",
        "## 任务内容",
        "",
        content,
        "",
    ])


def _resolve_knowledge_dir(knowledge_dir: str | None = None) -> str:
    """解析知识库根目录：显式传入 > KNOWLEDGE_DIR 环境变量 > 默认相对路径（与 mcp_tools._common 对齐）。"""
    if knowledge_dir:
        return os.path.abspath(knowledge_dir)
    env = (os.getenv("KNOWLEDGE_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(project_root, "knowledge"))


def _unique_target(archive_dir: str, date_str: str, base: str,
                   used_names: set[str]) -> str:
    """生成不重复的归档文件名（YYYYMMDD_{base}.md，重名追加序号后缀）。"""
    name = f"{date_str}_{base}.md"
    i = 2
    while name in used_names or os.path.exists(os.path.join(archive_dir, name)):
        name = f"{date_str}_{base}_{i}.md"
        i += 1
    used_names.add(name)
    return os.path.join(archive_dir, name)


def archive_tasks(store: TriviumStore, dry_run: bool = True,
                  knowledge_dir: str | None = None) -> dict:
    """已完成任务节点自动归档主入口。

    dry_run=True   只扫描预览：返回 candidates（含将写入的文件路径），不写文件、不删节点；
    dry_run=False  真正执行：先写归档 md 到 knowledge_dir/05_任务归档/，写入成功后才
                   store.delete_node(id) + fts_index.remove_node(id)；任一节点删除失败
                   记录到 errors，不中断整体。

    返回 {dry_run, candidates, archived, errors, skipped}。
    """
    completed, skipped = _scan(store)
    archive_dir = os.path.join(_resolve_knowledge_dir(knowledge_dir), "05_任务归档")
    date_str = datetime.now().strftime("%Y%m%d")

    # 统一计算各候选的落盘路径（dry-run 与执行共用，保证预览=执行）
    used_names: set[str] = set()
    planned: list[dict] = []
    for item in completed:
        base = _sanitize_filename(item["title"] or str(item["id"]))
        target = _unique_target(archive_dir, date_str, base, used_names)
        planned.append({**item, "target_path": target})

    preview = [{
        "id": p["id"],
        "title": p["title"],
        "target_path": p["target_path"],
    } for p in planned]

    if dry_run:
        return {
            "dry_run": True,
            "candidates": preview,
            "archived": [],
            "errors": [],
            "skipped": skipped,
        }

    # ---- 真正执行：写 md → 删节点 + 清 FTS 索引 ----
    archived: list[dict] = []
    errors: list[dict] = []
    os.makedirs(archive_dir, exist_ok=True)
    for p in planned:
        try:
            with open(p["target_path"], "w", encoding="utf-8") as f:
                f.write(build_archive_md(p))
        except Exception as e:
            errors.append({"id": p["id"], "title": p["title"], "error": f"写入归档文件失败: {e}"})
            logger.error(f"归档写入失败 node={p['id']} -> {p['target_path']}: {e}")
            continue
        try:
            store.delete_node(p["id"])
        except Exception as e:
            errors.append({"id": p["id"], "title": p["title"], "error": f"删除节点失败: {e}"})
            logger.error(f"归档节点删除失败 node={p['id']}: {e}")
            continue
        try:
            remove_node(p["id"])
        except Exception as e:
            errors.append({"id": p["id"], "title": p["title"], "error": f"移除 FTS 索引失败: {e}"})
            logger.warning(f"FTS 索引清理失败 node={p['id']}: {e}")
        archived.append({"id": p["id"], "title": p["title"], "target_path": p["target_path"]})

    return {
        "dry_run": False,
        "candidates": preview,
        "archived": archived,
        "errors": errors,
        "skipped": skipped,
    }