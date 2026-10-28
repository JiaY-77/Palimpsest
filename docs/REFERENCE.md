# 命令参考（Reference）

> 本文是**查的时候才看**的参考表：MCP 工具、CLI 子命令、REST 路由、区块语义、索引规则。
> 第一次上手请看 [README 的 Install](../README.md)；日常怎么用见 [USAGE.md](USAGE.md)。

---

## 自定义索引规则

`build_novel_index.py` 与 `build_kb_index.py` 的扫描范围、节点 kind 分类、分块策略、payload domain 均来自一套**声明式 JSON 规则**（默认内置，零配置即用），可用 `--rules` 显式指定：

```bash
python scripts/build_novel_index.py --source <vault路径> --rules rules.json
python scripts/build_kb_index.py --rules rules.json
```

**加载优先级**：`--rules <path>`（路径不存在会报错）> `<vault 根>/<legacy 文件名>`（novel 脚本兼容旧约定 `.palimpsest-novel-index.json`）> `<vault 根>/.palimpsest-index.json` > 内置默认；旧格式 `.palimpsest-index.yaml` **不会解析**，仅在警告里提示改用 `.json`。顶层键为 `kind_map` / `default_kind` / `chunk_strategy` / `min_chunk_len` / `max_chunk_len` / `domain` / `include` / `exclude` / `require_frontmatter_id`，未知键只警告、非法取值报错。`kind_map` 每条支持 `dir_prefix` / `filename` / `glob`（同条可组合，多个字段 = AND），按声明顺序先匹配者胜，全不命中 → `default_kind`（默认 `"default"`，不静默归并，未命中路径出现在 `unmatched_paths`）。完整示例见 [`examples/index-rules.example.json`](../examples/index-rules.example.json)。

## 工具与接口

> 本节是**命令参考表**。日常用法（什么时候该用哪个工具、参数怎么定、输出怎么读）见 **[`docs/USAGE.md`](USAGE.md)**。

### MCP 工具（21 个）— `mcp_tools/*`

| 工具 | 说明 |
|---|---|
| `mem_search` | 统一检索：记忆 / 知识库 / 两者；可选图谱邻居扩展、域偏置、**域软加权 `domain_boost`**、块级隔离、**记忆分层 `tier`**（默认 `facts` 只回事实层，不含 `record`/`event`/`git_commit`；`""` = 不过滤） |
| `mem_hybrid_search` | 混合检索：FTS5 + 向量；`mode=rrf`（k=60）或 `cascade`；同样支持 `domain_boost` 域软加权与 `tier` 分层；命中标注 `fts_hit` / `sem_hit` |
| `mem_retrieve` | 语义检索，返回 150 字摘要 + 元数据（绝不返回全文） |
| `mem_get_full` | 按 ID 拉取节点完整内容 |
| `mem_ingest` | 写入新记忆——含冲突检测（取代默认**由写入方显式声明**：`replaces="<id>,<id>"` 标旧节点 `outdated` 并建 `REVISED_BY` 修订链）、敏感扫描、长度护栏 |
| `mem_recent` | 最近的记忆（新的在前） |
| `tasks_active` | 活跃任务列表（`type=task` & `status=active`）：按 `project` / 状态集过滤，`doing > blocked > todo` → 最近触碰倒序排序；默认排除 `legacy=true` 的老节点 |
| `mem_review` | 最近 N 天的周期性回顾 + 治理候选（高价值升级 / outdated 清理 / 低价值）；`tier`（默认 `facts`）作用于 `recent_ingests`：`logs` 只回日志层、`""` 不过滤 |
| `mem_stats` | 库级盘点：类型 / 域 / 重要度 / 时间 / 图谱分布 + 热点节点；`tiers` 分节按检索侧 tier 语义分组（facts / logs / unclassified）并输出实际生效的 `TIER_FACTS` / `TIER_LOGS` 清单；`write_judgment` 分节汇总近 30 天写入判定观测（观测数据不进记忆库、不含记忆正文） |
| `mem_version_history` | 沿 `REVISED_BY` 链展开，查看事实演化过程 |
| `mem_fact_history` | **单条事实的时间线**：返回其 bi-temporal 时间字段（`valid_at` / `invalid_at` / `expired_at` / `created_at`）与取代关系（它取代了谁 / 谁取代了它） |
| `mem_consolidate` | 近似重复检测；dry-run 预览或 apply 合并 |
| `mem_communities` | Leiden 社区发现：把记忆库聚成主题簇，回答「有哪些圈子」 |
| `kb_index` | 将知识库 `.md` 文件索引为 `kb_chunk` 节点（向量化） |
| `kb_search` | 对已索引知识切片的语义搜索 |
| `skill_search` | 技能语义检索：检索 Hermes 技能（`skill_chunk` 节点），返回 name / description / category / source_path |
| `graph_neighbors` | 从某节点出发对知识图谱做 BFS（关系过滤、深度 1–3、弱边过滤） |
| `mem_link` | 手动创建图边（`RELATED_TO` / `CAUSES` / `REFERS_TO`；默认双向） |
| `mem_unlink` | 删除图边（幂等：边不存在返回 `deleted=false`；用于清理误标产生的 `REVISED_BY` 边） |
| `mem_unpack` | 只读解开 MemoryHub 记忆包：校验格式 / 版本、扫描敏感信息、给人读摘要；不写入任何东西 |
| `mem_import` | 把记忆包里的节点「入籍」（写回本库）；默认只读预览，`select` 显式选中才写，`include_secrets=True` 才放行强敏感节点 |

> 记忆包是什么、三步用法（解开看 → 挑着选 → 入籍）、格式定义与敏感扫描规则，见 [`docs/MEMORYHUB.md`](MEMORYHUB.md)；可直接跑的示例包在 [`docs/examples/memoryhub-sample.json`](examples/memoryhub-sample.json)。

