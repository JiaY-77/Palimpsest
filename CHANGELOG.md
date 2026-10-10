# Changelog

本项目遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)（Semantic Versioning）。

版本格式：`主版本.次版本.修订号`。发布流程见 [RELEASING.md](docs/RELEASING.md)。

## [Unreleased]

## [2.6.0] - 2026-10-10

### 新增

- **事实时间维度（bi-temporal）：记忆开始记录「事实何时为真」**：此前 Palimpsest 只记 `created_at`（记录何时写入系统）。知识有时效性——事实会变真、也会不再为真，只有系统时间就无法回答「三个月前它认为这条配置是什么」。现引入与系统时间正交的**世界时间**维度，仅作用于事实类节点（`memory` / `task` / `plan`）：写入时自动补 `valid_at`（事实开始为真，缺省=写入时刻，显式传入不被覆盖）；高相似冲突命中、旧节点被标 `outdated` 时补 `invalid_at`（=取代者的 `valid_at`）与 `expired_at`（=本次系统标记时刻，仅首次写入）。`kb_chunk` 与历史留痕类型豁免。检索结果新增 `meta.times` 透出时间字段；新增 MCP 工具 `mem_fact_history`——沿 `REVISED_BY` 出边与**反向边**（`TriviumStore.get_incoming_edges` 新封装）返回单条事实的完整时间线与取代关系（它取代了谁 / 谁取代了它）。详见 [`docs/BITEMPORAL.md`](docs/BITEMPORAL.md)。
  分期说明：本次为**第一阶段「存字段 + 反向索引」**；`as_of` 历史视图查询与时间窗冲突判定属后续阶段，不在本 PR 范围（不改动现有冲突判定语义，历史节点无字段走原行为）。
- **混合检索通道降级留痕**：RRF / 级联融合在某路检索不可用时本就静默退回另一路，但「FTS 通道挂了」与「确实没命中」在结果上无法区分——检索质量下降不留痕迹。现 `core.fts_index.search_fts_status()` 返回 `(rows, status)`，把通道状态分为 `ok`（查询正常执行，空结果即真无命中）/ `empty`（空查询）/ `degraded`（索引文件不存在或查询抛异常）；`search_fts()` 保留为只取 rows 的向后兼容包装。`_hybrid_rrf` / `_hybrid_cascade` 改用前者，FTS 降级时记 warning 并在对外结果里新增 `channels` 字段（如 `{"semantic": "ok", "fts": "degraded"}`），调用方可据此判断降级。
- **`doctor` 新增「运行时路径字符集」预检（第 8 项）**：同类项目在中文/非 ASCII 用户名路径下会因底层图库扩展创建文件失败而开箱即崩（且报错误导为 "Access is denied"）。Palimpsest 实测在该场景正常，但仍做一次预检——关键路径（数据目录 / FTS 索引目录）含非 ASCII 字符时只**提示风险**（`ok=True`），仅当路径实测不可写才判失败并给出「改用英文目录」的修复方向。

### 修复

- **技能索引在技能目录缺失时会清空全库技能节点**：`scripts/build_skill_index.py` 的 `_skill_files()` 对不存在的目录返回空列表，`build()` 随即以空 `known_paths` 执行孤儿清理，把库中**全部** `skill_chunk` 节点当作「源文件已删除」删光——一次 `HERMES_HOME` 指向不存在的目录即可静默清空技能检索。现在技能目录缺失一律 fail-fast（抛 `SkillsDirNotFoundError`，CLI 以退出码 2 结束并提示 `--skills-dir` / `HERMES_HOME` / `HERMES_PROFILE`），仅在目录真实存在并通过扫描时才执行孤儿清理；「目录存在但没有 SKILL.md」仍按合法空目录处理。
  `tests/test_skill.py` 新增回归（目录缺失不删任何节点）与「空目录仍可清理孤儿」的对照用例。
- **技能目录解析改为 profile 感知**：此前技能目录恒取 `<HERMES_HOME>/skills`。档案模式下 Hermes 会把 `HERMES_HOME` 指向 `<root>/profiles/<name>`，此时该式等价于档案目录、行为正确；但若启动器只钉了 `HERMES_PROFILE` 而 `HERMES_HOME` 停在根，则会误指向根下的技能目录。现新增 `resolve_skills_dir()`，按 `--skills-dir` → `<root>/profiles/<profile>/skills`（存在时）→ `<HERMES_HOME>/skills` → 平台默认 `<root>/skills` 的顺序解析，取第一个存在者。`tests/test_skill.py` 覆盖各档顺序与「全部缺失则报错」。

## [2.5.0] - 2026-10-09

### 新增

- **记忆策略引擎下沉本体 + 宿主无关的 lifecycle 协议**：此前「什么值得记、记哪一层、什么时候召回、怎么去重提炼」的判定住在 Hermes 接入插件（`hermes-plugin/__init__.py`）里——那是产品核心资产，却寄居在某个宿主的插件接口内：换个宿主智能即丢失，能力上限也被该宿主的钩子协议框住。现将决策整体收进 `core/strategy.py`（强信号正则、近似重复阈值、importance 分档、分层归属、trivial/长度门槛等），并暴露一组**宿主无关**的 lifecycle 端点：`POST /lifecycle/pre-turn`（决定召回什么，返回可注入 prompt 的文本）、`/lifecycle/post-turn`（决定是否沉淀 / 写什么 / 写哪层）、`/lifecycle/session-end`（会话要点提炼 + 去重 + 写入 facts 层）、`/lifecycle/pre-compress`（压缩前抽取，不写库）。每个决策都返回 `decision_log`，让「为什么召这些、为什么没写那条」可观测——判断力因此可审计、可评估，而不只是藏在代码里。
  落地要点：①分层语义保持不变（自动抓到的用户原话片段 → logs 层 `type=record`；提炼后的要点 → facts 层 `type=memory`），历史修复（#46 scope 默认 memory、#47 会话要点写 facts、#48 明确指令正则）随之下沉；②`is_trivial_prompt` 此前借自宿主的 `agent.memory_provider`，现于本体自实现，`core/strategy.py` 不依赖任何宿主模块；③Hermes 适配器压薄为纯传输（读配置、拼 payload、转发、注入结果），不再含任何阈值/正则；④MCP 工具保留冷路径（模型主动用记忆），lifecycle 协议覆盖热路径（记忆自动找到模型），二者共享同一策略引擎与存储层。测试：决策行为迁移至 `tests/test_strategy_engine.py`，适配器传输由 `tests/test_hermes_plugin_adapter.py` 锁定（含「决策符号不得再出现在适配器」的边界守卫），端到端契约由 `tests/test_lifecycle_endpoints.py` 覆盖（含 10 轮模拟会话的分层断言）。
