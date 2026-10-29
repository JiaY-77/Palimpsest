"""TriviumDB 存储封装"""

import atexit
import contextlib
import logging
import os
import threading
import time
from typing import Any, Literal

import triviumdb

from config import Config
from core import confidence
from core import tdb_text_index as _tdb_text
from core.fts_index import remove_node, sync_node
from core.secret_scan import SecretScanError, scan_secret_classified

# 「库被占用」的标记表与判定函数统一放 core/utils.py（db_health 共用同一份）；
# _DB_LOCK_MARKERS / _is_db_locked_error 再导出，保持既有导入方与测试可用。
from core.utils import _DB_LOCK_MARKERS, _is_db_corrupt_error, _is_db_locked_error, _to_float  # noqa: F401

logger = logging.getLogger(__name__)


# 进程内 DB 访问串行锁 + 共享连接注册表（issue #167 单例长连接）
# ------------------------------------------------------------
# triviumdb 是**连接级排他**的：只要有一个连接持着库，第二个连接连打开都会失败
# （报 `Database locked / already opened`）。
#
# 此前 REST 端点是 `async def` + 阻塞同步代码，被事件循环**顺带串行化**，这个冲突
# 被掩盖了。端点改同步 def（FastAPI 丢线程池）后请求真并发，并发 open 会失败——
# 而检索类失败路径「吞掉 + 返回空列表」，表现为**静默返回空结果**（实测 4 并发
# 100 次检索：命中 44、静默空结果 56）。
#
# 两件套解法：
#   1) 进程级共享连接注册表（_SHARED_CONNECTIONS）——每个 (库路径, 访问模式)
#      只开**一条长连接**，全程复用。不再每操作重开库、重读侧车、走 generation
#      记账（历史实测单次开库 ~20-60ms，检索热路径纯浪费）。
#   2) 这把锁——让同进程内的 DB 访问**排队而不是失败**：慢的那部分（embedding
#      HTTP）在锁外，仍然并发；只有真正碰库的操作窗口串行（锁语义见 _LockedDB）。
# RLock 而非 Lock：同一线程嵌套取连接时不自锁死；单例后嵌套取到的是同一条
# 连接（每层代理各持一个窗口，close 各释放一次，计数平衡）。
# 注册表的读写只发生在 _DB_ACCESS_LOCK 持有期间（_acquire_shared /
# try_acquire_existing / shutdown），故不需要第二把注册表锁。
_DB_ACCESS_LOCK = threading.RLock()

# 进程级共享连接注册表：{(归一化库路径, 访问模式): TriviumDB 实例}。
# 同一 db_path 实际只会有一种模式的连接：reader 与 writer 在 triviumdb 层互斥，
# 第二种模式的请求会被 AccessModeConflictError 拒绝（见 _acquire_shared）。
_SHARED_CONNECTIONS: dict[tuple[str, str], Any] = {}


def _norm_db_path(path: str) -> str:
    """注册表键归一：绝对路径 + Windows 大小写/分隔符归一（同库不同写法视为同库）。"""
    return os.path.normcase(os.path.abspath(path))


# 进程内本地 embedding 模型（provider=local）单例：
# 懒加载 + 双重检查锁——TriviumStore() 构造时绝不装模型（GGUF 700MB 级内存），
# 第一次真正要向量才加载；加载失败不缓存（下次调用可重试）。
_LOCAL_LLM: Any = None  # llama_cpp.Llama（延迟导入，未装包时保持 None）
_LOCAL_LLM_LOCK = threading.Lock()


class _LockedDB:
    """DB 访问锁的「操作窗口」代理：close / with 退出 = 窗口结束，**只释放锁**。

    issue #167 前锁跟着连接走（连接 close 即关库 + 释锁，每操作开关一次）；
    单例长连接后连接不再关（由模块级注册表持有复用），锁改为跟着**操作窗口**
    走：``_acquire()`` 取到代理即开窗，``close()`` / ``with`` 退出 / 生成器
    ``finally`` close 即关窗释放锁——底层连接**不关闭**。若 close 仍关闭连接，
    下一次 ``_acquire()`` 就得重开库，单例失去意义；且同线程嵌套取连接时内层
    close 会把外层正用的连接关掉。

    close 幂等：同一代理多次 close 不得重复 release（RLock 过量 release 会
    RuntimeError），靠置空 ``_release_lock`` 保证只释放一次。

    为什么用代理而不是锁在方法里：既有代码有三类连接用法——`with self._acquire()
    as db:`、显式 `db.close()`、生成器（`iter_payloads` 等）在 `finally` 里
    close——代理让所有这些调用点一行都不用改。
    """

    __slots__ = ("_release_lock", "_wrapped")

    def __init__(self, wrapped, release_lock):
        self._wrapped = wrapped
        self._release_lock = release_lock

    def __getattr__(self, item):
        return getattr(self._wrapped, item)

    def close(self) -> None:
        """结束操作窗口：只释放锁，**不关闭底层连接**（连接由注册表持有复用）。"""
        lock, self._release_lock = self._release_lock, None
        if lock is not None:
            lock.release()

    def __enter__(self) -> "_LockedDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # 兜底：生成器被提前丢弃、close 未被调用时不至于永久占锁
        with contextlib.suppress(Exception):
            self.close()


class EmbeddingUnavailableError(Exception):
    """Embedding 服务不可用。

    当 Ollama / OpenAI 兼容 embedding 后端在生成向量时失败（网络不可达、
    HTTP 错误、无 API key、返回空向量等）抛出。调用方应将其视为 fail-fast：
    异常自然向上传播，而不是静默降级为全零向量（全零向量会污染检索排序，
    危及 RAG 正确性）。异常消息含修复指引，便于运维快速定位。
    """


class EmbeddingInputTooLongError(EmbeddingUnavailableError):
    """输入超出 embedding 模型可处理长度。

    与 `EmbeddingUnavailableError` 语义拆开（issue #121）：模型侧明确报
    「input length exceeds the context length」时抛本类——服务在跑、模型在，
    `503「检查 Ollama 是否启动」`会把排查方向带偏。REST 侧映射为 **422**。
    继承 `EmbeddingUnavailableError`：既有按后者捕获的调用方（如 pre-turn
    兜底、插件适配）继续生效，新代码用本类精确定位。
    """


class DatabaseBusyError(RuntimeError):
    """记忆库被其他进程占用，无法打开。

    继承 `RuntimeError` 是**有意为之**：triviumdb 的锁错误本身以 `RuntimeError`
    抛出，既有调用方与测试（如 tests/test_concurrency.py 的写者互斥用例）都按
    `RuntimeError` 捕获。继承它可让「新增精确类型」与「既有契约」两全——新代码
    用 `except DatabaseBusyError` 精确定位，老代码的 `except RuntimeError` 照旧生效。

    TriviumDB 对库文件是**连接级排他**的：只要有一个连接持着库，第二个连接
    连打开都会失败（报 `Database locked / already opened`）。Palimpsest 的常规
    模式是「每操作开-关库」，所以其他入口有时能抢到空闲窗口、有时不能——故障
    表现为**间歇性**；一旦静默吞掉，就退化成「偶发写失败 → 文件组残留 →
    generation 校验不过 → 库从可读写变读不动」，且会自我复制。

    抛出本异常即 fail-fast：把「不知为何失败」变成「明确告知被占用 + 怎么处理」。
    异常消息含库路径与处理指引。
    """


