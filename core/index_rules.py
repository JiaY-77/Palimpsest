"""
声明式索引规则加载层
====================
为 build_novel_index / build_kb_index 等索引脚本提供统一的规则加载、
路径匹配与分块策略，实现「配置驱动 + 内置默认」的解耦设计。

核心接口：
  - load_rules(root, explicit, legacy_filename) → IndexRules
  - match_kind(rel_path, rules) → str
  - is_included(rel_path, is_dir, rules) → bool
  - chunk_markdown(text, rules) → list[str]
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RULES_FILENAME = ".palimpsest-index.json"
LEGACY_RULES_FILENAME = ".palimpsest-index.yaml"

VALID_CHUNK_STRATEGIES = ("file", "heading", "chars")


class IndexRulesError(ValueError):
    """规则加载或校验失败时抛出。"""


@dataclass(frozen=True)
class Rule:
    """kind_map 中的单条匹配规则。"""
    kind: str
    filename: str | None = None
    dir_prefix: str | None = None
    glob: str | None = None


@dataclass(frozen=True)
class IndexRules:
    """完整的索引规则配置。"""
    kind_map: tuple[Rule, ...] = ()
    default_kind: str = "default"
    chunk_strategy: str = "file"
    min_chunk_len: int = 300
    max_chunk_len: int = 800
    domain: str | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    require_frontmatter_id: bool = False
    source: str = "builtin"
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
#  内置默认规则（等价于原硬编码逻辑）
# ---------------------------------------------------------------------------
_BUILTIN_EXCLUDE = ("03_章节", "04_草稿", ".obsidian")


def _builtin_rules() -> IndexRules:
    return IndexRules(
        kind_map=(),
        default_kind="default",
        chunk_strategy="file",
        min_chunk_len=300,
        max_chunk_len=800,
        exclude=_BUILTIN_EXCLUDE,
        source="builtin",
    )


# 内置默认规则按用途分两份：
#   NOVEL_BUILTIN_RULES —— 创作 vault：整文件入库 + 排除 03_章节/04_草稿/.obsidian。
#   KB_BUILTIN_RULES    —— 知识库：按 ## / ### 标题切 300~800 字符块，只排除 .obsidian。
# 两份的 default_kind 都是 "default"：**解析不出 kind 就是「未匹配」，不伪装成某个业务
# kind**（旧实现用 `return "character"` 兜底，把「不知道」写成了「知道」，77 张角色卡被
# 误判为 setting 时全程静默——这是本条规则存在的根因）。
NOVEL_BUILTIN_RULES = _builtin_rules()
KB_BUILTIN_RULES = IndexRules(
    kind_map=(),
    default_kind="default",
    chunk_strategy="heading",
    min_chunk_len=300,
    max_chunk_len=800,
    exclude=(".obsidian",),
    source="builtin",
)


# ---------------------------------------------------------------------------
#  校验辅助
# ---------------------------------------------------------------------------

def _validate_chunk_strategy(val: str) -> None:
    if val not in VALID_CHUNK_STRATEGIES:
        raise IndexRulesError(
            f"chunk_strategy 取值非法: {val!r}，仅支持 {VALID_CHUNK_STRATEGIES}"
        )


def _validate_kind_map(entries: list[Any]) -> list[Rule]:
    rules: list[Rule] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise IndexRulesError(f"kind_map[{i}] 不是对象: {type(entry).__name__}")
        if "kind" not in entry:
            raise IndexRulesError(f"kind_map[{i}] 缺少必填字段 'kind'")
        kind = entry["kind"]
        if not isinstance(kind, str) or not kind:
            raise IndexRulesError(f"kind_map[{i}].kind 非法: {kind!r}")
        rules.append(Rule(
            kind=kind,
            filename=entry.get("filename"),
            dir_prefix=entry.get("dir_prefix"),
            glob=entry.get("glob"),
        ))
    return rules


# ---------------------------------------------------------------------------
#  JSON 加载
# ---------------------------------------------------------------------------

def _load_json_file(path: str | os.PathLike) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise IndexRulesError(f"JSON 解析失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise IndexRulesError(f"配置文件顶层应为对象，实际为 {type(data).__name__}")
    return data


def _build_rules_from_dict(
    data: dict[str, Any],
    source: str,
) -> IndexRules:
    """从 dict 构建 IndexRules，校验类型与取值。"""
    warnings: list[str] = []
    known_keys = {
        "kind_map", "default_kind", "chunk_strategy",
        "min_chunk_len", "max_chunk_len", "domain",
        "include", "exclude", "require_frontmatter_id",
    }
    for key in data:
        if key not in known_keys:
            warnings.append(f"未知配置键 '{key}' 已忽略（向前兼容）")

    kind_map: tuple[Rule, ...] = ()
    if "kind_map" in data:
        km = data["kind_map"]
        if not isinstance(km, list):
            raise IndexRulesError("kind_map 应为数组")
        kind_map = tuple(_validate_kind_map(km))

    default_kind = data.get("default_kind", "default")
    if not isinstance(default_kind, str) or not default_kind:
        raise IndexRulesError(f"default_kind 非法: {default_kind!r}")

    chunk_strategy = data.get("chunk_strategy", "file")
    if not isinstance(chunk_strategy, str):
        raise IndexRulesError(f"chunk_strategy 类型非法: {type(chunk_strategy).__name__}")
    _validate_chunk_strategy(chunk_strategy)

    min_chunk_len = data.get("min_chunk_len", 300)
    if not isinstance(min_chunk_len, int) or min_chunk_len < 0:
        raise IndexRulesError(f"min_chunk_len 非法: {min_chunk_len!r}")

    max_chunk_len = data.get("max_chunk_len", 800)
    if not isinstance(max_chunk_len, int) or max_chunk_len < 0:
        raise IndexRulesError(f"max_chunk_len 非法: {max_chunk_len!r}")

    if max_chunk_len < min_chunk_len:
        raise IndexRulesError(
            f"max_chunk_len({max_chunk_len}) < min_chunk_len({min_chunk_len})"
        )

    domain: str | None = data.get("domain")
    if domain is not None and (not isinstance(domain, str) or not domain):
        raise IndexRulesError(f"domain 非法: {domain!r}")

    include: tuple[str, ...] = ()
    if "include" in data:
        inc = data["include"]
        if not isinstance(inc, list) or not all(isinstance(s, str) for s in inc):
            raise IndexRulesError("include 应为字符串数组")
        include = tuple(inc)

    exclude: tuple[str, ...] = ()
    if "exclude" in data:
        exc = data["exclude"]
        if not isinstance(exc, list) or not all(isinstance(s, str) for s in exc):
            raise IndexRulesError("exclude 应为字符串数组")
        exclude = tuple(exc)

    require_frontmatter_id = bool(data.get("require_frontmatter_id", False))

    return IndexRules(
        kind_map=kind_map,
        default_kind=default_kind,
        chunk_strategy=chunk_strategy,
        min_chunk_len=min_chunk_len,
        max_chunk_len=max_chunk_len,
        domain=domain,
        include=include,
        exclude=exclude,
        require_frontmatter_id=require_frontmatter_id,
        source=source,
        warnings=tuple(warnings),
    )


def load_rules(
    root: str | os.PathLike | None = None,
    explicit: str | os.PathLike | None = None,
    legacy_filename: str | None = None,
    builtin: IndexRules | None = None,
) -> IndexRules:
    """加载索引规则。

    优先级：explicit > <root>/<legacy_filename> > <root>/.palimpsest-index.json > 内置默认。

    builtin 指定「无任何配置文件时」返回的内置默认（不同索引脚本的默认策略不同：
    创作 vault 整文件 / 知识库按标题切片），缺省取 NOVEL_BUILTIN_RULES。
    """
    if builtin is None:
        builtin = NOVEL_BUILTIN_RULES
    warnings: list[str] = []

    # 1. explicit（命令行 --rules 路径）
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise IndexRulesError(f"指定的规则文件不存在: {explicit}")
        data = _load_json_file(p)
        return _build_rules_from_dict(data, source=str(p))

    if root is None:
        return builtin

    root_path = Path(root)

    # 2. legacy filename（库根下的旧约定文件名）
    if legacy_filename:
        legacy_path = root_path / legacy_filename
        if legacy_path.is_file():
            data = _load_json_file(legacy_path)
            return _build_rules_from_dict(data, source=str(legacy_path))

    # 3. .palimpsest-index.yaml 检测（不解析，只警告）
    yaml_path = root_path / LEGACY_RULES_FILENAME
    if yaml_path.is_file():
        warnings.append(
            f"{LEGACY_RULES_FILENAME} 格式不再支持，请改用 {DEFAULT_RULES_FILENAME}"
        )

    # 4. .palimpsest-index.json
    json_path = root_path / DEFAULT_RULES_FILENAME
    if json_path.is_file():
        data = _load_json_file(json_path)
        rules = _build_rules_from_dict(data, source=str(json_path))
        return dataclasses.replace(rules, warnings=rules.warnings + tuple(warnings))

    # 5. 内置默认
    return dataclasses.replace(builtin, warnings=tuple(warnings))


# ---------------------------------------------------------------------------
#  路径匹配
# ---------------------------------------------------------------------------

def match_kind(rel_path: str, rules: IndexRules) -> str:
    """按声明顺序逐条匹配 kind_map，全不命中返回 default_kind。"""
    norm = rel_path.replace("\\", "/")
    basename = norm.rsplit("/", 1)[-1] if "/" in norm else norm

    for rule in rules.kind_map:
        matched = True

        if rule.filename is not None and basename != rule.filename:
            matched = False

        if rule.dir_prefix is not None:
            prefix = rule.dir_prefix
            if not (norm == prefix
                    or norm.startswith(prefix + "/")
                    or ("/" + prefix + "/") in norm
                    or norm.endswith("/" + prefix)):
                matched = False

        if rule.glob is not None and not fnmatch.fnmatchcase(norm, rule.glob):
            matched = False

        if matched:
            return rule.kind

    return rules.default_kind


def is_included(rel_path: str, is_dir: bool, rules: IndexRules) -> bool:
    """判断 rel_path 是否被规则包含（用于目录/文件过滤）。"""
    norm = rel_path.replace("\\", "/")
    basename = norm.rsplit("/", 1)[-1] if "/" in norm else norm

    # exclude 检查（目录与文件都适用）
    for pattern in rules.exclude:
        if fnmatch.fnmatchcase(basename, pattern) or fnmatch.fnmatchcase(norm, pattern):
            return False

    if is_dir:
        return True

    # 文件：include 非空时必须命中某个 include
    if rules.include:
        for pattern in rules.include:
            if fnmatch.fnmatchcase(basename, pattern) or fnmatch.fnmatchcase(norm, pattern):
                return True
        return False

    return True


# ---------------------------------------------------------------------------
#  Markdown 分块（从 build_kb_index.split_markdown 迁移）
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*$")


def chunk_markdown(text: str, rules: IndexRules) -> list[str]:
    """按规则的 chunk_strategy 对 markdown 文本分块。"""
    strategy = rules.chunk_strategy
    min_len = rules.min_chunk_len
    max_len = rules.max_chunk_len

    if strategy == "file":
        body = _strip_frontmatter(text)
        body = body.strip()
        return [body] if body else []

    # heading / chars 共享前段：跳过 frontmatter → 按 ##/### 切段
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break

    if strategy == "heading":
        sections: list[str] = []
        cur: list[str] = []
        for line in lines:
            if re.match(r"^#{2,3}\s", line):
                if cur:
                    sections.append("\n".join(cur))
                cur = [line]
            else:
                cur.append(line)
        if cur:
            sections.append("\n".join(cur))
    else:
        # chars: 整段切，按标题先分段再按 max_len 切
        sections = ["\n".join(lines)]

    # 超长段按行切块
    chunks: list[str] = []
    for sec in sections:
        if len(sec) <= max_len:
            chunks.append(sec)
            continue
        buf: list[str] = []
        buf_len = 0
        for line in sec.splitlines():
            buf.append(line)
            buf_len += len(line) + 1
            if buf_len >= max_len:
                chunks.append("\n".join(buf))
                buf = []
                buf_len = 0
        if buf:
            chunks.append("\n".join(buf))

    # 相邻过小块合并
    merged: list[str] = []
    for c in chunks:
        if (merged
                and len(merged[-1]) < min_len
                and len(merged[-1]) + len(c) <= max_len):
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)

    return [c.strip() for c in merged if c and c.strip()]


def _strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter，返回正文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:])
    return text
