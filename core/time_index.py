"""时间索引：相对/绝对日期解析 + 到期扫描（写侧打标 + 读侧 RRF 第五路）。

用途
    聊天里的「后天 / 下周三 / 这个月 27 号」这类日期表达，语义向量与 FTS 都
    抓不住（提问侧往往没有任何字面与语义线索）。本模块在写入时把日期表达式
    解析成绝对日期存进 ``payload["dates"]``，检索时按「今天到期」扫出候选
    节点，作为一条额外通道并入既有 RRF 融合（不改变任何既有通道的权重与
    排序，不注入置顶、不改返回结构）。

边界（已知不解析，返回 None，绝不猜测）
    - 农历日期（「农历八月初八」「八月十七」）——需要农历换算表，标准库没有；
    - 节日（「春节」「中秋」「国庆」「过年」）——公历日期逐年漂移；
    - 「节前 N 天」「节后 N 天」——依赖节日定位，同上。
    这类到期提醒请由调用方自己换算成绝对日期（或 ISO 时间戳）写进正文。

接入点（默认关闭，见 config.TIME_INDEX_ENABLED，关闭时零成本）
    - 写侧：``mcp_tools.memory.mem_ingest(event_time=...)`` → :func:`time_date_fields`
    - 读侧：``mcp_tools.memory.mem_hybrid_search(today=...)`` → :func:`due_candidates`
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

from core.trivium_store import domain_in_block, node_domain

logger = logging.getLogger(__name__)

# ---- 常量 -----------------------------------------------------------------

# 中文数字：口语里的「两」与「二」等价
CN_NUM: dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# 星期几 → datetime.weekday()（周一是 0）；「日」与「天」都是星期日
WEEKDAY: dict[str, int] = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 日期里的数字段：1~2 位阿拉伯数字，或 1~3 个中文数字（「二十七」「十二」）
NUM = r"([0-9]{1,2}|[一二两三四五六七八九十]{1,3})"

# 周期表达（「每周三」）：不是单日到期，扫描前整段剔除——否则里面的「周三」
# 会被 weekday 模式吃成「本周三」，把周期承诺错存成某一天到期
_WEEKLY_RE = re.compile(r"每\s*(?:周|星期|礼拜)[一二三四五六日天]")

# (名字, 编译好的正则)——顺序即优先级：绝对日期最先，逐级到月内表达。
# 每个模式独立扫全文，但先成功者**占有**匹配区间（见 parse_dates 的 claimed
# 区间抑制），因此先长后短能防幻影日期：「2026年9月7日」里的「7日」不会再被
# month_day 吃成「本月 7 号」。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 绝对日期：2026-09-07 / 2026/9/7 / 2026年9月7日
    ("abs", re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?")),
    # 月日：9月7日 / 9月7号
    ("abs_md", re.compile(r"(\d{1,2})月(\d{1,2})[日号]")),
    # 相对天数：今天/今日/明天/明日/后天/大后天/当天
    ("rel_day", re.compile(r"(大后天|后天|明天|明日|今天|今日|当天)")),
    # N 天后：三天后 / 3天后 / 三日之后
    ("rel_nd", re.compile(NUM + r"\s*[天日](?:以?后|之后)")),
    # 过/隔 N 天：隔两天 / 过三天
    ("rel_prev_nd", re.compile(r"(?:过|隔)\s*" + NUM + r"\s*天")),
    # N 个星期后：三个星期后 / 两星期之后
    ("rel_week_later", re.compile(r"(" + NUM + r"|半)\s*个?星期(?:以?后|之后)")),
    # 半个月后
    ("rel_half_month", re.compile(r"半\s*个?月(?:以?后|之后)")),
    # N 周后：两周后 / 三周之后
    ("rel_week_later2", re.compile(NUM + r"\s*周(?:以?后|之后)")),
    # 周几：周三 / 这周三 / 本周三 / 下周三 / 下下周三 / 上周五
    ("weekday", re.compile(r"(下下个?|下个?|这个?|本|上个?)?\s*(?:周|星期|礼拜)([一二三四五六日天])")),
    # 月内：这个月27号 / 本月27日 / 27号 / 下个月5号
    ("month_day", re.compile(r"(下个?月|这个?月|本月|本)?\s*" + NUM + r"\s*[号日]")),
    # 月底/月末：月底 / 下月底 / 下个月底 / 本月末
    # （「下」单字前缀必须放在「下个月/下月」之后依次回退：「下个月底」= 下 + 个月底）
    ("month_end", re.compile(r"(下个?月|下月|下)?\s*个?月[底末]")),
    # 月初：月初 / 下月初 / 下个月初
    ("month_start", re.compile(r"(下个?月|下月|下)?\s*个?月[初]")),
]

# 「今天/明天/后天/大后天」的偏移天数
_REL_DAY_OFFSETS: dict[str, int] = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "今天": 0, "今日": 0, "当天": 0}

# 聊天正文里的 ISO 时间戳（历史批量导入时时间戳就写在正文里）
_ISO_TS_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?")

# payload 要小：due / mention 各最多保留 8 条（升序去重后取最早的 8 条）
_MAX_DATES_PER_LIST = 8

# 到期扫描的模块级缓存 TTL（秒）：到期提醒容忍 5 秒陈旧；过期靠时间自然淘汰，
# 不设手工清除接口。缓存键含 id(store)，不要求跨进程一致。
_SCAN_CACHE_TTL_SECONDS = 5.0
_scan_cache: dict[tuple, tuple[float, list[int]]] = {}


# ---- 数字与解析 -----------------------------------------------------------


def cn2int(s: str) -> int | None:
    """中文/阿拉伯数字 → int；「二十七」「十二」「27」都吃；失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if len(s) == 2 and s.startswith("十") and s[1] in CN_NUM:
        return 10 + CN_NUM[s[1]]
    if len(s) == 2 and s.endswith("十") and s[0] in CN_NUM:
        return CN_NUM[s[0]] * 10
    if len(s) == 3 and s[0] in CN_NUM and s[1] == "十" and s[2] in CN_NUM:
        return CN_NUM[s[0]] * 10 + CN_NUM[s[2]]
    if len(s) == 1 and s in CN_NUM:
        return CN_NUM[s]
    return None


