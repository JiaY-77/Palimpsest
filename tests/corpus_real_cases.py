"""真实存量语料解析夹具（2026-10-08 复核导出的 18 条真实 task 节点首行）。

来源：``task-state/task_corpus_raw.md``（原库 18 个 type=task 节点的 content 原文，
此处裁剪成首行）。**这是回归护栏**：早期测试全用干净输入（``[doing] T-1``），
导致解析器对真实语料只有 3/18 命中的硬伤没被任何测试发现。

``expected`` 取值：``todo`` / ``doing`` / ``blocked`` / ``done`` / ``canceled``
或 ``None``（本条本来就没有状态词 / 判断不了，按「不猜」返回 None）。
"""

# (条目序号, 首行, 期望状态, 说明)
REAL_CORPUS = [
    (
        1,
        "【任务·任务节点定位架构改造（进行中）2026-10-08 启动】用户指令「去和网页 AI 商量彻底解决这个架构问题，然后叫上 assistant 去做好」。方案见架构咨询结论。分工：assistant 主导设",
        "doing",
        "全角【】+ 内部全角（）+ 状态词在括号中间",
    ),
    (
        2,
        "【任务·技能库瘦身（进行中）2026-09-21 挂起待续】用户问「palimpsest-protocol 怎么这么长，技能跟 Palimpsest 结合还没实现吗」→ assistant 查明两件事，用户定「两个都排上，先瘦身再做索引",
        "doing",
        "全角【】内含「进行中」与「挂起」——首现「进行中」优先（仍 active，未真的挂起）",
    ),
    (
        3,
        "【Palimpsest 第二批（GitHub issue #15）2026-09-15 开工】范围＝外部审核汇总的 10 条 issue，全部修完并合入 main。10 条清单：①`eval/run_eval.py` ",
        "doing",
        "全角【】含「开工」",
    ),
    (
        4,
        "[已开工 2026-09-14 晚，用户拍板] Palimpsest 第一批＝**退役 rule 机制**（原三条按退役方向改写）。范围：全链路删除 rule 通道——router_query /",
        "doing",
        "半角块含空格 / 长标记 / 「已开工」",
    ),
    (
        5,
        "[待决策 + 第二批 2026-09-14] Palimpsest 架构体检六问后续。用户待拍三事：①规则类文档归宿——重建（副官加班协议/宪法/模型军团管理办法/模型路由决策树）还是彻底退役 rule 机制（连带删 ",
        "todo",
        "「待决策」→ todo",
    ),
    (
        6,
        "T-73：palimpsest doctor 体检命令（部署门槛降低 ②③ 之一）→ [待办，2026-09-10 用户指令「计入 Palimpsest」]（把 core/startup_check.py 的启动自检升",
        "todo",
        "状态词不在开头、在行中；块含空格/超长/全角引号",
    ),
    (
        7,
        "【挂账·等复测触发】注入噪声专项（由原专项拆分，用户 2026-09-05 拍板单独立项）：prefetch 召回策略（hermes-plugin prefetch include_neighb",
        "blocked",
        "【挂账】→ blocked",
    ),
    (
        8,
        "T-71-B1：mem_graph_analysis → [完成]（2026-09-02 晚，opencode/big-pickle 执行 + assistant 验收修复 + 测试补齐 5 个）。MCP 工具 me",
        "done",
        "真完成（复核表格未单列，按正文「完成」判 done）",
    ),
    (
        9,
        "[挂账保留] T-71-B2/B3/B4 + 杂项 → [挂账]（P2，阶段 3；B1 已拆出立项，剩余保持挂账）。B2 mem_path_query（ALL_PATHS/SHORTEST_PATHS",
        "blocked",
        "「挂账保留」+「挂账」→ blocked",
    ),
    (
        10,
        "T-72：TriviumDB 0.8.3→0.8.5 同步升级 → [完成]（2026-09-02，上游 v0.8.4 hardened query + v0.8.5 prepared vectors",
        "done",
        "「完成」→ done",
    ),
    (
        11,
        "T-70：Palimpsest 改造 A1+A3 → [待批准]（P1，阶段 2）。A1：_expand_neighbors Python BFS → search_advanced SA-PPR（v2 实测 4.6x",
        "todo",
        "「待批准」→ todo（复核原表格写作「待批准」→ todo）",
    ),
    (
        12,
        "T-57：复杂查询多路扩展（r1 出策略升级：模糊查询拆解 2-3 视角子查询 + 并行检索 + RRF 融合，简单查询仍 qwen 直查）→ [挂账，T-55 换脑落地后实施]（用户 2026-08",
        "blocked",
        "块含「挂账」+「落地」——「挂账」在前（本任务已挂账），「落地」是别人 T-55 的"
        "完成条件（换脑落地后**才**实施）→ 块内首现的状态词「挂账」胜出 → blocked",
    ),
    (
        13,
        "T-17：assistant 开源部署 → [挂起·非当前焦点]（2026-09-05 用户：非期末考试、与 Palimpsest 无关；短期专维护 Palimpsest。要求仍：完全独立+脱敏红线，等用户另行安排启动）",
        "blocked",
        "「挂起」→ blocked",
    ),
    (
        14,
        "T-14：硬件升级计划（RTX 5070 Ti 16GB）→ [计划中]（按预算计划购买，可跑 30B MoE / 14B 推理兵）",
        "todo",
        "「计划中」→ todo",
    ),
    (
        15,
        "T-37：行为层 KPI（被提醒次数=执行失败记录）→ [挂账持续]（token 消耗优化：会话收敛+工具节制，详见 token-cost-control 技能）",
        "blocked",
        "「挂账持续」→ blocked",
    ),
    (
        16,
        "T-52：某办公套件开放平台集成 → [部分完成]（md→PDF 已通；剩：知识库创建节点/移动文档、成员 API 授权、批量整理，待需求推进）",
        "doing",
        "「部分完成」不能被「完成」抢 → doing",
    ),
    (
        17,
        "T-30：Palimpsest 阶段四——记忆生命周期 → [完成 2026-09-05]（用户拍板用小说语料启动；①87 md 入库 domain=novel ✅85 节点 ②批量建边",
        "done",
        "「完成 2026-09-05」带日期后缀 → done",
    ),
    (
        18,
        "T-55：Palimpsest×Hermes 融合「插件 promax」→ [第二步入落地]（换脑已激活，剩：成本观察挂账 + 第三步治理自动化/多 Agent 共享；详情见知识库 03_技术学习/PalimpsestHermes融",
        "doing",
        "「第二步入落地」→ 命中「落地」doing（复核原表格写 doing）",
    ),
]

# 18 条中「本条本来就没状态词」的条数：期望 None。
EXPECTED_NONE = sum(1 for _i, _c, exp, _w in REAL_CORPUS if exp is None)
