# -*- coding: utf-8 -*-
"""
Palimpsest 本地 MCP Server
=========================
把小七（Hermes）的记忆层能力暴露为标准 MCP 工具，供 Hermes 通过 MCP 协议调用。
底层使用 Palimpsest 的 TriviumStore（向量 + 图 + 文档存储），embedding 由本地
Ollama 的 qwen3-embedding:0.6b 生成（1024 维，已验证可用）。

本文件作为 MCP 入口与传统 re-export 汇聚点：工具实现已拆分至 mcp_tools/ 包
（memory.py / kb.py / graph.py / routing.py），此处只负责从 mcp_tools 引入全部
工具并在 FastMCP 上完成注册，同时保留旧的外部导入接口（main.py / palimpsest_cli
等继续 from mcp_server import ... 而不改动）。

工具列表：
  1. mem_retrieve - 语义检索记忆（只返回 150 字摘要 + meta，绝不返回全文，省 token）
  2. mem_get_full - 按 id 取完整记忆内容（全文由本工具单独取）
  3. mem_ingest   - 写入新记忆（带冲突检测：相似旧记忆标记 outdated + REVISED_BY 链）
  4. mem_recent   - 最近记忆列表（按 created_at 倒序）
  5. kb_index     - 知识库文件索引（扫描知识库根目录下所有 .md）
  6. kb_search    - 知识库语义检索（向量检索，只查 build_kb_index.py 建的 kb_chunk 节点，
                   含 domain=rule 规则类切片）
  7. mem_search   - 统一检索入口：scope=memory/kb/all 混合检索记忆与知识库
                   （v2.0：domain=rule 规则切片内置 ×1.3 加权；
                    v3.0：include_neighbors=True 时返回图关联区（分区返回），
                    语义区原样 + neighbors 区展示已命中节点的一跳邻居）
  8. router_query - 任务路由查询（v2.0）：查规则类知识切片，提取推荐模型/配置
  9. mem_version_history - 版本历史查询：沿 REVISED_BY 修订链返回版本演进摘要
                 （如 SOUL 版本日志；domain/full_content/offset/limit 参数）
  10. graph_neighbors  - 图谱邻居查询：从任意节点沿出边 BFS 遍历
                 （relation 过滤 / depth 1-3 / limit 截断，去重）
  11. mem_link         - 手动建边（RELATED_TO / CAUSES / REFERS_TO 等）
   12. mem_hybrid_search - 混合检索（FTS5 精确 + 语义向量：RRF 融合 k=60 / 级联粗筛→精排）
   13. mem_consolidate  - 容量自动合并（扫描高相似度 memory 节点对，dry_run 预览 / apply 真正合并）

边类型约定：
  - REVISED_BY : 版本修订链（mem_ingest 自动建，新 → 旧；单向语义）
  - RELATED_TO : 关联（mem_link 手动建；无向语义，双向建边协议自动补反向）
  - CAUSES     : 因果（预留，未来 ingest 提取；无向语义，双向补反向）
  - REFERS_TO  : 引用（预留；无向语义，双向补反向）
双向建边协议：RELATED_TO / CAUSES / REFERS_TO 在 mem_link(bidirectional=True)
  时自动补反向边（先查存在则跳过），绕开 get_edges 只返回出边、入边不可查的
  API 限制；REVISED_BY 保持单向（版本链方向语义）。

运行方式（由 Hermes 以 stdio 方式拉起）：
    python mcp_server.py
"""

from mcp_tools import (  # noqa: E402
    graph_neighbors, kb_index, kb_search, mem_consolidate, mem_get_full,
    mem_hybrid_search, mem_ingest, mem_link, mem_recent, mem_retrieve,
    mem_review, mem_search, mem_version_history, router_query, store, mcp,
)
from mcp_tools._common import _shorten, _to_json, _kb_md_files, KNOWLEDGE_DIR  # noqa: E402
from mcp_tools.graph import _BIDIRECTIONAL_RELATIONS, _collect_neighbors, _edge_exists  # noqa: E402
from mcp_tools.memory import _l1_sniff, _mem_search_impl, _parse_version_content  # noqa: E402
from mcp_tools.routing import _extract_recommendation  # noqa: E402


if __name__ == "__main__":
    mcp.run()
