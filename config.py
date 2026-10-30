import os

from dotenv import load_dotenv

load_dotenv()

# ---- Embedding provider auto-detect ----
# When EMBEDDING_PROVIDER env var is not explicitly set, auto-detect:
#   - Valid cloud EMBEDDING_API_KEY present → "openai"
#   - Otherwise → "local" (in-process llama.cpp GGUF, zero-dependency-on-services)
# "Valid key" excludes empty / whitespace-only / common placeholders
# (e.g. YOUR_API_KEY from .env.example).
_PLACEHOLDER_KEYS = frozenset({"", "your_api_key", "xxx", "changeme", "sk-xxx"})


def _detect_embedding_provider() -> str:
    """Return the resolved embedding provider string.

    Explicit ``EMBEDDING_PROVIDER`` env var always wins (fully backward-compatible).
    When unset, a non-placeholder ``EMBEDDING_API_KEY`` implies ``"openai"``;
    otherwise fall back to ``"local"``.
    """
    raw = os.getenv("EMBEDDING_PROVIDER")
    if raw:
        return raw.strip().lower()
    key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if key.lower() not in _PLACEHOLDER_KEYS:
        return "openai"
    return "local"


def embedding_provider_source() -> str:
    """``EMBEDDING_PROVIDER`` 的**来源**（issue #192）——供启动自检回显。

    返回值：
      - ``"env"``           用户显式设置了 `EMBEDDING_PROVIDER`
      - ``"auto:api-key"``  未设置，但检测到可用的 `EMBEDDING_API_KEY` → openai
      - ``"auto:fallback"`` 未设置且无可用 key → 落到默认 ``local``

    为什么需要它：2.16.0 把默认值从 ``ollama`` 改成 ``local``，老 `.env` 里通常
    没写这一项，升级后会**静默切到内置后端**（缺 llama-cpp-python / GGUF 就起不来），
    而配置面上看不出任何变化。把来源回显出来，「我以为是 ollama，其实是自动探测的
    local」才看得见。

    刻意做成模块级函数而不是 ``Config`` 属性：它不是配置项、也不该出现在
    `.env.example` 里，落进 ``Config`` 会被配置项一致性检查当成漏登记的旋钮。
    """
    if os.getenv("EMBEDDING_PROVIDER", "").strip():
        return "env"
    key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if key.lower() not in _PLACEHOLDER_KEYS:
        return "auto:api-key"
    return "auto:fallback"


# 项目根 = config.py 所在目录（直接运行时为仓库根，pip 安装后为 site-packages 下包目录）。
# DB_PATH 等相对路径一律以它为基准解析为绝对路径，不再依赖/修改进程当前工作目录
# （此前依赖 mcp_tools 里 os.chdir 切换到项目根来解析相对路径，作为包安装后会污染
# 调用方工作目录——现已改为纯绝对路径解析，彻底消除该副作用）。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve_db_path() -> str:
    """解析 DB_PATH：环境变量给绝对路径直接用；给相对路径（非盘符/非 / 开头）
    则基于项目根解析为绝对路径；未给时默认 <项目根>/data/mh_memory.db。"""
    env = os.getenv("DB_PATH", "")
    if env and os.path.isabs(env):
        return env
    if env:
        return os.path.join(PROJECT_ROOT, env)
    return os.path.join(PROJECT_ROOT, "data", "mh_memory.db")


