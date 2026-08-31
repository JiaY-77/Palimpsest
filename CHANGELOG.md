# Changelog

本项目遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)（Semantic Versioning）。

版本格式：`主版本.次版本.修订号`。发布流程见 [RELEASING.md](docs/RELEASING.md)。

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
