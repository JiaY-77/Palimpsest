"""时间索引（到期扫描）的解析 / 分类 / 扫描 / 接线 / 回归测试。

覆盖（feat/time-index-channel 任务书要求的六组）：

1. 解析器（表驱动）：全部表达式类型 + 跨月 / 跨年 + 非法日期不崩 +
   周期（每周三）→ None + 农历 / 节日 → None
2. 分类：过去与未来同句分别落 mention / due；各最多 8 条
3. due_candidates：outdated / domain / block 过滤 + created_at 升序 + limit
4. ingest 写标：event_time / 正文时间戳 / created_at 三级回退；开关关 → 无 dates 键
5. 读侧一致性：开关关 → 输出与引入前逐位一致；开 + today → time_hit / channels.time
6. 缓存：同天连查只扫一次（iter_payloads 计数）；过期（TTL）后重新扫描

隔离：沿用 conftest 的临时库与 fake embedder，不碰生产库；测试数据全部
通用假数据（张三 / 还书 / 复查），不引用任何外部题库素材。
"""

import datetime as dt

import pytest

from core import time_index

# ================================================================
# 1. 解析器（表驱动）
# ================================================================

# (文本, ref ISO, 期望绝对日期列表)
_PARSE_CASES = [
    # 绝对日期
    ("2026-09-07 交报告", "2026-09-05T10:00:00", ["2026-09-07"]),
    ("2026年9月7日交报告", "2026-09-05T10:00:00", ["2026-09-07"]),
    ("9月7日交报告", "2026-09-05T10:00:00", ["2026-09-07"]),
    # 相对天数
    ("后天到驿站", "2026-09-05T10:00:00", ["2026-09-07"]),
    ("明天要交", "2026-09-05T10:00:00", ["2026-09-06"]),
    ("明日交", "2026-09-05T10:00:00", ["2026-09-06"]),
    ("今天盘点", "2026-09-05T10:00:00", ["2026-09-05"]),
    ("今日盘点", "2026-09-05T10:00:00", ["2026-09-05"]),
    ("当天结算", "2026-09-05T10:00:00", ["2026-09-05"]),
    ("大后天验收", "2026-09-05T10:00:00", ["2026-09-08"]),
    ("三天后复查", "2026-09-05T10:00:00", ["2026-09-08"]),
    ("2天后复查", "2026-09-05T10:00:00", ["2026-09-07"]),
    ("三天之后见", "2026-09-05T10:00:00", ["2026-09-08"]),
    ("三十天后交付", "2026-09-05T10:00:00", ["2026-10-05"]),
    ("隔两天取件", "2026-09-05T10:00:00", ["2026-09-07"]),
    ("过三天回访", "2026-09-05T10:00:00", ["2026-09-08"]),
    ("两周后交稿", "2026-09-05T10:00:00", ["2026-09-19"]),
    ("半个月后回访", "2026-09-05T10:00:00", ["2026-09-20"]),
    # 周几
    ("周三碰头", "2026-09-04T10:00:00", ["2026-09-09"]),  # 周五说 → 下周三
    ("这周三碰头", "2026-09-04T10:00:00", ["2026-09-09"]),
    ("本周三碰头", "2026-09-04T10:00:00", ["2026-09-09"]),
    ("下周三碰头", "2026-09-04T10:00:00", ["2026-09-09"]),  # 周五说 → 下周三
    ("下下周三碰头", "2026-09-04T10:00:00", ["2026-09-16"]),
    ("上周五复盘过", "2026-09-07T10:00:00", ["2026-09-04"]),  # 周一说 → 上周五
    ("星期天大扫除", "2026-09-07T10:00:00", ["2026-09-13"]),  # 周一说 → 本周日
    ("礼拜天休息", "2026-09-07T10:00:00", ["2026-09-13"]),
    # 月内 / 跨月
    ("这个月27号复查", "2026-09-05T10:00:00", ["2026-09-27"]),
    ("本月27号复查", "2026-09-05T10:00:00", ["2026-09-27"]),
    ("27号复查", "2026-09-05T10:00:00", ["2026-09-27"]),
    ("27日复查", "2026-09-05T10:00:00", ["2026-09-27"]),
    ("下个月5号复查", "2026-09-05T10:00:00", ["2026-10-05"]),
    ("月底对账", "2026-09-05T10:00:00", ["2026-09-30"]),
    ("月末对账", "2026-09-05T10:00:00", ["2026-09-30"]),
    ("下月底对账", "2026-09-05T10:00:00", ["2026-10-31"]),
    ("下个月底对账", "2026-09-05T10:00:00", ["2026-10-31"]),
    ("月初盘点", "2026-09-05T10:00:00", ["2026-09-01"]),
    ("下月初盘点", "2026-09-05T10:00:00", ["2026-10-01"]),
    ("下个月初盘点", "2026-09-05T10:00:00", ["2026-10-01"]),
    # 跨月 / 跨年边界
    ("下周三碰头", "2026-01-30T10:00:00", ["2026-02-04"]),  # 从 1 月 30 日起跨月
    ("12月31日说明天就到", "2026-12-31T10:00:00", ["2026-12-31", "2027-01-01"]),  # 跨年
    ("下个月5号对账", "2026-12-20T10:00:00", ["2027-01-05"]),  # 跨年月
    # 中文数字
    ("二十七号要还书", "2026-09-05T10:00:00", ["2026-09-27"]),
    ("十二号开工", "2026-09-05T10:00:00", ["2026-09-12"]),
    # 绝对日期里的「N日」不被 month_day 二次吃成幻影
    ("9月7日", "2026-09-05T10:00:00", ["2026-09-07"]),
    ("2026年9月7日", "2026-09-05T10:00:00", ["2026-09-07"]),
    # 周期表达：不当成单日到期
    ("每周三例会", "2026-09-05T10:00:00", []),
    ("每星期五交周报", "2026-09-05T10:00:00", []),
    # 显式不解析（农历 / 节日 / 节前节后）：None，绝不猜
    ("农历八月初八到家", "2026-09-05T10:00:00", []),
    ("农历五月初五", "2026-09-05T10:00:00", []),
    ("八月十七见", "2026-09-05T10:00:00", []),
    ("春节前聚一下", "2026-09-05T10:00:00", []),
    ("中秋吃饭", "2026-09-05T10:00:00", []),
    ("国庆放假", "2026-09-05T10:00:00", []),
    ("过年回家", "2026-09-05T10:00:00", []),
    ("节前三天交", "2026-09-05T10:00:00", []),
    ("节后两天复工", "2026-09-05T10:00:00", []),
    # 非法日期：不抛异常，解析不出就跳过
    ("这个月31号复查", "2026-02-10T10:00:00", []),
    ("2月30日见面", "2026-02-10T10:00:00", []),
    ("无日期的普通句子", "2026-09-05T10:00:00", []),
    ("", "2026-09-05T10:00:00", []),
]


