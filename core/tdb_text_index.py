"""
TDB（TriviumDB）BM25 文本通道 —— 第二路中文文本召回（默认关闭）。

背景（2026-10-24 生产库副本实测，2714 节点）：
    FTS5 trigram 对 2 字短词打不到（回落 LIKE 全表扫、top3 全无关），
    对 6~8 字长短语走整串 MATCH 几乎 0 命中；TDB 的 CJK 2-gram tokenize
    在这些场景召回与排序明显更好，且 .text 侧车远小于 fts.db。
    本通道与 FTS5 并存、由 `Config.TDB_TEXT_ENABLED` 控制、**默认关闭**；
    供 A/B 对比（scripts/tdb_text_ab.py），不做任何默认切换。

必须直面的两个 TDB 特性（不给它机会静默吃查询）：
    1. `load_text_index` 默认 False → 不加载 .text 侧车时 `TEXT BM25`
       **静默返回 0 条、不抛错**（实测 0.4~1.0ms）。本模块：
       - search 每次用 `load_text_index=True` 显式加载；开启前先自查
         侧车文件存在，缺失直接判 `degraded`，绝不把「没加载」演成空结果。
       - 进程内必须先 `warmup()` 成功过一次，否则直接 `degraded`
         （冷启动未预热 = 通道不可用，与「确实无命中」语义不同）。
    2. 没有独立文本 API，检索只能走 TQL：
       `TEXT BM25 "查询" TOP n AS hits WITH hits RETURN hits`。

用法（对齐 core/fts_index.py 的风格）：

    from core.tdb_text_index import warmup, search, index_node, rebuild

    warmup()                      # 启动自检/开启通道后预热一次
    rows, status = search("兜底")  # rows=[{"node_id": int}], status ∈ ok/empty/degraded
    index_node(nid, content)      # 空 content = 清除该节点正文
    rebuild(store)                # 全量重建 + flush
"""

import logging
import os
import time
from typing import Any, Literal

import triviumdb

logger = logging.getLogger(__name__)

# 进程内「本通道已预热」标记。TriviumDB 的文本索引是连接内存态（.text 侧车
# 每次打开显式 load），warmup 只需要成功过一次——它负责确保侧车存在且 BM25
# 结构已重建/加载。未预热时 search 一律 degraded（失败路径留痕，不装空结果）。
_warmed: bool = False

# 撞「连接级排他」时的开库重试（TriviumDB 同一时刻只允许一个连接持有库；
# 与 REST / CLI 的连接窗口相撞时短暂重试，两连败再按不可用降级）。
_OPEN_ATTEMPTS = 3
_OPEN_RETRY_DELAY_S = 0.05


def _db_path() -> str:
    from config import Config  # 延迟导入：测试按路径 monkeypatch Config.DB_PATH

    return Config.DB_PATH


def _sidecar_path() -> str:
    """`.text` 侧车文件路径（实测文件名形如 mh_memory.db.text）。"""
    return _db_path() + ".text"


def _dim() -> int:
    """文本连接维度：只读构造 TriviumStore 复用维度判定逻辑（零副作用）。"""
    from core.trivium_store import TriviumStore

    return TriviumStore(read_only=True).dim


def _acquire(
    access_mode: Literal["read_write", "read_only"],
    load_text_index: bool,
):
    """开一个持有进程级访问锁的短连接（与 TriviumStore 同锁、同排他语义）。

    撞连接级排他（库被 REST/CLI 窗口占用）时短退避重试 `_OPEN_ATTEMPTS` 次，
    仍败向上抛 RuntimeError，由调用方按契约降级/报错。
    """
    from core.trivium_store import _DB_ACCESS_LOCK, _LockedDB

    for attempt in range(_OPEN_ATTEMPTS):
        _DB_ACCESS_LOCK.acquire()
        try:
            db: Any = triviumdb.TriviumDB(
                _db_path(),
                dim=_dim(),
                access_mode=access_mode,
                load_text_index=load_text_index,
            )
            return _LockedDB(db, _DB_ACCESS_LOCK)
        except Exception:
            # 含 DatabaseBusyError（同库被占）与其他失败：本窗口锁先释放，
            # 末次尝试才把异常上抛（由调用方按契约降级/报错）。
            _DB_ACCESS_LOCK.release()
            if attempt == _OPEN_ATTEMPTS - 1:
                raise
            time.sleep(_OPEN_RETRY_DELAY_S)
    raise RuntimeError("unreachable")  # pragma: no cover


def _clean_query(query: str) -> str:
    """剥掉双引号：TQL 字符串字面量用 " 包裹，内嵌引号直接 parse error
    （实测「查询解析错误： Unterminated string literal」）——检索场景引号
    无区分价值，剥掉比整路 degraded 合算。"""
    return (query or "").replace('"', "").strip()


def warmup() -> bool:
    """预热 TDB 文本索引：短连接重建 BM25 结构并 flush 侧车。

    - 成功：置进程内预热标记，日志记耗时；返回 True。
    - 失败：warning 留痕、返回 False、**不抛出**（自检/启动路径不阻塞）。
    """
    global _warmed
    t0 = time.perf_counter()
    try:
        with _acquire("read_write", load_text_index=False) as db:
            db.build_text_index()  # 实测生产库副本 2714 节点 ≈169ms
            db.flush()
        _warmed = True
        cost_ms = (time.perf_counter() - t0) * 1000
        logger.info("TDB 文本索引预热完成 build+flush=%.1fms (path=%s)", cost_ms, _db_path())
        return True
    except Exception as e:  # noqa: BLE001 —— 预热失败仅告警返回 False 不阻塞启动
        logger.warning("TDB 文本索引预热失败: %s (path=%s)", e, _db_path())
        return False