- **压缩前图谱增强也收进本体（`POST /lifecycle/context-enhance`）**：压缩引擎适配器（`hermes-plugin/context_engine.py`）此前自己挑主题、**逐个主题发 HTTP** 查 `/mem/search`、再拼装注入文本——同属「记忆的智能」却留在宿主侧，同样受宿主钩子形态牵制。现新增 `POST /lifecycle/context-enhance`：宿主只提交「将要压缩的消息 + 保护段参数 + 耗时预算」，主题选取（`core.strategy.extract_graph_topics`）、图谱检索（**在本进程内完成**，省掉每主题一次 HTTP 往返）、注入文本排版（`core.strategy.format_graph_enhancement`）全部在本体发生，并返回 `decision_log`。适配器随之压薄为「读配置 → 转发 → 把 `enhancement_text` 合并进 `memory_context`」，不再含任何阈值/模板（有边界守卫测试防止回流）；主题数上限由 `core.strategy.clamp_max_topics` 统一收敛。
- **服务守护脚本 `scripts/service_guard.py`（防重复拉起）**：README 要求 REST 服务常驻运行，但仓库此前只提供 `start_rest.vbs`（一次性启动），没有守护——服务崩掉后不会自动恢复。新增的守护脚本守 REST 与 Ollama 两项，且**在拉起前先确认目标端口无人监听、上一次启动已过宽限期**。这道检查是必需的而非可选的：TriviumDB 以独占方式打开库文件，而 Palimpsest 采用「每操作开-关库」模式，因此两个 REST 进程同时运行会争抢库文件；抢输的写入被中途打断，会让 storage generation 不一致（`.flush_ok` 与 `.vec`/`.pld` 对不上），库从可读写退化为读不动，且不可原地修复（只能从备份恢复）。一个只看 HTTP 的守护恰好会制造这一场景——服务**正在启动**（加载 embedding、建索引）时 HTTP 无响应，被判为「已死」而再次拉起，而旧进程其实还活着。
  实现上有两处必须留意：①端口探测**先于**HTTP 探测执行——HTTP 探测会阻塞至超时并在监听 backlog 上占位，先跑它会让端口探测自身超时（实测的 false-negative，会反过来触发重复拉起）；②端口探测用 `connect_ex` 并在 `finally` 中立即关闭套接字——留下半开连接同样会占用 backlog 槽位，让后续探测误判。`tests/test_service_guard.py` 锁定端口/HTTP 判定、启动宽限期与事件记录三组契约（含用只监听不响应的桩复现「启动中」场景）。

### 修复

- **存储 generation 损坏时 fail-fast（不再静默降级）**：`TriviumStore._init_indexes()` 此前把一切异常都当「索引创建失败」静默吞掉——包括 triviumdb 的「存储 generation 损坏」（如 `.flush_ok` 与 `.vec` 不匹配）。后果是库实际已不可读写（`stats` 全空、写入回滚），`startup-check` 却五项全绿，只有 `doctor` 的向量维度 / 迁移两项才暴露，损坏因此被长期忽视、延误恢复。现新增 `core.utils._is_db_corrupt_error()`（消息标记识别，与既有锁判据 `_is_db_locked_error` 同构），`_init_indexes()` 遇损坏一律 fail-fast，与「库被其他进程占用」的处理一致。`tests/test_corrupt_error_failfast.py` 锁定该契约（含真实损坏措辞识别与「与损坏无关的索引错误仍降级」基准）。
- **写入失败路径补回归测试（`tests/test_partial_write_rollback.py`）**：此前「事务失败不留半状态」与「失败后触发健康探测」两条契约无测试覆盖。新增三例：正常写入落库（对照组）、事务中途抛异常后节点未落库且返回 `stored: False`、失败路径确实调用 `check_db_health` 并回报 `db_healthy`。测试复用 `conftest.py` 的全局临时库，不自行改写 `Config.DB_PATH`——后者是无效的，因为 `mcp_tools._common.store` 是导入时构造的模块级单例，其路径在 import 那一刻已固定。

## [2.4.1] - 2026-10-06

### 修复

- **MCP 端点 `/mcp` 无尾斜杠时返回 307，导致按文档接入的客户端连不上**：`/mcp`（不带尾斜杠）由 Starlette 的 `Mount` 307 重定向到 `/mcp/`，而多数 MCP 客户端（含 Hermes）不跟随重定向——于是按注释与 README 给出的接入方式（`url = http://127.0.0.1:8090/mcp`）连接会直接失败，表现为 503。新增 `_NormalizeMcpPath` ASGI 中间件，在路由匹配前把裸 `/mcp` 就地改写为 `/mcp/`，两种写法均直达 MCP 子应用、不再产生重定向。中间件经 `app.add_middleware(...)` 挂载而非包装 `app` 对象——后者会使其后的 `@app.exception_handler` / `@app.get` 注册全部静默失效。
  `tests/test_mcp_path_normalization.py` 锁定该契约（`/mcp` 与 `/mcp/` 均须 200；断言刻意使用 `follow_redirects=False`，否则测试客户端会自动跟随 307 导致回归「假过」）。

## [2.4.0] - 2026-10-03

### 新增

- **存储协议与客户端实现（`protocols.Store` / `client.RemoteStore` / `server.LocalStore`）**：引入依赖倒置点，为「一份数据、一个写者、多条接入方式」铺路。`protocols.Store` 只定义接口；`server.LocalStore` 是服务端对 `TriviumStore` 的包装（REST 进程专用），`client.RemoteStore` 内部走 REST HTTP（CLI / dashboard / 脚本用）。`core/` 从此只依赖协议，不再感知具体实现，也避免与 REST 形成循环依赖。`RemoteStore` 放在 `client/`（不是 `core/`）是架构纪律
- **CLI 读命令改走 REST（不再自己开库）**：`search` / `hybrid-search` / `recent` / `graph` / `kb` / `stats` / `ingest` / `link` 原先直接调用 `mcp_tools` 的工具函数，而那些函数内部使用 `mcp_tools._common.store` —— 一个模块级 `TriviumStore`。结果是 CLI 每跑一次读命令就在本进程打开一次记忆库，与常驻 REST 争抢同一份库文件；triviumdb 对库文件是连接级排他的，偶发抢不到窗口的失败正是「库偶尔坏」的成因。现在这些命令一律经 REST（`PALIMPSEST_BASE_URL` 可覆盖，默认 `http://127.0.0.1:8090`）；REST 端点内部复用的仍是同一套检索实现，故结果逐字段一致。`mcp_tools` 同时改为命令内**惰性 import** —— 它的 `_common` 在模块级就执行 `store = TriviumStore()`，仅 import 便会建出 `data/` 目录
- **`POST /mem/recent` 端点**：CLI `recent` 此前无对应端点，新增之（复用 `mcp_tools.memory.mem_recent`，不复制逻辑）

### 文档

- **明确 MCP 接入优先级（HTTP 优先，stdio 为逃生梯）**：两份 README 的「启动」一节原先把 `python mcp_server.py` 与 REST / CLI 平铺为并列选项，容易被误解为常规用法。现改为显式三层优先级：① REST 的 `/mcp`（首选，与 REST 共用进程）② CLI / dashboard / 脚本（同样经 REST）③ stdio `mcp_server.py`（**仅限完全不跑 REST 的场景**，⚠️ 与 REST 同跑会争抢同一库）。stdio 代码予以保留，定位为「REST 出问题时的独立救援通道」，而非并列入口

### 变更

