# 事实时间维度（bi-temporal）

> 本文说明 Palimpsest 如何记录「事实何时为真」，以及它与既有「记录时间」的区别。

## 为什么需要第二个时间维度

Palimpsest 原本只记一个时间：`created_at`——这条记录**何时被写入系统**。这是「系统时间」。

但知识有时效性，事实会变真、也会不再为真。「删除旧事实」会丢失历史，也就无法回答这类问题：

> 「三个月前，关于这个配置，它当时认为是什么？」

要回答它，需要第二个维度：**世界时间**——事实在这个世界里从何时为真、到何时不再为真。两个维度正交，各自独立记录：

| 维度 | 字段 | 含义 |
|---|---|---|
| 世界时间 | `valid_at` | 事实**开始为真**的时间 |
| 世界时间 | `invalid_at` | 事实**停止为真**的时间 |
| 系统时间 | `created_at` | 这条记录**写入系统**的时间 |
| 系统时间 | `expired_at` | 系统把这条记录**标为历史**（不再是当前采纳版本）的时间 |

## `invalid_at` 与 `expired_at` 的区别

两者常同时发生，但语义不同：

- `invalid_at` 是**世界层面**的判断：「这个事实不再为真了」。
- `expired_at` 是**系统层面**的动作：「这条记录不再是我当前采纳的版本」。

分开记录的价值：一条被取代后又发现仍然成立的事实，`invalid_at` 可能被修正，而 `expired_at` 仍是当初标记的那一刻——「它是什么时候被换下来的」这个问题有独立答案。

## 写入时的行为

对**事实类**节点（`memory` / `task` / `plan`）：

1. **新写入**：自动补 `valid_at`，缺省等于写入时刻（「写下即认为当下为真」）。调用方可显式传入以表达未来时态的计划/承诺——显式值不被覆盖。
2. **被取代**（高相似冲突检测命中，旧节点标 `outdated`）：
   - `invalid_at` ← 取代者的 `valid_at`（旧事实自新事实为真之时起不再为真）
   - `expired_at` ← 本次系统标记的时刻（仅在首次标记时写入）

**豁免**：`kb_chunk`（知识库切片，外来文档的语义索引，没有「事实何时为真」的概念）与历史留痕类型（`record` / `event` / `git_commit` / `review` / `correction`）不打时间字段。

时间戳一律用数值（`time.time()`），与 `created_at` 一致——写 ISO 字符串会让 `/mem/recent` 的排序在 float/str 之间抛错。

## 读取时的行为

- **检索结果**：`mem_search` 等结果条目的 `meta.times` 携带该记忆的时间字段（存在才带，保持 meta 精简）。
- **单条追溯**：`mem_fact_history(node_id)` 返回一条事实的完整时间线——

  ```json
  {
    "found": true,
    "node_id": 42,
    "type": "memory",
    "status": "outdated",
    "times": {"valid_at": 1000.0, "invalid_at": 1500.0, "expired_at": 1500.0, "created_at": 1000.0},
    "superseded":    [{"id": 17, "content_summary": "…", "times": {…}}],
    "superseded_by": [{"id": 57, "content_summary": "…", "times": {…}}]
  }
  ```

  其中 `superseded_by` 走的是**反向边**（`get_incoming_edges`）：谁指向了本节点——即「谁取代了我」。这正是「谁失效了谁」的反向索引，与既有的 `REVISED_BY` 出边（我取代了谁）互为镜像。
- **整链视图**：`mem_version_history` 仍按 domain 沿 `REVISED_BY` 链展开（面向版本日志）。

## 实现位置

| 关注点 | 位置 |
|---|---|
| 时间字段定义与打标函数 | `core/bitemporal.py` |
| 新写入打 `valid_at` | `mcp_tools/memory.py`（`mem_ingest`） |
| 被取代补 `invalid_at` / `expired_at` | `core/conflict.py`（`resolve_conflict`） |
| 反向边查询 | `core/trivium_store.py`（`get_incoming_edges`） |
| 单条时间线工具 | `mcp_tools/memory.py`（`mem_fact_history`） |

## 范围与后续

本次是**第一阶段：存字段 + 反向索引**。后续阶段（尚未实现）：

- **`as_of` 查询**：按时间点回看「那一刻它认为什么」。
- **时间窗冲突判定**：两条事实的时间窗不重叠就不算矛盾（如「十年前住北京」≠「现在住上海」），降低误报。

历史数据无时间字段，迁移只能为 `null`——不追溯回填（自动从文本抽时间不可靠）。缺失时间字段的节点走原有行为，不受影响。
