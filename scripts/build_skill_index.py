"""Hermes 技能语义索引构建脚本
================================

递归扫描技能目录中的 ``SKILL.md``，每个文件作为一个 ``skill_chunk`` 节点写入
TriviumDB，供 ``mcp_tools.skill.skill_search`` 做语义检索。

技能目录按下列顺序解析（见 :func:`resolve_skills_dir`）：

1. 显式 ``--skills-dir``
2. 当 ``HERMES_HOME`` 未指向档案目录、而 ``HERMES_PROFILE`` 指定的档案目录存在时，
   ``<hermes 根>/profiles/<profile>/skills``
3. ``<HERMES_HOME>/skills``（Hermes 的规范位置；档案模式下 ``HERMES_HOME`` 本身即
   指向 ``profiles/<profile>``，故此处等价于档案目录）
4. ``~/.hermes/skills``

**解析不到任何存在的技能目录时脚本以非零码退出**，绝不静默按空目录继续——否则
孤儿清理会把库中全部 ``skill_chunk`` 节点当作已删除源文件清空。

默认采用增量模式：按 ``source_path`` 和 ``source_mtime`` 复用未变化的节点；
``--full`` 会强制刷新所有现存技能文件。两种模式都会清理源文件已删除的孤儿节点，
且只在技能目录真实存在并通过扫描时执行。
"""

import argparse
import json
import os
import re
import sys
from typing import Any

# 直接执行 scripts/build_skill_index.py 时，把项目根加入 sys.path，确保 core/config
# 与现有 TriviumStore 实现可导入；以模块方式导入时也不依赖当前工作目录。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.trivium_store import TriviumStore  # noqa: E402

# PyYAML 不是项目运行依赖：优先使用它解析标准 YAML；不可用时使用下面的最小解析器，
# 覆盖 Hermes SKILL.md frontmatter 常见的标量、引号和块标量写法。
try:  # pragma: no cover - 取决于运行环境是否安装 PyYAML
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


CHUNK_TYPE = "skill_chunk"
SKILL_DOMAIN = "skill"
CONTENT_LIMIT = 800
MTIME_TOLERANCE = 1e-3


class SkillsDirNotFoundError(RuntimeError):
    """解析不到任何存在的技能目录时抛出。

    调用方必须据此中止运行：把「目录缺失」当成「目录为空」会让孤儿清理
    删除库中全部 ``skill_chunk`` 节点。
    """


def _env_home(env: dict[str, str]) -> str | None:
    """``HERMES_HOME``（去空白）或 None；路径已展开 ``~`` / ``$VAR``。"""
    raw = (env.get("HERMES_HOME") or "").strip()
    if not raw:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))


def _platform_default_home(env: dict[str, str]) -> str:
    """与 Hermes 一致的平台默认根（Windows: %LOCALAPPDATA%/hermes；其余: ~/.hermes）。"""
    if sys.platform == "win32":
        local_appdata = (env.get("LOCALAPPDATA") or "").strip()
        base = local_appdata or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "hermes")
    return os.path.join(os.path.expanduser("~"), ".hermes")


