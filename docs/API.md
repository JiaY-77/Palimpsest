# REST API 参考

Palimpsest 的 REST 服务由 `main.py` 提供，默认监听 `127.0.0.1:8090`，是访问记忆库的**唯一写者**（所有接入方式最终都经它）。启动：

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8090
```

> README 只列常用端点；本页是完整清单（26 条）。若设置了 `PALIMPSEST_API_KEY`，
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
| `POST` | `/mem/search` | 统一检索 |
| `POST` | `/skill/search` | 技能语义检索 |
| `POST` | `/mem/hybrid-search` | FTS5 + 向量混合检索 |
| `POST` | `/mem/ingest` | 写入新记忆（含冲突检测 + 敏感扫描） |
| `POST` | `/mem/link` | 创建图边 |
| `DELETE` | `/mem/edge` | 删除图边（body：`source_id` / `target_id` / `relation`；幂等） |
| `POST` | `/mem/recent` | 最近记忆列表（按 created_at 倒序） |
| `GET` | `/tasks/active` | 活跃任务列表（query：`project` / `states` / `limit`；按状态优先级 → 最近触碰倒序） |
| `POST` | `/mem/stats` | 库级盘点统计 |
| `POST` | `/graph/neighbors` | 某节点的图谱邻居 |
| `POST` | `/graph/communities` | Leiden 社区发现 |
| `POST` | `/lifecycle/pre-turn` | **记忆策略**：每轮模型调用前决定召回哪些记忆，返回可注入 prompt 的文本（trivial/过短自动跳过） |
| `POST` | `/lifecycle/post-turn` | **记忆策略**：每轮回复后决定是否沉淀、写什么内容、写哪一层（logs/facts） |
| `POST` | `/lifecycle/session-end` | **记忆策略**：会话结束提炼要点、去重、写入 facts 层 |
| `POST` | `/lifecycle/pre-compress` | **记忆策略**：压缩前抽取要点（只回文本，不写库） |
| `POST` | `/lifecycle/context-enhance` | **记忆策略**：压缩前挑主题、查图谱关键链、组装注入文本（只回文本，不写库） |

## 示例

```bash
curl -X POST http://127.0.0.1:8090/mem/search \
  -H "Content-Type: application/json" \
  -d '{"query": "架构", "scope": "all", "top_k": 5}'
```
