# TriviumDB 能力盘点：我们有什么、用了什么、漏了什么

> 目的：Palimpsest 构建在 TriviumDB（TDB）之上。在自研任何能力之前，先确认 TDB 提供了什么 —— 避免重复造轮子。
> 版本：triviumdb **0.8.10**（2026-10-21 发布）——**盘点基线**。
>
> ⚠️ 当前依赖已升至 **0.9.0b2**（见 `requirements.txt`）；本文的能力结论与数字基于 0.8.10，升级后未重新盘点，引用时请注意版本差异。
> 方法：`dir()` + `inspect.signature` 读实际绑定，再对代码库做调用统计；关键方法在真库副本上实测
> 时间：2026-10-11

---

## 一、结论先行

| 发现 | 影响 |
|---|---|
| TDB 提供 **65 个公开方法**，我们用了 **24+ 个** | **图算法（Leiden）已接入生产**（`mem_communities` MCP 工具 + `/graph/communities` 端点）|
| **检索类有 6 个方法未用**，其中 `search_hybrid` 最值得评估 | 可能正在应用层重复实现引擎原生能力 |
| `reachable_detailed` / `query_subgraph` 覆盖我们手写的 BFS | 可删自研代码、减少维护面 |
| **多方法 `__doc__` 为空（Rust 绑定）** | 不能据"无文档"判断"无能力" —— 必须看签名/读源码/实测 |

**方法论要点（最重要）**：本次盘点中，我一度因 `inspect` 取不到文档、`grep` 没搜到调用，**误判 `leiden_cluster` 未使用**；实际它早已是 `mcp_tools/graph.py` 里的正式工具。**盘点必须交叉验证：`dir()` 只证明"存在"，`grep` 可能漏（换行/别名），最终要落到"跑一次"或"读定义处"。**

---

## 二、TDB 能力全景 vs 我们的使用

### 2.1 检索类（⭐ 盲区最集中）

| 方法 | 签名 | 我们是否用 |
|---|---|---|
| `search` | 基础向量检索 | ✅ |
| `search_advanced` | 高级检索（图扩展） | ✅ 38 处 |
| **`search_hybrid`** | `(query_vector, query_text, top_k=5, expand_depth=2, min_score=0.1, hybrid_alpha=0.7, payload_filter=None)` | ❌ **未用** ⭐ |
| **`search_graph_first`** | `(query_vector, anchor_ids, top_k, max_anchor_nodes=100000)` | ❌ **未用** ⭐ |
| **`search_grouped`** | `(query_vector, top_k=5, recall_k=0, rerank_k=0, expand_depth=2, ..., min_edge_weight=0.0, edge_direction='out')` | ❌ 未用 |
| **`search_batch`** | 批量检索 | ❌ 未用 |
| **`search_with_context`** | 返回 `(hits, ctx)`，ctx 含**管线各阶段计时**（`{'hook_pre_search':…, 'graph_expand':…}`）| ❌ 未用 ⭐ |
| **`search_exact`** | 精确检索 | ❌ 未用 |

**重点：`search_hybrid` 原生做了「向量 + 全文 + 图扩展」的融合**（`hybrid_alpha` 调两者权重、`expand_depth` 控制图扩展），**与我们在应用层手写的 RRF 融合高度重叠**。

**`search_with_context` 自带管线计时**，我们目前靠自己在应用层埋点。

### 2.2 图算法类（⭐ 新能力）

| 方法 | 说明 | 我们是否用 |
|---|---|---|
| **`leiden_cluster`** | **Leiden 社区检测**（Rust 原生）| ❌ 未用 ⭐ |
| `graph_stats` | 图统计（度分布等）| ✅ 4 处 |
| `validate_graph` | 图完整性校验 | ✅ 5 处 |
| `repair_graph_indexes` | 图索引修复 | ✅ 3 处 |
| **`reachable_detailed`** | `(id, min_depth, max_depth, labels, direction, max_visited_nodes, max_results, max_edges)` | ⚠️ 我们**自己写了 BFS** |
| **`query_subgraph`** | 子图查询（同上一族参数）| ❌ 未用 |
| `neighbors` | 邻居查询 | ✅ 2 处 |
| `reachable` | 可达性 | ✅（少量）|

### 2.3 索引类

