# -*- coding: utf-8 -*-
"""
mem_recent 行为锁定回归测试
==============================
背景：T072 升级 triviumdb 0.8.5 后，mem_recent 空 domain 分支原注释
「MATCH (n) 硬截断 5000 条故走 iter_payloads」已过时（0.8.5 修复 #32 后
MATCH 可全量返回）。本文件在【简化前】锁定现行为，作为重构的安全网。

锁定语义：
  1. 空 domain = 全量枚举，按 (created_at or 0, id) 双键倒序，取 limit 条
     —— created_at 缺失（None→0）排在最后，同 created_at 内 id 大者优先
  2. 带 domain = 只返回该 domain 的记忆，排序同上
  3. limit 默认 10，超出截断

隔离设计（重要）：
  - conftest 把 DB_PATH 指向 session 级临时库，所有测试文件共享；
  - 本文件断言一律按【唯一 domain 命名空间】过滤自己插入的节点，
    不假设全库为空、不按 content 前缀过滤；
  - content 用彼此词面差异大的短语：fake embedder 是字符 2-gram，
    共享短前缀的 content 会互相高相似，干扰 test_consolidate_dryrun
    （它断言 find_similar_pairs 的 candidates[0] 必须是自己那对）。

写入说明：真实链路 mem_ingest 走 insert_node_tx 事务并把 created_at
透传进 payload（insert_node 非事务版恒置 None）。helper 复刻该路径并
同步 FTS 索引（index_node），避免共享库主库/FTS 不一致。
"""
import json
import uuid


def _ns():
    return f"mrb_{uuid.uuid4().hex[:10]}"


def _ingest(store, content, ts=None, domain=None):
    """经事务路径插入记忆；返回 (node_id, domain)。ts=None → 无 created_at。"""
    domain = domain or _ns()
    pl = {"type": "memory", "content": content,
          "importance": 0.5, "domain": domain}
    if ts is not None:
        pl["created_at"] = ts
    emb = store.embed_text(content)

    db = store._acquire()
    try:
        with db.transaction() as tx:
            existing = db.all_node_ids()
            next_id = (max(existing) + 1) if existing else 1
            nid = store.insert_node_tx(tx, pl, emb, next_id=next_id)
    finally:
        try:
            db.close()
        except Exception:
            pass
    # 与 mem_ingest 链路一致：事务提交后同步 FTS 索引
    try:
        from mcp_tools.memory import index_node
        index_node(nid, content)
    except Exception:
        pass
    return nid, domain


def _recent(domain, limit=10):
    from mcp_tools.memory import mem_recent
    return json.loads(mem_recent(domain=domain, limit=limit))


def _filter_by_domain(data, ns):
    """只取本命名空间域（ns 为前缀）的结果，避免其他文件节点干扰。"""
    return [it for it in data["results"]
            if str(it.get("domain", "")).startswith(ns)]


def test_mem_recent_empty_domain_sorts_by_created_at_desc():
    """空 domain：有 created_at 的记忆按时间倒序，跨 domain 也出现。"""
    from mcp_tools import store

    ns = _ns()
    d1, d2 = f"{ns}a", f"{ns}b"
    _ingest(store, "山间清泉在晨光里缓缓流淌", ts=100, domain=d1)
    _ingest(store, "码头货轮正在装卸彩色集装箱", ts=200, domain=d1)
    _ingest(store, "图书馆顶层藏着泛黄的手稿", ts=300, domain=d2)

    data = _recent("", limit=50)
    mine = _filter_by_domain(data, ns)
    contents = [it["content"] for it in mine]
    assert contents == ["图书馆顶层藏着泛黄的手稿",
                        "码头货轮正在装卸彩色集装箱",
                        "山间清泉在晨光里缓缓流淌"]


def test_mem_recent_missing_created_at_goes_last():
    """created_at 缺失（None→0）必须排在最后——TQL ORDER BY 会把缺失排最前，
    此测试锁定 Python 侧兜底排序不被下推破坏。"""
    from mcp_tools import store

    ns = _ns()
    d = f"{ns}d"
    _ingest(store, "琥珀色琥珀标本陈列在旧书架", ts=None, domain=d)
    _ingest(store, "南方的候鸟在秋夜启程远行", ts=500, domain=d)
    _ingest(store, "石英矿脉深处藏有远古水痕", ts=None, domain=d)

    data = _recent("", limit=50)
    mine = _filter_by_domain(data, ns)
    contents = [it["content"] for it in mine]
    # 有时间戳的排最前；两条缺失按 id 倒序（后插入的先出）
    assert contents[0] == "南方的候鸟在秋夜启程远行"
    assert contents[-1] == "琥珀色琥珀标本陈列在旧书架"
    assert contents[1] == "石英矿脉深处藏有远古水痕"


def test_mem_recent_domain_filter():
    """带 domain：只返回该 domain 的记忆。"""
    from mcp_tools import store

    ns = _ns()
    d1, d2 = f"{ns}a", f"{ns}b"
    _ingest(store, "清晨薄雾笼罩着寂静的村庄", ts=100, domain=d1)
    _ingest(store, "深夜电台播放着怀旧金曲", ts=200, domain=d2)

    data = _recent(d1, limit=10)
    contents = [it["content"] for it in data["results"]]
    assert contents == ["清晨薄雾笼罩着寂静的村庄"]


def test_mem_recent_limit_truncates():
    """limit 截断：只返回前 limit 条（最新 5 条）。"""
    from mcp_tools import store

    ns = _ns()
    d = f"{ns}d"
    words = ["苹果园", "河流石", "星辰图", "火车站", "纸飞机",
             "火焰山", "岛屿链", "诗歌集", "时钟塔", "森林浴",
             "露水珠", "峡谷风", "风筝线", "灯塔光", "麦田浪"]
    for i, w in enumerate(words):
        _ingest(store, f"{w}旁的木屋在黄昏亮起暖灯", ts=1000 + i, domain=d)

    data = _recent("", limit=5)
    mine = _filter_by_domain(data, ns)
    assert len(mine) == 5
    # 最新 5 条 = ts 最大（麦田浪/灯塔光/风筝线/峡谷风/露水珠）
    assert [it["content"] for it in mine] == [
        f"{w}旁的木屋在黄昏亮起暖灯" for w in
        ["麦田浪", "灯塔光", "风筝线", "峡谷风", "露水珠"]]


def test_mem_recent_tie_break_by_id_desc():
    """同 created_at：id 大的先出（双键倒序的第二个键）。"""
    from mcp_tools import store

    ns = _ns()
    d = f"{ns}d"
    _ingest(store, "北斗七星在子夜指向北极方向", ts=100, domain=d)
    _ingest(store, "礁石灯塔在雾中忽明忽暗", ts=100, domain=d)
    _ingest(store, "白鹭掠过暮色中的芦苇荡", ts=100, domain=d)

    data = _recent("", limit=50)
    mine = _filter_by_domain(data, ns)
    # 同 ts=100 → id 倒序：白鹭 > 礁石 > 北斗
    assert [it["content"] for it in mine] == [
        "白鹭掠过暮色中的芦苇荡",
        "礁石灯塔在雾中忽明忽暗",
        "北斗七星在子夜指向北极方向"]
