"""记忆检索器 - 基于关键词匹配的最简版本"""

import re
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
        根据查询文本检索最相关的记忆节点 (单字精确匹配版)
        """
        # 1. 精准分词：直接按单字拆分，确保人名等专名能被匹配
        keywords = list(query.replace(" ", ""))

        if not keywords:
            keywords = [query.strip()]

        # 2. 遍历所有节点，对每个节点打分
        all_nodes = self._get_all_nodes()
        scored = []

        for node in all_nodes:
            content = node.get("payload", {}).get("content", "")
            if not content:
                continue

            # 关键词命中次数作为分数
            score = sum(1 for kw in keywords if kw in content)
            if score > 0:
                scored.append((score, node))

        # 3. 按分数降序排列，取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for score, node in scored[:top_k]:
            payload = node.get("payload", {})
            results.append(
                {
                    "id": node.get("id"),
                    "score": score,
                    "type": payload.get("type", "unknown"),
                    "content": payload.get("content", ""),
                    "importance": payload.get("importance", 0),
                }
            )

        return results

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