def _hermes_root(env: dict[str, str]) -> str:
    """Hermes 根目录（含 ``profiles/``、``skills/`` 的那一层）。

    ``HERMES_HOME`` 在档案模式下指向 ``<root>/profiles/<name>``——此时上溯两级到
    根；否则 ``HERMES_HOME`` 自身即根。未设置时回落平台默认根。
    """
    home = _env_home(env)
    if home is None:
        return os.path.abspath(_platform_default_home(env))
    # <root>/profiles/<name> → <root>；仅当末段是 profiles 的**父辈**结构才上溯，
    # 避免把恰好叫 profiles 的普通目录误判（要求父目录名确为 profiles）。
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def skills_dir_candidates(explicit: str | None = None, env: dict[str, str] | None = None) -> list[str]:
    """按优先级返回候选技能目录（均已绝对化、去重，保持顺序）。

    顺序：显式 ``--skills-dir`` → ``<root>/profiles/<profile>/skills``（仅当设了
    ``HERMES_PROFILE`` 且 ``HERMES_HOME`` 未直接指向该档案目录）→ ``<HERMES_HOME>/skills``
    → 平台默认 ``<root>/skills``。
    """
    env = dict(os.environ if env is None else env)
    candidates: list[str] = []

    def _add(path: str) -> None:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        if path not in candidates:
            candidates.append(path)

    if explicit:
        _add(explicit)
        return candidates

    home = _env_home(env)
    root = _hermes_root(env)
    profile = (env.get("HERMES_PROFILE") or "").strip()

    # 档案目录优先，但仅当 HERMES_HOME 没有已经指向它（否则会与下一项重复，_add 已去重）。
    if profile:
        profile_dir = os.path.join(root, "profiles", profile, "skills")
        if home is None or os.path.abspath(home) != os.path.abspath(os.path.dirname(os.path.dirname(profile_dir))):
            _add(profile_dir)
    if home:
        _add(os.path.join(home, "skills"))
    _add(os.path.join(root, "skills"))
    return candidates


def resolve_skills_dir(explicit: str | None = None, env: dict[str, str] | None = None) -> str:
    """解析技能目录：返回第一个**存在**的候选。

    一个都不存在时抛 :class:`SkillsDirNotFoundError`（附全部已尝试路径），
    绝不返回一个不存在的路径让调用方按空目录继续。
    """
    candidates = skills_dir_candidates(explicit=explicit, env=env)
    for path in candidates:
        if os.path.isdir(path):
            return path
    tried = "；".join(candidates)
    raise SkillsDirNotFoundError(f"未找到任何存在的技能目录（已尝试：{tried}）")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """返回 (YAML frontmatter 内容, frontmatter 之后的正文)。"""
    lines = text.splitlines()
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :]).strip()
    return "", text.strip()


def _strip_yaml_comment(value: str) -> str:
    """移除 YAML 标量末尾的注释，同时保留引号内的 #。"""
    quote = ""
    escaped = False
    for i, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in ("'", '"'):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value.rstrip()


def _parse_yaml_scalar(value: str) -> Any:
    """解析技能 frontmatter 中 name/description 所需的 YAML 标量。"""
    value = _strip_yaml_comment(value.strip())
    if not value:
        return ""
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001 - 回退为去除外层引号的兼容解析
            return value[1:-1]
    lowered = value.lower()
    if lowered in ("null", "~"):
        return ""
    if lowered in ("true", "false"):
        return lowered == "true"
    return value


def _block_scalar(lines: list[str], start: int, style: str) -> tuple[str, int]:
    """解析 YAML ``|`` / ``>`` 块标量，返回标量内容和下一行索引。"""
    block: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        block.append(line)
        i += 1

    nonempty = [line for line in block if line.strip()]
    indent = min((len(line) - len(line.lstrip(" \t")) for line in nonempty), default=0)
    stripped = [line[indent:] if line.strip() else "" for line in block]
    if style.startswith(">"):
        # Hermes 技能描述通常是普通段落；折叠换行可避免块标量产生无意义断句。
        paragraphs: list[str] = []
        current: list[str] = []
        for line in stripped:
            if line == "":
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
                paragraphs.append("")
            else:
                current.append(line)
        if current:
            paragraphs.append(" ".join(current))
        while paragraphs and paragraphs[0] == "":
            paragraphs.pop(0)
        while paragraphs and paragraphs[-1] == "":
            paragraphs.pop()
        return "\n".join(paragraphs), i
    return "\n".join(stripped).strip("\n"), i


