# -*- coding: utf-8 -*-
"""
              —— 核心冒烟测试 ——
覆盖 Palimpsest 记忆主链路：写入 → 语义检索 → 全文读取 → 图谱建边/邻居
→ 敏感信息拦截 → 混合检索（FTS 侧命中）。

隔离保证：conftest 已把 DB_PATH 指向临时库，全部测试不触碰正式库 data/mh_memory.db。
"""

import json

from mcp_tools import (  # noqa: E402
    graph_neighbors, mem_get_full, mem_hybrid_search, mem_ingest, mem_link,
    mem_search,
)


def _get(result: str) -> dict:
    """MCP 工具返回 JSON 字符串 → dict"""
    return json.loads(result)


def test_ingest_search_roundtrip(db_path):
    """写入一条记忆 → mem_search 能检索到 → mem_get_full 能取全文。"""
    content = "护栏冒烟：小帕在临时库写下的一条独一无二的记忆片段"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is True, r
    nid = r["node_id"]

    # mem_search 应能检索到该节点
    s = _get(mem_search(content, scope="memory"))
    assert s["results"], "mem_search 未返回任何结果"
    assert any(item["id"] == nid for item in s["results"]), (
        f"mem_search 未命中刚写入的节点 {nid}"

    )

    # mem_get_full 应能取回全文
    full = _get(mem_get_full(nid))
    assert full["found"] is True
    assert full["payload"]["content"] == content


def test_link_graph(db_path):
    """写入两条记忆 → mem_link 建边 → graph_neighbors 能看到邻居。"""
    a = _get(mem_ingest(content="图谱冒烟记忆甲：概念 X 与概念 Y 相关联", type="memory"))
    b = _get(mem_ingest(content="图谱冒烟记忆乙：概念 Y 是概念 X 的子集", type="memory"))
    assert a["stored"] and b["stored"]

    link = _get(mem_link(a["node_id"], b["node_id"], relation="RELATED_TO"))
    assert link["linked"] is True

    g = _get(graph_neighbors(a["node_id"], depth=1))
    assert g["count"] >= 1, g
    assert any(item["target_id"] == b["node_id"] for item in g["relations"]), g


def test_secret_scan(db_path):
    """写入含 API key 的内容 → 拒绝入库，stored:False 且命中 openai_key 规则。"""
    content = "这里泄露了一个密钥：sk-abcdefghijklmnopqrstuvwxyz 请勿保存"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is False, r
    assert "openai_key" in r.get("rules", []), r


def test_fts_hybrid(db_path):
    """写入后 mem_hybrid_search 应能命中（FTS 侧 trigram 精确子串）。"""
    needle = "混合检索护栏标记词xyzabc"
    r = _get(mem_ingest(content=needle, type="memory"))
    assert r["stored"] is True
    nid = r["node_id"]

    h = _get(mem_hybrid_search(needle, scope="memory", top_k=5))
    assert h["results"], "mem_hybrid_search 未返回任何结果"
    hit = next((item for item in h["results"] if item.get("id") == nid), None)
    assert hit is not None, f"混合检索未命中刚写入节点 {nid}: {h}"
    # FTS 侧应标记命中（mem_ingest 已同步 index_node 到临时 fts.db）
    assert hit["meta"].get("fts_hit") is True, hit


def test_consolidate_dryrun(db_path):
    """写入两条几乎相同的记忆 → consolidate(dry_run=True) 只预览候选，不真正合并。

    用 store.insert_node 直写（绕过 mem_ingest 的冲突检测会自动把相似旧记忆标
    outdated，导致找不到 active+active 对）。两条内容几乎一致、均为 active，
    应能被 find_similar_pairs 扫出为候选对。
    """
    from core.consolidator import consolidate
    from mcp_tools import store

    base = "容量合并护栏：一条几乎完全相同的记忆内容，用于验证 dry-run 预览候选"
    emb_a = store.embed_text(base)
    emb_b = store.embed_text(base + "（副本甲）")
    a_id = store.insert_node({"type": "memory", "content": base}, emb_a)
    b_id = store.insert_node({"type": "memory", "content": base + "（副本甲）"}, emb_b)
    assert a_id != b_id

    r = consolidate(store, dry_run=True)
    assert r["dry_run"] is True, r
    assert isinstance(r.get("candidates"), list), r
    assert r["candidates"], "高相似记忆应产生至少一对候选"

    # 单个候选对结构完整（a/b/score/a_imp/b_imp/a_content/b_content）
    c = r["candidates"][0]
    for k in ("a", "b", "score", "a_imp", "b_imp", "a_content", "b_content"):
        assert k in c, f"候选缺字段 {k}: {c}"
    assert {c["a"], c["b"]} == {a_id, b_id}, c

    # dry_run 不真正合并：不应包含 merged 结果，两个原始节点仍在库中（active）
    assert "merged" not in r, r
    assert store.get_node(a_id)["payload"]["status"] == "active"
    assert store.get_node(b_id)["payload"]["status"] == "active"


