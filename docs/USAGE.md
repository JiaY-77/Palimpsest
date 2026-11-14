# 使用指南（Usage）

> 这份文档回答「**装好之后怎么用**」：什么时候用哪个工具、参数怎么定、输出怎么读。
> 命令的完整清单在 [README](../README.md) 的「使用」一节——那是**参考表**，这里是**决策手册**。

装还没装好？先看 [README 的 Install](../README.md#install)。

---

## 0. 选入口（决定一次就够）

| 你是什么 | 用哪个 | 为什么 |
|---|---|---|
| MCP 客户端（各类 agent 框架） | MCP `http://127.0.0.1:8090/mcp/` | 与 REST 同进程，工具直接可见 |
| Hermes | `hermes-plugin/` 双插件 | 自动召回 + 会话沉淀，一行命令激活 |
| 写代码 / 写脚本 | REST `:8090` | 参数最全（CLI 有裁剪，见 §2.4） |
| 手动操作 / 排查 | CLI `python scripts/palimpsest_cli.py` | 人可读，方便验证 |

**REST 是唯一写者。** CLI / dashboard / 各脚本都经它；stdio `mcp_server.py` 只在不跑 REST 的纯 MCP 场景用（它会以独占写模式直接打开库文件）。

---

## 1. 一条记忆的生命周期

```
写入 ──► 被检索命中 ──► hit_count 增长 ──► promote 升权
  │                                              │
  └─► 被新版本取代（replaces）──► outdated        └─► 仍是 active
                │
                └─► REVISED_BY 链可回溯
```

| 阶段 | 动作 | 命令 |
|---|---|---|
| 写入 | 记一条 | MCP `mem_ingest` / `POST /mem/ingest` |
| 命中 | 检索时自动累加 | 任何检索入口 |
| 升权 | 反复用到的记忆加权 | `palimpsest_cli.py promote --apply` |
| 取代 | 新值替换旧值 | `mem_ingest(replaces="<id>")` |
| 合并 | 近似重复合并 | `palimpsest_cli.py consolidate --apply` |
| 归档 | 完成任务写回 md | `palimpsest_cli.py task-archive --apply` |

---

## 2. 判断手册（这一节比命令重要）

### 2.0 先分载体：文件、记忆，还是都不写

写之前先问一句「这条内容有没有文件归宿」。同一件事写错地方，两边都会变糟：文件是**可 diff、随代码一起演进**的真相；记忆是**没有文件归宿、跨会话仍然成立**的真相。

| 内容 | 写到哪 | 判据 |
|---|---|---|
| 接口约定、目录结构、SOP、验收口径、配置项 | **仓库 / 知识库文件** | 能 diff、能评审，会随代码一起改 |
| 决策理由、被否掉的方案、被纠正的旧结论、用户偏好 | **记忆节点** | 没有文件能承载，且换个会话仍然成立 |
| 一次性产物、原始日志、中间状态 | **都不写**（产物落工作区，日志落审计） | 写下去就过期，过期就误导 |

三组正反例：

| 内容 | 写对 | 写错会怎样 |
|---|---|---|
| 「REST 端口是 8090」 | `docs/CONFIGURATION.md` / README | 写进记忆 → 换端口后记忆仍是旧值；而改端口的人根本不会去看记忆 |
| 「为什么选 chunk 级向量索引，而不是把长文档拆成多个节点」 | 记忆（决策理由，`type=decision` 或方案节点） | 只留在 PR 描述里 → 三个月后没人翻得到。反向错误：把方案正文抄进记忆 → 方案改了三版，记忆里是第一版，读的人按旧方案动手 |
| 「正式产出放知识库、临时脚本放工作区」 | 记忆（用户偏好，`type=memory`） | 写进仓库的 `CONTRIBUTING.md` → 那是这个仓库的规范，不是这个用户的规定，换个项目就失效 |

写错的代价是双向的：**库越积越重**（过期的配置和口径堆在候选池里互相稀释，真正有效的决策被挤出 top-k），**文件越来越轻**（本该写进 README 的约定散落在记忆里，新来的人找不到）。

### 2.1 该不该记

| 记 | 不记 |
|---|---|
| 持久事实（端口、路径、约定、偏好） | 一次性结果（"这次跑通了"） |
| 决策 + 理由 + 被否掉的替代方案 | 一条命令就能重新查到的（版本号、文件列表） |
| 被纠正的旧结论（纠正价值最高） | 过程中的中间状态 |
| 项目 / 角色的状态推进 | 寒暄、纯问答 |

一句话判据：**三个月后还会不会用到？** 不会就别写。

### 2.2 `importance` 怎么给

| 值 | 用在哪 |
|---|---|
| 0.85–1.0 | 被纠正的规则、硬约束、安全边界 |
| 0.8–0.85 | 重要决策（含理由）、里程碑 |
| 0.6–0.8 | 项目推进、方案定型 |
| 0.5 | 普通观察、一般事实 |
| 0.4 及以下 | 挂账、待议、低置信 |

**分级权在写入方。** 不要交给模型默认值蒙。

### 2.3 `type` 怎么选

| type | 用在哪 | 默认检索可见 |
|---|---|---|
| `memory` | 事实、偏好、结论 | ✅ |
| `correction` | 被纠正的旧结论 | ✅ |
| `decision` | 决策 + 理由 + 替代方案 | ✅ |
| `plan` | 方案、路线图 | ✅ |
| `task` | 任务节点（见 §5） | ✅ |
| `review` | 复盘 | ✅ |
| `thought` | 推理过程（思考记录） | ❌ 日志层 |
| `record` | 操作留痕、状态变更日志 | ❌ 日志层 |
| `event` | 发生过的动作 | ❌ 日志层 |
| `git_commit` | 提交留痕 | ❌ 日志层 |

> ⚠️ **这是最容易踩的坑**：`record` / `event` / `thought` 属日志层，**默认检索不到**。想以后能查回来的东西，必须用 `memory` / `decision` / `plan` / `task`。
>
> 需要翻日志层时显式指定：`mem_search(tier="logs")` 或 CLI `search --tier logs`。

### 2.3.1 对话残渣降权（digest，issue #278）

session-end 的「会话要点」与 post-turn 的「[对话沉淀] 用户原话」是每次会话固定写入的对话残渣：语义极泛 + 数量随会话线性增长，在候选窗口里天然占位，把结论型记忆挤出去。治理是**降级可达性，不删历史节点**：

| 手段 | 行为 |
|---|---|
| 写入打标 | 两条 digest 写入路径自动带 `meta.origin`（`session_digest` / `dialogue_digest`）；历史节点用 `scripts/migrate_digest_origin.py` 按 content 前缀补标（幂等，只补字段） |
| 检索过滤 | `mem_search` / `mem_hybrid_search` / `mem_retrieve` 默认不返回 digest 类节点（`meta.origin` 打标或历史 content 前缀命中都算，不依赖迁移完成） |
| 审计通道 | 显式传 `include_digest=true` 才可达；`mem_stats` 新增 `digest` 分节报残留占比（active 数 / 占比 / by_origin / 存量命中 top10） |
| 停止新增 | `SESSION_MEMORY_INGEST` 默认 `0`：session-end 只走会话胶囊路径；设 `1` 回退要点路径 |
| 计数拆分 | 命中计数拆两个：`hit_count`（MCP / `/mem/search` 主动检索）与 `inject_count`（`/lifecycle/pre-turn` 注入）；`mem_stats` 的 `top_hit_nodes` 只按 `hit_count`（真实复用）排序，digest 类命中单列 digest 分节，另有 `top_inject_nodes` |

### 2.4 要不要取代：三问

写入前先 `mem_search` 查同主题（`scope="memory"`，`top_k=3~5`），然后：

| 情况 | 动作 |
|---|---|
| 全新事实 | 直接写，**不传** `replaces` |
| 旧值被新值更新 / 取代 | 写时带 `replaces="<旧 id>"` |
| 同主题补充 / 换个角度 | 直接写（相似内容自动进 `related_ids`） |
| **拿不准** | **按全新事实写** —— 宁可多一条，也不要误标 outdated |

**为什么不能图省事**：误标 `outdated` 会让旧事实从默认检索里消失；多留一条只是候选多一个。**存一次错，读数百次。**

> ⚠️ **CLI 缺口**：`palimpsest_cli.py ingest` 没有 `--replaces` 参数（只有 `--domain / --importance / --type`）。**声明取代必须走 MCP `mem_ingest(replaces=...)` 或 REST `/mem/ingest`。**

### 2.4.1 当前状态要单独成节点（状态与叙事分离）

「现在是什么」——版本号、当前模型、当前提交、当前端口、当前兜底——和「某天做了什么」是两种东西。把它们写在一起，问「现在的版本号」时最新答案**永远召不回**：

```
✅ 【状态·2026-11-14】小帕当前生产版本号：v2.22.0-18-gf6bc66a
   type=memory   importance 0.85   变更时写新节点 + replaces=<本条 id>

❌ 【完成·2026-11-14】小帕上线，接上节点 4552 的待办。
   1. 代理根因找到并修复…… 2. 迁移已执行…… 3. 服务重启验证 v2.22.0…… 4. GitHub 通道恢复……
   （版本号只是四件事之一，查询匹配到的是「代理 / 迁移 / GitHub」，不是「版本号」）
```

三条做法：

1. **叙事记录照旧写**（`type=record`，讲过程、证据、踩坑）；
2. **当前状态单独一条 `type=memory` 短事实**：答案放第一句，把用户会问的词写进正文；
3. **状态变更时 `replaces=<上一条状态节点 id>`**：旧状态自动 `outdated` + `REVISED_BY`，检索默认过滤 outdated，问「现在」天然只回最新一条。

**为什么不用「加时间 / 新鲜度权重」治**：加性分数要翻盘得 +0.1 以上，而候选之间余弦差通常只有 0.05~0.15——权重一大就污染所有查询（实测把「时间索引」做成 RRF 第五路，端到端合计 −3.4pt，默认保持关闭）。**问题在数据形态，不在排序算法。**

**实测（同一批 7 个问题，生产库 3486 节点）**：最新答案只写在长记录里时，gold 排 8~45 名（0/3 过）；按上面三条把状态写成独立节点后，**7/7 通过，gold 有 6 条排第 1**——排序代码一行未改。题集与判分器见 `eval/datasets/`。

### 2.5 写入前必查

不做这一步的典型后果：同一件事在库里长出三四个版本，下次检索谁都不知道该信哪个。

---

## 3. 场景配方

### A. 让 agent 记住你的偏好

```
mem_ingest(
  content    = "用户偏好：代码评审先讲风险，再讲改动；不要一上来夸。",
  type       = "memory",
  importance = 0.8,
  domain     = "work"
)
```

之后 `mem_search "怎么给我做代码评审"` 就能召回——**靠意思匹配，不是靠关键词**。

### B. 项目决策留痕（记住「为什么」）

```
mem_ingest(
  content = "【决策】检索融合用 RRF 不用加权求和。理由：两路分数尺度不同，加权要调参；"
            "RRF 只看排名，跨查询稳定。被否方案：cascade（实现简单但中文短查询偏弱）。",
  type       = "decision",
  importance = 0.85,
  domain     = "project-x"
)
```

三个月后问「当初为什么不用 cascade」，答得出来——这是 `decision` 和 `record` 的区别。

### C. 把 Markdown 知识库变成可语义搜索

```bash
# 1. .env 里指定知识库根目录
echo "KNOWLEDGE_DIR=D:/path/to/vault" >> .env

# 2. 首次索引（按 ## / ### 标题切片，保留 [[双链]]）
python scripts/build_kb_index.py

# 3. 之后增量：只重建 mtime 变化的文件，节点 id 不变、图谱边不丢
python scripts/build_kb_index.py

# 4. 查
python scripts/palimpsest_cli.py kb "上次那个部署方案"
```

### D. 跨会话续接（交接模式）

```
mem_ingest(
  content    = "【接续点】<主题>：本轮做了什么（含路径 / PR 号）／关键结论／下一步待办／踩的坑",
  type       = "task",
  importance = 0.9
)
```

- `type=task` 且首行以「接续点」开头 → 自动打 `kind=handoff`
  - 首行只有锚词 + session hash 的形态（补产胶囊产物）自动补可读 title：`补产胶囊 <日期> <session 前 8 位>`
- 新会话用 `tasks_active(include_handoff=true)` 取回，不会污染活跃任务列表；
  补产胶囊（孤儿缓冲自愈产物，`payload.recovered=true`）不进接续点置顶组——排在普通任务组里照常可见，手写接续点永远排在它前面

### E. 任务追踪

```
mem_ingest(content="[doing] 修 tier 分层遗漏", type="task", importance=0.8)
```

状态词写在 content 首行方括号里，后端自动解析成 `task_state`：

`[待办]/[todo]` → todo ・ `[进行中]/[doing]` → doing ・ `[挂账]/[阻塞]` → blocked ・ `[完成]` → done ・ `[取消]` → canceled

（`[部分完成]` 归 doing，不是 done。）

查询：`tasks_active`（默认只回 todo / doing / blocked，排序 doing > blocked > todo）

---

## 4. 知识库（Markdown）

| 动作 | 命令 |
|---|---|
| 扫描 + 切片 + 入库 | `python scripts/build_kb_index.py` |
| 小说 vault（整文件一节点） | `python scripts/build_novel_index.py --source <vault>` |
| 技能库（`SKILL.md`） | `python scripts/build_skill_index.py`（默认经 REST `POST /skill/reindex` 在服务进程内构建，REST 常驻时无需停服务；`--direct` 才直连库，要求库空闲；`--endpoint` / `PALIMPSEST_BASE_URL` 覆盖地址） |
| 自定义切片 / 分类规则 | `--rules rules.json`（模板见 `examples/index-rules.example.json`） |
| 语义搜索 | `palimpsest_cli.py kb "..."` 或 `mem_search(scope="kb")` |

**Markdown 的结构会被用上**：`##`/`###` 当切片边界、`[[双链]]` 转图谱边、frontmatter 当元数据、`⛔ 已退役` 当生命周期（不索引 + 清旧块）、`mtime` 用来增量重建。

完整规则说明见 [README 的「自定义索引规则」](../README.md#自定义索引规则) 与 [`docs/OBSIDIAN.md`](OBSIDIAN.md)。

---

## 5. 任务节点

```
建     mem_ingest(content="[todo] <标题>", type="task")
改状态 PUT /memory/{id}   {"content": "[完成] <原标题>"}
查询   tasks_active  /  palimpsest_cli.py tasks active
归档   palimpsest_cli.py task-archive          # 默认 dry-run
       palimpsest_cli.py task-archive --apply  # 写 md 进知识库并删节点
```

> ⚠️ **PUT 是合并语义**：body 直接传 payload 字段，**不要再包一层 `{"payload": {...}}`**（会写进垃圾嵌套键，且合并语义删不掉）。改完复查 `task_state` 是否真的变了。

> ⚠️ **PUT 改 `content` 后向量不会自动重算**，必须补 `POST /memory/{id}/reembed`，否则语义检索仍按旧文本召回。

`status` 与 `task_state` 是两层：`status` = 节点生命周期（active / outdated / completed），`task_state` = 任务工作流（todo / doing / blocked / done / canceled）。**不要用 `status` 表达工作流。**

---

## 6. 治理（定期跑，不跑库会脏）

| 动作 | 命令 | 说明 |
|---|---|---|
| 盘点 | `palimpsest_cli.py stats` / `mem_stats` | 类型 / 域 / 重要度 / tier 分布 |
| 复盘 | `palimpsest_cli.py review --days 7` | 近期写入 + 治理候选 |
| 合并近似重复 | `palimpsest_cli.py consolidate` → 确认后 `--apply` | 默认阈值 0.85；`importance ≥ 0.8` 受保护不合并 |
| 高频升权 | `palimpsest_cli.py promote --apply` | 默认 30 天 / `hit_count ≥ 5` |
| 全量重嵌 | `palimpsest_cli.py reindex --force --yes` | **换 embedding 模型 / provider 后必跑**（先停 REST） |

**全部默认 dry-run**，先看预览再 `--apply`。

---

## 7. 备份与搬家

```bash
python scripts/backup_db.py --keep 7
```

- 必须**整组**备份：`.db` / `.vec` / `.gidx` / `.pidx` / `.flush_ok` / `.pld.*` / `.wal` —— 它们同属一个 generation，缺一个就起不来
- 脚本会回读校验，不通过直接报错退出
- 搬家 = 拷整个 `data/` 目录 + `.env`

---

## 8. 反模式（这些坑别人一定踩）

| ❌ 别这么干 | 后果 |
|---|---|
| 用 `record` / `event` 存「以后要查」的内容 | 日志层默认不进检索，永远搜不到 |
| 写入前不 `mem_search` | 同一件事长出多个版本，检索结果自相矛盾 |
| 因为「相似度高」就自动取代 | 相似只证明话题相关，不证明是同一事实 |
| PUT 改完 content 不 reembed | 语义检索仍按旧文本召回，越改越歪 |
| 多进程写同一个库 | `Database locked`；并发写还可能污染文件组且不自愈 |
| REST 起多个 worker（`--workers N`） | 同上，库是单写者；本服务**禁止多实例** |
| 用 `task_state` 表达生命周期 | `status` 与 `task_state` 是两层，别混 |
| 检索一次拉全量上下文再灌进提示词 | 一次高精度召回就够；重复检索 + 二次总结只会更贵更糊 |

---

## 9. 时间索引（到期扫描）

**解决什么问题**：聊天里说「后天到驿站」「下周三还书」「这个月 27 号复查」，
几周后问「今天有啥事要办」——这类问题里没有任何字面和语义线索，语义向量
和 FTS 都是 0 命中。时间索引把相对日期在**写入时**解析成绝对日期存下来，
检索时把「今天到期」的节点作为一条额外通道并入 RRF 融合。

默认关闭（`TIME_INDEX_ENABLED=0`），零成本：写读两侧都不解析，payload 不
出现 `dates` 键，检索输出与未引入前逐位一致。

**写侧**（`mem_ingest`，开关打开时生效）：

- 新增可选参数 `event_time`：这条消息发生时的时间（`2026-09-05T17:45:00+08:00`
  或 `2026-09-05`）。相对日期（明天 / 下周三）以它为基准展开——**不是**
  写入时的墙钟；不给则依次回退：正文首个 ISO 时间戳 → 写入墙钟。
- 命中日期的节点 payload 多一个 `dates` 键：

  ```json
  {
    "due": ["2026-09-07"],
    "mention": ["2026-09-01"],
    "ref": "2026-09-05T10:00:00"
  }
  ```

  `due` = 未来声明（可能是到期日/计划日）；`mention` = 当天提及与过去回顾
  （不当到期候选，防止回顾性提及挤爆当天的候选池）。两个列表各自升序去重、
  各最多 8 条。

**读侧**（`mem_hybrid_search` 且 `mode=rrf`，开关打开时生效）：

- 新增可选参数 `today`：到期扫描的「今天」锚（`YYYY-MM-DD` 或完整 ISO；
  空 = 本机今天）。
- 到期节点作为第五路并入 RRF 融合（按 `created_at` 升序，谁先承诺谁先到）；
  命中条目附 `meta["time_hit"]`，整体状态看 `channels["time"]`（`ok` / `empty`）。
- **不做置顶注入、不改返回结构**——到期节点只是多了一条进 top-k 的路。

**相关配置**（全表见 [CONFIGURATION.md](CONFIGURATION.md)）：
`TIME_INDEX_ENABLED` / `TIME_INDEX_WEIGHT` / `TIME_INDEX_LIMIT` /
`TIME_INDEX_WINDOW_DAYS`（到期日窗口，`0` = 只取今天）。

**已知边界（设计如此，不要报 bug）**：

- 农历（「八月初八」）、节日（「春节 / 中秋 / 国庆 / 过年」）、「节前/节后 N 天」
  **不解析**——需要农历换算与节日表，标准库没有；这类提醒请调用方自己换算成
  绝对日期或 ISO 时间戳写进正文。
- 周期表达（「每周三例会」）不当成单日到期。
- `mode=cascade`（级联）暂不支持时间通道，`channels["time"]` 如实标 `skipped`。
- 存量节点没有 `dates`，不参与到期扫描（回填是后续工作）。

---

## 命令参考去哪看

- **MCP 工具 / CLI 子命令 / REST 端点**：[README 的「使用」一节](../README.md#使用)
- **全部环境变量**：[`docs/CONFIGURATION.md`](CONFIGURATION.md)
- **REST 接口细节**：[`docs/API.md`](API.md)
- **检索质量与评测**：[`docs/RETRIEVAL_QUALITY.md`](RETRIEVAL_QUALITY.md)