| 方法 | 我们是否用 |
|---|---|
| `create_index` / `create_composite_index` | ✅ 12 处 |
| `create_ordered_index` | ✅ 9 处 |
| `create_bitmap_index` | ✅ 9 处 |
| `create_unique_index` / `create_unique_composite_index` | ⚠️ 待核 |
| **`create_ngram_index`** | ❌ 未用 |
| **`index_keyword`** / `index_text` / `substring_lookup` / `indexed_lookup` | ⚠️ 部分未用 |
| **`set_auto_build_quiver`** | ❌ 未用 |
| `build_text_index` | ✅ |

### 2.4 事务 / 并发类

| 方法 | 我们是否用 |
|---|---|
| `transaction` | ⚠️ 待核 |
| **`delete_many_atomic`** | ❌ 未用 |
| **`compare_and_set_payload_field`** | ❌ 未用 ⭐（**CAS 原语**——我们借鉴 Letta 时想要的东西）|
| `batch_insert` / `batch_insert_with_ids` | ✅ 3 处 |
| `upsert_with_id` / `upsert_edge` / `update_edge` | ⚠️ 部分 |
| `reserve_nodes` | ❌ 未用 |

### 2.5 TQL（查询语言）

| 方法 | 我们是否用 |
|---|---|
| `tql` | ✅ **140 处**（重度使用）|
| `execute_prepared_tql` | ✅ 36 处 |
| `prepare_tql` | ✅ 22 处 |
| `tql_mut` | ⚠️ 1 处 |

**TQL 是我们用得最充分的部分。**

### 2.6 运维 / 生命周期

| 方法 | 我们是否用 |
|---|---|
| `compact` | ✅ 13 处 |
| `storage_info` / `index_info` / `estimated_memory` | ✅ |
| `migrate` | ⚠️ 待核 |
| **`publish_generation_manifest`** | ❌ 未用 |
| `set_hook` / `load_ffi_hook` / `clear_hook` / `clear_search_state` | ⚠️ 部分（`search_with_context` 用到 Hook）|
| `set_memory_limit` / `set_sync_mode` / `disable/enable_auto_compaction` | ⚠️ 待核 |

---

## 三、实测：`leiden_cluster` 在真实库上的表现

在生产库副本（1,558 节点、2,090 边）上实测：

```python
r = db.leiden_cluster()  # → {"communities": [[...], [...], ...]}
```

| 指标 | 实测值 |
|---|---|
| **耗时** | **0.024 s**（24 毫秒）|
| 社区数 | **121** |
| 最大社区 | 61 / 54 / 53 / 46 / 38 / 32 节点 |
| ≥10 节点的社区 | 18 个 |
| **覆盖节点** | **877 / 1,558（56%）** |
| 单节点社区 | 0 个 |

**两个关键读数**：

1. **速度不是问题** —— Rust 原生 24ms，可以放进交互路径。
2. **677 个节点不属于任何社区** —— 这是一个比「孤立率 16.6%」更精细的指标：孤立率统计的是「零边节点」，而社区检测能看出「**有边但结构上飘在外面**」的节点。

---

## 四、建议的利用顺序

| 优先级 | 动作 | 理由 |
|---|---|---|
| **1** | **评估 `search_hybrid` 能否替代应用层 RRF** | 若可行，可删掉自研融合代码（少一处维护面），且图扩展由引擎原生完成 |
| **2** | **用 `leiden_cluster` 做结构诊断** | 677 个未入社区节点 = 「该连没连」与「本不该连」的分界线，比硬加边更治本 |
| **3** | 用 `reachable_detailed` / `query_subgraph` 替换自研 BFS | 减少自研代码量，参数更全（`max_visited_nodes` / `max_edges` 等防护）|
| **4** | 用 `search_with_context` 拿管线计时 | 免去应用层埋点 |
| **5** | 评估 `compare_and_set_payload_field` | 原生 CAS 原语，可直接支撑并发写安全 |
| **6** | 评估 `create_ngram_index` / `index_keyword` | 中文场景可能提升子串检索 |

---

## 五、方法论（可复用于任何"底层依赖"）

1. **先枚举，再自研**：`dir()` + `inspect.signature` 是零成本的能力盘点；我们此前多次在应用层实现引擎已提供的能力。
2. **区分"文档没有"与"能力没有"**：TDB 多个原生方法 `__doc__` 为空（Rust 绑定），**不能据此判断其不存在或不可用** —— 必须看签名、读上游源码、跑实测。
3. **升级前先压测**：TDB 迭代快（0.8.5→0.8.8 仅一周），但我们已有过一次「未压测就升级」的教训；带图算法的版本更需要实测。
4. **社区检测是回答"孤立率是否健康"的正确工具**：孤立率只看零边，「不属于任何社区」才能看出结构上的离散。
