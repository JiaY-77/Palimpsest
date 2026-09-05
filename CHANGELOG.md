# Changelog

本项目遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)（Semantic Versioning）。

版本格式：`主版本.次版本.修订号`。发布流程见 [RELEASING.md](docs/RELEASING.md)。

## [1.1.1] - 2026-09-05

### 安全

- **可选 API Key 鉴权**：默认不启用（localhost 本机直连，保持原行为）；设置 `PALIMPSEST_API_KEY` 后，除 `/` 健康检查外所有请求须带 `Authorization: Bearer ***` 或 `X-API-Key: ***`，否则 401（`secrets.compare_digest` 防时序攻击）。适用于局域网/受信网络部署；公网部署应配 HTTPS 反向代理
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

[Unreleased]: https://github.com/JiaY-77/Palimpsest/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/JiaY-77/Palimpsest/releases/tag/v1.0.0
