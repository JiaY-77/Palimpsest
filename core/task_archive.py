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

# 行首序号/前缀（如「ABC123：」「ITEM7:」），归档标题清洗时剔除
_LEADING_PREFIX_RE = re.compile(r"^[A-Za-z]*\d+[：:]\s*")
_LEADING_TRIM_CHARS = " \t-–—•·。：:,.，"


def _sanitize_filename(name: str, fallback: str = "task") -> str:
    """文件名安全化：剔除 Windows 非法字符（反斜杠、/、:、*、?、"、<、>、|）与空白，去尾部点/空格。

    安全护栏：清洗后若结果为 "."、".." 或仍以 ".." 开头（路径遍历意图），
    返回 fallback 兜底名，防止归档时逃逸出目标目录。
    """
    name = re.sub(r'[\\/:*?"<>|\r\n]', "", name or "")
    name = re.sub(r"\s+", "", name)
    # 路径遍历防护：检测连续点号开头（"."、".."、"..secret" 等），
    # 在剥离首尾点/空格之前判断，否则 ".." 会被 strip 成空串而漏网
    if name in (".", "..") or name.startswith(".."):
        return fallback
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


# 归档 md 的 YAML frontmatter 幂等键（重跑归档时据此判断该节点是否已归档）
_FRONTMATTER_NODE_ID_RE = re.compile(r"^node_id:\s*(\d+)\s*$", re.MULTILINE)


def build_archive_md(item: dict) -> str:
    """生成归档 Markdown 内容（YAML frontmatter + 标题、元数据表、任务正文原文）。

    frontmatter 的 `node_id` 是幂等键：写文件与删节点非原子（崩在中途、或删节点
    失败），重跑归档时据它复用磁盘上已有的那份文件，不再产出 `_2` 重复归档
    （见 `_scan_archived_node_ids`）。
    """
    title = item.get("title") or str(item.get("id", ""))
    now = datetime.now().isoformat(timespec="seconds")
    content = (item.get("content") or "").rstrip()
    return "\n".join([
        "---",
        f"node_id: {item.get('id', '')}",
        f"archived_at: {now}",
        f"type: {item.get('type', '')}",
        "---",
        "",
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


def _scan_archived_node_ids(archive_dir: str) -> dict[int, str]:
    """扫描归档目录，返回 {node_id: 文件路径}（幂等键来自 frontmatter 的 node_id）。

    只读每个文件前 1KB（frontmatter 在文件头）。没有 node_id 的文件（手工写的归档、
    本工具早期版本产出的归档）直接跳过——不认识的格式不影响正常写入。
    """
    found: dict[int, str] = {}
    if not os.path.isdir(archive_dir):
        return found
    for fname in sorted(os.listdir(archive_dir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(archive_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(1024)
        except OSError as e:  # 单个文件读不动不阻塞整批归档
            logger.warning(f"归档目录读取失败 {path}: {e}")
            continue
        m = _FRONTMATTER_NODE_ID_RE.search(head)
        if m:
            found.setdefault(int(m.group(1)), path)
    return found


def _write_archive_file(path: str, text: str) -> None:
    """原子写归档文件：先写同目录 `.tmp`，flush + fsync 后 os.replace 成正式名。

    崩在 replace 之前只会留下 .tmp（不参与幂等扫描，下次写正式文件时被覆盖），
    正式归档文件不会出现半截内容。
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def archive_tasks(store: TriviumStore, dry_run: bool = True,
                  knowledge_dir: str | None = None) -> dict:
    """已完成任务节点自动归档主入口。

    dry_run=True   只扫描预览：返回 candidates（含将写入的文件路径），不写文件、不删节点；
    dry_run=False  真正执行：先写归档 md 到 knowledge_dir/05_任务归档/，写入成功后才
                   store.delete_node(id) + fts_index.remove_node(id)；任一节点删除失败
                   记录到 errors，不中断整体。

    幂等性：写文件与删节点不是同一个事务，中途失败会留下「文件已写、节点还在」的
    状态。因此候选先按 frontmatter 的 node_id 与磁盘比对——已归档的复用原文件
    （跳过写入，直接补删节点），重跑不会产出 `_2` 重复归档。

    返回 {dry_run, candidates, archived, errors, skipped}；candidates / archived 的
    每个条目带 `already_archived`，预览与执行决策一致。
    """
    completed, skipped = _scan(store)
    archive_dir = os.path.join(_resolve_knowledge_dir(knowledge_dir), "05_任务归档")
    date_str = datetime.now().strftime("%Y%m%d")

    # 统一计算各候选的落盘路径（dry-run 与执行共用，保证预览=执行）
    # 幂等：磁盘上已有该 node_id 的归档 → 复用原文件，不再新写一份
    archived_ids = _scan_archived_node_ids(archive_dir)
    used_names: set[str] = set()
    planned: list[dict] = []
    for item in completed:
        existing = archived_ids.get(item["id"])
        if existing:
            planned.append({**item, "target_path": existing, "already_archived": True})
            continue
        base = _sanitize_filename(item["title"] or str(item["id"]))
        target = _unique_target(archive_dir, date_str, base, used_names)
        planned.append({**item, "target_path": target, "already_archived": False})

    preview = [{
        "id": p["id"],
        "title": p["title"],
        "target_path": p["target_path"],
        "already_archived": p["already_archived"],
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
    # 幂等命中的（already_archived）跳过写入，直接补删残留节点 —— 把上次中断的
    # 「文件已写、节点还在」收敛回一致状态，且不会产生第二份归档文件
    archived: list[dict] = []
    errors: list[dict] = []
    os.makedirs(archive_dir, exist_ok=True)
    for p in planned:
        if not p["already_archived"]:
            try:
                _write_archive_file(p["target_path"], build_archive_md(p))
            except Exception as e:  # noqa: BLE001 —— 写入失败记 errors 继续归档其余节点
                errors.append({"id": p["id"], "title": p["title"], "error": f"写入归档文件失败: {e}"})
                logger.error(f"归档写入失败 node={p['id']} -> {p['target_path']}: {e}")
                continue
        try:
            store.delete_node(p["id"])
        except Exception as e:  # noqa: BLE001 —— 节点删除失败记 errors 继续处理其余项
            errors.append({"id": p["id"], "title": p["title"], "error": f"删除节点失败: {e}"})
            logger.error(f"归档节点删除失败 node={p['id']}: {e}")
            continue
        try:
            remove_node(p["id"])
        except Exception as e:  # noqa: BLE001 —— FTS 清理失败仅告警不阻塞归档主流程
            errors.append({"id": p["id"], "title": p["title"], "error": f"移除 FTS 索引失败: {e}"})
            logger.warning(f"FTS 索引清理失败 node={p['id']}: {e}")
        archived.append({
            "id": p["id"],
            "title": p["title"],
            "target_path": p["target_path"],
            "already_archived": p["already_archived"],
        })

    return {
        "dry_run": False,
        "candidates": preview,
        "archived": archived,
        "errors": errors,
        "skipped": skipped,
    }