# 月初后紧跟数字 → 是农历「X月初N」形态（如「八月初八」），不是公历月初
_LUNAR_MONTH_START_RE = re.compile(r"[0-9一二两三四五六七八九十]")


def parse_dates(text: str, ref: dt.datetime) -> list[tuple[str, dt.date]]:
    """从文本里抽出所有可解析的日期表达式 → [(原表达式, 绝对日期)]，可重复、可乱序。

    ``ref`` 是「这条消息发生时的时间」：所有相对表达（明天 / 下周三 / 月底）
    一律以它为基准展开，不用写入时的墙钟——差一天，「明天」就存错一天，
    到期当天就扫不出来。

    解析不出的片段（非法日期如 2 月 31 日）跳过不产出、不占匹配区间；周期
    表达（每周三）在扫描前整段剔除。农历/节日/「节前N天」本模块就不认识，
    详见模块 docstring 的边界说明。
    """
    out: list[tuple[str, dt.date]] = []
    if not text:
        return out
    work = _WEEKLY_RE.sub(" ", text)
    claimed: list[tuple[int, int]] = []  # 已被更高优先级模式成功解析的 (start, end) 区间
    for name, pat in PATTERNS:
        for m in pat.finditer(work):
            start, end = m.span()
            if any(start < ce and cs < end for cs, ce in claimed):
                continue  # 与先命中的更长/更优先表达重叠：已被吃掉，不再产出幻影日期
            if name == "month_start" and _LUNAR_MONTH_START_RE.match(work[end:]):
                continue  # 「八月初八」是农历「初N」日期形态，不当成公历月初
            try:
                d = _resolve(name, m, ref)
            except (ValueError, OverflowError):
                d = None  # 2月31号这类非法日期：解析不出就跳过，绝不抛异常打断主链
            if d is None:
                continue  # 未命中不占区间，让后面的模式仍可尝试这段文本
            claimed.append((start, end))
            out.append((m.group(0), d))
    return out


def date_fields(text: str, ref: dt.datetime) -> dict | None:
    """解析文本里的日期并按「未来 / 过去」分类。

    → ``{"due": ["2026-09-07", ...], "mention": [...], "ref": "<iso>"}``；
    解析不出任何日期时返回 None。

    分类判据（离线实测结论，勿随意改）：
      - ``d > ref.date()``  → ``due``（未来声明 = 可能的到期日/计划日）；
      - ``d <= ref.date()`` → ``mention``（当天提及、过去回顾）。
    回顾性提及（「他今天塞给我纸条」）不当到期候选：把 mention 也算进 due
    会挤爆当天的候选池，due_day 题 top10 命中实测 70% → 44%。
    两个列表各自升序去重、各最多 8 条（控制 payload 体积）。
    """
    got = parse_dates(text, ref)
    if not got:
        return None
    due = sorted({d.isoformat() for _, d in got if d > ref.date()})
    mention = sorted({d.isoformat() for _, d in got if d <= ref.date()})
    return {
        "due": due[:_MAX_DATES_PER_LIST],
        "mention": mention[:_MAX_DATES_PER_LIST],
        "ref": ref.isoformat(),
    }