class AccessModeConflictError(RuntimeError):
    """同一进程内同一库被以第二种访问模式请求（issue #167）。

    背景：注册表按 (库路径, 访问模式) 分键，但 TriviumDB 的 reader 与 writer
    **互斥**——同一 db_path 在进程内只能存在一条连接，单例长连接只能取一种
    模式。若同一路径已被以 read_write 打开、又来一个 read_only 请求（或反之），
    既不能静默复用错的连接（read_only 连接上写会失败；read_write 连接不满足
    「只读不推进 generation」的 #53 契约），也不能静默失败——fail-fast 抛出
    本异常，消息写清原因与出路。继承 RuntimeError：与 DatabaseBusyError 同理，
    既有按 RuntimeError 捕获的调用方（REST 503 映射等）继续生效。
    """


def _open_shared_connection(db_path: str, dim: int, access_mode: Literal["read_write", "read_only"]) -> Any:
    """进程内共享连接的**唯一**开库点：所有构造参数集中在此（issue #167）。

    - load_text_index=True（**必须**，不许 False）：
      同一库在进程内只此一条连接，读（TDB 文本检索）写（index_text）共用；
      而 TriviumDB **未加载内存态就写 + flush 会把整个 .text 侧车洗成单条**
      （破坏性，见 core/tdb_text_index.index_node 的注释）。单例没有「读连接
      不带侧车」的余地，必须统一带上。成本可忽略：无侧车时 open(load=True)
      63ms vs open(load=False) 18ms，一次性差 45ms（2026-10 实测；当前
      TDB_TEXT_ENABLED 默认 0、生产库无 .text 侧车，几乎零成本）。
      ★ 将来若启用 TDB 文本（TDB_TEXT_ENABLED=1）且侧车长大（历史峰值
      11.4MB），**必须重测**侧车加载耗时与常驻内存，再决定是否仍值得常驻。
    - auto_build_quiver=False：与既有行为一致——不跟随上游 0.9.0 收紧的自动
      构图默认阈值（dim=1024 在 652 节点即触发，本库 3000+ 节点会长期处于
      自动构图状态；小帕当前检索路径不依赖 QuIVer）。
    - sync_mode 保持上游默认 normal：写 WAL 到 OS 缓冲区，进程崩溃不丢已提交
      数据；全量落盘 .tdb 靠 flush()（进程退出路径见 shutdown）。
    """
    return triviumdb.TriviumDB(
        db_path,
        dim=dim,
        access_mode=access_mode,
        load_text_index=True,
        auto_build_quiver=False,
    )


def _acquire_shared(db_path: str, access_mode: Literal["read_write", "read_only"], dim: int) -> "_LockedDB":
    """取 (库路径, 访问模式) 共享连接的操作窗口代理；没有则开一条并登记。

    调用方必须以代理 close()/with 退出结束窗口（释放 _DB_ACCESS_LOCK）。
    - 库被**其他进程**占用（连接级排他）→ fail-fast 抛 DatabaseBusyError；
    - 同库已被以**另一种访问模式**打开 → 抛 AccessModeConflictError（说明见类）。
    """
    key_path = _norm_db_path(db_path)
    _DB_ACCESS_LOCK.acquire()
    try:
        for p, m in _SHARED_CONNECTIONS:
            if p == key_path and m != access_mode:
                raise AccessModeConflictError(
                    f"同一进程内同一记忆库只允许一种访问模式：{db_path} 已以 "
                    f"{m} 打开（进程内单例长连接，reader/writer 在 TriviumDB 层互斥，"
                    f"不能并存第二条），不能再以 {access_mode} 请求。"
                    f"请复用已有 TriviumStore 实例，或改用 REST 服务"
                    f"（http://127.0.0.1:8090，含 /mcp）。"
                )
        conn = _SHARED_CONNECTIONS.get((key_path, access_mode))
        if conn is None:
            conn = _open_shared_connection(db_path, dim, access_mode)
            _SHARED_CONNECTIONS[(key_path, access_mode)] = conn
        return _LockedDB(conn, _DB_ACCESS_LOCK)  # 锁随代理交棒，close 时释放
    except Exception as e:
        _DB_ACCESS_LOCK.release()  # 未交棒成功：本窗口自释，防锁泄漏
        if _is_db_locked_error(e):
            raise DatabaseBusyError(
                f"记忆库被其他进程占用，无法打开：{db_path}\n"
                f"底层错误：{e}\n"
                f"处理：同一时刻只允许一个进程访问该库。请先停止占用它的进程"
                f"（常见为 Palimpsest REST 服务 / dashboard / 另一个 CLI 调用），"
                f"或改为访问已运行的 REST 服务（http://127.0.0.1:8090，含 /mcp）。\n"
                f"提示：同进程内的并发访问由共享长连接排队（issue #167），"
                f"不会触发本错误——它只可能来自另一个进程。"
            ) from e
        raise


def try_acquire_existing(db_path: str) -> "_LockedDB | None":
    """该库在本进程已有共享连接时取一个窗口代理；没有则返回 None（**绝不新开**）。

    供 db_health / dims 等「探测类」调用方复用共享连接——单例常开后，它们若
    裸开第二条连接必被连接级排他拒绝，会把「自己占着自己」误报成库被占用。
    """
    key_path = _norm_db_path(db_path)
    _DB_ACCESS_LOCK.acquire()
    try:
        for (p, _m), conn in _SHARED_CONNECTIONS.items():
            if p == key_path:
                return _LockedDB(conn, _DB_ACCESS_LOCK)  # 锁随代理交棒
    except BaseException:
        _DB_ACCESS_LOCK.release()
        raise
    _DB_ACCESS_LOCK.release()  # 未找到、未交棒：本窗口自释
    return None


def shutdown() -> None:
    """flush 并关闭进程内**所有**共享连接，清空注册表（issue #167 退出落盘）。

    三个入口：main.py 的 ASGI lifespan shutdown（优雅退出）、atexit 兜底
    （脚本 / CLI / 测试进程退出）、测试隔离钩子 reset_connections_for_test。

    - 持 _DB_ACCESS_LOCK 执行：等在飞操作窗口结束、阻止新窗口；
    - read_write 连接先 flush()（WAL → .tdb 全量落盘）再 close()；flush 失败
      只记日志**不抛**——sync_mode=normal 下已提交事务在 WAL 里不依赖本次
      flush 持久化，绝不因落盘失败阻塞进程退出；
    - 关闭后清空注册表：下一次 _acquire() 会重新建连接（测试复位语义）。
    """
    _DB_ACCESS_LOCK.acquire()
    try:
        entries = list(_SHARED_CONNECTIONS.items())
        _SHARED_CONNECTIONS.clear()
        for (path, mode), conn in entries:
            if mode == "read_write":
                try:
                    conn.flush()
                except Exception as e:  # noqa: BLE001 —— flush 失败不阻塞退出
                    logger.warning("共享连接 flush 失败（不阻塞退出）: %s: %s", path, e)
            try:
                conn.close()
            except Exception as e:  # noqa: BLE001 —— 关闭失败同样不阻塞退出
                logger.warning("共享连接关闭失败（忽略）: %s: %s", path, e)
    finally:
        _DB_ACCESS_LOCK.release()


