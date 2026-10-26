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
| `thought` | 推理过程（思维底片） | ❌ 日志层 |
| `record` | 操作留痕、状态变更日志 | ❌ 日志层 |
| `event` | 发生过的动作 | ❌ 日志层 |
| `git_commit` | 提交留痕 | ❌ 日志层 |

> ⚠️ **这是最容易踩的坑**：`record` / `event` / `thought` 属日志层，**默认检索不到**。想以后能查回来的东西，必须用 `memory` / `decision` / `plan` / `task`。
>
> 需要翻日志层时显式指定：`mem_search(tier="logs")` 或 CLI `search --tier logs`。

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
- 新会话用 `tasks_active(include_handoff=true)` 取回，不会污染活跃任务列表

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
| 技能库（`SKILL.md`） | `python scripts/build_skill_index.py` |
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
| 全量重嵌 | `palimpsest_cli.py reindex --force` | **换 embedding 模型 / provider 后必跑** |

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

## 命令参考去哪看

- **MCP 工具 / CLI 子命令 / REST 端点**：[README 的「使用」一节](../README.md#使用)
- **全部环境变量**：[`docs/CONFIGURATION.md`](CONFIGURATION.md)
- **REST 接口细节**：[`docs/API.md`](API.md)
- **检索质量与评测**：[`docs/RETRIEVAL_QUALITY.md`](RETRIEVAL_QUALITY.md)