def time_date_fields(content: str, event_time: str, created_at: float) -> dict | None:
    """mem_ingest 写侧入口：确定解析基准（三级回退，顺序写死）后再分类。

    1. ``event_time`` 非空 → 用它（调用方上报的消息时刻，最准；接受
       ``2026-09-05T17:45:00+08:00`` 或 ``2026-09-05``）；
    2. 否则取 ``content`` 里第一个 ISO 时间戳（聊天历史批量导入时时间戳就在
       正文里）；
    3. 都不成 → ``created_at``（写入墙钟，兜底）。

    解析基准永远是「这条消息发生时的时间」，不是写入时的墙钟。
    ``event_time`` 给了但解析失败时记 warning 并继续回退（主写链不断）。
    """
    ref: dt.datetime | None = None
    if (event_time or "").strip():
        ref = _parse_iso_datetime(event_time)
        if ref is None:
            logger.warning("event_time 无法解析为 ISO 时间（%r），回退下一级基准", event_time[:40])
    if ref is None:
        m = _ISO_TS_IN_TEXT_RE.search(content or "")
        if m:
            ref = _parse_iso_datetime(m.group(0))
    if ref is None:
        ref = dt.datetime.fromtimestamp(created_at)
    return date_fields(content or "", ref)


# ---- 读侧：到期扫描 -------------------------------------------------------


def due_candidates(
    store,
    day: dt.date,
    domain: str = "",
    block: str = "",
    limit: int = 20,
    window_days: int = 0,
) -> list[int]:
    """扫 ``payload["dates"]["due"]`` 命中 ``[day-window, day+window]`` 的节点 → 节点 id 列表。

    过滤与排序口径（离线实测定出，勿随意改）：
      - ``payload["status"] == "outdated"`` 跳过（旧版本不再提醒）；
      - ``domain`` 非空 → 按 ``payload["domain"]`` 等值过滤；
      - ``block`` 非空 → 复用 :func:`core.trivium_store.domain_in_block`
        （与既有 block 语义一致，不另立一套）；
      - 命中节点按 ``created_at`` 升序（声明越早越靠前）：离线实测平均名次
        2.1；降序为 4.7——谁先承诺谁先到，排序就是提醒的先后。

    遍历用 ``store.iter_payloads()``（7000+ 节点全量实测约 99ms，可接受；
    刻意不建 sidecar 索引——写侧多一条要维护的索引线，得不偿失）。

    模块级 5 秒 TTL 缓存，键 ``(id(store), day, domain, block, limit, window_days)``；
    缓存只是省重复扫描，不要求跨进程一致。
    """
    cache_key = (id(store), day, domain, block, limit, window_days)
    now = time.monotonic()
    hit = _scan_cache.get(cache_key)
    if hit is not None and now - hit[0] < _SCAN_CACHE_TTL_SECONDS:
        return list(hit[1])
    ids = _scan(store, day, domain, block, limit, window_days)
    # 顺手淘汰已过期条目，防止长驻进程里键随 store 重建无限累积
    for key, (ts, _ids) in list(_scan_cache.items()):
        if now - ts >= _SCAN_CACHE_TTL_SECONDS:
            del _scan_cache[key]
    _scan_cache[cache_key] = (now, ids)
    return list(ids)


def _scan(store, day: dt.date, domain: str, block: str, limit: int, window_days: int) -> list[int]:
    """真正的一次全量扫描（无缓存）。"""
    window = max(0, window_days)
    lo = day - dt.timedelta(days=window)
    hi = day + dt.timedelta(days=window)
    scored: list[tuple[float, int]] = []
    for nid, payload in store.iter_payloads():
        if not isinstance(payload, dict):
            continue
        if payload.get("status") == "outdated":
            continue
        if domain and (payload.get("domain") or "").strip() != domain.strip():
            continue
        if block and not domain_in_block(node_domain(payload), block):
            continue
        dates = payload.get("dates")
        if not isinstance(dates, dict):
            continue
        for iso in dates.get("due") or []:
            try:
                d = dt.date.fromisoformat(str(iso))
            except ValueError:
                continue  # 脏数据（非 ISO 日期串）：跳过该条，不炸整次扫描
            if lo <= d <= hi:
                # 只要有任一到期日落在窗口内就入选；排序按声明时间（created_at）
                scored.append((float(payload.get("created_at") or 0.0), nid))
                break
    scored.sort(key=lambda t: (t[0], t[1]))
    return [nid for _created, nid in scored[:limit]]


