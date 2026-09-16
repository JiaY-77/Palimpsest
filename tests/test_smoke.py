"""
              —— 核心冒烟测试 ——
覆盖 Palimpsest 记忆主链路：写入 → 语义检索 → 全文读取 → 图谱建边/邻居
→ 敏感信息拦截 → 混合检索（FTS 侧命中）。

隔离保证：conftest 已把 DB_PATH 指向临时库，全部测试不触碰正式库 data/mh_memory.db。
"""

import contextlib
import json
import os
import shutil
import tempfile

import pytest

from config import Config
from core.trivium_store import TriviumStore
from mcp_tools import (
    graph_neighbors,
    mem_get_full,
    mem_hybrid_search,
    mem_ingest,
    mem_link,
    mem_search,
)


def _get(result: str) -> dict:
    """MCP 工具返回 JSON 字符串 → dict"""
    return json.loads(result)


@pytest.fixture
def iso_consolidator_store():
    """consolidate 事务测试专用独立临时库（不污染会话级共享库）。

    mkdtemp + 临时把 Config.DB_PATH 指到独立库文件，结束即删；embedding 走
    conftest 的类级 fake（TriviumStore.embed_text 已在 session 夹具里替换），
    不需要每次手动赋 _fake_embed。
    """
    tmp = tempfile.mkdtemp(prefix="palimpsest_consolidator_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "consolidator.db")
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_domain_unified(db_path):
    """写入侧统一 + 读侧统一（2026-08-29 记忆领域无二义性）：
    mem_ingest 的 domain 只写到 payload.domain（character_name 兼容镜像已退役）；
    node_domain(payload) 返回 domain；mem_search 按 domain 过滤能命中。
    """
    from core.trivium_store import node_domain
    from mcp_tools import store

    content = "记忆领域防二义性护栏：domain 字段统一冒烟测试内容"
    r = _get(mem_ingest(content=content, type="memory", domain="testdom"))
    assert r["stored"] is True, r
    nid = r["node_id"]

    payload = store.get_node(nid)["payload"]
    assert payload.get("domain") == "testdom", f"payload 应写 domain: {payload}"
    # 兼容镜像已退役：新写入不应再带 character_name（旧库读侧仍可回退）
    assert "character_name" not in payload, f"新写入不应再带 character_name: {payload}"
    assert node_domain(payload) == "testdom", node_domain(payload)

    # 读侧按 domain 过滤能命中刚写入的节点
    s = _get(mem_search(content, scope="memory", domain="testdom"))
    assert any(item["id"] == nid for item in s["results"]), s
    # domain 过滤反向：错误 domain 查不到该节点
    s2 = _get(mem_search(content, scope="memory", domain="hermes"))
    assert not any(item["id"] == nid for item in s2["results"]), s2


def test_ingest_search_roundtrip(db_path):
    """写入一条记忆 → mem_search 能检索到 → mem_get_full 能取全文。"""
    content = "护栏冒烟：Palimpsest 在临时库写下的一条独一无二的记忆片段"
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


def test_secret_scan_weak_phone(db_path):
    """写入含手机号 → 弱规则放行，stored:True，且 payload 含 secret_hint 含 phone。"""
    content = "客户联系方式 13800138000 请参考办理"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is True, r
    assert "phone" in r.get("secret_hint", []), r
    nid = r["node_id"]
    full = _get(mem_get_full(nid))
    assert "phone" in full["payload"].get("secret_hint", []), full


def test_secret_scan_weak_idcard(db_path):
    """写入含 18 位身份证格式数字 → 弱规则放行，stored:True，secret_hint 含 id_card。"""
    content = "登记信息 11010119900307749X 已录入系统"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is True, r
    assert "id_card" in r.get("secret_hint", []), r
    nid = r["node_id"]
    full = _get(mem_get_full(nid))
    assert "id_card" in full["payload"].get("secret_hint", []), full


def test_secret_scan_strong_wins_over_weak(db_path):
    """一条文本同时含手机号 + API key：强规则优先、整体拒绝（stored:False）。

    实测（Issue 3 #5）：拒绝由强规则决定——rules 含 openai_key、不含 phone，
    error 含「敏感信息」与规则名关键字段；弱命中仅体现在扫描分类，不覆盖强拒。
    """
    content = "13800138000 sk-abcdefghijklmnopqrstuvwxyz123 请勿外泄"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is False, r
    rules = r.get("rules", [])
    assert "openai_key" in rules, rules
    assert "phone" not in rules, rules
    assert "敏感信息" in r.get("error", ""), r
    assert "openai_key" in r.get("error", ""), r


def test_secret_scan_weak_hint_shape(db_path):
    """弱规则命中时 secret_hint 形状 = 规则名列表（精确断言，含入库原样保留）。"""
    content = "13800138000"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is True, r
    assert r.get("secret_hint") == ["phone"], r
    nid = r["node_id"]
    full = _get(mem_get_full(nid))
    assert full["payload"].get("secret_hint") == ["phone"], full


def test_fts_check(db_path):
    """临时库写入后 check_fts_consistency 应判定主库与 FTS 索引一致。"""
    from mcp_tools import store
    from scripts.check_fts_consistency import check

    for i in range(3):
        mem_ingest(content=f"巡检护栏记忆片段编号{i:02d}内容唯一", type="memory")

    result = check(store)
    assert result["consistent"] is True, result
    assert result["total_nodes"] >= 3, result
    assert result["total_nodes"] == result["fts_count"], result


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


def test_task_archive(tmp_path):
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
    # 注意：用 pytest 的 tmp_path 而不是从 db_path 派生——DB_PATH 可能被 eval
    # 模块改成 eval/.tmp 下的持久副本，上一轮 apply 留下的 05_任务归档 会让本轮
    # dry-run 的「不应写目录」断言必红（套件第二次运行就失败）。
    kb_dir = str(tmp_path / "kb_archive")

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
    """出厂内置 block 为通用区块（task/kb/hermes/general/novel，work 不在列）。

    注：novel 已于「register novel domain block for fiction corpus import」提交
    注册为小说创作设定区块（领域入库），因此为合法 block；work 仍非出厂区块。
    """
    from core.trivium_store import is_valid_block

    assert is_valid_block("task") is True
    assert is_valid_block("kb") is True
    assert is_valid_block("hermes") is True
    assert is_valid_block("general") is True
    assert is_valid_block("") is True
    assert is_valid_block("novel") is True
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


def test_sanitize_filename_dotdot():
    """_sanitize_filename 路径遍历防护：危险文件名回退 fallback，正常标题不变。"""
    from core.task_archive import _sanitize_filename

    # 路径遍历意图 → 返回 fallback（默认 "task"）
    assert _sanitize_filename("..") == "task"
    assert _sanitize_filename("..secret") == "task"
    assert _sanitize_filename("...foo") == "task"
    assert _sanitize_filename(".") == "task"
    # 显式自定义 fallback
    assert _sanitize_filename("..", fallback="memo_1") == "memo_1"
    # 正常中文标题清洗后不变（含空白则去除空白）
    assert _sanitize_filename("正常标题") == "正常标题"
    assert _sanitize_filename("优化 流程") == "优化流程"
    # 尾部点/空格正常剥离，不触发防护
    assert _sanitize_filename("hello.") == "hello"
    # 中间含 ".." 但非开头 → 不拦截
    assert _sanitize_filename("a..b") == "a..b"


def test_transaction_merge(db_path):
    """consolidate(dry_run=False) 走事务合并路径：合并节点/边/标脏状态正确。"""
    from core.consolidator import consolidate
    from mcp_tools import store

    base = "事务合并护栏：一条用于验证事务合并路径的记忆内容"
    emb_a = store.embed_text(base)
    emb_b = store.embed_text(base + "（副本乙）")
    # importance 一高一低（0.3 / 0.5）且均 < max_importance(0.8)：既非 both_important
    # 也非 high_value，确保真正走合并路径（双方都高会被 _filter_candidates 跳过）
    a_id = store.insert_node(
        {"type": "memory", "content": base, "importance": 0.3}, emb_a)
    b_id = store.insert_node(
        {"type": "memory", "content": base + "（副本乙）", "importance": 0.5}, emb_b)
    assert a_id != b_id

    r = consolidate(store, dry_run=False)
    assert r["dry_run"] is False, r
    assert r["merged"] >= 1, r

    merged_ids = [m for m in r["merged_ids"]
                  if {m["old_a"], m["old_b"]} == {a_id, b_id}]
    assert merged_ids, f"本测试的合并对应出现在 merged_ids: {r['merged_ids']}"
    m = merged_ids[0]
    new_id = m["new_id"]

    # 新合并节点存在、status=active、内容含合并标记
    new_payload = store.get_node(new_id)["payload"]
    assert new_payload["status"] == "active", new_payload
    assert "由 Palimpsest 自动合并自节点" in new_payload.get("content", ""), new_payload

    # 两个旧节点被标 outdated
    assert store.get_node(a_id)["payload"]["status"] == "outdated"
    assert store.get_node(b_id)["payload"]["status"] == "outdated"

    # 新节点到两个旧节点各建了一条 REVISED_BY 边
    edges = store.get_node(new_id)["num_edges"]
    assert edges >= 2, f"新合并节点应有 >=2 条 REVISED_BY 边, 实际 {edges}"


def _edge_count(store) -> int:
    """库内全部节点的边数总和（合并会新增 REVISED_BY 边，回滚应还原）。"""
    total = 0
    for nid in store._get_all_node_ids():
        total += store.get_node(nid)["num_edges"]
    return total


def _pair_will_merge(a_id, b_id, score=0.9, imp_a=0.3, imp_b=0.5) -> dict:
    """构造一条 _apply_merge 可直接消费的候选对。"""
    return {
        "a": a_id, "b": b_id, "score": score,
        "a_imp": imp_a, "b_imp": imp_b,
        "a_content": "x", "b_content": "x",
    }


def test_consolidate_batch_rollback(iso_consolidator_store, monkeypatch):
    """per_pair_commit=False（默认）：整批事务内一对失败 → 整批回滚。

    注入 _merge_one_pair 对第二对抛 SecretScanError（模拟 strong 敏感命中），
    即便第一对已写入事务，断言整批回滚后节点数、边数、旧节点状态全部还原，
    无「前 N-1 对已提交、第 N 对失败」的部分合并状态。
    """
    import core.consolidator as consolidator
    from core.secret_scan import SecretScanError

    s = iso_consolidator_store
    n1 = s.insert_node({"type": "memory", "content": "整批回滚护栏甲", "importance": 0.3},
                       s.embed_text("整批回滚护栏甲"))
    n2 = s.insert_node({"type": "memory", "content": "整批回滚护栏甲副", "importance": 0.5},
                       s.embed_text("整批回滚护栏甲副"))
    n3 = s.insert_node({"type": "memory", "content": "整批回滚护栏乙", "importance": 0.3},
                       s.embed_text("整批回滚护栏乙"))
    n4 = s.insert_node({"type": "memory", "content": "整批回滚护栏乙副", "importance": 0.5},
                       s.embed_text("整批回滚护栏乙副"))
    will_merge = [_pair_will_merge(n1, n2), _pair_will_merge(n3, n4)]

    before_ids = set(s._get_all_node_ids())
    before_edges = _edge_count(s)
    before_status = {i: s.get_node(i)["payload"]["status"] for i in (n1, n2, n3, n4)}

    real_merge = consolidator._merge_one_pair

    def _explode_on_second(db, tx, c, next_id):
        if {c["a"], c["b"]} == {n3, n4}:
            raise SecretScanError("注入的敏感强命中")
        return real_merge(db, tx, c, next_id)

    monkeypatch.setattr(consolidator, "_merge_one_pair", _explode_on_second)

    with pytest.raises(SecretScanError):
        consolidator._apply_merge(s, will_merge)

    after_ids = set(s._get_all_node_ids())
    after_edges = _edge_count(s)
    assert after_ids == before_ids, f"整批回滚后不应残留合并节点: {before_ids} -> {after_ids}"
    assert after_edges == before_edges, f"整批回滚后边数应还原: {before_edges} -> {after_edges}"
    for i in (n1, n2, n3, n4):
        assert s.get_node(i)["payload"]["status"] == before_status[i], \
            f"整批回滚后节点 {i} 不应被标 outdated"


def test_consolidate_per_pair_commit(iso_consolidator_store, monkeypatch):
    """per_pair_commit=True：失败对之前已提交、失败对自身回滚、异常上抛循环中断。

    三对候选：第一对成功提交；第二对抛 SecretScanError 回滚并传播异常；
    第三对不应被执行（循环中断）。断言第一对的合并节点/边/标脏真实落库，
    第二对无残留，第三对保持 active、无新合并节点。
    """
    import core.consolidator as consolidator
    from core.secret_scan import SecretScanError

    s = iso_consolidator_store
    n1 = s.insert_node({"type": "memory", "content": "逐对提交护栏甲", "importance": 0.3},
                       s.embed_text("逐对提交护栏甲"))
    n2 = s.insert_node({"type": "memory", "content": "逐对提交护栏甲副", "importance": 0.5},
                       s.embed_text("逐对提交护栏甲副"))
    n3 = s.insert_node({"type": "memory", "content": "逐对提交护栏乙", "importance": 0.3},
                       s.embed_text("逐对提交护栏乙"))
    n4 = s.insert_node({"type": "memory", "content": "逐对提交护栏乙副", "importance": 0.5},
                       s.embed_text("逐对提交护栏乙副"))
    n5 = s.insert_node({"type": "memory", "content": "逐对提交护栏丙", "importance": 0.3},
                       s.embed_text("逐对提交护栏丙"))
    n6 = s.insert_node({"type": "memory", "content": "逐对提交护栏丙副", "importance": 0.5},
                       s.embed_text("逐对提交护栏丙副"))
    will_merge = [
        _pair_will_merge(n1, n2),
        _pair_will_merge(n3, n4),
        _pair_will_merge(n5, n6),
    ]

    max_id = max(s._get_all_node_ids())
    merged_first_id = max_id + 1
    failed_second_id = max_id + 2
    unreached_third_id = max_id + 3

    real_merge = consolidator._merge_one_pair

    def _explode_on_second(db, tx, c, next_id):
        if {c["a"], c["b"]} == {n3, n4}:
            raise SecretScanError("注入的敏感强命中")
        return real_merge(db, tx, c, next_id)

    monkeypatch.setattr(consolidator, "_merge_one_pair", _explode_on_second)

    with pytest.raises(SecretScanError):
        consolidator._apply_merge(s, will_merge, per_pair_commit=True)

    # 第一对已逐对提交：合并节点 + 2 条 REVISED_BY 边 + 旧节点标 outdated
    first = s.get_node(merged_first_id)
    assert first is not None, f"逐对模式下第一对应已提交: id={merged_first_id}"
    assert first["payload"]["status"] == "active", first["payload"]
    assert first["num_edges"] >= 2, f"第一对合并节点应有 REVISED_BY 边: {first}"
    assert s.get_node(n1)["payload"]["status"] == "outdated"
    assert s.get_node(n2)["payload"]["status"] == "outdated"

    # 第二对自身事务回滚：无残留合并节点，旧节点保持 active
    assert s.get_node(failed_second_id) is None, \
        f"失败对的事务应回滚: id={failed_second_id} 不应存在"
    assert s.get_node(n3)["payload"]["status"] == "active", "n3 不应被标 outdated"
    assert s.get_node(n4)["payload"]["status"] == "active", "n4 不应被标 outdated"

    # 第三对未执行（异常传播 → 循环中断）：无新合并节点，旧节点保持 active
    assert s.get_node(unreached_third_id) is None, \
        f"异常后循环应中断: id={unreached_third_id} 不应存在"
    assert s.get_node(n5)["payload"]["status"] == "active", "n5 不应被标 outdated"
    assert s.get_node(n6)["payload"]["status"] == "active", "n6 不应被标 outdated"


def test_insert_tx_success(db_path):
    """事务写入链路正常路径：insert_node_tx 在事务内落 SQL，created_at 一并入 payload。

    验证 mem_ingest 事务化后：节点真实写入、created_at 不再为 None（事务内透传，
    不再依赖事务外补写）、clash 检测正常。
    """
    from mcp_tools import store

    content = "事务成功护栏：一条标记为成功写入路径的独特记忆内容"
    r = _get(mem_ingest(content=content, type="memory", domain="hero"))
    assert r["stored"] is True, r
    nid = r["node_id"]

    node = store.get_node(nid)
    assert node is not None, f"事务提交后节点应可见: {nid}"
    payload = node["payload"]
    assert payload.get("created_at") is not None, f"created_at 应为事务内写入: {payload}"
    assert payload.get("content") == content
    assert payload.get("domain") == "hero"
    assert payload.get("type") == "memory"
    assert r["domain"] == "hero"


def test_ingest_tx_rollback(db_path, monkeypatch):
    """mem_ingest 事务回滚：resolve_conflict 在事务内抛异常时，插入的新节点一并回滚。

    核心止血目标：不允许「新节点已写、旧记忆未标 outdated」的半状态。
    注入方式：把 mcp_tools.memory.resolve_conflict 替换为抛异常的版本（模拟
    resolve_conflict 中途失败，如事务期间撞锁）。则 insert_node_tx 已写入的
    新节点会随事务 rollback 一起消失 → mem_ingest 返回 stored:False，节点数不变。
    """
    from mcp_tools import store

    before = set(store._get_all_node_ids())
    len(before)

    def _boom(store_, embedding, node_id, tx=None, db=None, new_payload=None):
        raise RuntimeError("注入的冲突检测失败（模拟中途再开库撞锁）")

    monkeypatch.setattr("mcp_tools.memory.resolve_conflict", _boom)
    try:
        r = _get(mem_ingest(content="事务回滚护栏：这条记忆不应残留在库中", type="memory"))
    finally:
        monkeypatch.setattr("mcp_tools.memory.resolve_conflict",
                            __import__("core.conflict", fromlist=["resolve_conflict"]).resolve_conflict)

    assert r["stored"] is False, f"应返回 stored:False: {r}"
    assert "回滚" in r["error"], r

    after = set(store._get_all_node_ids())
    assert after == before, (
        f"回滚后不应残留新节点（无半状态）：before={before} after={after}"
    )


def test_insert_node_tx_rollback(db_path):
    """insert_node_tx 单事务回滚：事务内 insert 后抛异常 → 提交态下节点不存在。

    直接验证事务边界最底层保证（供他人 fork 重构时防回归）。
    """
    from mcp_tools import store

    existing = store._get_all_node_ids()
    next_id = (max(existing) + 1) if existing else 1

    db = store._acquire()
    try:
        with db.transaction() as tx:
            store.insert_node_tx(tx, {"type": "memory", "content": "回滚单元护栏"},
                                 store.embed_text("回滚单元护栏"), next_id=next_id)
            raise RuntimeError("中途抛错触发回滚")
    except RuntimeError:
        pass
    finally:
        db.close()

    current = store._get_all_node_ids()
    assert next_id not in current, f"事务回滚后新节点 {next_id} 不应存在: {current}"