def reset_connections_for_test() -> None:
    """测试隔离钩子：关闭并清空全部共享连接（等价 shutdown，命名表意给测试用）。

    单例后 ``_acquire().close()`` 只释放操作窗口、连接仍被注册表持有——既有
    测试里「close 掉以释放库文件」的清理语义必须改用本钩子：fixture teardown
    在 rmtree 临时库**之前**、子进程/裸连接要接管库文件**之前**调用。
    """
    shutdown()


# 进程退出兜底落盘：优雅路径走 main.py lifespan 的 ASGI shutdown；这里兜住
# 脚本 / CLI / 测试进程等非 lifespan 退出。shutdown 内部吞掉 flush/close 异常，
# 不干扰解释器退出。
atexit.register(shutdown)


# 图谱扩散参数来自 Config（性能优化）：每节点最多扩散最强 N 条边；弱边阈值。

# 出厂通用区块（domain 分组概念：图谱分区块防跨域污染）。
# novel = 小说创作设定库（本地小说 vault 设定语料）。
DEFAULT_BLOCKS = ("task", "kb", "hermes", "novel", "general")

# 命中计数缓冲回写阈值/周期：检索返回热路径只做内存累加，缓冲累计
# _HIT_FLUSH_THRESHOLD 次命中、或距上次回写超过 _HIT_FLUSH_INTERVAL 秒
# 才批量合并回写一次（见 TriviumStore.flush_hit_counts）。
_HIT_FLUSH_THRESHOLD = 50
_HIT_FLUSH_INTERVAL = 30.0


def is_valid_block(block: str) -> bool:
    """区块合法性校验：空串（全量模式）或出厂通用区块为 True，否则 False。"""
    if not block:
        return True
    return (block or "").strip().lower() in DEFAULT_BLOCKS


def domain_in_block(node_domain: str, block: str) -> bool:
    """区块匹配（分区块）：domain 是否属于 block。"""
    d = (node_domain or "general").strip().lower()
    b = (block or "").strip().lower()
    if not b:
        return True
    return d == b


def node_domain(payload: dict) -> str:
    """节点区块域：统一读 payload.domain；character_name 为历史兼容回退（2026-08-29 迁移期）。

    迁移目标：所有节点用 domain 字段表达区块归属，character_name 退役。
    当前兼容期：domain 优先，缺失时回退 character_name（老数据），再缺省 general。
    注意：无 domain 的节点（general）在带 block 的分区查询中会被彻底忽略
    （既不能当起点也不能当邻居）——「无标签 = 未分类」，隔离场景下丢弃防污染；
    只有全量模式（block 为空）才可见。
    """
    return (payload.get("domain", "") or payload.get("character_name", "") or "general").strip().lower()


