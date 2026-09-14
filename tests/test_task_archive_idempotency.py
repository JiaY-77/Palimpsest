"""
任务归档幂等回归
================
真实故障路径：归档 md 写入成功 → `store.delete_node` 失败（进程被杀 / 库锁冲突）
→ 节点仍在库里、文件已落盘 → 下次归档再写一份，产出 `_2` 重复文件。

本文件锁三条：
  1. 归档 md 带 YAML frontmatter 的 `node_id` 幂等键；
  2. 重跑归档复用已有文件（不产生 `_2`），并把残留节点删掉，结果标 `already_archived=True`；
  3. dry-run 预览与执行决策一致 —— 已归档的候选同样标 `already_archived`，不提议新文件。

隔离：归档目录用 `tmp_path`；conftest 已把 DB_PATH 指向 session 临时库，且断言只针对
本测试自己插入的 node_id，不受库里其他测试残留任务节点的影响。
"""

import os
import re

from mcp_tools import store


def _archives_for(node_id: int, archive_dir: str) -> list[str]:
    """返回归档目录里 frontmatter 声明了该 node_id 的文件名（按名排序）。"""
    hits: list[str] = []
    if not os.path.isdir(archive_dir):
        return hits
    for fname in sorted(os.listdir(archive_dir)):
        if not fname.endswith(".md"):
            continue
        with open(os.path.join(archive_dir, fname), encoding="utf-8") as f:
            head = f.read(1024)
        if re.search(rf"^node_id:\s*{node_id}\s*$", head, re.MULTILINE):
            hits.append(fname)
    return hits


def _insert_completed(content: str) -> int:
    """直插一个「已完成」task 域节点（绕过 mem_ingest 冲突检测，与冒烟测试同风格）。

    注意：`store.insert_node` 固定写 `status="active"`（extra_fields 只补 payload 里
    没有的键），因此完成状态必须在插入后 `update_payload` 补写。
    """
    nid = store.insert_node({
        "type": "task",
        "domain": "task",
        "character_name": "task",
        "content": content,
        "importance": 0.6,
    }, store.embed_text(content))
    store.update_payload(nid, {**store.get_node(nid)["payload"], "status": "completed"})
    return nid


def test_archive_rerun_reuses_file_and_removes_leftover(tmp_path, monkeypatch):
    from core.task_archive import archive_tasks

    kb_dir = str(tmp_path / "kb")
    content = "幂等任务甲：归档文件写入成功但删节点失败"
    nid = _insert_completed(content)
    archive_dir = os.path.join(kb_dir, "05_任务归档")

    real_delete = store.delete_node
    state = {"failed": False}

    def _flaky_delete(node_id):
        """只对本测试的节点失败一次，模拟「写成功、删节点中断」。"""
        if node_id == nid and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("删除探针失败")
        return real_delete(node_id)

    monkeypatch.setattr(store, "delete_node", _flaky_delete)

    # ---- 第一次执行：文件写入成功、删节点失败 → 节点残留 ----
    r1 = archive_tasks(store, dry_run=False, knowledge_dir=kb_dir)
    mine1 = _archives_for(nid, archive_dir)
    assert len(mine1) == 1, f"首次归档应写 1 份: {os.listdir(archive_dir)}"
    assert store.get_node(nid) is not None, "删除失败后节点应仍在库里"
    assert any("删除节点失败" in e["error"] for e in r1["errors"] if e["id"] == nid), r1

    # ---- dry-run 预览：已归档 → 复用原文件，不提议新文件 ----
    r2 = archive_tasks(store, dry_run=True, knowledge_dir=kb_dir)
    cand = [c for c in r2["candidates"] if c["id"] == nid]
    assert len(cand) == 1, r2
    assert cand[0]["already_archived"] is True, cand
    assert cand[0]["target_path"] == os.path.join(archive_dir, mine1[0]), cand

    # ---- 重跑执行：复用文件 + 删掉残留节点 ----
    r3 = archive_tasks(store, dry_run=False, knowledge_dir=kb_dir)
    assert _archives_for(nid, archive_dir) == mine1, "重跑不得产生重复归档文件"
    assert store.get_node(nid) is None, "重跑应把残留节点删掉"
    mine3 = [a for a in r3["archived"] if a["id"] == nid]
    assert len(mine3) == 1, r3
    assert mine3[0]["already_archived"] is True, mine3
    assert [e for e in r3["errors"] if e["id"] == nid] == [], r3


def test_archive_md_carries_frontmatter_node_id(tmp_path):
    from core.task_archive import archive_tasks

    kb_dir = str(tmp_path / "kb")
    content = "幂等任务乙：frontmatter 幂等键"
    nid = _insert_completed(content)

    archive_tasks(store, dry_run=False, knowledge_dir=kb_dir)

    archive_dir = os.path.join(kb_dir, "05_任务归档")
    hits = _archives_for(nid, archive_dir)
    assert len(hits) == 1, hits
    with open(os.path.join(archive_dir, hits[0]), encoding="utf-8") as f:
        text = f.read()
    assert text.startswith("---\n"), text[:80]
    frontmatter = text.split("---", 2)[1]
    assert f"node_id: {nid}" in frontmatter, frontmatter
    assert "archived_at:" in frontmatter, frontmatter
    assert content in text, "归档正文应保留任务原文"