def _ref(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


def test_parse_dates_table_driven():
    """任务书表达式清单逐条对拍（≥25 条，含跨月 / 跨年 / 非法 / 农历 / 周期）。"""
    assert len(_PARSE_CASES) >= 25
    for text, ref_iso, want in _PARSE_CASES:
        got = [d.isoformat() for _, d in time_index.parse_dates(text, _ref(ref_iso))]
        assert got == want, f"文本={text!r} 期望={want} 实得={got}"


def test_cn2int_variants():
    """中文数字组合与阿拉伯数字都能吃；失败返回 None。"""
    assert time_index.cn2int("二十七") == 27
    assert time_index.cn2int("十二") == 12
    assert time_index.cn2int("十") == 10
    assert time_index.cn2int("两") == 2
    assert time_index.cn2int("27") == 27
    assert time_index.cn2int("廿七") is None
    assert time_index.cn2int("") is None


def test_weekly_is_not_a_single_due():
    """「每周三」被整段剔除后，文本里不再产出任何单日日期（周期 ≠ 单日到期）。"""
    got = time_index.parse_dates("每周三例会，每周五交周报", _ref("2026-09-05T10:00:00"))
    assert got == []


def test_lunar_and_festival_never_parse():
    """农历 / 节日 / 节前节后是已知边界：返回空列表，不猜、不崩。"""
    ref = _ref("2026-09-05T10:00:00")
    for text in ("农历八月初八", "春节", "中秋", "国庆", "过年", "节前三天", "节后两天"):
        assert time_index.parse_dates(text, ref) == [], text


# ================================================================
# 2. 分类（due / mention）
# ================================================================


def test_date_fields_splits_past_and_future():
    """同一句里过去与未来日期分别落 mention / due。"""
    fields = time_index.date_fields("9月1日他塞给我纸条，后天到驿站", _ref("2026-09-05T10:00:00"))
    assert fields is not None
    assert fields["due"] == ["2026-09-07"]
    assert fields["mention"] == ["2026-09-01"]
    assert fields["ref"] == "2026-09-05T10:00:00"


def test_date_fields_none_when_no_dates():
    assert time_index.date_fields("没有日期的一句话", _ref("2026-09-05T10:00:00")) is None


def test_date_fields_dedup_sorted_and_capped_at_8():
    """升序去重；超过 8 条时各列表只保留前 8 条（payload 要小）。"""
    text = "、".join([f"{i}月1日备忘" for i in range(1, 13)])  # 12 条，1~9 月早于 ref（9月5日）
    fields = time_index.date_fields(text, _ref("2026-09-05T10:00:00"))
    assert fields is not None
    # 1~9 月是过去/当天 → mention（9 条，封顶 8，丢掉最晚的 9 月 1 日）；10~12 月 → due
    assert fields["mention"] == [f"2026-0{i}-01" for i in range(1, 9)]
    assert fields["due"] == ["2026-10-01", "2026-11-01", "2026-12-01"]


def test_mention_not_treated_as_due():
    """回顾性提及（d <= ref）不进 due——这是实测定下的口径，防止挤爆当天候选池。"""
    fields = time_index.date_fields("他今天塞给我纸条", _ref("2026-09-05T10:00:00"))
    assert fields is not None
    assert fields["due"] == []
    assert fields["mention"] == ["2026-09-05"]


# ================================================================
# 3. due_candidates（过滤 / 排序 / limit）
# ================================================================


@pytest.fixture
def scan_store(tmp_path, monkeypatch):
    """指向临时库的 TriviumStore，造 3 个节点：不同到期日 / 一个 outdated / 一个别的 domain。"""
    from config import Config
    from core.trivium_store import TriviumStore

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mh.db"))
    store = TriviumStore()
    n1 = store.insert_node(
        {
            "type": "memory",
            "content": "张三后天要还书",
            "created_at": 300.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("张三后天要还书"),
    )
    n2 = store.insert_node(
        {
            "type": "memory",
            "content": "李四今天要复查",
            "created_at": 100.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("李四今天要复查"),
    )
    # insert_node 硬编码 status=active（写侧统一经冲突检测标脏），测试构造
    # outdated 节点须走 update_payload（与生产路径同款浅合并）
    store.update_payload(n2, {"status": "outdated"})
    n3 = store.insert_node(
        {
            "type": "memory",
            "content": "王五要交材料",
            "created_at": 200.0,
            "domain": "work",
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("王五要交材料"),
    )
    n4 = store.insert_node(
        {
            "type": "memory",
            "content": "赵六下周三对账",
            "created_at": 400.0,
            "dates": {"due": ["2026-09-16"], "mention": [], "ref": "x"},
        },
        store.embed_text("赵六下周三对账"),
    )
    return store, {"early": n1, "outdated": n2, "other_domain": n3, "other_day": n4}


def test_due_candidates_filters_outdated_and_other_domain(scan_store):
    store, ids = scan_store
    time_index._scan_cache.clear()
    got = time_index.due_candidates(store, dt.date(2026, 9, 7))
    # 无过滤：work 域节点也是候选，只剔 outdated；按 created_at 升序（200 < 300）
    assert got == [ids["other_domain"], ids["early"]]
    time_index._scan_cache.clear()
    got = time_index.due_candidates(store, dt.date(2026, 9, 7), domain="work")
    assert got == [ids["other_domain"]]
    time_index._scan_cache.clear()
    got = time_index.due_candidates(store, dt.date(2026, 9, 7), domain="work", block="work")
    assert got == [ids["other_domain"]]
    time_index._scan_cache.clear()
    got = time_index.due_candidates(store, dt.date(2026, 9, 7), block="general")
    assert got == [ids["early"]]  # block 隔离：work 域节点被区块丢掉


def test_due_candidates_sorted_by_created_at_asc(scan_store):
    """声明越早越靠前（created_at 升序）——离线实测平均名次 2.1 的口径。"""
    from core.trivium_store import TriviumStore  # noqa: F401

    store, ids = scan_store
    # 再造两个同日到期、created_at 更晚的节点，验证整体升序
    late1 = store.insert_node(
        {
            "type": "memory",
            "content": "孙七今晚交周报",
            "created_at": 500.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("孙七今晚交周报"),
    )
    late2 = store.insert_node(
        {
            "type": "memory",
            "content": "周八今晚取件",
            "created_at": 600.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("周八今晚取件"),
    )
    time_index._scan_cache.clear()
    got = time_index.due_candidates(store, dt.date(2026, 9, 7))
    assert got == [ids["other_domain"], ids["early"], late1, late2]


def test_due_candidates_window_and_limit(scan_store, tmp_path, monkeypatch):
    store, ids = scan_store
    time_index._scan_cache.clear()
    # 窗口：9 月 16 日的节点在 ±10 天窗口内也算到期
    got = time_index.due_candidates(store, dt.date(2026, 9, 7), window_days=10)
    assert got == [ids["other_domain"], ids["early"], ids["other_day"]]
    # limit 截断
    time_index._scan_cache.clear()
    got = time_index.due_candidates(store, dt.date(2026, 9, 7), limit=1)
    assert got == [ids["other_domain"]]
    # 窗口外
    time_index._scan_cache.clear()
    assert time_index.due_candidates(store, dt.date(2026, 12, 25)) == []


# ================================================================
# 4. ingest 写标（三级回退 + 开关关闭形状不变）
# ================================================================


def _ingest(content: str, event_time: str = "", enabled: bool = True):
    import json

    import mcp_tools.memory as mm
    from mcp_tools import store as global_store

    result = json.loads(mm.mem_ingest(content, event_time=event_time))
    assert result["stored"] is True, result
    node = global_store.get_node(result["node_id"])
    return node["payload"]


def test_ingest_event_time_wins(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    payload = _ingest("张三说后天要还书", event_time="2026-09-05T17:45:00+08:00")
    # 带时区的时间换算到本地墙钟再展开（CI 与本机时区不同也对拍同一逻辑）
    ref = dt.datetime.fromisoformat("2026-09-05T17:45:00+08:00").astimezone().replace(tzinfo=None)
    assert payload["dates"]["due"] == [(ref + dt.timedelta(days=2)).date().isoformat()]
    assert payload["dates"]["ref"] == ref.isoformat()


def test_ingest_falls_back_to_content_timestamp(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    payload = _ingest("[2026-09-05T17:45:00+08:00] 张三说大后天要复查")
    ref = dt.datetime.fromisoformat("2026-09-05T17:45:00+08:00").astimezone().replace(tzinfo=None)
    assert payload["dates"]["due"] == [(ref + dt.timedelta(days=3)).date().isoformat()]
    assert payload["dates"]["ref"] == ref.isoformat()


def test_ingest_falls_back_to_created_at(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    payload = _ingest("三天后复查")
    # mem_ingest 内部自取墙钟，测试从落库的 created_at 反推期望（同一时区口径）
    created = payload["created_at"]
    expected = (dt.datetime.fromtimestamp(created) + dt.timedelta(days=3)).date().isoformat()
    assert payload["dates"]["due"] == [expected]
    assert payload["dates"]["ref"] == dt.datetime.fromtimestamp(created).isoformat()


def test_ingest_event_time_date_only(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    payload = _ingest("张三说下周三要还书", event_time="2026-09-05")
    assert payload["dates"]["due"] == ["2026-09-09"]


def test_ingest_no_dates_when_no_date_expression(monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    payload = _ingest("聊了聊家常，天气不错")  # 不含任何日期表达
    assert "dates" not in payload


def test_ingest_disabled_payload_shape_unchanged(monkeypatch):
    """开关关闭（默认）：payload 不含 dates 键——payload 形状与引入前逐键一致。"""
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", False)
    payload = _ingest("张三说后天要还书", event_time="2026-09-05T17:45:00+08:00")
    assert "dates" not in payload


# ================================================================
# 5. 读侧一致性（开关关 = 逐位一致；开 = 时间通道生效）
# ================================================================


def _patch_sem_fts(monkeypatch, sem_ids, fts_ids):
    """固定替身：语义侧 sem_ids，FTS 侧 fts_ids（都是 rank 顺序）。"""
    import mcp_tools.memory as mm

    monkeypatch.setattr(
        mm,
        "_sem_candidate_items",
        lambda *a, **kw: [
            {"id": nid, "type": "memory", "score": 0.9, "summary": "替身", "meta": {}} for nid in sem_ids
        ],
    )
    monkeypatch.setattr(mm, "search_fts_status", lambda q, limit=50: ([{"node_id": nid} for nid in fts_ids], "ok"))


def test_hybrid_rrf_disabled_is_bitwise_identical(monkeypatch):
    """开关关闭：channels 恰为引入前的四键、无 time 键 / time_hit，today 传不传都一样。"""
    import mcp_tools.memory as mm
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", False)
    _patch_sem_fts(monkeypatch, [11, 13], [11, 13])
    base_items, base_channels = mm._hybrid_rrf("固定查询甲乙丙", "memory", "", "", 5, 50, "")
    assert "time" not in base_channels
    assert base_channels == {"semantic": "ok", "fts": "ok", "exact_query": False, "fts_weight": 0.1}
    assert all("time_hit" not in (it["meta"] or {}) for it in base_items)
    # 与改动前一致的金标：语义 + FTS 两路 fixture 下 node11 > node13
    # （分母 = k + rank，rank 0 起：11 = 1/60 + 0.1/60，13 = 1/61 + 0.1/61）
    assert [(it["id"], it["score"], it["meta"]["fts_hit"], it["meta"]["sem_hit"]) for it in base_items] == [
        (11, round(1.1 / 60, 4), True, True),
        (13, round(1.1 / 61, 4), True, True),
    ]
    # today 参数传了也没效果：输出逐位一致
    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "memory", "", "", 5, 50, "", today="2026-09-07")
    assert items == base_items
    assert channels == base_channels


def test_hybrid_rrf_enabled_surfaces_due_nodes(monkeypatch, tmp_path):
    """开关打开 + today：到期节点经时间通道进结果，meta.time_hit / channels.time 如实标注。"""
    import mcp_tools.memory as mm
    from config import Config
    from mcp_tools._common import store

    # 插入时打独立 domain（ti_iso）：共享 store 里别的用例也往同一天插节点，
    # 只靠日期区分会在全量跑时互相污染（实测 [7,1] vs [7]）。domain 隔离最稳。
    due_id = store.insert_node(
        {
            "type": "memory",
            "content": "张三今天要还书",
            "domain": "ti_iso",
            "created_at": 100.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("张三今天要还书"),
    )
    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    time_index._scan_cache.clear()
    _patch_sem_fts(monkeypatch, [], [])  # 语义 / FTS 零命中：只有时间路能捞到
    items, channels = mm._hybrid_rrf("今天有啥事", "memory", "ti_iso", "", 5, 50, "", today="2026-09-07")
    assert channels["time"] == "ok"
    assert [it["id"] for it in items] == [due_id]
    assert all(it["meta"]["time_hit"] is True for it in items)

    # 无到期节点 → time=empty，条目仍带 time_hit=False（启用即带标记）
    monkeypatch.setattr(
        time_index,
        "_scan",
        lambda *a, **kw: [],
    )
    items, channels = mm._hybrid_rrf("今天有啥事", "memory", "", "", 5, 50, "", today="2026-10-01")
    assert channels["time"] == "empty"
    assert items == []


def test_hybrid_rrf_enabled_empty_day_marks_empty(monkeypatch, tmp_path):
    import mcp_tools.memory as mm
    from config import Config
    from core.trivium_store import TriviumStore

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mh.db"))
    store = TriviumStore()
    store.insert_node(
        {
            "type": "memory",
            "content": "张三今天要还书",
            "created_at": 100.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("张三今天要还书"),
    )
    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    time_index._scan_cache.clear()
    _patch_sem_fts(monkeypatch, [42], [])
    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "memory", "", "", 5, 50, "", today="2030-01-01")
    assert channels["time"] == "empty"
    assert all(it["meta"]["time_hit"] is False for it in items)


def test_hybrid_cascade_marks_time_skipped(monkeypatch):
    """级联模式暂不支持时间通道：开关打开时 channels.time 如实标 skipped。"""
    import mcp_tools.memory as mm
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    _patch_sem_fts(monkeypatch, [11], [])
    items, channels = mm._hybrid_cascade("固定查询甲乙丙", "memory", "", "", 5, 50, "")
    assert channels["time"] == "skipped"
    assert all("time_hit" not in (it["meta"] or {}) for it in items)


def test_hybrid_cascade_disabled_has_no_time_key(monkeypatch):
    import mcp_tools.memory as mm
    from config import Config

    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", False)
    _patch_sem_fts(monkeypatch, [11], [])
    _items, channels = mm._hybrid_cascade("固定查询甲乙丙", "memory", "", "", 5, 50, "")
    assert "time" not in channels


def test_mem_hybrid_search_full_path_with_today(monkeypatch, tmp_path):
    """全链路：mem_hybrid_search(today=...) → JSON 里 channels.time / meta.time_hit 如实透出。"""
    import json

    import mcp_tools.memory as mm
    from config import Config
    from mcp_tools._common import store

    # 同 test_hybrid_rrf_enabled_surfaces_due_nodes：插到被测代码实际使用的共享 store，
    # 并用独立 domain 与其他用例隔离。
    due_id = store.insert_node(
        {
            "type": "memory",
            "content": "张三今天要还书",
            "domain": "ti_iso2",
            "created_at": 100.0,
            "dates": {"due": ["2026-09-07"], "mention": [], "ref": "x"},
        },
        store.embed_text("张三今天要还书"),
    )
    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", True)
    time_index._scan_cache.clear()
    _patch_sem_fts(monkeypatch, [], [])
    result = json.loads(mm.mem_hybrid_search("今天有啥事", domain="ti_iso2", today="2026-09-07"))
    assert result["channels"]["time"] == "ok"
    assert [it["id"] for it in result["results"]] == [due_id]
    assert result["results"][0]["meta"]["time_hit"] is True

    # 开关关（回归）：同库同 query 同 top_k，无 time 键、结果不含到期节点
    monkeypatch.setattr(Config, "TIME_INDEX_ENABLED", False)
    result_off = json.loads(mm.mem_hybrid_search("今天有啥事", domain="ti_iso2", today="2026-09-07"))
    assert "time" not in result_off["channels"]
    assert result_off["results"] == []


def test_resolve_today():
    from datetime import date as _date

    assert time_index.resolve_today("") == _date.today()
    assert time_index.resolve_today("2026-09-07") == _date(2026, 9, 7)
    assert time_index.resolve_today("2026-09-07T08:00:00+08:00") == _date(2026, 9, 7)
    assert time_index.resolve_today("不是日期") == _date.today()  # 非法降级，主链不炸


# ================================================================
# 6. 缓存：同天只扫一次；TTL 过期后重新扫描
# ================================================================


def test_due_candidates_cache_scans_once(scan_store, monkeypatch):
    store, _ids = scan_store
    time_index._scan_cache.clear()
    calls: list[int] = []

    original_iter = store.iter_payloads

    def counting_iter():
        calls.append(1)
        yield from original_iter()

    monkeypatch.setattr(store, "iter_payloads", counting_iter)
    day = dt.date(2026, 9, 7)
    first = time_index.due_candidates(store, day)
    second = time_index.due_candidates(store, day)
    assert len(calls) == 1  # 同一天连查两次只扫一次
    assert first == second
    # 键不同（limit 变了）→ 重新扫描
    time_index.due_candidates(store, day, limit=1)
    assert len(calls) == 2


def test_due_candidates_cache_expires_by_ttl(scan_store, monkeypatch):
    """缓存过期靠时间自然淘汰：把条目时间戳拨老，下次调用即重新扫描（无手工清除接口）。"""
    store, _ids = scan_store
    time_index._scan_cache.clear()
    calls: list[int] = []

    original_iter = store.iter_payloads

    def counting_iter():
        calls.append(1)
        yield from original_iter()

    monkeypatch.setattr(store, "iter_payloads", counting_iter)
    day = dt.date(2026, 9, 7)
    time_index.due_candidates(store, day)
    assert len(calls) == 1
    key = next(iter(time_index._scan_cache))
    ts, ids = time_index._scan_cache[key]
    time_index._scan_cache[key] = (ts - (time_index._SCAN_CACHE_TTL_SECONDS + 1.0), ids)
    time_index.due_candidates(store, day)
    assert len(calls) == 2  # TTL 过期 → 重新扫描