def index_node(node_id: int, content: str) -> bool:
    """索引/更新单个节点的文本（唯一写入口 = db.index_text）。

    - `content` 非空 → 写入该节点文本；空串/None → 清除该节点正文（实测
      `index_text(id, "")` + flush 后该节点不再命中）。
    - 连接用 `load_text_index=True`：侧车在 → 载入既有文本再增量写，防
      「未加载内存态写 flush 把整个侧车洗成单条」的破坏性侧车洗写。
    - 返回 bool：True=成功；False=失败（失败 warning 不抛，写路径容错同
      fts_index.sync_node 的契约）。
    """
    global _warmed
    try:
        text = (content or "").strip()
        with _acquire("read_write", load_text_index=True) as db:
            db.index_text(int(node_id), text)
            db.flush()
        _warmed = True  # 写入即侧车已建立且可查（「写完可查」的通道可用性）
        logger.info("TDB 文本索引已同步 node=%s (%s)", node_id, "写入" if text else "清除")
        return True
    except Exception as e:  # noqa: BLE001 —— 写索引失败仅告警返回 False 不破坏契约
        logger.warning("TDB 文本索引同步失败 node=%s: %s", node_id, e)
        return False


def search(query: str, limit: int = 10) -> tuple[list[dict], str]:
    """TDB BM25 文本检索（带通道状态，语义对齐 `fts_index.search_fts_status`）。

    返回 `(rows, status)`：
    - `"ok"`       —— 查询正常执行（rows 可能为空，表示确实无命中）
    - `"empty"`    —— 空查询，未执行检索
    - `"degraded"` —— 通道不可用：进程未预热 / .text 侧车缺失 / TQL 抛异常
                     （含连接级排他重试耗尽）。
    """
    global _warmed
    cleaned = _clean_query(query)
    if not cleaned:
        return [], "empty"
    if not _warmed:
        # 未预热时的「静默 0 条」是 TDB 的固有坑（load_text_index=False 必然
        # 静默空）——这正是任务要求设 degraded 的那条失败路径。
        logger.debug("TDB 文本通道未预热（warmup 未成功过），按不可用降级")
        return [], "degraded"
    if not os.path.exists(_sidecar_path()):
        logger.warning("TDB 文本侧车缺失（%s），文本通道不可用", _sidecar_path())
        return [], "degraded"
    top_k = max(1, int(limit or 0))  # TOP 0 会抛「超出预算」，先归一
    try:
        with _acquire("read_only", load_text_index=True) as db:
            rows = db.tql(f'TEXT BM25 "{cleaned}" TOP {top_k} AS hits WITH hits RETURN hits')
        out: list[dict] = []
        for row in rows:
            hit = (row.row or {}).get("hits") or {}
            nid = hit.get("id")
            if nid is not None:
                out.append({"node_id": int(nid)})
        return out, "ok"
    except Exception as e:  # noqa: BLE001 —— 检索失败返回空列表查询侧天然降级
        logger.warning("TDB 文本检索失败（%s：%s），通道降级", type(e).__name__, str(e)[:120])
        return [], "degraded"


def rebuild(store) -> int:
    """全量重建 TDB 文本索引：按 store.payload.content 逐条 index_text + flush。

    文本索引的唯一写入口是 index_text（`build_text_index()` 只重建 BM25 结构，
    不从 payload 收文本——探针实测），所以重建必须显式逐条写。与
    `fts_index.rebuild(store)` 对称。返回索引条目数（有正文的节点数）。
    """
    # 先在锁外收全 payloads：文本连接持有库期间，store 的第二个连接
    # 会因 TriviumDB 库文件级排他直接打不开（连接必须先关再开）。
    node_texts: list = []
    for nid, payload in store.iter_payloads():
        content = payload.get("content", "")
        if content:
            node_texts.append((int(nid), str(content)))
    entries = 0
    text_nids = {t[0] for t in node_texts}  # 本轮缺失即视为该节点正文清空
    with _acquire("read_write", load_text_index=False) as db:
        db.build_text_index()  # 无侧车时也能安全调用（空起点）
        # 全量覆盖语义：主库已存在节点一律写正文（空=清除），不残留旧文本
        for nid, text in node_texts:
            db.index_text(nid, text)
            entries += 1
        for nid in set(db.all_node_ids()) - text_nids:
            db.index_text(int(nid), "")  # 清掉无正文章节点的残留文本
        db.flush()
    global _warmed
    _warmed = True  # 重建即预热：侧车已存在且与主库内容对齐
    logger.info("TDB 文本索引全量重建完成 entries=%s (path=%s)", entries, _db_path())
    return entries


def is_warmed() -> bool:
    """暴露预热状态（测试与自检用）。"""
    return _warmed


def reset_warmed_for_test() -> None:  # pragma: no cover
    """显式复位预热标记（仅供测试隔离使用）。"""
    global _warmed
    _warmed = False