def _parse_simple_yaml(raw: str) -> dict[str, Any]:
    """无 PyYAML 时的轻量 frontmatter 解析器（仅需要顶层 name/description）。"""
    lines = raw.splitlines()
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
            i += 1
            continue
        match = re.match(r"^([^:#][^:]*):(?:\s*(.*))?$", line)
        if not match:
            i += 1
            continue
        key = match.group(1).strip()
        value = (match.group(2) or "").strip()
        i += 1
        if key not in ("name", "description"):
            continue
        if value in ("|", "|-", "|+", ">", ">-", ">+"):
            parsed, i = _block_scalar(lines, i, value)
            result[key] = parsed
        else:
            result[key] = _parse_yaml_scalar(value)
    return result


def _load_frontmatter(raw: str) -> dict[str, Any]:
    """读取 frontmatter 映射；解析失败时退化为空映射，不阻断其他技能。"""
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                return loaded
        except Exception:  # noqa: BLE001, S110 - 轻量解析器作为无依赖兜底
            pass
    return _parse_simple_yaml(raw)


def _parse_skill_file(fp: str) -> dict[str, Any]:
    """读取并解析一个 SKILL.md，返回索引 payload 所需字段。"""
    with open(fp, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    frontmatter, body = _split_frontmatter(text)
    metadata = _load_frontmatter(frontmatter)
    name = metadata.get("name")
    if name is None or name == "":
        name = os.path.splitext(os.path.basename(fp))[0]
    description = metadata.get("description", "")
    if description is None:
        description = ""
    body = body[:CONTENT_LIMIT]
    return {
        "type": CHUNK_TYPE,
        "domain": SKILL_DOMAIN,
        "name": str(name),
        "description": str(description),
        "category": "",  # 由调用方按技能根目录补全
        "source_path": os.path.abspath(fp),
        "source_mtime": os.path.getmtime(fp),
        "content": f"{name!s}\n{description!s}\n{body}",
    }


def _skill_files(skills_dir: str) -> list[str]:
    """递归查找所有文件名严格为 SKILL.md 的技能文件，按绝对路径稳定排序。

    目录不存在时抛 :class:`SkillsDirNotFoundError`：把「缺失」当「为空」会让孤儿清理
    删除库中全部 skill_chunk 节点。目录存在但没有任何 SKILL.md 时正常返回空列表
    （这是合法的「技能目录为空」状态）。
    """
    if not os.path.isdir(skills_dir):
        raise SkillsDirNotFoundError(f"技能目录不存在：{skills_dir}")
    files: list[str] = []
    for root, dirs, names in os.walk(skills_dir):
        dirs.sort()
        if "SKILL.md" in names:
            files.append(os.path.abspath(os.path.join(root, "SKILL.md")))
    return sorted(files)


def _load_existing_index(store) -> dict[str, tuple[float | None, int]]:
    """建立 {绝对 source_path: (source_mtime, node_id)} 映射。"""
    mapping: dict[str, tuple[float | None, int]] = {}
    for nid, payload in store.iter_payloads():
        if payload.get("type") != CHUNK_TYPE:
            continue
        source_path = payload.get("source_path")
        if not source_path:
            continue
        source_path = os.path.abspath(os.fspath(source_path))
        mtime = payload.get("source_mtime")
        try:
            mtime = float(mtime) if mtime is not None else None
        except (TypeError, ValueError):
            mtime = None
        # 一个 SKILL.md 只应有一个节点；保留首个节点，重复数据由调用方清理。
        mapping.setdefault(source_path, (mtime, nid))
    return mapping


def _cleanup_orphans(store, existing: dict, known_paths: set[str]) -> int:
    """删除库中存在但磁盘源文件已不存在的 skill_chunk 节点。"""
    deleted = 0
    for source_path, (_mtime, nid) in existing.items():
        if source_path in known_paths:
            continue
        try:
            store.delete_node(nid)
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - 单个孤儿失败不应阻断其他清理
            print(f"[清理失败] {source_path}: {exc}")
    return deleted


def _upsert_skill(store, payload: dict[str, Any], existing: dict) -> str:
    """写入单个技能；已存在时更新 payload 和向量并保持 node_id 不变。"""
    source_path = payload["source_path"]
    entry = existing.get(source_path)
    embedding = store.embed_text(payload["content"])
    if entry is not None:
        _mtime, nid = entry
        store.update_payload(nid, payload)
        store.update_vector(nid, embedding)
        return "updated"
    store.insert_node(payload, embedding)
    return "inserted"


def build(skills_dir: str | None = None, store=None, full: bool = False) -> dict[str, Any]:
    """构建技能语义索引。

    ``skills_dir`` 为 None 时按 :func:`resolve_skills_dir` 自动解析（``--skills-dir`` →
    档案目录 → ``HERMES_HOME/skills`` → 平台默认）。
    ``full=False`` 为增量模式：新增或 mtime 变化的文件走 upsert，未变化文件跳过；
    ``full=True`` 强制刷新所有文件。每次运行都执行孤儿清理，并保持已有节点 ID。

    技能目录不存在时抛 :class:`SkillsDirNotFoundError`（在触碰 store 之前即失败），
    确保不会把「目录缺失」当成「目录为空」而清空全库技能节点。
    """
    skills_dir = os.path.abspath(skills_dir) if skills_dir else resolve_skills_dir()
    # 先扫描（目录缺失在此 fatal），确认拿到有效文件列表后再建立 store 连接与清理，
    # 使「目录缺失」不会走到孤儿清理那一步。
    files = _skill_files(skills_dir)
    store = store or TriviumStore()
    existing = _load_existing_index(store)
    known_paths = {os.path.abspath(fp) for fp in files}

    if full:
        pending = list(files)
        skipped = 0
    else:
        skipped = 0
        pending = []
        for fp in files:
            source_path = os.path.abspath(fp)
            current_mtime = os.path.getmtime(fp)
            entry = existing.get(source_path)
            if entry is None:
                pending.append(fp)
                continue
            old_mtime = entry[0]
            if old_mtime is not None and abs(old_mtime - current_mtime) <= MTIME_TOLERANCE:
                skipped += 1
            else:
                pending.append(fp)

    cleaned = _cleanup_orphans(store, existing, known_paths)
    indexed = 0
    failed = 0
    failed_paths: list[str] = []

    for fp in pending:
        source_path = os.path.abspath(fp)
        try:
            payload = _parse_skill_file(fp)
            # category 是相对技能根目录的父目录；根目录下的技能为空串。
            parent = os.path.dirname(source_path)
            category = os.path.relpath(parent, skills_dir).replace("\\", "/")
            if category == ".":
                category = ""
            payload["category"] = category
            _upsert_skill(store, payload, existing)
            indexed += 1
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断整批索引
            failed += 1
            failed_paths.append(source_path)
            print(f"[跳过] 技能索引失败 {source_path}: {exc}")

    total = sum(1 for _nid, payload in store.iter_payloads() if payload.get("type") == CHUNK_TYPE)
    result = {
        "skills_dir": skills_dir,
        "mode": "full" if full else "incremental",
        "indexed": indexed,
        "skipped": skipped,
        "cleaned": cleaned,
        "failed": failed,
        "failed_paths": failed_paths,
        "total": total,
    }
    print(f"技能索引构建完成：索引 {indexed} | 跳过 {skipped} | 清理 {cleaned} | 失败 {failed} | 总节点 {total}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes 技能语义索引构建（默认增量，--full 全量）")
    parser.add_argument(
        "--skills-dir",
        default=None,
        help="技能根目录；省略时自动解析（档案目录 → <HERMES_HOME>/skills → ~/.hermes/skills）",
    )
    parser.add_argument("--full", action="store_true", help="强制刷新所有技能文件")
    args = parser.parse_args()

    try:
        skills_dir = resolve_skills_dir(args.skills_dir)
    except SkillsDirNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        print("提示：用 --skills-dir 显式指定技能目录，或设置 HERMES_HOME / HERMES_PROFILE", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"技能目录: {skills_dir}")
    print(f"模式: {'全量刷新' if args.full else '增量更新（mtime 对比）'}")
    build(skills_dir=skills_dir, full=args.full)


if __name__ == "__main__":
    main()