- **dashboard 改为纯客户端（不再自己打开数据库）**：`scripts/dashboard.py` 原先在模块级 `TriviumStore()` 常驻并直接全表遍历，等于在 REST 之外多出一个写者——与 REST 争库，正是「库老是坏」的成因之一。现所有数据一律经主 REST 服务获取（`PALIMPSEST_BASE_URL` 可覆盖，默认 `http://127.0.0.1:8090`）；对外 `/api/*` 接口与 `dashboard.html` 保持不变。REST 不可达时返回 `502` 并附排查指引；合并功能因 REST 尚无对应业务端点，显式返回 `501` 说明原因（不再静默返回空）。注意：`/api/mem/search` 由本地 FTS 全文检索改为 REST 语义检索，排序语义随之变化

### 修复

- **Hermes 插件：修正写入层归属与召回范围（三处同根因）**：插件 v1.0.0 的写入层归属与读取过滤和分层设计不一致，三处一并修正。
  1. `sync_turn` 自动沉淀从前写 `type=memory`（facts 层），且用一套含 `启动`/`安排`/`计划`/`方案`/`优先` 等操作动词的宽关键词表匹配——安装日志、构建输出、后台进程通知都会被误判为“重要信号”而落库。现：改用独立的「明确指令」正则（只认记住/纠正/偏好/规则类），并写入 `type=record`（logs 层）——自动抓到的是未经加工的用户原话，本就不是事实。落库 payload 同时记录 `matched_keyword` / `match_pos`，并在正文超 300 字时加截断标记，使每条自动写入都可事后审计。
  2. `on_session_end` 从前写 `type=record`（logs 层），而 `prefetch` 默认 `tier="facts"`——会话要点只写不读，静默失效。现：改写 `type=memory`（facts 层），与读取侧对齐（提炼后的结论本就该回到上下文）。
  3. `prefetch` 从前用 `scope="all"`，导致 `kb_chunk` 混入注入池并挤占 `top_k` 名额（`kb_chunk` 的 `domain` 恒为 `"kb"`，对 `domain` 过滤免疫，只有 `scope` 能挡住）。现：新增 `PALIMPSEST_PREFETCH_SCOPE` 配置项，默认 `memory`（只召回记忆节点）；需要把知识库切片一并注入时显式设为 `all`。
  三处遵循同一原则：**自动抓取的对话片段 → logs 层；提炼后的结论 → facts 层；知识库切片由 `scope` 隔离**。

- **库被其他进程占用时 fail-fast（不再静默降级）**：`TriviumStore._acquire()` 遇到 triviumdb 的连接级排他错误时，改抛带库路径与处理指引的 `DatabaseBusyError`；`_init_indexes()` 不再把它当作「索引创建失败」静默吞掉——此前正因如此，「偶发写失败 → 文件组残留 → 库从可读写变读不动」会被伪装成一切正常。CLI / 脚本经既有顶层兜底输出明确指引，REST 侧新增 `503` 处理器（`detail` = 记忆库被其他进程占用）

- **Ollama 端点默认值改用 IPv4 字面量**：`OLLAMA_BASE_URL` / `OLLAMA_EMBEDDING_BASE_URL` 的默认值由 `http://localhost:11434` 改为 `http://127.0.0.1:11434`（含 `.env.example` 与两份 README 配置表）。部分系统把 `localhost` 优先解析为 IPv6 回环 `[::1]`，而 Ollama 默认只监听 IPv4，导致每个请求都要先经历一次连接超时再回落——实测单次 embedding 由数十毫秒退化为约 2 秒，检索与写入吞吐随之下降约两个数量级

- **FTS 索引与主库同步（三处同根因）**：store 层的写方法此前不负责同步自己的派生状态——FTS5 全文索引（独立于主库的 SQLite 文件）的清理责任被推给每个调用方，而调用方会漏。三处一并修正：
  1. `TriviumStore.delete_node` 只删主库节点、不清 FTS，`scripts/build_*_index.py` 三个调用点从未清理（仅靠全量 `fts_rebuild` 掩盖）。现：`delete_node` 内部同步清理 FTS。
  2. `consolidate` 经事务写入合并节点（只带向量），调用方未补索引——合并出的记忆**能被语义检索命中、却对全文检索不可见**。现：`consolidate()` 公开入口统一为新合并节点补 FTS，所有调用方自动获得。
  3. 改 `content` 后 FTS 仍指向旧文本。现：新增 `TriviumStore.update_content`，改内容时同步 FTS 并置 `vector_stale` 标记——**不**内联重算向量（重嵌是网络调用，不该塞进写路径；重嵌仍走显式 `POST /memory/{id}/reembed`）。
  FTS 失败一律非致命（仅告警，可 `fts-rebuild` 兜底），与既有 REST 端点行为一致。

### 测试

- **补齐写路径与共存的回归测试**：新增 `tests/test_fts_write_consistency.py`（delete / merge / content-update 三条路径的 FTS 终态一致）与 `tests/test_multi_writer_cli_rest.py`（dry-run 只读不写、`consolidate(dry_run=True)` 不新增节点、库被占用时抛带指引的 `DatabaseBusyError`、以及 #52 的漂移本身——改 content 后向量仍为旧值）。锁的**时序**不做断言（triviumdb 开窗行为非确定），只断言无论谁赢得竞态都必须成立的性质，避免 flaky。

## [2.3.0] - 2026-09-19

### 新增

- **Hermes 技能语义索引与检索**：`scripts/build_skill_index.py` 扫描技能目录（默认 `$HERMES_HOME/skills`，`--skills-dir` 可覆盖）下的每个 `SKILL.md`，以一个 `type=skill_chunk` 节点入库（frontmatter `name` / `description`，`category` 取相对父目录），增量按 `source_mtime`、孤儿清理、幂等；新增 MCP / REST 工具 `skill_search`（`POST /skill/search`）按语义检索技能

## [2.2.0] - 2026-09-17

### 新增

- **索引规则可配置（`core/index_rules.py`）**：`build_novel_index.py` 与 `build_kb_index.py` 的扫描范围、目录 → `kind` 映射、分块策略、payload `domain` 不再写死在代码里，改由一套声明式 JSON 规则驱动。加载优先级：`--rules <path>` > `<库根>/<legacy 文件名>`（novel 脚本兼容 `.palimpsest-novel-index.json`）> `<库根>/.palimpsest-index.json` > 内置默认。规则文件字段：`kind_map`（支持 `dir_prefix` / `filename` / `glob` 三种匹配，按声明顺序先匹配者胜）、`default_kind`、`chunk_strategy`（`file` / `heading` / `chars`）、`min_chunk_len` / `max_chunk_len`、`domain`、`include` / `exclude`、`require_frontmatter_id`。未知键只告警（向前兼容），类型或取值非法直接报错
- **未匹配路径显式可见**：`kind` 落到 `default_kind` 的文件会单列进统计新字段 `unmatched_paths` 并打印在摘要里。此前 `_kind_of()` 用 `return "character"` 兜底，把「不知道」伪装成「知道」——某库角色卡迁移目录后 77 张卡被误判为 `setting` 而脚本无任何提示，正是这条静默兜底造成的
- **`--require-frontmatter-id`（novel 脚本）**：只索引 frontmatter 含 `id:` 键的文件，其余跳过并计入未匹配
- **`examples/index-rules.example.json`**：不含任何个人目录名的规则示例，演示全部字段与三种匹配方式
- README / README_EN 新增「自定义索引规则 / Custom index rules」一节

