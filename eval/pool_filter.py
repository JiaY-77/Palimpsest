"""Pure-function pool filter for eval node selection.

No project imports — stdlib only (re, hashlib).
"""

from __future__ import annotations

import hashlib
import re


def filter_pool(
    nodes: list[dict],
    *,
    exclude_sources: tuple[str, ...] = (),
    drop_duplicates: bool = True,
) -> tuple[list[dict], dict]:
    """Filter a sampling pool of eval nodes.

    Args:
        nodes: [{"node_id": int, "payload": dict}, ...]
        exclude_sources: source values to exclude (exact match on payload["source"]).
        drop_duplicates: if True, deduplicate by sha256 of whitespace-collapsed content.

    Returns:
        (kept_nodes, stats)
        stats keys: input, excluded_source, excluded_duplicate, kept
    """
    stats: dict[str, int] = {
        "input": len(nodes),
        "excluded_source": 0,
        "excluded_duplicate": 0,
        "kept": 0,
    }

    kept: list[dict] = []
    seen_hashes: dict[str, tuple[str, int]] = {}  # sha256 -> (node_id, index in kept)

    for node in nodes:
        payload = node.get("payload", {})
        source = payload.get("source")
        if source and source in exclude_sources:
            stats["excluded_source"] += 1
            continue

        if drop_duplicates:
            content = payload.get("content", "")
            collapsed = re.sub(r"\s+", "", content)
            if not collapsed:
                stats["excluded_duplicate"] += 1
                continue
            h = hashlib.sha256(collapsed.encode("utf-8")).hexdigest()
            nid = node["node_id"]
            if h in seen_hashes:
                prev_nid, prev_idx = seen_hashes[h]
                if nid < prev_nid:
                    # Replace in-place to preserve input order
                    kept[prev_idx] = node
                    seen_hashes[h] = (nid, prev_idx)
                stats["excluded_duplicate"] += 1
                continue
            seen_hashes[h] = (nid, len(kept))

        kept.append(node)

    stats["kept"] = len(kept)
    return kept, stats


def should_write_output(
    success_count: int, existing_items: list, new_items: list
) -> tuple[bool, str]:
    """Decide whether the generated eval set may be written back to file.

    Returns (allow, reason).  ``allow`` is *False* when the write must be
    blocked to protect existing data.
    """
    if success_count <= 0:
        return False, "生成成功数为 0，拒绝覆盖已有题集文件"
    return True, ""