def test_task_archive(db_path):
    """已完成任务节点自动归档：dry-run 只预览；apply 写归档 md + 删节点。

    用 store.insert_node 直写 3 个 domain=task 节点（绕过 mem_ingest 冲突检测，
    与 test_consolidate_dryrun 同风格）：
      ① status=completed（type=task）
      ② content 含「已完成」（type=plan）
      ③ 未完成（type=task，content 含「待启动」）
    归档到独立临时知识库目录（05_任务归档/），不碰真实知识库。
    """
    import os

    from core.task_archive import archive_tasks
    from mcp_tools import store

    emb = store.embed_text("任务归档冒烟向量")
    a_id = store.insert_node({
        "type": "task",
        "character_name": "task",
        "content": "冒烟任务甲：完成状态直写节点，验证 status 级归档判定",
        "importance": 0.7,
    }, emb)
    store.update_payload(a_id, {**store.get_node(a_id)["payload"], "status": "completed"})
    b_id = store.insert_node({
        "type": "plan",
        "character_name": "task",
        "content": "冒烟任务乙：优化流程已完成，验证内容级归档判定",
        "importance": 0.5,
    }, emb)
    c_id = store.insert_node({
        "type": "task",
        "character_name": "task",
        "content": "冒烟任务丙：待启动，尚未开工，属未完成任务",
        "importance": 0.3,
    }, emb)

    # 独立临时知识库目录（与正式 KNOWLEDGE_DIR 完全隔离）
    kb_dir = os.path.join(os.path.dirname(db_path), "kb_archive")

    # ---- dry_run：只预览，不写文件、不删节点 ----
    r = archive_tasks(store, dry_run=True, knowledge_dir=kb_dir)
    assert r["dry_run"] is True, r
    cands = r["candidates"]
    assert len(cands) == 2, f"应识别 2 个已完成任务: {cands}"
    assert {c["id"] for c in cands} == {a_id, b_id}, cands
    assert c_id not in {c["id"] for c in cands}, cands
    for c in cands:
        assert c["title"], f"候选缺标题: {c}"
        assert c["target_path"].endswith(".md"), c
        assert "05_任务归档" in c["target_path"], c
    assert r["archived"] == [] and r["errors"] == [], r
    assert r["skipped"] == 1, r
    assert not os.path.exists(os.path.join(kb_dir, "05_任务归档")), "dry-run 不应写归档目录"
    assert store.get_node(a_id) is not None and store.get_node(b_id) is not None, "dry-run 不应删节点"

    # ---- apply：写归档 md + 删节点 ----
    r2 = archive_tasks(store, dry_run=False, knowledge_dir=kb_dir)
    assert r2["dry_run"] is False, r2
    assert len(r2["archived"]) == 2, r2
    assert r2["errors"] == [], r2
    assert store.get_node(a_id) is None, "已归档节点应被删除"
    assert store.get_node(b_id) is None, "已归档节点应被删除"
    assert store.get_node(c_id) is not None, "未完成任务不应被删除"
    assert r2["skipped"] == 1, r2

    archive_dir = os.path.join(kb_dir, "05_任务归档")
    files = [f for f in os.listdir(archive_dir) if f.endswith(".md")]
    assert len(files) == 2, f"应生成 2 个归档 md: {files}"
    texts = []
    for fname in files:
        with open(os.path.join(archive_dir, fname), encoding="utf-8") as f:
            texts.append(f.read())
    joined = "\n".join(texts)
    assert "冒烟任务甲：完成状态直写节点" in joined, "归档内容应含任务①原文"
    assert "冒烟任务乙：优化流程已完成" in joined, "归档内容应含任务②原文"


def test_startup_check_embedding():
    """startup-check 应包含「Embedding 服务可用」检查项。

    本机可能没在跑 Ollama（ok 可为 False），测试只验证：有这一项、字段存在、类型正确、
    且不抛异常。Ollama 在跑则应为 True，不在跑为 False。
    """
    from core.startup_check import run_startup_check

    result = run_startup_check()
    assert isinstance(result, dict) and "checks" in result, result
    names = [c["name"] for c in result["checks"]]
    assert any("Embedding" in n for n in names), f"缺少 Embedding 检查项: {names}"

    item = next(c for c in result["checks"] if "Embedding" in c["name"])
    assert isinstance(item, dict)
    assert "ok" in item and isinstance(item["ok"], bool), item
    assert "detail" in item and isinstance(item["detail"], str), item


def test_block_validation():
    """出厂默认不含 personal block（novel/work），只含通用 block。"""
    from core.trivium_store import is_valid_block

    assert is_valid_block("task") is True
    assert is_valid_block("kb") is True
    assert is_valid_block("hermes") is True
    assert is_valid_block("general") is True
    assert is_valid_block("") is True
    assert is_valid_block("novel") is False
    assert is_valid_block("work") is False
    assert is_valid_block("随便") is False


def test_block_custom_domain_not_blocked():
    """自定义 domain 作为 block 应放行（提示而非拦截退出）——CLI _validate_block 行为。"""
    from scripts.palimpsest_cli import _validate_block

    # 内置 block 原样返回，无提示
    assert _validate_block("task") == "task"
    # 自定义 domain 也原样返回（放行），不抛异常、不退出
    assert _validate_block("myproject") == "myproject"
    assert _validate_block("") == ""