### 变更

- 两个索引脚本的内置默认不再包含任何具体库的目录名（`01_世界观/` 之类的约定从开源仓库移除），只保留通用行为：novel 脚本「整文件 + 排除 `03_章节` / `04_草稿` / `.obsidian`」，kb 脚本「按 `##`/`###` 标题切 300~800 字符 + 排除 `.obsidian`」
- `scripts/build_kb_index.py` 的 `split_markdown()` 实现迁至 `core/index_rules.chunk_markdown()`；公开函数名与签名保留（新增可选 `rules` 参数），默认行为不变
- 返回统计新增 `unmatched_paths`（novel 脚本另加 `rules_source` / `rules_warnings`），既有键保持不变

### 测试

- 新增 `tests/test_index_rules.py`：27 条，覆盖默认加载、加载优先级（含 legacy 文件名与 `.yaml` 迁移提示）、非法配置与未知键、三种匹配方式、`is_included` 白/黑名单、`chunk_markdown` 与 `split_markdown` 的迁移等价性、内置默认可被调用方覆盖、以及「未匹配不静默归并」的回归断言

## [2.1.0] - 2026-09-16

### 新增

- **`mem_review` 支持 `tier` 参数**：复盘盘点的 `recent_ingests` 现在受 `tier` 视图约束，与 `mem_search` 同款判定（`facts` 默认 / `logs` / `""` 不过滤）。CLI 新增 `review --tier`。`tier=""` 保持改动前的输出不变
- **`mem_stats` 新增 `tiers` 分节**：按检索侧 tier 语义输出 type 分布（`facts` / `logs` / `unclassified`），并回显本次生效的 `facts_types` / `logs_types` / `default_tier`，让「哪些 type 落在哪层」自解释。`totals` 保持不变
- **`_apply_merge` 新增 `per_pair_commit` 开关**（默认 `False`）：默认整批单事务保持不变；置 `True` 时每对合并独立提交。该开关暂未通过 `consolidate()` 对外暴露
- **`docs/DEPRECATIONS.md`**（新增）：登记字段 / 接口的弃用与退役计划。首条为 `character_name`（`domain` 的历史兼容镜像），含四阶段退役步骤与验证命令

### 修复

- **`eval/run_eval.py` 四条检索路径口径统一**：`_run_fts` / `_run_vec` 此前绕过共享过滤链，`tier` 与 `outdated` 语义与 `_run_rrf` / `_run_cascade` 不一致，四种模式不可比。现四条路径共用同一套 `scope` / `tier` / `outdated` 判定，`_run_vec` 不再直调 `store.search_similar`；新增 `--tier` 暴露口径
- **应用生命周期改用 `lifespan`**：替换已弃用的 `@app.on_event("startup")`；全局 `TriviumStore` 改为懒加载（`_get_store()`），import `main` 不再作为副作用打开数据库、建索引；启动自检经 `asyncio.to_thread` 移出事件循环

### 文档

- **`readme_check.py` 三项增强**：① `config.py` 中默认值非字面量的 `os.getenv` 键必须登记，否则报 warning；② 新增 `root()` 端点索引一致性检查（与真实路由比对）；③ 文档区块标记缺失时由静默假绿改为显式 warning
- **`main.py` `root()` 的 `endpoints` 列表补全**：此前缺 `/`、`/summary`、`/report`、`/memory/{node_id}/vector`、`/graph/communities`、`/mem/stats`
- **`eval/README.md` 说明题集版本**：明确 `eval_set.json` 为当前基准，`eval_set_v1_149.json` / `manual_set.json` 为历史/手工题集且均不入库
- **`core/doctor.py` 的修复建议不再硬编码模型名**：改从 `Config.OLLAMA_EMBEDDING_MODEL` 读取（带兜底），换 embedding 模型后建议不再误导

### 技术债与整洁度

- 记忆分层常量（`TIER_FACTS` / `TIER_LOGS` / `DEFAULT_TIER`）移至 `mcp_tools/memory.py` 顶部——此前定义在 `mem_review` 之后，模块导入时求值默认参数会取不到
- `core/reporting.py` 的 `Config` 导入提到模块级；`openai` 保持函数内延迟导入并注明原因
- `mcp_tools/memory.py` 的 import 块恢复连续，去掉 8 处 `# noqa: E402`；删除两处 `if include_outdated is None` 死代码
- `core/conflict.py` 的 `_similar_hits` docstring 修正为与代码一致（召回 `max(9, 10)` 取前 3）
- `eval/pool_filter.py` 的 `should_write_output` 补上此前被忽略的两个参数检查：已有题集而新题集为空时拒绝覆盖，避免失败运行清空已存题目
- `core/dims.py` 注明对 triviumdb「打开已有库忽略 `dim` 入参」这一未文档化行为的依赖与风险
- `core/doctor.py` 的全量遍历检查注明保持理由（triviumdb 等值索引无法表达「字段缺失」判断）

### 测试

- 新增 `tests/test_eval_mode_parity.py`（四模式口径一致性）、`tests/test_readme_check.py` 扩充（三项新检查的漂移注入）、secret-scan 边界用例 6 组（邻位数字 / 全角数字 / `Bearer` 长度阈值 / 强弱规则同时命中 / `secret_hint` 形状）、`mem_review` 的 tier 过滤、`mem_stats` 的 `tiers` 分节、`_apply_merge` 的整批回滚与逐对提交

## [2.0.0] - 2026-09-15

### 破坏性变更

- **移除规则域（rule domain）**：整个规则域机制连同其对外接口一并删除——MCP 工具 `router_query`、REST 端点 `/mem/router`、Hermes 插件的 `palimpsest_router` 工具、配置键 `RULE_RETRIEVAL_WEIGHT` 与内置 `×1.3` 加权、`domain_bias="rule"` 取值、`DEFAULT_BLOCKS` 中的 `rule` 区块及 `domain_in_block` 的 rule→kb 兼容分支、`build_kb_index.py` 的规则类文档标记（知识切片统一为 `domain=kb`）、`scripts/sync_rules.py` / `scripts/check_kb_consistency.py`。**迁移指南**：调用方改用 `mem_search` / `mem_hybrid_search` 配合 `scope` / `domain` / `domain_bias` / `domain_boost`；原 `domain_bias="rule"` 传空串即可。生产库 `domain=rule` 节点为 0，故未提供兼容垫片
- **检索默认只回事实层**：`mem_search` / `mem_hybrid_search` 新增 `tier` 参数并默认 `facts`，日志层（`record` / `event` / `git_commit`）不再出现在默认结果中。**迁移指南**：需要旧行为的调用方显式传 `tier=""`；要专门查日志传 `tier="logs"`
- **Hermes 插件注入默认收敛**：`include_neighbors` 注入默认由开改为关；`PALIMPSEST_PREFETCH_TOP_K` 默认 `5` → `3`。需要旧行为的部署显式设 `PALIMPSEST_PREFETCH_NEIGHBORS=true` 与 `PALIMPSEST_PREFETCH_TOP_K=5`

### 新增