### CLI 命令 — `scripts/palimpsest_cli.py`

| 命令 | 说明 |
|---|---|
| `search "QUERY"` | 统一检索（`--scope all\|memory\|kb`、`--neighbors`、`--block`） |
| `hybrid-search "QUERY"` | FTS5 + 向量混合检索（`--mode rrf\|cascade`） |
| `ingest "CONTENT"` | 写入新记忆（`--importance 0.5`、`--type memory`、`--domain`） |
| `link --source N --target N` | 创建图边（`--relation`、`--one-way`） |
| `index` | 扫描并索引知识库 |
| `graph --id N` | 某节点的图谱邻居（`--depth`、`--relation`、`--min-weight`） |
| `recent` | 最近的记忆（`--limit`、`--domain`） |
| `tasks` | 任务节点状态注册表（子命令 `active` / `backfill`） |
| `memoryhub` | MemoryHub 记忆包（子命令 `read` / `import`） |
| `memoryhub read PATH` | 只读解开记忆包（校验 + 人读摘要，`--preview-chars`） |
| `memoryhub import PATH` | 把包内节点入籍（`--select 12,15,31`；为空 = 只预览不写；`--include-secrets` 放行强敏感节点） |
| `tasks active` | 活跃任务列表（`--project`、`--states todo,doing,blocked`、`--limit`、`--include-handoff` 列出会话接续点——接续点由写入层自动打 `kind=handoff`，显式列出时不受状态过滤） |
| `tasks backfill` | 存量任务节点回填 `task_state`（默认 dry-run 预览，`--apply` 才写库） |
| `review` | 最近 N 天的周期回顾（`--tier facts\|logs\|''` 作用于 recent_ingests） |
| `stats` | 库级盘点统计（totals/域/重要度/时间/图谱） |
| `kb "QUERY"` | 知识切片的语义搜索 |
| `consolidate` | 合并预览；`--apply` 执行合并（`--threshold 0.85`、`--max-importance 0.8`） |
| `promote` | 高频记忆升级候选；`--apply` 升权打标（`--days`、`--min-hits`） |
| `ingest-git` | 将近期 git 提交索引为 `git_commit` 节点（幂等） |
| `fts-rebuild` | 重建完整 FTS5 索引 |
| `fts-search "QUERY"` | 原始 FTS5 搜索（trigram 子串） |
| `doctor` | 部署体检：关键文件 / 存储 / FTS / 依赖 / Embedding / 向量维度一致性 / **向量空间指纹** / 运行时路径字符集，失败项给出修复命令（`--json` 机器可读） |
| `startup-check` | 运行启动自检（`doctor` 的轻量子集，失败时退出码 1） |
| `task-archive` | 归档已完成任务；`--apply` 写入 markdown 并删除节点 |
| `reindex` | 全库向量重嵌入（换 embedding 模型 / provider 后使用；`--check` 体检、`--dry-run` 预览、`--force` 接受当前向量空间并全量重算并写指纹） |

### 区块（Blocks）

`block` 是「域分组」概念：图谱按区块隔离，扩散检索只沿同区块的边，防止跨域污染。内置 `task` / `kb` / `hermes` / `novel` / `general`，也可把自己的 `domain` 当作区块（如 `--block myproject`），留空按全量模式检索。节点归属统一由 `payload.domain` 表达：写入时通过 `--domain X` 或 `mem_ingest(domain=...)` 指定，`kb` 节点由索引自动设置。

**域取舍口径**：`domain` 未指定（空 / 缺省）的节点被归一为隐式 `general`——所有按域过滤与检索（`mem_search`、`mem_recent`、区块过滤）都以这个归一后的口径为准，因此**不带 domain 写入的节点在 `domain="general"` 查询下可见**；`mem_recent(domain="general")` 走载荷归一口径逐年扫描，其余 domain 走索引快路径，两者结论一致。lifecycle 通道的思考底片经 `POST /lifecycle/post-turn` 的可选 `domain` 字段落各自的域（不传仍为隐式 `general`）。

### REST API — `main.py`，端口 8090

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 服务信息 + 版本 + 端点索引 |
| `POST` | `/mem/search` | 统一检索 |
| `POST` | `/mem/ingest` | 写入新记忆（含冲突检测 + 敏感扫描；`replaces` 字段显式声明取代；可选 `meta` 元数据字典见 docs/API.md） |
| `POST` | `/mem/purge-by-meta` | 按「域名 + 自定义元数据」批量删除（`domain` 必填非空；`dry_run` 默认 true；超出 `max_delete` 409 拒绝） |
| `POST` | `/memoryhub/unpack` | 只读解开 MemoryHub 记忆包（校验 + 人读摘要） |
| `POST` | `/memoryhub/import` | 把记忆包里的节点入籍（`select` 为空 = 只预览不写；`include_secrets` 放行强敏感节点） |
| `POST` | `/graph/neighbors` | 某节点的图谱邻居 |
| `POST` | `/lifecycle/pre-turn` | **记忆策略 + 思维预设层**：每轮模型调用前决定召回哪些记忆，返回可注入 prompt 的文本 + 按情境动态挑的思考骨架（`thought_skeleton`） |
| `POST` | `/lifecycle/recover-orphan-capsules` | **断线自愈**：进程被硬砍（关机 / 崩溃）没跑 session-end 时，把悬空的活跃会话缓冲补产成胶囊；`dry_run=true` 只报告不写库 |

> 完整路由清单见 [`docs/API.md`](API.md)。若设置了 `PALIMPSEST_API_KEY`，除 `/` 外所有端点要求 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。