def resolve_today(today: str = "") -> dt.date:
    """读侧「今天」的解析：接受 ``YYYY-MM-DD`` 或完整 ISO；空 / 非法 → 本机今天。

    非法值降级为本机今天并记 warning（到期扫描是检索的一条附加通道，主链
    不能因一个参数挂掉；与「空 = 本机今天」的默认语义保持一致）。
    """
    s = (today or "").strip()
    if not s:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        pass
    ref = _parse_iso_datetime(s)
    if ref is not None:
        return ref.date()
    logger.warning("today 参数无法解析为日期（%r），回退本机今天", s[:40])
    return dt.date.today()


# ---- 内部工具 -------------------------------------------------------------


def _parse_iso_datetime(raw: str) -> dt.datetime | None:
    """解析 ISO 时间戳（``2026-09-05T17:45:00+08:00`` / ``2026-09-05``）；失败返回 None。

    带时区的时间先换算到本地墙钟再丢掉时区：相对日期（「明天」是哪个明天）
    按消息发生地本地的日子算。
    """
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")  # Python 3.10 的 fromisoformat 不认 Z 后缀
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _resolve(name: str, m: re.Match[str], ref: dt.datetime) -> dt.date | None:
    """单个模式匹配 → 绝对日期；不认识/算不出返回 None（非法日期在此抛 ValueError）。"""
    s = m.group(0)
    if name == "abs":
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if name == "abs_md":
        return dt.date(ref.year, int(m.group(1)), int(m.group(2)))
    if name == "rel_day":
        return (ref + dt.timedelta(days=_REL_DAY_OFFSETS[m.group(1)])).date()
    if name in ("rel_nd", "rel_prev_nd"):
        n = cn2int(m.group(1))
        return (ref + dt.timedelta(days=n)).date() if n is not None else None
    if name in ("rel_week_later", "rel_week_later2"):
        g = m.group(1)
        if g == "半":
            # 「半个星期后」口语约等于 3~4 天，取 3：宁早勿晚，到期当天扫得到
            return (ref + dt.timedelta(days=3)).date()
        n = cn2int(g)
        return (ref + dt.timedelta(weeks=n)).date() if n is not None else None
    if name == "rel_half_month":
        # 「半个月后」= 15 天的口语约定
        return (ref + dt.timedelta(days=15)).date()
    if name == "weekday":
        prefix = (m.group(1) or "").strip()
        wd = WEEKDAY[m.group(2)]
        base = ref.date()
        cur = base.weekday()
        if prefix.startswith("下下"):
            return base + dt.timedelta(days=(7 - cur) + 7 + wd)
        if prefix.startswith("下"):
            return base + dt.timedelta(days=(7 - cur) + wd)
        if prefix.startswith("上"):
            return base - dt.timedelta(days=cur + (7 - wd) % 7)
        # 这周 / 本 / 无前缀：取本周内未来的那个（含今天）
        return base + dt.timedelta(days=(wd - cur) % 7)
    if name == "month_day":
        prefix = (m.group(1) or "").strip()
        day = cn2int(m.group(2))
        if day is None:
            return None
        y, mo = ref.year, ref.month
        if prefix.startswith("下"):
            mo += 1
            if mo > 12:
                mo, y = 1, y + 1
        # 2月31号这类非法日期由 dt.date 构造抛 ValueError，调用方按「跳过」处理
        return dt.date(y, mo, day)
    if name == "month_end":
        y, mo = ref.year, ref.month
        if "下" in s:
            mo += 1
            if mo > 12:
                mo, y = 1, y + 1
        first_of_next = dt.date(y + (mo == 12), (mo % 12) + 1, 1)
        return first_of_next - dt.timedelta(days=1)
    if name == "month_start":
        y, mo = ref.year, ref.month
        if "下" in s:
            mo += 1
            if mo > 12:
                mo, y = 1, y + 1
        return dt.date(y, mo, 1)
    return None  # 周期等未支持表达：不当成单日到期