- **记忆分层（tier 过滤）**：检索侧新增 `tier` 视图，把日志层（`record` / `event` / `git_commit`，约占活跃节点四成）从默认检索与自动注入池摘出，提升检索精度、削减注入噪音。**不改存储、不迁数据**，仅在 `_mem_search_impl` / `_hybrid_search_impl` 的后置过滤加判定（与 `scope` / `domain` / `block` 并列）。取值：`facts`（默认，只回事实层）/ `logs`（只回日志层）/ `""`（不过滤，等价于此前行为——显式历史通道）。`kb_chunk` / `novel_chunk` 不入本体系（走既有 `scope` 隔离）；未登记 `type` 一律保守归 `facts`，不静默丢结果。全链路透传：MCP 工具 → REST 路由 → CLI `--tier` → Hermes 插件。语义侧与 FTS-only 侧同时受约束（否则日志层会从 FTS 路漏回）
- **注入降噪三项**（Hermes 插件）：`include_neighbors` 注入默认关（记忆域图近乎无边，原为硬编码 `True` 纯空转）；`PALIMPSEST_PREFETCH_TOP_K` 默认 `5` → `3`；注入最低相关度门槛提为可配 `PALIMPSEST_PREFETCH_MIN_SCORE`（默认 `0.3`，与原硬编码一致）。另新增 `PALIMPSEST_PREFETCH_NEIGHBORS` / `PALIMPSEST_PREFETCH_TIER` 开关

### 修复

- **任务归档幂等**：归档不再重复或漏删——归档文件 frontmatter 写入 `node_id` 作幂等键，重跑复用已有归档并补齐残留节点；写盘改为 `.tmp` → `fsync` → `os.replace` 原子替换（此前「写临时文件 → 删节点 → 重命名」在重命名前崩溃会丢归档）
- **`mem_communities` 未绑定变量**：`db` 在异常路径下未赋值即使用，触发 `UnboundLocalError`（全仓唯一漏 `if db is not None` 的位置）
- **`/report` 阻塞事件循环**：改为 `AsyncOpenAI` + `await`，全库扫描挪进 `asyncio.to_thread`，避免长报告卡住整个 REST 服务
- **`core/version.py` 版本缓存**：修正缓存后打 tag 不刷新的问题（指纹退化到 HEAD-only 时旧值残留）

### 重构

- **拆分 4 个 >100 行函数**（`core/trivium_store.search_similar` 137 行 · `mcp_tools/memory.mem_ingest` 125 · `core/stats.compute_stats` 119 · `core/consolidator._apply_merge` 100）：按单一职责抽出 helper（候选过期过滤 / 衰减重排 / block 过滤 / 统计累加器 / 事务写入 / 单对合并），主函数退回编排。**行为不变**，且不是靠「测试绿」自证：每处都在**生产库副本**上做了跨版本等价比对（`compute_stats` 输出、`mem_ingest` 两次写入（其中一次触发冲突检测）、`consolidate` 的 dry-run 与真实合并，重构前后零差异）；端到端检索评测 40 题 × 4 模式排序 **160/160 一致**（唯一差异是浮点末位 ≤2e-08，同版本重跑可复现，来自 embedding 服务而非本次改动）
- **消除 `core → scripts` 反向依赖**：`core/doctor.py` 等 6 处改为不引用 `scripts/`，依赖方向回归单向

### 工程化

- **引入 ruff 静态质量门禁**：`pyproject.toml` 新增 `[tool.ruff]`（显式写 `select` 钉住规则集，避免随 ruff 版本漂移；中文项目下忽略 `RUF001/002/003` 的全角标点误报），CI 新增独立 `lint` job，`requirements-dev.txt` 与 CI 均钉 `ruff==0.16.7`。此前仓库从未有过 lint / 类型 / 覆盖率门禁（`git log --all -S "tool.ruff"` 为空）
- **存量问题清零**（635 处 → 0）：自动修复 + 人工处理。`BLE001`（宽泛 except）与 `S110`（try-except-pass）逐处写明「为什么这里吞异常是设计」的理由；`try/except/pass` 关闭连接改为 `contextlib.suppress`；`raise` 补 `from e` / `from None` 标明异常链；未使用的导入、循环变量、模糊变量名等一并清理。全部 `# noqa` 重新生效（无失效指令）
- **引入 mypy 类型检查**（非严格起步，当前覆盖 `core/`）：目标是让已有注解变成真约束而非一步到位 strict。配套补 `core/conflict.py` 两处列表标注、`types-requests` 类型存根（此前 `requests` 报 import-untyped）
- **覆盖率基线**（`pytest-cov`，暂不设 `--cov-fail-under` 门槛）：`core/` + `mcp_tools/` 合计 **76%**（1770 语句 / 416 未覆盖）；最低为 `mcp_tools/routing.py` 22%、`mcp_tools/kb.py` 28%、`core/reporting.py` 5%。CI 输出报告但不阻断，先用数据定位缺口
- **新增 `scripts/readme_check.py` 文档一致性检查**：对齐 MCP 工具清单 / CLI 子命令 / REST 路由 / 配置项（`config.py` ↔ `.env.example` ↔ 两份 README）/ 文档内文件引用 / 行内代码配对，支持 `--json` 与 `--strict`；`docs/RELEASING.md` 已将其列入发版前置检查
- **文档一致性检查进 CI**（新增 `docs` job，`--strict`）：此前这个检查只活在发版清单里，PR 上无人拦截——唯一约束是模板里一个复选框，属自律而非门禁。现改为纯标准库的独立 job（无需安装依赖），文档与代码漂移在 PR 上直接红
- **配置项默认值纳入比对**：`check_config_keys` 原先只比对键名是否存在，改了代码默认值而文档没跟着改会静默漂移。现解析 `config.py` 的 `os.getenv(..., DEFAULT)` 字面量（逐字符括号配平取实参，`str(50_000)` 这类嵌套不会被正则截断）并与两份 README 表格第二列逐项比对；默认值为空串的键（API Key 类）要求文档写成空值标记；`.env.example` 增加反向校验（有、但代码不读 → 报警）。派生默认值（`DB_PATH` / `EMBEDDING_PROVIDER`）与 `config.py` 之外读取的键（`KNOWLEDGE_DIR`）在新代码里显式登记原因，不再静默跳过；配套新增 `tests/test_readme_check.py` 覆盖漂移注入与 `--strict` 退出码
- **静默异常留痕**：`mcp_tools/graph.py` 的「读节点 payload 失败按空处理」「读边数失败按 0 计」两处补 `logger.debug`——此前静默吞掉，出问题时没有任何线索。（其余静默点复核后确认无需补：关连接失败集中在 `contextlib.suppress`，业务失败路径本就有 `logger.warning/error`）

### 重构

- **文档（rule 退役配套）**：两份 README / CONTRIBUTING / hermes-plugin README 同步刷新，配置表新增「生效前提（Precondition）」列
- **拆分 4 个 >100 行函数**（`core/trivium_store.search_similar` 137 行 · `mcp_tools/memory.mem_ingest` 125 · `core/stats.compute_stats` 119 · `core/consolidator._apply_merge` 100）：按单一职责抽出 helper（候选过期过滤 / 衰减重排 / block 过滤 / 统计累加器 / 事务写入 / 单对合并），主函数退回编排。**行为不变**，且不是靠「测试绿」自证：每处都在**生产库副本**上做了跨版本等价比对（`compute_stats` 输出、`mem_ingest` 两次写入（其中一次触发冲突检测）、`consolidate` 的 dry-run 与真实合并，重构前后零差异）；端到端检索评测 40 题 × 4 模式排序 **160/160 一致**（唯一差异是浮点末位 ≤2e-08，同版本重跑可复现，来自 embedding 服务而非本次改动）

