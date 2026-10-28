# REST API 参考

Palimpsest 的 REST 服务由 `main.py` 提供，默认监听 `127.0.0.1:8090`，是访问记忆库的**唯一写者**（所有接入方式最终都经它）。启动：

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8090
```

> README 只列常用端点；本页是完整清单。若设置了 `PALIMPSEST_API_KEY`，
> 除 `/` 外所有端点要求 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。

## REST API — `main.py`，端口 8090

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 服务信息 + 版本 + 端点索引 |
| `GET` | `/export` | 导出记忆为分页 JSON 快照（默认每页 100 条，上限 500） |
| `GET` | `/summary` | 人类可读的记忆摘要（事件 / 角色状态 / 计划） |
| `GET` | `/memory/{id}` | 读取单节点完整 payload |
| `POST` | `/report` | 基于当前存储生成 LLM 分析报告（Prompt 面向小说创作 / 角色扮演场景，不是通用摘要） |
| `DELETE` | `/memory/{id}` | 删除记忆节点（FTS 索引同步） |
| `PUT` | `/memory/{id}` | 更新节点 payload（**合并语义**：只改传入字段，其余保留；自动同步 FTS） |
| `PATCH` | `/memory/{id}` | 部分更新节点 payload（与 PUT 同合并语义，REST 语义更精确） |
| `PATCH` | `/memory/{id}/vector` | 更新节点的向量（维度需一致） |
| `POST` | `/memory/{id}/reembed` | 按当前 content 重算并写回向量（改 content 后语义漂移的补救入口，服务端自己生成向量） |
| `POST` | `/memory/{id}/open` | 记录胶囊被会话接手（body：`session_id` 可空串=未知会话；首次写 `opened_at`、每个新会话 `open_count` +1、`opened_by` 去重按首次顺序上限 10；同 `session_id` 重复调用幂等不重复计数；不改 content / `last_touched_at`——检索命中计数 `hit_count` 与「被接手」是两回事） |
| `POST` | `/mem/search` | 统一检索 |
| `POST` | `/skill/search` | 技能语义检索 |
| `POST` | `/mem/hybrid-search` | FTS5 + 向量混合检索 |
| `POST` | `/mem/ingest` | 写入新记忆（含冲突检测 + 敏感扫描；`replaces` 字段显式声明取代；可选 `meta` 是键值均为字符串的元数据字典——键 ≤8 / 键名 `^[a-zA-Z0-9_]{1,32}$` / 值 ≤256 字符，通过校验原样落 payload 的 `meta` 子字典，不传则 payload 不含该键） |
| `POST` | `/mem/purge-by-meta` | 按「域名 + 自定义元数据」精确定位并批量删除（body：`domain` 必填非空（缺失/空串 400，不存在全库级删除）、`meta_key` / `meta_value` 必填、`dry_run`（默认 true 只预览）/ `max_delete`（默认 50，匹配数超出 409 拒绝执行）；真实删除逐条走节点级联删除（边 + FTS/TDB 文本通道一并清理），每次写一条不带 `meta` 的 `type=record` 审计节点） |
| `POST` | `/mem/retag-by-meta` | 按「域名 + 自定义元数据」批量改写 `meta` 值——重命名聊天后同步改标，防记忆变孤儿（body：`domain` 必填非空、`meta_key` / `old_value` / `new_value` 必填、`old_value == new_value` → 400、`dry_run`（默认 true 只预览）/ `max_update`（默认 500，匹配数超出 409 拒绝执行）；改写为合并式 `{**cur_meta, key: new_value}`，其他 meta 键不冲掉；写一个 `meta_key: new_value` 不合法 → 400；每次写一条不带 `meta` 的 `type=record` 审计节点） |
| `POST` | `/mem/link` | 创建图边 |
| `POST` | `/memoryhub/unpack` | 只读解开 MemoryHub 记忆包（body：`path` / `preview_chars`；校验格式 / 版本 + 敏感扫描 + 人读摘要） |
| `POST` | `/memoryhub/import` | 把记忆包里的节点入籍（body：`path` / `select` / `include_secrets`；`select` 为空 = 只预览不写） |
| `DELETE` | `/mem/edge` | 删除图边（body：`source_id` / `target_id` / `relation`；幂等） |
| `POST` | `/mem/fact-history` | 单条事实的 bi-temporal 时间线与取代关系（body：`node_id`） |
| `POST` | `/mem/recent` | 最近记忆列表（按 created_at 倒序） |
| `GET` | `/tasks/active` | 活跃任务列表（query：`project` / `states` / `limit` / `include_handoff`（列出会话接续点，默认 false——接续点由写入层自动打 `kind=handoff`，显式列出时不受状态过滤，`task_state` 为空也能取回）；按状态优先级 → 最近触碰倒序） |
| `POST` | `/mem/stats` | 库级盘点统计 |
| `POST` | `/graph/neighbors` | 某节点的图谱邻居 |
| `POST` | `/graph/communities` | Leiden 社区发现 |
| `POST` | `/lifecycle/pre-turn` | **记忆策略 + 思维预设层**：每轮模型调用前决定召回哪些记忆，返回可注入 prompt 的文本（trivial/过短自动跳过）+ 按情境动态挑的思考骨架（`thought_skeleton`；闲聊/过短不给，宿主可关） |
| `POST` | `/lifecycle/post-turn` | **记忆策略 + 思维底片**：每轮回复后决定是否沉淀、写什么内容、写哪一层（logs/facts）；并从 `assistant_message` 抽取 `<thought>...</thought>` 段写 `type="thought"`（tier=logs，不命中不落库但响应可观测） |
| `POST` | `/lifecycle/session-end` | **记忆策略**：会话结束提炼要点、去重、写入 facts 层 |
| `POST` | `/lifecycle/pre-compress` | **记忆策略**：压缩前抽取要点（只回文本，不写库） |
| `POST` | `/lifecycle/context-enhance` | **记忆策略**：压缩前挑主题、查图谱关键链、组装注入文本（只回文本，不写库） |
| `POST` | `/mem/consolidate` | 容量合并候选预览（只读 dry-run；请求体可选 `threshold`（默认 0.85）、`max_importance`（默认 0.8）；不接受 apply） |
| `GET` | `/doctor` | 部署体检（只读，返回 `{"ok", "checks"}`） |

## 示例

```bash
# 按域名 + 元数据干跑预览（dry_run 默认 true，不删除任何节点）
curl -X POST http://127.0.0.1:8090/mem/purge-by-meta \
  -H "Content-Type: application/json" \
  -d '{"domain": "rp", "meta_key": "chat_id", "meta_value": "chat-42"}'

# 重命名聊天后同步改标（dry_run 默认 true，这里显式真改）
curl -X POST http://127.0.0.1:8090/mem/retag-by-meta \
  -H "Content-Type: application/json" \
  -d '{"domain": "rp", "meta_key": "chat_id", "old_value": "chat-42", "new_value": "chat-43", "dry_run": false}'
```

```bash
curl -X POST http://127.0.0.1:8090/mem/search \
  -H "Content-Type: application/json" \
  -d '{"query": "架构", "scope": "all", "top_k": 5}'
```
