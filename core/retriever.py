"""记忆检索器 - 基于关键词匹配的最简版本"""

import logging
from typing import Any
from core.trivium_store import TriviumStore

logger = logging.getLogger(__name__)


class Retriever:
    """检索已存储的记忆节点，返回相关上下文"""

    def __init__(self, store: TriviumStore):
        self.store = store

    def retrieve(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """
        根据查询文本检索最相关的记忆节点（语义检索版）
        """
        # 生成查询向量
        query_vec = self.store.embed_text(query)

        # 用语义相似度检索
        results = self.store.search_similar(query_vec, top_k=top_k, expand_depth=1)

        return [
            {
                "id": r.get("id"),
                "score": r.get("score", 0),
                "type": r.get("payload", {}).get("type", "unknown"),
                "content": r.get("payload", {}).get("content", ""),
                "importance": r.get("payload", {}).get("importance", 0),
            }
            for r in results
        ]

    def _get_all_nodes(self) -> list[dict[str, Any]]:
        """直接使用手动遍历获取所有节点"""
        with self.store._acquire() as db:
            ids = db.all_node_ids()
            nodes = []
            for nid in ids:
                node = db.get(nid)
                if node:
                    nodes.append(
                        {
                            "id": node.id,
                            "payload": node.payload,
                            "num_edges": node.num_edges,
                        }
                    )
            return nodes