### 文档

- **文档与代码一致性刷新**：`README.md` / `README_EN.md` 补上此前只进了代码、没进文档的三块——`domain_boost` 域软加权、`eval/` 离线检索评测框架、质量门禁（ruff / mypy / 覆盖率）；修复英文版 CLI 表漏掉的 `reindex` 条目与两版 API Key 说明中未配对的行内代码；`.env.example` 补齐 9 个 `config.py` 已支持但模板缺失的配置项；项目结构树补 `eval/`、评测脚本与仓库根文件；`CONTRIBUTING.md` 去掉会漂移的硬编码测试数 / 工具数并新增质量门禁段；PR 模板同步检查项
- `docs/refactor_plan.md` 更新状态（原文写 P1/P2「待启动」，实际已完成）：`mcp_server.py` 882 → 49 行、`generate_report` 抽入 `core/reporting.py`、数据访问层补全完成、大函数拆分完成；`scripts/` 瘦身仍归 P3，并新增「静态质量门禁」章节

## [1.2.0] - 2026-09-11

### 新增

- **域软加权 `domain_boost`**（`mem_search` / `mem_hybrid_search` 新增可选参数）：命中节点的域与检索域一致时按加性权重提升排序，跨域候选仍保留兜底；域判定正确时与硬过滤等效，判定错误时只降权不过滤。118 题 / 同库副本 / 同候选口径 A/B：R@5 **+6.78pp**、R@1 **+9.32pp**、MRR **+8.90pp**，逐题零损失
- **`reindex` 全库向量重嵌入**（`scripts/reindex.py` + CLI `reindex` 子命令）：换 embedding 模型后一键重建所有节点向量。支持 `--check` 体检（provider / 模型 / 实测维度 / 库实际维度 / 节点分布只读报告）、`--dry-run` 预览、`--only` / `--skip` 按 payload.type 过滤、`--resume` 断点续跑 / `--restart` 从头重嵌；状态文件跟随所操作的库（`<库目录>/reindex_state_<库文件名>.json`），不污染项目 `data/`。
  - **维度红线**：预检比对「实测 provider 维度 vs **库的实际维度**」，不一致时一个字节都不写、退出码 `2`、打印换库指引（导出 → 重建 → 切换配置）
  - **退出码**：`2` 维度不匹配 · `3` 库被占用（提示先停 REST :8090 / MCP）· `4` embedding 服务不可用且未写入任何向量 · `1` 中途失败（已写部分）
  - 结束报告四类计数（重嵌 / 跳过（缺 content）/ 修复空向量 / 失败）+ 抽样节点重嵌前后向量对比；重嵌文本严格取 `payload["content"]`（与入库口径一致），不拼接元数据
- **`doctor` 部署体检命令**（`core/doctor.py` + CLI `doctor` 子命令）：复用 `startup-check` 的 5 项检查 + 向量维度一致性检查，共 6 项；每项失败给出可直接复制的修复命令，降低部署门槛
- **离线检索质量评测框架**（`eval/`）：题集抽样（自动过滤机器生成节点）+ 4 种检索模式基线 + 报告落盘，让检索改动的收益/回归可量化（此前只有主观感受）
- **`scripts/retrieval_probe.py`**：内置已验证探针的检索基线工具，用于跨版本 / 跨 embedding 模型对比
- **embedding provider 自动探测**：云 / 本地两条部署路径自动识别，配套 quickstart 文档

### 修复

- **语义主序与图扩散解耦**（`core/trivium_store.search_similar`）：图扩散结果不再直接参与主排序，仅在语义分数相同或缺失时兜底，消除图跳数对语义排序的覆盖。真实库 A/B：`mem_search` R@5 **0.4831 → 0.7458**
- **检索候选过滤 outdated 节点**：被新版取代的旧事实不再进入候选集
- **Hermes 插件不再灌入原始工具转录与重复会话要点**（`hermes-plugin`）：此前 agent 对话被整段写入记忆库，污染检索池
- **`startup-check` 自动创建数据目录**：数据目录缺失时不再直接判失败，行为与首次部署一致
- **FTS 长查询改写**（整句 trigram → 3-gram OR）：长自然语言查询（>8 字符）改写为 3-gram 均匀采样 OR 查询，实测 recall@10 从 0.0000 提升至 0.5116；端到端 rrf recall@5 从 0.3333 提升至 0.4884，融合通道从「负资产」变回「正贡献」。短查询（≤8 字符）行为不变

### 移除

- **L1 嗅探通道**（`memory_file_hits`）——零消费方，MEMORY.md 每轮已注入上下文，属重复开销

### 依赖