class Config:
    # ---- 服务端口（2026-08-26：8000 让给酒馆 SillyTavern，Palimpsest REST 用 8090；dashboard 独立 8010）----
    REST_PORT = int(os.getenv("REST_PORT", "8090"))
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8010"))

    # ---- 通用 ----
    # DB_PATH 为绝对路径（相对路径/默认值均已基于项目根解析），任何 cwd 下取用都正确
    DB_PATH = _resolve_db_path()

    # ---- 记忆时间衰减（记忆生命周期）----
    # MEMORY_DECAY_FACTOR 生效前提：检索排序走入库时间衰减分支——soft 模式（默认）
    # 下它只进入 ε 微调项 recency_norm（ε=SOFT_RERANK_EPS 默认 0.02，故排序影响
    # 上限 0.02、一年内约 0.01 量级，近乎半死参数）；hard 模式下才做乘性硬加权。
    # kb_chunk 知识块在两种模式下都不衰减。
    #   有效分 = 余弦分 × importance × MEMORY_DECAY_FACTOR^(距创建天数/30)
    # 0.95 ≈ 每月衰减 5%（保守）；设为 1.0 完全关闭衰减
    MEMORY_DECAY_FACTOR = float(os.getenv("MEMORY_DECAY_FACTOR", "0.95"))
    # 重排模式：soft = 语义分为主线 + ε 级元数据微调（默认）；hard = 旧版乘性硬加权（可回退）
    MEMORY_RERANK_MODE = os.getenv("MEMORY_RERANK_MODE", "soft").strip().lower()
    # ---- 常驻域（issue #176）------------------------------------------------
    # 每轮**无条件**注入的 domain：该 domain 的 active 节点按 importance 倒序拼进
    # pre-turn 的注入文本，不参与语义相似度竞争——解决「常驻规则与当前消息语义无关
    # 就永远检索不到」这个硬缺口。空串 = 关闭，行为与改动前完全一致。
    PIN_DOMAIN = os.getenv("PIN_DOMAIN", "").strip()
    # 常驻块字符预算：按**节点边界**截断（绝不切半个节点），超预算在响应的
    # `pin.truncated` / `pin.dropped` 里如实报告，不静默少注入。
    PIN_BUDGET_CHARS = int(os.getenv("PIN_BUDGET_CHARS", "3000"))
    # soft 模式的 ε：落在余弦分差区间的 15%-40%，只做 tie-break
    SOFT_RERANK_EPS = float(os.getenv("SOFT_RERANK_EPS", "0.02"))
    # ---- 记忆置信度（可选，默认关闭）----
    # 置信度由可观测证据按 Subjective Logic 公式算出（见 core/confidence.py），
    # 只参与检索排序的 ε 级微调。实测：做成硬门槛会让召回掉 4.81pp，故不做门槛。
    # 0 = 关闭（默认）；实测最优 0.05（56 题：MRR@5 +3.27pp / nDCG@5 +3.87pp，
    # 语义类子集相对 +34.6%）。详见 docs/CONFIDENCE.md
    MEMORY_CONFIDENCE_EPS = float(os.getenv("MEMORY_CONFIDENCE_EPS", "0"))
    # b 映射到 [-1,1] 的中心与跨度：b == center → 0，b >= center + span → +1
    MEMORY_CONFIDENCE_CENTER = float(os.getenv("MEMORY_CONFIDENCE_CENTER", "0.27"))
    MEMORY_CONFIDENCE_SPAN = float(os.getenv("MEMORY_CONFIDENCE_SPAN", "0.15"))
    # 域软加权加分（加性，作用在语义分上；domain_boost 非空时对同域候选加此值）
    DOMAIN_BOOST_EPS = float(os.getenv("DOMAIN_BOOST_EPS", "0.10"))
    # kb_chunk（知识块不老化）在 soft 模式下的 ε 加成倍率
    KB_SOFT_RERANK_MULT = float(os.getenv("KB_SOFT_RERANK_MULT", "1.5"))

    # ---- 检索权重（魔法数字配置化）----
    DOMAIN_BIAS_WEIGHT = float(os.getenv("DOMAIN_BIAS_WEIGHT", "1.15"))

    # ---- mem_search 新鲜度加权（数据优先，默认关闭）----
    # 语义分之外的时效信号：score += WEIGHT × decay(节点年龄)，在过滤之后、
    # 排序之前应用（与 domain_bias/domain_boost 同层）。衰减信号 ∈ [0,1]，
    # 事实类节点按世界时间 valid_at（缺省 created_at），其余按 created_at。
    # 默认 0 = 关闭（行为与改动前逐位一致，有回归测试保证）；非零取值必须
    # 由真实查询集的数据定出（eval/freshness_sweep.py），不许拍脑袋定常数。
    MEM_SEARCH_FRESHNESS_WEIGHT = float(os.getenv("MEM_SEARCH_FRESHNESS_WEIGHT", "0"))
    # 衰减时间尺度（天）：exp 形式下为半衰期，linear 形式下为归零天数
    MEM_SEARCH_FRESHNESS_SCALE_DAYS = float(os.getenv("MEM_SEARCH_FRESHNESS_SCALE_DAYS", "30"))
    # 衰减形式：exp（默认）| linear；填了别的值按 exp 处理
    _FRESHNESS_DECAY_RAW = os.getenv("MEM_SEARCH_FRESHNESS_DECAY", "exp").strip().lower()
    MEM_SEARCH_FRESHNESS_DECAY = _FRESHNESS_DECAY_RAW if _FRESHNESS_DECAY_RAW in ("exp", "linear") else "exp"

    # ---- 图谱扩散精馏（魔法数字配置化）----
    # 每节点最多扩散的最强边数（防高节点全量扩散撑爆计算/污染结果）
    EXPAND_MAX_EDGES_PER_NODE = int(os.getenv("EXPAND_MAX_EDGES_PER_NODE", "20"))
    # 扩散时弱边过滤阈值（默认 0.0 不启用）
    EXPAND_MIN_EDGE_WEIGHT = float(os.getenv("EXPAND_MIN_EDGE_WEIGHT", "0.0"))

    # ---- mem_ingest 内容上限（魔法数字配置化）----
    # 单条记忆 content 最大字符数，超长拒绝写入（防超大 payload 拖垮库）
    MEM_INGEST_MAX_LENGTH = int(os.getenv("MEM_INGEST_MAX_LENGTH", str(50_000)))

    # ---- mem_search 检索 query 上限（与写入侧 policy 同源，见 core/policy.py）----
    # 超长 query 先在本体入口被 422 拒绝，而不是打到 embedding 模型后炸出
    # 「input length exceeds the context length」（曾被误译成「embedding 服务
    # 不可用」，排查方向被带偏）。默认 8000 字符。
    MEM_SEARCH_MAX_QUERY_CHARS = int(os.getenv("MEM_SEARCH_MAX_QUERY_CHARS", "8000"))

    # ---- 写入口约束护栏（write-guard policy，见 core/policy.py）----
    # 模式：warn（默认，只记日志不改行为）/ enforce（真正拦截）。
    # 默认 warn——攒一阶段数据后再显式开 enforce；非法值回退 warn。
    POLICY_MODE = os.getenv("POLICY_MODE", "warn").strip().lower()
    # 受保护 type（逗号分隔）：命中即不参与自动覆盖（enforce 模式下）。
    # 默认 rule——治理规则类节点不应被自动冲突检测标 outdated。
    POLICY_PROTECTED_TYPES = frozenset(
        t.strip() for t in os.getenv("POLICY_PROTECTED_TYPES", "rule").split(",") if t.strip()
    )
    # 按 type 分级的软上限（形如 "task:20000,plan:80000"）。
    # 默认值按**实测内容长度分布**设定（2026-10-11 生产库 1550 节点统计）：
    # 上限取各类型当前 p90 的 3-5 倍，既不误伤正常写入，又能拦住异常膨胀
    # （如把整篇文档塞进一条 memory）。
    #   record  p90=1220 max=2300  -> 8000
    #   memory  p90= 729 max=2201  -> 8000
    #   task    p90=2155 max=2743  -> 10000（任务卡随进度累积，留足余量）
    #   plan    p90=2224 max=3153  -> 12000（方案文档天然长）
    #   kb_chunk p90= 856 max=1308 -> 8000（知识库切片）
    #   novel_chunk p90=1827 max=3118 -> 12000（小说段落长）
    POLICY_TYPE_LIMITS = os.getenv(
        "POLICY_TYPE_LIMITS",
        "record:8000,memory:8000,task:10000,plan:12000,kb_chunk:8000,novel_chunk:12000",
    )
    # 核心记忆（facts tier）总字符上限；0 = 不限。
    POLICY_MAX_CORE_TOTAL = int(os.getenv("POLICY_MAX_CORE_TOTAL", "0"))
    # 配置人工闸门：仅当此环境变量为 1 时，运行时策略覆盖才被接受。
    # agent 侧写工具不暴露改策略入口——策略只能改 .env / 重启生效。
    PALIMPSEST_POLICY_UPDATE = os.getenv("PALIMPSEST_POLICY_UPDATE", "") == "1"

    # ---- 混合检索 RRF（魔法数字配置化）----
    # Reciprocal Rank Fusion 标准 k 值：单侧命中也算贡献
    RRF_K = float(os.getenv("RRF_K", "60.0"))

    # ---- 冲突检测：按 type 豁免（issue #50）----
    # 冲突检测分两条路径：显式取代（replaces）无论相似度都标 outdated；自动相似判定
    # （相似度 >0.75）自 2.9.0 起默认关闭，可由 CONFLICT_AUTO_SUPERSEDE 开启。
    # task 是累积关系而非「同一事实被取代」——连续写入两条独立任务时，
    # 旧任务会被误标并从默认检索（include_outdated=False）中消失。
    # 逗号分隔的 type 列表，列在这里的 type 完全跳过冲突检测（不标 outdated、
    # 不建 REVISED_BY 边）。默认空 = 行为完全不变（向后兼容）。
    #   例：CONFLICT_SKIP_TYPES=task
    CONFLICT_SKIP_TYPES = frozenset(t.strip() for t in os.getenv("CONFLICT_SKIP_TYPES", "").split(",") if t.strip())

    # ---- 冲突检测：时间窗判定（bi-temporal 三期，高风险，默认关）----
    # 开启后：相似度 >0.75 的候选，若其世界时间窗与新事实「不重叠」，则不判为
    # 「同一事实被取代」（不标 outdated）——如「十年前住北京」≠「现在住上海」。
    # 默认 false = 行为完全不变（高风险语义变更的稳妥默认，验证后再开）。
    CONFLICT_TIME_WINDOW = os.getenv("CONFLICT_TIME_WINDOW", "").strip().lower() in ("1", "true", "yes", "on")

    # ---- 冲突检测：自动取代开关（默认关闭 = 取代只由显式声明触发）----
    # 关闭（默认）：写入口不再仅凭相似度把旧记忆标 outdated——相似候选一律记入
    #   related_ids（话题相关提示），取代只经 mem_ingest 的显式 replaces 声明发生。
    #   理由：中文同主题文本相似度天然高，「相似」不等于「同一事实被取代」，
    #   仅凭阈值会把同主题互补内容误吞（越认真沉淀越容易被互相标 outdated）。
    # 开启：回退到旧行为——相似度 >0.75 且过 type/domain 门的旧节点自动标 outdated。
    #   作为过渡兼容开关，验证期可一键回退。
    CONFLICT_AUTO_SUPERSEDE = os.getenv("CONFLICT_AUTO_SUPERSEDE", "").strip().lower() in ("1", "true", "yes", "on")

    # 语义主序的图扩散深度：0 = 纯语义排序（默认；实测 R@5 0.7542）
    # 1 = 原行为（图邻居参与语义主序，实测 R@5 0.5000；可一键回退）
    RETRIEVAL_EXPAND_DEPTH = int(os.getenv("RETRIEVAL_EXPAND_DEPTH", "0"))

    # 混合检索 RRF 两侧权重（语义侧 / 精确侧）
    # 语义主序干净后 FTS 小幅加成：实测 R@1 +4.2pp、R@5 +1.7pp
    RRF_SEM_WEIGHT = float(os.getenv("RRF_SEM_WEIGHT", "1.0"))
    RRF_FTS_WEIGHT = float(os.getenv("RRF_FTS_WEIGHT", "0.1"))

    # 精确标识符查询专用 FTS 权重（query 含 commit hash / PR 号 / 工单号时生效）。
    # 此类 query 下语义通道天然失灵（hash 与目标节点的中文正文不相似），FTS 是
    # 唯一能救场的通道，但全局小权重（0.1）会让 FTS-only 命中挤不进 top_k
    # （实测 hash query 在 FTS 通道排第 2 名、最终 top_k 全 MISS）。
    #
    # 默认 1.5，不是拍的，两个依据：
    #
    # ① 下限是 RRF 的数学结果：语义第 0 名得 1/(K+0)=1/61≈0.0164，FTS-only 命中得
    #    w/(K+q)。要让 FTS 第 2 名（q=1）越过语义头部进入 top_k=5，需
    #    w/(61) > 1/(65)，即 w > 0.94；w=0.5 时只有 0.5/62≈0.0081，永远追不上
    #    （实测总排名 63，与 w=0.1 同效）。
    #
    # ② 但 1.0 刚好等于 w_sem，会**打平**：FTS 第 0 名与语义第 0 名分数完全相同
    #    （都是 1/60），胜负取决于实现细节 → 目标节点被压在后面。离线实测（22 条
    #    标识符 query）：w=1.0 时 recall@5 0.356 / MRR@10 0.636；w=1.05 时 MRR 即达
    #    1.000；w=1.10 时 recall@5 到 0.690 并**完全饱和**（1.1~3.0 一模一样）。
    #    取 1.5 是为了留余量抵抗库分布与 embedding 版本变化——临界值附近对这类
    #    变化敏感，余量不花钱。
    #
    # 仅对判定为标识符查询的请求用本权重，普通自然语言 query 仍走 RRF_FTS_WEIGHT。
    RRF_FTS_EXACT_WEIGHT = float(os.getenv("RRF_FTS_EXACT_WEIGHT", "1.5"))

    # ---- RP 专用实例（酒馆记忆桥专用部署，默认关闭 = 行为完全不变）----
    # RP_MODE=1 时服务变成「RP 专用实例」，行为变化只有两条：
    #   1. 域锁死：所有带 domain 参数（或不带）的入口，域一律强制为 RP_DOMAIN
    #      （不传自动补、传别的 domain → 400 拒绝，解析逻辑见 main._resolve_domain）；
    #   2. ingest 不再挂知识库关联边（RP 独立库里没有 kb_chunk，挂边纯属浪费，
    #      见 mcp_tools.memory._find_linked_kb_ids 的开关）。
    # 未设置时两条都不生效，默认行为 100% 不变。
    RP_MODE = os.getenv("RP_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    RP_DOMAIN = os.getenv("RP_DOMAIN", "rp").strip() or "rp"

    # ---- TDB（TriviumDB）BM25 文本通道：第二路中文文本召回，默认关闭 ----
    # 背景：FTS5 trigram 对 2 字短词与 6~8 字长短语召回明显弱于 TDB 的
    # CJK 2-gram（2026-10-24 生产库副本实测）。三件套默认关闭，线上行为与
    # 未引入本通道前逐位一致；开启后与 FTS5 并存供 A/B（scripts/tdb_text_ab.py）。
    # 总开关：1=启用（检索时多取一路并计入 RRF）；0=完全零成本（连 warmup 都不做）。
    TDB_TEXT_ENABLED = os.getenv("TDB_TEXT_ENABLED", "0") == "1"
    # 该路在 RRF 融合中的权重（对比 FTS 路默认 0.1 / 精确标识符路 1.5）。
    TDB_TEXT_WEIGHT = float(os.getenv("TDB_TEXT_WEIGHT", "1.0"))
    # 该路候选量：0 = 沿用 FTS 的 limit（不单独另配）。
    TDB_TEXT_LIMIT = int(os.getenv("TDB_TEXT_LIMIT", "0"))
    # 条件触发下限：单次查询 FTS 命中数 ≥ 此值说明 FTS 已经够好 → 跳过 TDB 路
    # （不 import、不开连接，多数查询零成本）；< 此值才走 TDB 补召回。
    # 0 = 永不跳过（等价于引入本项前的「启用即永远走 TDB」）。
    TDB_TEXT_FTS_FLOOR = int(os.getenv("TDB_TEXT_FTS_FLOOR", "3"))

    # ---- lifecycle session-end：旧「会话要点」路径开关 ----
    # 会话胶囊（type=task kind=handoff，由 session 缓冲合并）上线后保留的兼容
    # 开关：默认开 = 既有行为完全不变（要点照旧写 facts 层）；关闭后 session-end
    # 只走胶囊路径，要点不再写（可观测：skip_reason=session_memory_disabled）。
    # 将来观察胶囊质量后再决定是否调整默认值。
    SESSION_MEMORY_INGEST = os.getenv("SESSION_MEMORY_INGEST", "1") == "1"

    # ---- lifecycle post-turn：会话缓冲的实质轮筛选（issue #170，胶囊第二刀）----
    # 判定在 core/strategy.is_substantive_turn（三条判据任一满足才进缓冲：
    # ①本轮有工具调用；②思考正文 ≥ 此门槛且结论类字段非空；③用户消息含指令
    # 动词）。这里只是判据 2 的思考正文长度门槛（strip 后字符数）：全不满足的
    # 短轮（打招呼 / 确认 / 一句话回复）不进缓冲（skip_reason=non_substantive），
    # session-end 胶囊不再被噪声塞满。纯函数不读环境——本值由端点注入。
    SESSION_BUFFER_MIN_THOUGHT_CHARS = int(os.getenv("SESSION_BUFFER_MIN_THOUGHT_CHARS", "400"))

    # ---- 会话胶囊断线自愈（孤儿缓冲补产）----
    # 背景：胶囊只在 session-end 落地；进程被硬砍（计划内关机 / 桌面 app 崩溃）时
    # session-end 不会跑，post-turn 累进的缓冲永远 status=active 悬空在库里，胶囊
    # 永远不产。三项配置构成自愈：后台循环（ENABLED）每 INTERVAL 秒扫一次，把静默
    # 超过 AFTER_SECONDS 的活跃缓冲按 session 补产成胶囊；也可经
    # POST /lifecycle/recover-orphan-capsules 手动触发（验收 / 人工补救）。
    # 总开关：1=启用（默认）；0=后台循环不启动（手动端点仍可用）。
    SESSION_CAPSULE_RECOVER_ENABLED = os.getenv("SESSION_CAPSULE_RECOVER_ENABLED", "1") == "1"
    # 孤儿判定的静默阈值（秒）：缓冲 updated_at（缺时间戳的老数据回退 started_at）
    # 距今超过它才判孤儿。30 分钟刻意保守——正聊着的会话轮间隔远小于它，不能误伤；
    # 即使误判也**不丢数据**：session-end 合并的是全部缓冲轮次，误补的「半截胶囊」
    # 会在真 session-end 时原节点重合成（node_id 不变），代价只是中间多一条可观测
    # 的中间产出，而不是丢轮次。
    SESSION_CAPSULE_RECOVER_AFTER_SECONDS = int(os.getenv("SESSION_CAPSULE_RECOVER_AFTER_SECONDS", "1800"))
    # 后台扫描间隔（秒）：服务常驻时每 5 分钟自愈一轮——肉眼可等，又不至于空转。
    SESSION_CAPSULE_RECOVER_INTERVAL_SECONDS = int(os.getenv("SESSION_CAPSULE_RECOVER_INTERVAL_SECONDS", "300"))

    # ---- 记忆分层（tier）：检索侧视图，不改存储、不迁数据 ----
    # facts = 事实层（默认检索与注入池）；logs = 日志层（从默认池摘出）；
    # ""（空串）= 不过滤，回到改动前的全量行为（显式历史通道）。
    # kb_chunk / novel_chunk 不入本体系，走既有 scope 隔离；
    # 未登记的 type 一律归 facts（保守兜底，避免静默丢结果）。
    # 逗号分隔便于按部署调整，无需改代码（默认值与 2.0 行为一致）。
    TIER_FACTS = frozenset(
        t.strip()
        for t in os.getenv(
            "TIER_FACTS",
            "memory,correction,decision,plan,task,review,solution,inspiration,user_intent,character_state",
        ).split(",")
        if t.strip()
    )
    TIER_LOGS = frozenset(
        t.strip() for t in os.getenv("TIER_LOGS", "record,event,git_commit,thought,session").split(",") if t.strip()
    )
    DEFAULT_TIER = os.getenv("DEFAULT_TIER", "facts")

    # 代码内已知会写入的节点 type 全集（守护测试 tests/test_tier_registry.py 用：
    # 断言它 ⊆ TIER_FACTS ∪ TIER_LOGS，防止新 type 既不登记 facts 也不登记 logs、
    # 错误走「未登记保守归 facts」兜底 —— issue #151 时 thought 即如此漏网）。
    # 来源（grep 仓库实际写入 / 分支处）：
    #   事实层语义 type（mem_ingest 调用方契约，与 TIER_FACTS 默认同源）：
    #     memory      mcp_tools/memory.py（ingest 默认值）、core/strategy.py（session-end
    #                 提炼）、core/consolidator.py（合并产物）
    #     review      mcp_tools/memory.py（mem_review：复盘裁决后存 type=review）
    #     event / character_state / user_intent / correction / decision / plan /
    #     task / inspiration   调用方语义 type 侧分支：main.py（event / character_state
    #                 展示分支）、mcp_tools/memory.py（task 接续点 / thought 专属字段分支）、
    #                 core/conflict.py + core/bitemporal.py（留痕类豁免分支列举）
    #   日志层 type（代码内写入点）：
    #     record      core/strategy.py（对话原话 capture）、core/task_state.py（任务状态
    #                 变更日志节点）、core/embedding_fingerprint.py（向量空间指纹记录）
    #     thought     core/strategy.py（THOUGHT_TYPE，思维底片，tier=logs 设计意图见该处注释）
    #     session     main.py（会话缓冲节点：post-turn 累进各轮编号思考，session-end
    #                 合并成 kind=handoff 胶囊后标 closed——过程簿记，tier=logs 同 thought）
    #     git_commit  scripts/palimpsest_cli.py（git 提交同步落库）
    # 例外（不入本清单）：kb_chunk / novel_chunk / skill_chunk —— 索引切片类走既有
    # scope / domain 隔离，不参与 tier 分层（见上方 TIER_FACTS 注释）。
    KNOWN_TYPES = frozenset(
        {
            "memory",
            "correction",
            "decision",
            "plan",
            "task",
            "review",
            "solution",
            "inspiration",
            "user_intent",
            "character_state",
            "record",
            "event",
            "git_commit",
            "thought",
            "session",
        }
    )

    # ---- LLM 后端选择 ----
    # 可选: "ollama" 或 "deepseek"
    LLM_BACKEND = os.getenv("LLM_BACKEND", "deepseek")

    # ---- 可选 API Key 鉴权 ----
    # 默认空 = 不启用鉴权（localhost 本机直连，保持现状）。
    # 设置后，除 / 健康检查外所有请求须带 Authorization: Bearer <key> 或
    # X-API-Key: <key>，否则 401。适用于局域网/受信网络部署；
    # 公网部署必须配 HTTPS 反向代理（API Key 仅做校验，不做加密传输）。
    API_KEY = os.getenv("PALIMPSEST_API_KEY", "")

    # ---- DeepSeek 配置 ----
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # ---- Ollama 配置（备用）----
    # 端点默认写 IPv4 字面量而非 localhost：部分系统把 localhost 优先解析为
    # IPv6 回环 [::1]，而 Ollama 默认只监听 IPv4，于是每个请求都要先经历一次
    # 连接超时再回落（实测单次 embedding 由数十毫秒退化为约 2 秒）。
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")

    # ---- Embedding 配置（2026-08-25 开源多选择：本地隐私优先，云端精度可选）----
    # EMBEDDING_PROVIDER：显式设置时以环境变量为准（完全向后兼容）；
    # 未设置时自动探测：有可用云端 EMBEDDING_API_KEY → "openai"，否则 → "local"
    # （进程内 llama.cpp 跑 GGUF，不再依赖 Ollama 常驻服务）。
    # 「可用」排除空值 / 纯空白 / 常见占位符（YOUR_API_KEY 等，见 _detect_embedding_provider）。
    # 注意：换 provider = 换向量空间，必须全量重建知识库索引（scripts/build_kb_index.py）
    EMBEDDING_PROVIDER = _detect_embedding_provider()
    # 本地 Ollama Embedding
    OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    # Ollama 原生 embedding API 根地址（/api/embeddings），与 LLM 用的
    # OLLAMA_BASE_URL（OpenAI 兼容 /v1 路径）解耦。
    # 同上：用 IPv4 字面量而非 localhost，避免 IPv6 优先解析带来的连接超时。
    OLLAMA_EMBEDDING_BASE_URL = os.getenv("OLLAMA_EMBEDDING_BASE_URL", "http://127.0.0.1:11434")
    OLLAMA_EMBEDDING_DIM = int(os.getenv("OLLAMA_EMBEDDING_DIM", "1024"))
    # 云端 OpenAI 兼容 Embedding API（默认 Voyage，可换任意 OpenAI 兼容端点）
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.voyageai.com/v1")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "voyage-3")
    EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

    # ---- 本地词嵌入（provider=local：进程内 llama.cpp 跑 GGUF，不依赖 Ollama 服务）----
    # 背景（2026-10-26 实测）：Qwen3-Embedding-0.6B-Q8_0.gguf 在 llama.cpp 下
    # pooling 必须 LAST（decoder-only 因果 LM，只有末位 token 见过整句；mean/cls
    # 余弦只有 0.755/0.341），输出再 L2 归一化即可与 Ollama 同一向量空间（cos 0.9994）。
    # 非空 = 直接指向本地 GGUF 绝对路径：跳过下载与缓存目录逻辑，缺文件直接报错。
    EMBEDDING_MODEL_PATH = os.getenv("EMBEDDING_MODEL_PATH", "")
    # 下载用的 ModelScope/HuggingFace 仓库（国内首选 ModelScope，auto 会先试它）
    EMBEDDING_MODEL_REPO = os.getenv("EMBEDDING_MODEL_REPO", "Qwen/Qwen3-Embedding-0.6B-GGUF")
    # 下载用的文件名（同时也是缓存目录内的文件名）
    EMBEDDING_MODEL_FILE = os.getenv("EMBEDDING_MODEL_FILE", "Qwen3-Embedding-0.6B-Q8_0.gguf")
    # 完整性校验：sha256（不匹配=删 .part 报错，绝不留坏文件）
    EMBEDDING_MODEL_SHA256 = os.getenv(
        "EMBEDDING_MODEL_SHA256",
        "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
    )
    # 字节数（用于进度条与完整性校验）
    EMBEDDING_MODEL_SIZE = int(os.getenv("EMBEDDING_MODEL_SIZE", "639150592"))
    # 模型缓存目录；空 = <项目根>/models
    EMBEDDING_MODELS_DIR = os.getenv("EMBEDDING_MODELS_DIR", "")
    # 缺模型时是否自动下载（0 = 拒绝下载并给出手动放置指引）
    EMBEDDING_AUTO_DOWNLOAD = os.getenv("EMBEDDING_AUTO_DOWNLOAD", "1") == "1"
    # 下载源：auto（ModelScope→HuggingFace 依次试）/ modelscope / huggingface
    # 注意：不要用 hf-mirror.com——它对本仓库返回 308 永久重定向回 huggingface.co。
    EMBEDDING_DOWNLOAD_SOURCE = os.getenv("EMBEDDING_DOWNLOAD_SOURCE", "auto").strip().lower()
    # 本地模型输出维度（Qwen3-Embedding-0.6B = 1024）
    EMBEDDING_LOCAL_DIM = int(os.getenv("EMBEDDING_LOCAL_DIM", "1024"))
    # llama.cpp 上下文长度：实测基准值 2048，勿擅自放大（放大直接吃内存/速度）
    EMBEDDING_LOCAL_N_CTX = int(os.getenv("EMBEDDING_LOCAL_N_CTX", "2048"))
    # llama.cpp 批大小。**它同时是分块池化的块长上限**（EMBEDDING_LOCAL_CHUNK_TOKENS
    # 必须 ≤ 它），所以调小它不是「只省内存」的旋钮：
    #   1) 块长跟着变小 → 长节点被切得更碎，已有向量全部失效，必须整库重嵌
    #      （2026-10-30 实测：2900 节点按 128 token 重切，一小时仍未跑完）；
    #   2) 639MB 的模型本身是硬地板，省下的只是计算批缓冲那一段。
    # 默认 512 保持不变。**不建议调小**：2GB 机器请改用进程外 embedding
    # （llama-server / Ollama / 云端 key），不要靠这个参数硬挤。
    EMBEDDING_LOCAL_N_BATCH = int(os.getenv("EMBEDDING_LOCAL_N_BATCH", "512"))
    # 分块池化块长（issue #232）。**必须 ≤ EMBEDDING_LOCAL_N_BATCH**，否则块
    # 白做：短于块长的切块没问题，但任何要送进 embed() 的块本身也受
    # `tokens = tokens[:n_batch]` 静默截断约束——块比 n_batch 大，块内又被截，
    # 长节点的向量照样只覆盖前缀，截断事实还会被 audit 误标成「已覆盖全文」。
    # 默认 512 = 与 n_batch 相同（切块尺寸即批尺寸，一块一次 embed()）。
    EMBEDDING_LOCAL_CHUNK_TOKENS = int(os.getenv("EMBEDDING_LOCAL_CHUNK_TOKENS", "512"))
    # CPU 线程数；0 = 交 llama.cpp 自动决定
    EMBEDDING_LOCAL_N_THREADS = int(os.getenv("EMBEDDING_LOCAL_N_THREADS", "0"))

    # ---- 根据后端动态取得当前使用的配置 ----
    @classmethod
    def get_llm_config(cls):
        if cls.LLM_BACKEND == "deepseek":
            return {
                "base_url": cls.DEEPSEEK_BASE_URL,
                "api_key": cls.DEEPSEEK_API_KEY,
                "model": cls.DEEPSEEK_MODEL,
            }
        else:  # ollama
            return {
                "base_url": cls.OLLAMA_BASE_URL,
                "api_key": "ollama",  # Ollama 不需要真 key
                "model": cls.OLLAMA_MODEL,
            }