def _drop_outdated_candidates(scored: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """候选层过滤：剔除 status="outdated" 的节点。

    被新版取代的旧记忆不应占用候选名额（6x 候选余量中有约 24% 是 outdated）。
    """
    return [(s, n) for s, n in scored if (n.get("payload", {}) or {}).get("status") != "outdated"]


def _rerank_by_decay(scored: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """按 MEMORY_RERANK_MODE 做时间衰减重排，返回新列表。

    hard（旧版，可回退）：非 kb_chunk 乘性硬加权
        final = score × importance × decay^(days/30)
    soft（默认）：语义分为主线 + ε 级元数据微调
        非 kb_chunk: final = score + ε × (importance_norm + recency_norm)
        kb_chunk:    final = score + ε × KB_SOFT_RERANK_MULT
    kb_chunk（知识不老化）在两种模式下都不做时间衰减。
    """
    mode = getattr(Config, "MEMORY_RERANK_MODE", "soft")
    decay = getattr(Config, "MEMORY_DECAY_FACTOR", 1.0)
    now = time.time()

    if mode == "hard":
        if decay == 1.0:
            return scored
        reranked = []
        for score, node in scored:
            payload = node.get("payload", {}) or {}
            if payload.get("type") == "kb_chunk":
                reranked.append((score, node))
                continue
            days = _days_since_created(payload.get("created_at"), now)
            importance = _to_float(payload.get("importance"), 0.5)
            reranked.append((score * importance * (decay ** (days / 30.0)), node))
        return reranked

    eps = getattr(Config, "SOFT_RERANK_EPS", 0.02)
    kb_mult = getattr(Config, "KB_SOFT_RERANK_MULT", 1.5)
    conf_eps = getattr(Config, "MEMORY_CONFIDENCE_EPS", 0.0)
    conf_center = getattr(Config, "MEMORY_CONFIDENCE_CENTER", 0.27)
    conf_span = getattr(Config, "MEMORY_CONFIDENCE_SPAN", 0.15)
    reranked = []
    for score, node in scored:
        payload = node.get("payload", {}) or {}
        # 置信度只做 ε 级 tie-break，默认关闭（conf_eps=0 时零开销）。
        # 不做硬门槛：实测按 b 过滤会让召回掉 4.81pp（见 docs/CONFIDENCE.md）。
        conf_bonus = conf_eps * confidence.normalized(payload, conf_center, conf_span) if conf_eps else 0.0
        if payload.get("type") == "kb_chunk":
            reranked.append((score + eps * kb_mult + conf_bonus, node))
            continue
        days = _days_since_created(payload.get("created_at"), now)
        importance = _to_float(payload.get("importance"), 0.5)
        importance_norm = max(-1.0, min(1.0, (importance - 0.5) / 0.5))
        recency_norm = decay ** (days / 30.0)
        reranked.append((score + eps * (importance_norm + recency_norm) + conf_bonus, node))
    return reranked


def _filter_candidates_by_block(scored: list[tuple[float, dict]], block: str) -> list[tuple[float, dict]]:
    """block 后置过滤：domain 匹配 block 时保留，否则丢弃。"""
    return [(s, n) for s, n in scored if domain_in_block(node_domain(n.get("payload", {})), block)]


# 模型侧「输入超上下文」错误关键词（issue #121：Ollama 实测 50,000 字符 prompt
# 返回 HTTP 500 {"error": "the input length exceeds the context length"}）。
_EMBEDDING_TOO_LONG_MARKERS = (
    "exceeds the context length",
    "context length exceeded",
    "maximum context length",
)


def _raise_embedding_http_error(response: Any, provider: str) -> None:
    """HTTP 状态判别（issue #121）：模型侧「输入超上下文」与「服务不可用」拆开。

    响应体含模型侧长度错误关键词 → 抛 `EmbeddingInputTooLongError`（REST 侧
    映射 422）；其余 HTTP 错误按 ``raise_for_status`` 原样抛出，由调用方外层
    统一包成 `EmbeddingUnavailableError`（503 语义不变），排查方向不再被
    「检查 Ollama 是否启动」的误译带偏。
    """
    try:
        response.raise_for_status()
        return
    except Exception as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = (response.text or "").lower()
        if any(marker in body for marker in _EMBEDDING_TOO_LONG_MARKERS):
            raise EmbeddingInputTooLongError(
                f"输入超出 embedding 模型可处理长度（{provider}）：{(response.text or '')[:200]}"
            ) from exc
        raise


class TriviumStore:
    """封装 TriviumDB 操作，提供记忆存储和检索接口"""

    def __init__(self, read_only: bool = False) -> None:
        self.db_path = Config.DB_PATH
        # issue #53：预览/扫描类操作（dry-run）此前也以 read_write 打开库并推进
        # generation，无法与 REST 并发。read_only=True 时所有连接走只读模式，
        # 不再写库、不再抢写锁。默认 False 保持既有行为不变。
        self.read_only = read_only
        # embedding 维度从配置读取（按 provider 显式三分支：local 进程内 llama.cpp /
        # ollama 本地服务 / openai 兼容云端——不用 else 兜底，新 provider 编译期就暴露）
        self.provider = getattr(Config, "EMBEDDING_PROVIDER", "local") or "local"
        if self.provider == "local":
            self.dim = Config.EMBEDDING_LOCAL_DIM
        elif self.provider == "openai":
            self.dim = Config.EMBEDDING_DIM
        else:  # ollama
            self.dim = Config.OLLAMA_EMBEDDING_DIM
        # 性能优化：常用字段索引建一次即可，不再随每次 insert_node 重建
        # issue #53：只读实例不建索引（索引创建是写操作，只读连接会失败）。
        if not read_only:
            self._init_indexes()
        # 命中计数内存缓冲（检索返回热路径零写库）：
        #   _hit_buffer        {node_id: [累计次数, 最近命中时间戳]}
        #   _hit_pending_total 缓冲内待回写的命中总数（达阈值/超时触发回写）
        #   _hit_last_flush    上次回写时刻（周期触发用）
        #   _hit_lock          缓冲读改写互斥（并发检索不丢计数）
        self._hit_buffer: dict[int, list] = {}
        self._hit_pending_total: int = 0
        self._hit_last_flush: float = time.time()
        self._hit_lock = threading.Lock()

    def _init_indexes(self) -> None:
        """一次性创建常用 payload 字段索引（Hash / Ordered / Composite / Bitmap）。

        索引策略（0.8.3 升级）：
          - Hash（create_index）：type / domain /character_name —— 精确匹配，兼容既有路径
          - Ordered（create_ordered_index）：importance —— 范围查询 / 排序
          - Composite（create_composite_index）：('type','domain') —— 组合过滤
          - Bitmap（create_bitmap_index）：status —— 枚举值过滤（active/outdated）

        所有 create_*_index 幂等（已存在静默成功）；**索引本身**创建失败静默降级，
        不影响启动。但两类错误**不降级**、直接 fail-fast：①「库被其他进程占用」
        ——那属于环境冲突而非索引问题；②「存储 generation 损坏」——库实际不可用。
        静默吞掉这两类致命状态会把它们都伪装成「一切正常」（startup-check 全绿而
        库已不可用），正是库退化 / 延误恢复的起点。
        """
        db = None
        try:
            db = self._acquire()
            for field in ("type", "domain", "character_name"):
                db.create_index(field)
            db.create_ordered_index("importance")
            db.create_composite_index(("type", "domain"))
            db.create_bitmap_index("status")
        except (DatabaseBusyError, AccessModeConflictError):
            raise  # 库被其他进程占用 / 访问模式冲突 → fail-fast，不伪装成「索引降级」
        except Exception as e:
            if _is_db_corrupt_error(e):
                # 存储 generation 损坏 → fail-fast：静默降级会让「库已损坏」伪装成
                # 「索引创建失败」，启动检查全绿而库实际不可用（延误恢复）。
                raise
            logger.warning(f"初始化字段索引失败（静默降级）: {e}")
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    def _acquire(self) -> Any:
        """获取共享连接的操作窗口代理（with 块 / 显式 close 结束窗口）。

        issue #167 单例长连接：每个 (库路径, 访问模式) 进程内只开**一条**连接、
        全程复用（此前每操作开-关库，重开要重读侧车、走 generation 记账）。
        返回 `_LockedDB` 代理（见其 docstring）：
          - 代理持有期间锁住进程内 DB 访问（排队而不是失败）；
          - close()/with 退出**只释放锁、不关闭连接**——连接由模块级注册表持有，
            供下一次 _acquire 复用。

        库被其他进程占用时（连接级排他）fail-fast 抛 `DatabaseBusyError`，附带
        库路径与处理指引（跨进程排他不变）；同一库被以另一种访问模式请求时抛
        `AccessModeConflictError`（reader/writer 在 triviumdb 层互斥，单例只能
        取其一）。
        """
        return _acquire_shared(self.db_path, "read_only" if self.read_only else "read_write", self.dim)

    def embed_text(self, text: str) -> list[float]:
        """生成文本向量，按 EMBEDDING_PROVIDER 分发（默认进程内 local，隐私优先）。"""
        if self.provider == "openai":
            return self._embed_openai(text)
        if self.provider == "local":
            return self._embed_local(text)
        return self._embed_ollama(text)

    def _embed_local(self, text: str) -> list[float]:
        """用进程内 llama.cpp 生成向量（不依赖 Ollama 服务）。

        pooling 必须 LAST：该模型（Qwen3-Embedding）是 decoder-only 因果 LM，
        只有末位 token 见过整句（实测 mean→0.755 / cls→0.341，均废）。
        输出必须 L2 归一化：与 Ollama 的输出尺度对齐（实测模长 1.0），
        归一化自己算——不依赖 llama.cpp 的 normalize 参数，版本行为不稳。
        """
        global _LOCAL_LLM
        if _LOCAL_LLM is None:
            with _LOCAL_LLM_LOCK:
                if _LOCAL_LLM is None:  # 双重检查：并发首调只加载一次
                    try:
                        from llama_cpp import LLAMA_POOLING_TYPE_LAST, Llama
                    except ImportError as exc:
                        raise EmbeddingUnavailableError(
                            '本地 embedding 依赖 llama-cpp-python 未安装。请执行：pip install "palimpsest[local-embed]"'
                        ) from exc
                    from core.model_fetch import resolve_model_path

                    model_path = ""
                    try:
                        model_path = resolve_model_path()
                        _LOCAL_LLM = Llama(
                            model_path=model_path,
                            embedding=True,
                            n_ctx=Config.EMBEDDING_LOCAL_N_CTX,
                            n_threads=Config.EMBEDDING_LOCAL_N_THREADS or None,
                            pooling_type=LLAMA_POOLING_TYPE_LAST,
                            verbose=False,
                        )
                    except EmbeddingUnavailableError:
                        raise
                    except Exception as exc:
                        raise EmbeddingUnavailableError(
                            f"本地 embedding 模型加载失败（模型路径：{model_path}）：{exc}。"
                            "请确认文件完整（缺失/损坏可删除后重新下载）、"
                            "llama-cpp-python 版本 >= 0.3.36。"
                        ) from exc

        try:
            # ★ 并发保护：ggml 的计算上下文不是线程安全的。REST 端点是同步 def
            # （FastAPI 丢线程池），因此多请求是真并发；多个线程同时进 embed()
            # 会触发 C 层 GGML_ASSERT（ggml-cpu/ops.cpp）直接 abort —— 进程消失，
            # Python 拦不住、也没有 5xx 降级（见 issue #153）。
            # 这里必须串行化；模型加载路径已在自己的 with 块内退出，不会重入本锁。
            with _LOCAL_LLM_LOCK:
                vec = _LOCAL_LLM.embed(text)
        except Exception as exc:
            raise EmbeddingUnavailableError(
                f"本地 embedding 推理出错（模型：{Config.EMBEDDING_MODEL_FILE}）：{exc}"
            ) from exc
        if not vec or len(vec) != self.dim:
            raise EmbeddingUnavailableError(
                f"本地 embedding 输出维度不符：实际 {len(vec) if vec else 0}，"
                f"期望 {self.dim}（EMBEDDING_LOCAL_DIM）。请检查模型文件与维度配置。"
            )
        # L2 归一化（自己算，模长 1.0，与 Ollama 输出尺度对齐）
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0.0:
            raise EmbeddingUnavailableError("本地 embedding 模型输出零向量，无法归一化。")
        return [v / norm for v in vec]

    def _embed_ollama(self, text: str) -> list[float]:
        """
        通过本地 Ollama 服务生成文本向量
        使用配置的 Ollama embedding 模型（默认 qwen3-embedding:0.6b，1024 维）
        """
        try:
            import requests

            url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/") + "/api/embeddings"
            response = requests.post(
                url,
                json={"model": Config.OLLAMA_EMBEDDING_MODEL, "prompt": text},
                timeout=30,
            )
            _raise_embedding_http_error(response, provider="Ollama")
            embedding = response.json().get("embedding")
            if not embedding:
                raise EmbeddingUnavailableError(
                    f"Ollama embedding 返回为空: model={Config.OLLAMA_EMBEDDING_MODEL}。"
                    "请确认模型已拉取（ollama pull qwen3-embedding:0.6b）并检查 "
                    "OLLAMA_EMBEDDING_BASE_URL 配置。"
                )
            return embedding
        except EmbeddingUnavailableError:
            raise
        except Exception as e:
            raise EmbeddingUnavailableError(
                f"Ollama embedding 生成失败: {e}。请确认已启动 Ollama "
                "(ollama serve)，model 已拉取（ollama pull qwen3-embedding:0.6b），"
                "且 OLLAMA_EMBEDDING_BASE_URL（默认 http://127.0.0.1:11434）正确。"
            ) from e

    def _embed_openai(self, text: str) -> list[float]:
        """
        通过 OpenAI 兼容 Embedding API 生成文本向量（Voyage/OpenAI/硅基流动等）。
        需配置 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL。
        """
        if not Config.EMBEDDING_API_KEY:
            raise EmbeddingUnavailableError(
                "EMBEDDING_PROVIDER=openai 但未配置 EMBEDDING_API_KEY。"
                "请检查 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL 配置。"
            )
        try:
            import requests

            url = Config.EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {Config.EMBEDDING_API_KEY}"},
                json={"model": Config.EMBEDDING_MODEL, "input": text},
                timeout=30,
            )
            _raise_embedding_http_error(response, provider="云端 Embedding")
            data = response.json()
            # OpenAI 兼容返回: {"data": [{"embedding": [...]}]}
            embedding = data["data"][0]["embedding"]
            if not embedding:
                raise EmbeddingUnavailableError(
                    f"云端 embedding 返回为空: model={Config.EMBEDDING_MODEL}。"
                    "请检查 EMBEDDING_MODEL / EMBEDDING_BASE_URL 配置。"
                )
            return embedding
        except EmbeddingUnavailableError:
            raise
        except Exception as e:
            raise EmbeddingUnavailableError(
                f"云端 Embedding 生成失败: {e}。请检查 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL 配置。"
            ) from e

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        """插入记忆节点，返回节点 ID"""
        with self._acquire() as db:
            # 基础 payload
            payload = {
                "type": node_data.get("type", "unknown"),
                "label": node_data.get("label", ""),
                "content": node_data.get("content", ""),
                "importance": node_data.get("importance", 0.5),
                "status": "active",
                # created_at（issue #226）：调用方**没给这个键**时落当前时刻
                # （epoch 秒数值——ISO 字符串会让 /mem/recent 排序在 float/str
                # 间抛 TypeError）；**显式传 None 视为「刻意无时间戳」并保留**，
                # 否则「已知未知」语义被消灭——导入演练、时间切分负样本、
                # 存量行模拟都要能构造无时间戳节点。
                "created_at": node_data.get("created_at") if "created_at" in node_data else time.time(),
            }
            # 合并额外字段（如 character_name, user_name）
            extra_fields = {k: v for k, v in node_data.items() if k not in payload and k not in ("label", "content")}
            payload.update(extra_fields)

            # 敏感信息强/弱分级扫描（安全审计整改）：
            #   强规则命中（API key/token/私钥）→ 拒绝入库
            #   弱规则命中（身份证/手机号）→ 放行但打 secret_hint 标记供审计
            scan_text = " ".join(str(v) for v in payload.values() if isinstance(v, str))
            classified = scan_secret_classified(scan_text)
            if classified["strong"]:
                raise SecretScanError(classified["strong"])
            if classified["weak"]:
                payload["secret_hint"] = classified["weak"]

            node_id = db.insert(embedding, payload)
            return node_id

    def insert_node_tx(self, tx, node_data: dict[str, Any], embedding: list[float], next_id: int | None = None) -> int:
        """事务内插入节点（供 mem_ingest 事务化链路使用），返回节点 ID。

        逻辑与 insert_node 一致（payload 组装、secret_scan 强拒弱标），但写入
        走传入的事务句柄 tx（tx.insert_with_id），不再独立 _acquire——保证与
        后续 created_at 补写、冲突标脏（resolve_conflict 的 tx.link）落在同一
        事务内原子提交/回滚，堵死「新节点已写、旧记忆未标 outdated」的半状态。

        next_id：显式指定新节点 ID。triviumdb 的 tx.insert 返回 None 且事务内
        未提交节点对 db.get 不可见，故用 tx.insert_with_id 精确指定 ID，才能
        在事务内立即拿到 node_id 供后续 tx.link / tx.update_payload 使用。
        由调用方按「已提交最大即 id + 1」计算（同 consolidate._apply_merge）；
        next_id 缺省时回退为 1。
        """
        # 基础 payload（与 insert_node 一致；created_at 由 node_data 透传：
        # 事务模式下新节点不可读回，需在插入时一次性带上，避免再用
        # tx.update_payload 补写时整包覆盖其他字段）
        payload = {
            "type": node_data.get("type", "unknown"),
            "label": node_data.get("label", ""),
            "content": node_data.get("content", ""),
            "importance": node_data.get("importance", 0.5),
            "status": "active",
        }
        extra_fields = {k: v for k, v in node_data.items() if k not in payload and k not in ("label", "content")}
        payload.update(extra_fields)
        # created_at（issue #226，与 insert_node 同口径）：键缺失才落当前时刻；
        # 显式 None 保留（「刻意无时间戳」的语义不能被消灭）。
        if "created_at" not in payload:
            payload["created_at"] = time.time()

        # 敏感信息强/弱分级扫描（与 insert_node 一致）：强拒入、弱打标
        scan_text = " ".join(str(v) for v in payload.values() if isinstance(v, str))
        classified = scan_secret_classified(scan_text)
        if classified["strong"]:
            raise SecretScanError(classified["strong"])
        if classified["weak"]:
            payload["secret_hint"] = classified["weak"]

        nid = next_id if next_id is not None else 1
        tx.insert_with_id(nid, embedding, payload)
        return nid

    def create_edge(
        self,
        source_id: int,
        target_id: int,
        relation_type: str,
        content: str = "",
        weight: float = 0.9,
    ) -> None:
        """在两个节点之间创建边"""
        with self._acquire() as db:
            db.link(source_id, target_id, label=relation_type, weight=weight)

    def search_similar(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        expand_depth: int = 1,
        apply_decay: bool = True,
        block: str = "",
        include_outdated: bool = False,
        payload_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """使用 search_advanced（SA-PPR 认知管线）检索，保留时间衰减

        A1 重构：将旧「db.search + Python BFS 扩散」两步合一，
        改为单次 db.search_advanced 调用，由 triviumdb 0.8.3 原生图扩散替代
        Python BFS（旧邻居扩展），获得约 4.6x 性能提升（10 万节点实测）。

        行为变化说明：
          - SA-PPR 认知管线的分数 scale 与旧 BFS 完全不同（非逐跳 ×weight），
            排序结果可能与旧版有差异——这是预期的、已通过一致性对比测试验证
            （该一致性对比为本地一次性探针，未入库）。
          - teleport_alpha=0.0（纯向量+扩散，无随机跳跃）+ expand_depth 透传
            参数，与现状行为最接近。
          - block 参数：若非空，search_advanced 返回的候选会经过 Python 后置
            domain 过滤。

        - expand_depth：透传给 search_advanced 的图扩散深度（默认 1）
        - apply_decay=False 供 mem_ingest 内部阈值判断保持原样
        - payload_filter：透传给 search_advanced 的负载过滤（如
          {"type": "skill_chunk"}）。用于候选池隔离：库里记忆/知识块节点远多于
          技能节点，同池竞争时少数类型会被挤出候选窗口，按类型先过滤再排序
          可保证该类节点的召回。

        重排模式（MEMORY_RERANK_MODE）：
          hard（旧版，可回退）：
            非 kb_chunk: final = score × importance × MEMORY_DECAY_FACTOR^(days/30)
            kb_chunk:    不做任何加权（原样保留）
          soft（默认）：
            非 kb_chunk:
              importance_norm = clamp((importance - 0.5) / 0.5, -1, 1)
              recency_norm    = MEMORY_DECAY_FACTOR^(days/30)
              final = score + ε × (importance_norm + recency_norm)
            kb_chunk（知识不老化）:
              final = score + ε × KB_SOFT_RERANK_MULT

        候选阶段已排除 status="outdated" 的节点（新版取代旧版的过期记忆）。
        """
        # 多取候选供衰减后重排（search_advanced 的 top_k 是检索量，
        # 衰减后可能改变排序，需预留余量；6x 因约 24% 候选是 outdated，
        # 过滤后仍需保留足够有效候选）
        cand_k = max(top_k * 6, 10)
        scored = []
        db = None
        try:
            db = self._acquire()
            hits = db.search_advanced(
                query_embedding,
                top_k=cand_k,
                recall_k=cand_k,
                min_score=0.0,
                expand_depth=expand_depth,
                teleport_alpha=0.0,
                enable_advanced_pipeline=True,
                max_edges_per_node=Config.EXPAND_MAX_EDGES_PER_NODE,
                min_edge_weight=Config.EXPAND_MIN_EDGE_WEIGHT,
                payload_filter=payload_filter,
            )
            scored = [(float(hit.score), {"id": hit.id, "payload": hit.payload}) for hit in (hits or [])]
        except Exception as e:  # noqa: BLE001 —— 检索失败告警后降级空结果不抛断主流程
            logger.warning(f"search_advanced 失败，返回空结果: {e}")
            return []
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

        # 过滤已过期（outdated）候选：被新版取代的旧记忆不占候选名额
        if not include_outdated:
            scored = _drop_outdated_candidates(scored)

        # 时间衰减（记忆生命周期）：kb_chunk 知识块不衰减
        if apply_decay:
            scored = _rerank_by_decay(scored)

        # block 后置过滤：domain 匹配 block 时保留，否则丢弃
        if block:
            scored = _filter_candidates_by_block(scored, block)

        # 排序、截断
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        top_results = [
            {
                "id": node.get("id"),
                "score": score,
                "payload": node.get("payload", {}),
            }
            for score, node in top
        ]
        # 记忆生命周期：命中计数（best-effort，不影响检索结果/排序）
        self._bump_hit_counts(top_results)
        return top_results

    def _bump_hit_counts(self, top_results: list) -> None:
        """命中计数累加进内存缓冲（best-effort，记忆生命周期 promote 的数据源）。

        对 search_similar 最终返回的命中节点 hit_count+1，供 promote 高频记忆
        升级识别——但**只记内存缓冲、绝不碰数据库**：检索返回热路径上不再开写
        事务（原版每次检索一次 read-modify-write，并发命中同一节点会丢计数、
        写锁竞争放大延迟），实际回写由 flush_hit_counts 批量合并完成。设计约束：
          - 只对【最终返回】的命中节点计数（top_results），不含中途召回后丢弃的；
          - 不改变检索返回内容/排序/行为（本方法纯内存，返回值原样不动）；
          - 缓冲读改写全程持 _hit_lock，并发检索不丢计数；
          - 回写失败由 flush_hit_counts 吞掉并 warning，绝不抛出影响检索。
          - hit_count 是 best-effort 指标：进程退出时未达阈值/未超时的缓冲会
            丢弃（最多 _HIT_FLUSH_THRESHOLD 次命中 / _HIT_FLUSH_INTERVAL 秒），
            长驻的 REST 进程由周期触发兜底落库。
        """
        if not top_results:
            return
        ids = [r.get("id") for r in top_results if r.get("id") is not None]
        if not ids:
            return
        now = time.time()
        with self._hit_lock:
            for nid in ids:
                entry = self._hit_buffer.get(nid)
                if entry is None:
                    self._hit_buffer[nid] = [1, now]
                else:
                    entry[0] += 1
                    entry[1] = now
                self._hit_pending_total += 1
        self._maybe_flush_hit_counts()

    def _maybe_flush_hit_counts(self) -> None:
        """缓冲达到阈值或超时则触发一次合并回写（由 _bump_hit_counts 尾部调用）。

        触发条件（满足其一即回写）：
          - 缓冲待回写总数 >= _HIT_FLUSH_THRESHOLD（50 次命中）；
          - 距上次回写 >= _HIT_FLUSH_INTERVAL（30 秒）——低频命中也能定时落库。
        决策在 _hit_lock 内读取；回写本体 flush_hit_counts 自行加锁，此处**不持锁**
        调用（threading.Lock 非重入，持锁再进会死锁）。
        """
        with self._hit_lock:
            due = (
                self._hit_pending_total >= _HIT_FLUSH_THRESHOLD
                or time.time() - self._hit_last_flush >= _HIT_FLUSH_INTERVAL
            )
        if not due:
            return
        self.flush_hit_counts()

    def flush_hit_counts(self) -> int:
        """把内存缓冲的命中计数按**合并语义**批量回写数据库，返回成功累加的计数总和。

        合并语义：读节点现有 payload，`hit_count = 原值 + 缓冲计数`、
        `last_hit_at = 缓冲时间戳`，再整包写回——对 hit_count 是加法，不会覆盖
        外部并发写入的其他字段/计数。契约（沿用原 _bump_hit_counts 的 best-effort）：
          - 单连接一次写完所有缓冲节点，避免 N+1 次开/关连接；
          - 全程持 _hit_lock：并发 flush 串行化，同一节点不会被重复累加；
            代价是回写期间 _bump_hit_counts 短暂阻塞——每 50 次命中才写一次，可接受；
          - 任何写失败（锁冲突/节点被删等）吞掉并 warning，绝不抛出影响检索；
          - 写成功（或节点已删无处可写）的条目从缓冲移除并扣减
            _hit_pending_total，最后刷新 _hit_last_flush。
        """
        with self._hit_lock:
            if not self._hit_buffer:
                self._hit_last_flush = time.time()
                return 0
            # 快照待回写条目（持锁期间缓冲不会被并发改写）
            snapshot = {nid: (int(e[0]), float(e[1])) for nid, e in self._hit_buffer.items()}
            written = 0
            handled: list[int] = []
            db = None
            try:
                db = self._acquire()
                for nid, (cnt, ts) in snapshot.items():
                    node = db.get(nid)
                    if not node:
                        # 节点已被删除：无处可写，按已处理出缓冲，避免永久残留
                        handled.append(nid)
                        continue
                    payload = dict(node.payload or {})
                    cur = payload.get("hit_count")
                    try:
                        cur = int(cur) if cur is not None else 0
                    except (TypeError, ValueError):
                        cur = 0
                    payload["hit_count"] = cur + cnt
                    payload["last_hit_at"] = ts
                    db.update_payload(id=nid, payload=payload)
                    handled.append(nid)
                    written += cnt
            except Exception as e:  # noqa: BLE001 — 写失败不影响检索，吞掉仅记录
                logger.warning("命中计数回写失败（不影响检索）: %s", e)
            finally:
                if db is not None:
                    with contextlib.suppress(Exception):
                        db.close()
            # 成功/已删条目出缓冲并扣减待回写总数；中途失败的节点留在缓冲待下次重试
            for nid in handled:
                snap_cnt = snapshot[nid][0]
                if self._hit_buffer.pop(nid, None) is not None:
                    self._hit_pending_total = max(0, self._hit_pending_total - snap_cnt)
            self._hit_last_flush = time.time()
            return written

    def get_edges(self, node_id: int) -> list:
        """获取节点的出边列表（Edge 对象，含 label/target_id/weight）"""
        with self._acquire() as db:
            return db.get_edges(node_id)

    def get_incoming_edges(self, node_id: int) -> list:
        """获取节点的**入边**列表（Edge 对象，含 label/source_id/weight）。

        与 get_edges（出边）相对，是 bi-temporal 反向索引的底层支撑：
        「谁取代了我」＝ 指向本节点的 REVISED_BY 入边。底层 triviumdb 提供
        get_incoming_edges，这里做薄封装把它接出来（与 get_edges 对称）。
        """
        with self._acquire() as db:
            return db.get_incoming_edges(node_id)

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """获取单个节点的详细信息（包含向量）"""
        with self._acquire() as db:
            node = db.get(node_id)
            if node:
                return {
                    "id": node.id,
                    "payload": node.payload,
                    "num_edges": node.num_edges,
                    "vector": node.vector,  # 新增：返回向量
                }
            return None

    def delete_edge(self, source_id: int, target_id: int, relation_type: str = "RELATED_TO") -> bool:
        """删除一条边（issue #51）。

        此前建边只能通过 create_edge / mem_link，却没有任何删除途径：冲突检测
        自动产生的 REVISED_BY 误标边只能停机直连库手写 db.unlink 清理。底层
        triviumdb 早就提供 unlink(src, dst, label)，这里做薄封装把它接出来。

        返回 True 表示确实删除了一条边，False 表示该边不存在（幂等，不抛异常）。
        """
        label = (relation_type or "").strip()
        with self._acquire() as db:
            edges = db.get_edges(source_id) or []
            if not any(
                getattr(e, "target_id", None) == target_id and getattr(e, "label", None) == label for e in edges
            ):
                return False
            db.unlink(source_id, target_id, label)
        logger.info("已删除边 %s -[%s]-> %s", source_id, label, target_id)
        return True

    def delete_node(self, node_id: int) -> None:
        """删除节点（同时删除所有关联边），并同步清理 FTS 全文索引。

        FTS 是独立于主库的全文索引文件（core/fts_index.py），删除节点时若
        不同步清理，索引会残留幽灵条目（全文检索命已删节点）。此处让 store
        自己负责清理，避免调用方遗漏（此前 build_*_index.py 三个脚本就漏了）。
        清理失败不阻断主删除——FTS 可 fts-rebuild 兜底，与 REST 端点既有行为一致。
        """
        with self._acquire() as db:
            db.delete(node_id)
        logger.info(f"已删除节点 ID={node_id}")
        # 同步清理 FTS 全文索引（失败不阻断主删除，FTS 可 fts-rebuild 兜底）
        try:
            remove_node(node_id)
        except Exception as e:  # noqa: BLE001 —— FTS 清理失败仅告警不抛出
            logger.warning("FTS 索引清理失败 node=%s: %s", node_id, e)
        # TDB 文本通道（默认关闭零成本）：清空该节点正文，语义同 FTS remove_node
        try:
            _tdb_text.sync_node(node_id, "")
        except Exception as e:  # noqa: BLE001 —— TDB 清理失败仅告警不抛出
            logger.warning("TDB 文本索引清理失败 node=%s: %s", node_id, e)

    def update_payload(self, node_id: int, new_payload: dict[str, Any]) -> None:
        """更新节点的 payload 元数据（合并语义，不覆盖未涉及的字段）。

        读现有节点后与 new_payload 做浅合并：只更新 new_payload 里出现的键，
        其余字段（content/type/importance/domain 等）原样保留——防止「部分更新
        把整包清空」的生产事故。节点不存在时抛 ValueError。
        """
        with self._acquire() as db:
            node = db.get(node_id)
            if node is None:
                raise ValueError(f"节点 ID={node_id} 不存在，无法更新 payload")
            merged = {**(node.payload or {}), **(new_payload or {})}
            db.update_payload(id=node_id, payload=merged)
        logger.info(f"已更新节点 ID={node_id} 的 payload")

    def update_content(self, node_id: int, content: str) -> None:
        """更新节点内容并同步派生状态（语义化写路径）。

        与 update_payload 的区别：**改 content 会连带影响派生状态**——
        FTS 全文索引必须同步（否则全文检索仍命中旧文本），向量也与新内容
        不一致。把这条「content 变更 → 同步 FTS」的知识收进 store，而不是
        让每个调用方各写一遍（此前 PUT/PATCH 端点各写、consolidate 漏写）。

        向量**不在此处重算**（重嵌是网络调用，塞进写路径会超时/阻塞，见
        issue #52 的取舍）；只打 vector_stale 标记，重嵌仍走显式
        reembed_node。FTS 同步失败仅告警——可 fts-rebuild 兜底。
        """
        self.update_payload(node_id, {"content": content, "vector_stale": True})
        # FTS 全文索引同步（失败仅告警不阻断主更新，可 fts-rebuild 兜底）
        try:
            sync_node(node_id, content)
        except Exception as e:  # noqa: BLE001 —— FTS 同步失败仅告警不阻断主更新
            logger.warning("FTS 索引同步失败 node=%s: %s", node_id, e)
        # TDB 文本通道（默认关闭零成本；失败 warning 不阻断，可 rebuild 兜底）
        try:
            _tdb_text.sync_node(node_id, content)
        except Exception as e:  # noqa: BLE001 —— TDB 同步失败仅告警不阻断主更新
            logger.warning("TDB 文本索引同步失败 node=%s: %s", node_id, e)
        logger.info(f"已更新节点 ID={node_id} 的内容（向量标记为 stale）")

    def update_vector(self, node_id: int, new_vector: list[float]) -> None:
        """更新节点的向量（维度必须一致）"""
        with self._acquire() as db:
            db.update_vector(vector=new_vector, id=node_id)  # 改为关键字参数
        logger.info(f"已更新节点 ID={node_id} 的向量")

    def reembed_node(self, node_id: int) -> bool:
        """按当前 content 重算并写回该节点的向量（issue #52）。

        PUT/PATCH 改 content 只会浅合并 payload 并同步 FTS，向量不重算——
        语义检索与冲突检测会继续按旧文本执行，且完全静默。此前唯一的补救是
        调用方自己在外部生成向量再打 PATCH /memory/{id}/vector。

        这里把该流程收进服务端：读当前 content → embed_text → update_vector。
        返回 False 表示节点不存在；embedding 不可用时由 embed_text 抛
        EmbeddingUnavailableError，由调用方决定如何上报。
        """
        node = self.get_node(node_id)
        if not node:
            return False
        content = (node.get("payload") or {}).get("content", "")
        vector = self.embed_text(content)
        self.update_vector(node_id, vector)
        logger.info(f"已重算节点 ID={node_id} 的向量")
        return True

    def _get_all_node_ids(self) -> list[int]:
        """获取数据库中所有节点的 ID 列表（供内部使用）"""
        with self._acquire() as db:
            return db.all_node_ids()

    def iter_payloads(self):
        """遍历所有节点，yield (node_id, payload)。

        单连接遍历（性能优化）：一次 _acquire 拿全部 node_id，
        同一连接内逐个 db.get，替代原「_get_all_node_ids + 每节点 get_node」
        （N+1 次开/关连接）。行为与原版逐字节一致：
        节点缺失跳过；payload 统一归一为 dict（只取 payload，不读 vector）。
        """
        db = None
        try:
            db = self._acquire()
            for nid in db.all_node_ids():
                node = db.get(nid)
                if not node:
                    continue
                yield nid, node.payload or {}
        finally:
            # 0.7.6 的 with 退出不释放锁，必须显式 close；0.8.2 兼容（close 幂等）
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    def iter_nodes(self):
        """遍历所有节点，yield (node_id, {"id", "payload", "num_edges", "vector"})。

        单连接遍历（2026-08-29 优化，与 iter_payloads 同模式）：一次 _acquire
        拿全部 node_id，同一连接内逐个 db.get，替代 N+1 次开/关连接。
        节点缺失跳过；向量为 1024 维浮点数组，调用方按需取用（体量大，勿无条件拷贝）。
        """
        db = None
        try:
            db = self._acquire()
            for nid in db.all_node_ids():
                node = db.get(nid)
                if not node:
                    continue
                yield (
                    nid,
                    {
                        "id": node.id,
                        "payload": node.payload or {},
                        "num_edges": node.num_edges,
                        "vector": node.vector,
                    },
                )
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    def count_by_type(self) -> dict[str, int]:
        """统计各节点类型数量，返回 {type: count}。

        遍历 iter_nodes 统计 payload.type，缺失/空值归入 "unknown"。
        空库返回空 dict。
        """
        counter: dict[str, int] = {}
        for _, node in self.iter_nodes():
            t = (node.get("payload") or {}).get("type") or "unknown"
            counter[t] = counter.get(t, 0) + 1
        return counter

    def recent_ids(self, limit: int = 20) -> list[int]:
        """返回最近写入的节点 ID 列表（按 id 倒序取前 limit 个）。

        供 dashboard/recent 等「最新节点」场景使用。空库返回空列表。
        """
        ids = self._get_all_node_ids()
        ids.sort(reverse=True)
        return ids[:limit]

    def find_similar_pairs(self, sim_threshold: float = 0.85) -> list[dict]:
        """单连接内扫描高相似 memory 节点对。

        遍历 active + type=memory 节点，对每个节点用原生
        db.search(vec, top_k=6, min_score=0.0, expand_depth=0) 找相似候选。
        返回原始候选列表 [{a, b, score, a_imp, b_imp, a_content, b_content}]：
          - a<b 按 id 去重（避免重复对）；
          - 双方均须为 active + memory；
          - score 低于 sim_threshold 的候选对被过滤（阈值由本方法直接应用），
            通过的对 score 原样保留（round 4 位），最终按 score 降序返回。
        方法内用 with self._acquire() as db 管理连接；异常返回空列表不抛出。
        """
        memory_nodes: list[dict] = []
        with self._acquire() as db:
            try:
                for nid in db.all_node_ids():
                    node = db.get(nid)
                    if not node:
                        continue
                    payload = node.payload or {}
                    if payload.get("type") != "memory":
                        continue
                    if payload.get("status") != "active":
                        continue
                    vec = node.vector
                    if not vec:
                        continue
                    memory_nodes.append(
                        {
                            "id": nid,
                            "vector": vec,
                            "payload": payload,
                        }
                    )
            except Exception as e:  # noqa: BLE001 —— 节点遍历失败返回空列表不阻断检测
                logger.warning(f"find_similar_pairs 收集节点失败，返回空列表: {e}")
                return []

        seen_pairs: set[tuple[int, int]] = set()
        candidates: list[dict] = []
        try:
            with self._acquire() as db:
                for mn in memory_nodes:
                    nid = mn["id"]
                    vec = mn["vector"]
                    hits = db.search(vec, top_k=6, min_score=0.0, expand_depth=0)
                    for hit in hits or []:
                        hid = hit.id
                        score = float(hit.score)
                        if hid == nid:
                            continue
                        if score < sim_threshold:
                            continue
                        hnode = db.get(hid)
                        if not hnode:
                            continue
                        hpayload = hnode.payload or {}
                        if hpayload.get("type") != "memory":
                            continue
                        if hpayload.get("status") != "active":
                            continue
                        # 去重：a<b 按 id 排序
                        a, b = (nid, hid) if nid < hid else (hid, nid)
                        pair = (a, b)
                        if pair in seen_pairs:
                            continue
                        seen_pairs.add(pair)
                        a_payload = mn["payload"] if a == nid else hpayload
                        b_payload = hpayload if b == hid else mn["payload"]
                        a_imp = _to_float(a_payload.get("importance"), 0.5)
                        b_imp = _to_float(b_payload.get("importance"), 0.5)
                        candidates.append(
                            {
                                "a": a,
                                "b": b,
                                "score": round(score, 4),
                                "a_imp": round(a_imp, 2),
                                "b_imp": round(b_imp, 2),
                                "a_content": (a_payload.get("content") or "")[:80],
                                "b_content": (b_payload.get("content") or "")[:80],
                            }
                        )
        except Exception as e:  # noqa: BLE001 —— 扫描失败保留已收集候选尽量返回部分结果
            logger.warning(f"find_similar_pairs 扫描失败，返回已收集候选: {e}")

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates


def _days_since_created(created_at: Any, now: float) -> float:
    """距创建天数；created_at 缺失/非法按 0 天（不衰减）"""
    try:
        ts = float(created_at)
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    return max(0.0, (now - ts) / 86400.0)