- **triviumdb 0.8.7 → 0.8.8**：上游修复我们上报的 #54 的一半——**有序索引路径**的 `FIND ... LIMIT` 早停恢复并反超 0.8.6（同机同库 / 50k 节点：0.8.8 78.3k/s vs 0.8.6 66.7k/s）。存储格式不变（WAL v3 / payload v9），**零迁移**；升级后真实库（880 节点）`mem_search` 冒烟通过。
  - **仍存在的回归（本版复测确认）**：**无索引**路径的 `FIND ... LIMIT` 仍比 0.8.6 慢约 25-28x（1.8k/s vs 50k/s）。根因已定位——不是早停丢失，而是「短路能力」丢失：0.8.7 起 planner 把「无可用索引 + LIMIT → 惰性 `FullNodeScan`」的短路判定从 `plan_filter` 开头挪到其之后，兜底路径会先 `all_node_ids()` 物化全库 NodeId（~200KB）再丢弃，每次查询白付约 0.58ms 固定成本；另有 3 处 `EXPLAIN` 输出与实际执行不同源、且无条件宣称优化生效的问题。
  - 上层记忆服务检索走 `search()` / payload_filter 路径，**不在受影响范围**。
  - 已同步上游：issue [#58](https://github.com/YoKONCy/TriviumDB/issues/58) + PR [#59](https://github.com/YoKONCy/TriviumDB/pull/59)（短路判定前置 + 物化计数器 + 回归测试）。

- **triviumdb 0.8.6 → 0.8.7**：上游修复我们上报的 #49（v9 存储 bulk payload 读路径比 0.8.5 慢 80-437x）。存储格式不变（WAL v3 / payload v9），**零迁移**，无需导出重建。同机同库 A/B（50k 节点 / 1024 维，0.8.6 建的库逐字节复制后两版各读）：

  | 场景 | 0.8.6 | 0.8.7 | |
  |---|---|---|---|
  | FIND 复合零命中（无索引，全扫确认） | 3.370s | 0.0142s | ~237x |
  | FIND 复合零命中（composite 索引） | 3.813s | ~0s | 索引快速缺失判定恢复 |
  | MATCH WHERE + COUNT 全扫 | 4.897s | 0.0641s | ~76x |
  | MATCH RETURN n（2000 行 payload 读） | 0.445s | 0.0132s | ~34x |
  | FIND 复合真命中（索引） | 0.0013s | ~0s | 持平 |

  图/算法侧同步恢复：pagerank TQL 3.995s → 0.212s（~19x）、leiden 1.049s → 0.885s、search_advanced 认知管线 31.8/s → 120.0/s（~3.8x）、老 API expand3 8.3/s → 24.5/s（~3x）。0.8.7 附带能力：TQL 单跳边变量一等投影（`MATCH (a)-[r]->(b) RETURN r`）、服务端图探索 API、投影列元数据。

- 已知遗留（0.8.7 新发现，非本项目主路径）：无索引 / 位图路径的 `FIND ... LIMIT` 早停比 0.8.6 慢约 20x（50k 节点等值 LIMIT 10：51k/s → 2.35k/s），系 PR #50「修 composite 零命中 + 去重复 payload 读」移除 planner 的 limit 早返回所致。上层记忆服务检索走 `search()`/payload_filter 路径，不受影响；同库 A/B 探针为本地一次性脚本（未入库）。→ 0.8.8 只修了有序索引路径，无索引路径仍未修（见上）。

- **triviumdb 0.8.5 → 0.8.6**：上游大版本（tiered payloads + composable analytics + 服务端加固 + 发布治理）。存储格式 v7 → v9（payload 迁至 generation-scoped mmap sidecar `.pld.<gen>`，flush marker v3），打开旧库自动兼容、flush/close 时自动升级（MINIMUM_SUPPORTED_VERSION 仍为 5，早于 0.7.0 的文件需手动迁移）。本地零手工迁移：备份 `data/backup_20260906` → 副本冒烟（v7 打开/flush 升 v9/重开验证）→ 真实库由服务打开自动升级。我们提的 #39（SEARCH VECTOR 科学计数法解析）与 #40（FIND 范围/复合谓词慢）上游已标 solved 并验证：科学计数法 30/30 全过；复合谓词同库 A/B 2.0ms→0.6ms（约 3.3x）。测试断言随格式更新（storage_info database_format_current 7→9）

### 文档

- **检索质量评测资料**（`eval/docs/`）：6 篇探针结论文档，含 4 条否证结论及口径判据（更换 embedding、索引期 small-to-big 分块、无上下文查询改写、自动域推断）——「否证也留档」省未来弯路

### 测试

- 全量 **316 passed + 2 xfailed + 2 xpassed**（依赖 0.8.5→0.8.8 连续三次升级后无新增失败；版本断言与存储格式断言随依赖更新：`package_version` 0.8.5→0.8.8、`database_format_current` 7→9）

## [1.1.1] - 2026-09-05

### 安全

- **可选 API Key 鉴权**：默认不启用（localhost 本机直连，保持原行为）；设置 `PALIMPSEST_API_KEY` 后，除 `/` 健康检查外所有请求须带 `Authorization: Bearer YOUR_API_KEY` 或 `X-API-Key: YOUR_API_KEY`，否则 401（`secrets.compare_digest` 防时序攻击）。适用于局域网/受信网络部署；公网部署应配 HTTPS 反向代理
- **`/export` 分页**：不再一次返回全部记忆；默认每页 100 条（上限 500），返回 `page` / `page_size` / `total_pages`
- **`GET /memory/{id}` 剥离内部字段**：`secret_hint` / `linked_from` / `linked_kb_ids` / `superseded` 不再随 payload 返回
- **报错不再泄漏内部异常**：DELETE/PUT/PATCH/向量端点与 embedding 错误统一固定提示语，`str(exc)` 细节只进日志

### 修复

- **outdated 检索语义**：被新版本取代的旧记忆（`status=outdated`）不再参与普通检索——`mem_search` / `mem_retrieve` / `mem_hybrid_search`（RRF 与级联）默认只回当前有效节点，图谱邻居区同样过滤；旧版保留库中可追溯，显式 `include_outdated=True` 或 `mem_version_history` / REVISED_BY 边可查历史
- **`/export` 排序容错**：脏 `importance`（非数值）不再触发排序类型错误（`_to_float` 兜底）

### 测试

- 新增安全加固回归（鉴权双态 / 分页 / 内部字段剥离 / 固定报错）与 outdated 语义测试（同内容两次写入触发 REVISED_BY → 默认只见新版、显式通道两版可见）；全量 **161 passed + 2 xfailed + 2 xpassed**（无 Ollama 环境同样全绿）

## [1.1.0] - 2026-09-05

### 依赖

- **triviumdb 0.8.3 → 0.8.5**：上游修复 pagerank panic（#31）与 5000 行硬截断（#32），MATCH 现可全量返回（含 LIMIT pushdown）；TQL 聚合/标量 RETURN 扁平化（COUNT/SUM/AVG/COLLECT 别名直映射值，不再嵌套 payload）。本地零迁移升级（存储格式 v7 兼容）。`mem_recent` 空 domain 路径随之统一走 TQL（不再绕道 iter_payloads），排序仍留 Python 保持 `(created_at, id)` 双键倒序语义

### 新增

- **`mem_communities` 社区发现工具**（leiden 聚类）：按 `min_community_size` 过滤、按规模降序截断 `top_k`；提供 MCP 工具 + `POST /graph/communities` REST 端点；配套测试

### 修复

- **字符串布尔反转**：Hermes 插件 `include_neighbors` / `bidirectional` 参数曾被 `bool("false") == True` 反转成相反语义，现改为字符串比较判定
- **`mem_ingest` 输入护栏**：空内容与超长内容（默认上限 50,000 字符，可配置）在嵌入/扫描前拒绝写入，REST 返回 422 + 友好提示（防超大 payload 拖垮库）
- **TQL domain 注入面**：`mem_recent` domain 白名单校验（仅 `[a-z0-9_-]`），非法输入安全降级为全遍历过滤，不崩溃、不注入
- **CLI 全文泄漏**：`fts-search` 不再打印全文 content，只输出 node_id + 截断摘要；带 secret 标记的节点隐藏内容
- **`float()` 裸转统一为防御性 `_to_float`**：memory / graph / kb 多处 score/weight 读取，脏 payload 不再 TypeError
- **图遍历与建边**：BFS 改 `deque`（消除 `list.pop(0)` O(n)）；`mem_link` 主边与返回统一大写 relation（防同关系两种 label）
- **合并去重**：consolidator 同批次重复合并（如 (A,B)+(A,C) 把 A 标脏两次）用 `seen_ids` 拦截
- **FTS 同步可感知**：`sync_node` 返回 bool（成功/失败），失败仍 warning 不抛、不破坏调用契约
- **CLI 健壮性**：顶层 try/except 友好报错 + 失败退出码非 0；`cmd_ingest` 删除重复的 FTS 索引写入（`mem_ingest` 内部已同步）
- **压缩链路护栏**：Hermes 插件图谱增强加总耗时预算（默认 8s，可配置），后端不可达时整体 fail-open，不再白等

### 工程化

- 启动自检、防御惯例、输入校验等按审计建议收敛；`MEM_INGEST_MAX_LENGTH` / `PALIMPSEST_GRAPH_TIMEOUT` 新增环境变量配置（魔法数字配置化）

### 测试

- 套件含 0.8.5 适配断言（聚合扁平化、MATCH 全量）、`mem_communities` 测试、`mem_recent` 行为锁定回归；全量 **143 passed + 2 xfailed + 2 xpassed**（无 Ollama 环境同样全绿）

## [1.0.1] - 2026-08-31

### 修复

- **`PUT /memory/{id}` 数据丢失**：原实现为整包替换，部分更新会把 `content` / `type` / `importance` / `domain` 等字段清空（内部生产事故路径）。现改为**合并语义**（只更新传入字段，其余保留）；新增 `PATCH /memory/{id}` 端点（REST 部分更新语义）；更新后自动同步 FTS 全文索引，杜绝幽灵命中
- **Embedding 静默降级**：embedding 服务不可用时原实现静默返回全零向量（检索排序被污染且无告警）。现改为 **fail-fast**——抛出 `EmbeddingUnavailableError`，REST 层返回 503 并附修复指引；新增 `OLLAMA_EMBEDDING_BASE_URL` 配置项（与 LLM 的 `OLLAMA_BASE_URL` 解耦），`startup-check` 同步使用该配置
- **FTS 内容漂移巡检**：`check_fts_consistency` 从「只比对节点 id」升级为**内容级对账**（逐节点比对 content），发现并修复了存量漂移；新增 `sync_node` 统一 FTS 同步入口（PUT/PATCH/DELETE 复用）；空内容节点不再误报缺失
- **并发与边界加固**：`mem_ingest` 节点 id 分配加进程内锁（防并发同 id 覆盖）；`mem_review` 对脏 payload（非数值 importance）安全兜底；FTS 查询含双引号时走 LIKE 兜底（不再被 FTS5 语法吞掉）；知识库根环境变量统一 `KNOWLEDGE_DIR`（兼容回退 `KNOWLEDGE_ROOT`）；KB 索引增量 upsert 先写向量后写 mtime（中途崩溃下次增量可自愈）

### 新增

- `GET /memory/{id}` 端点：读取单节点完整 payload（REST 读能力补齐）
- 确定性 fake embedder 注入测试基建：测试套件不再依赖在线 Ollama，CI 不再在每台 runner 安装 Ollama / 拉取模型

### 工程化

- 清理 7 处冗余 `os.chdir`（配置已绝对路径化）；删除 `--db-path` 死选项；代码注释/文档中的个人化术语清零

### 测试

- 失败路径测试：PUT/PATCH 部分更新保留字段、缺失节点报错、embedding 失败抛错、GET 端点、含引号查询、脏 payload、内容漂移对账；套件由 51 条增至 **61 条**（无 Ollama 环境同样全绿）

## [1.0.0] - 2026-08-29

第一个正式开源版本。此前内部迭代版本（v0.x / v1.x / v2.x）不对外发布，1.0 起为对外稳定基线。

### 核心能力

- **混合检索**：语义向量（cosine）+ FTS5 全文索引（trigram，中文子串匹配），RRF（k=60）或级联两种融合模式，命中来源 `fts_hit` / `sem_hit` 透明标注
- **知识图谱召回**：节点间有向加权边（`RELATED_TO` / `REVISED_BY`），BFS 沿边扩散；弱边过滤、分区块隔离、每节点扩散条数上限
- **冲突检测与版本链**：写入时与相似旧记忆比对，被覆盖记录标记 `outdated` 并通过 `REVISED_BY` 链向新版；多层防误标（阈值/type 隔离/domain 隔离）
- **写入前敏感扫描**：强规则（API Key / token / 私钥 / Bearer 等 8 条）拒绝入库；弱规则（身份证 / 手机号 2 条）放行并打 `secret_hint` 标记
- **容量自动合并**：`mem_consolidate` 相似记忆 dry-run 预览 / apply 合并（高价值保护、`REVISED_BY` 保留）
- **任务自动归档**：完成任务写 markdown 归档到知识库 `05_任务归档/` 后删除节点
- **记忆生命周期**：时间衰减加权（`MEMORY_DECAY_FACTOR`），陈旧记忆检索降权
- **150 字摘要设计**：检索默认只返回摘要 + 元数据，全文按需拉取（省 token）
- **三接口一核心**：MCP（stdio）/ REST（:8090）/ CLI（15 子命令），共用同一套 `mcp_tools` 实现

### 架构与工程化

- **分层**：`core/`（存储与算法）→ `mcp_tools/`（MCP 工具层）→ `main.py` / `mcp_server.py`（入口）
- **单连接遍历**：`iter_payloads` / `iter_nodes` 一次数据库连接完成遍历（原 N+1 次开关，200 节点 3.68s → 0.022s，约 165x）
- **事务化写入**：`mem_ingest` 与 `consolidate` 均在单事务内原子提交/回滚，杜绝半状态
- **启动自检**：`startup-check` 5 项（关键文件 / 存储 / FTS / 依赖 / Embedding 服务）
- **依赖锁定**：requirements.txt 全版本锁定；`mcp==1.29.0`（2.x 移除顶层 FastMCP）
- **可安装**：pyproject.toml，`pip install -e .` 后 `palimpsest-cli` 命令可用
- **CI**：GitHub Actions，Python 3.10/3.11/3.12 矩阵，自动装 Ollama + embedding 模型 + pytest
- **测试**：51 条（冒烟 16 + 核心算法单测 31 + 事务 4），临时库隔离不碰正式库
- **数据一致性**：FTS 索引失败显式记录；`check_fts_consistency.py` 巡检（`--repair` 全量重建）
- **路径安全**：归档文件名防路径遍历（`..` 前缀兜底）

### 配置与使用

- **配置化常量**：图谱扩散、RRF、L1 嗅探等阈值全部可经环境变量调整
- **区块（Blocks）**：出厂内置 `task` / `kb`（含 `rule`）/ `hermes` / `general`；节点归属统一 `payload.domain` 字段（`character_name` 退役）
- **双语 README**：中文主版 + 英文版，含语言切换、CI 徽章
- **友好引导**：依赖缺失时输出中英双语安装指引（非 traceback）；未知区块提示（不拦截自定义 domain）

### 修复

- 消除全局 `os.chdir` 副作用（DB_PATH 绝对化，import 不再改进程工作目录）
- 消除 L1 魔法 ID（`-1`）——记忆文件命中改为独立字段 `memory_file_hits`
- 清理代码注释中的任务编号与个人化术语
- TriviumDB 0.8.2 升级（WAL v2→v3 迁移，导出→重建→验证→换配套→FTS 重建）

## 更早版本

更早版本（v0.x / v1.x / v2.x）为内部迭代版本，未对外发布，不在此记录。

[Unreleased]: https://github.com/JiaY-77/Palimpsest/compare/v2.6.0...HEAD
[2.6.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.6.0
[2.5.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.5.0
[2.4.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.4.1
[2.4.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.4.0
[2.3.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.3.0
[2.2.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.2.0
[2.1.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.1.0
[2.0.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v2.0.0
[1.2.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.2.0
[1.1.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.1.1
[1.1.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.1.0
[1.0.1]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.0.1
[1.0.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.0.0
