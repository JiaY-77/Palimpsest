"""TriviumDB 存储封装"""

import json
import logging
from typing import Any

import triviumdb
from config import Config

logger = logging.getLogger(__name__)


class TriviumStore:
    """封装 TriviumDB 操作，提供记忆存储和检索接口"""

    def __init__(self) -> None:
        self.db_path = Config.DB_PATH
        # 使用 DeepSeek 默认的 embedding 维度
        self.dim = 1536

    def _acquire(self):
        """
        获取数据库连接（仅存在于 with 块内部）
        注意：triviumdb 库没有提供 Python 类型存根 (.pyi)，
        因此手动添加 type: ignore 注释来抑制 Pylance 的类型推断警告。
        """
        # type: ignore
        return triviumdb.TriviumDB(self.db_path, dim=self.dim)

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        """插入记忆节点，返回节点 ID"""
        with self._acquire() as db:
            payload = {
                "type": node_data.get("type", "unknown"),
                "label": node_data.get("label", ""),
                "content": node_data.get("content", ""),
                "importance": node_data.get("importance", 0.5),
                "status": "active",
                "created_at": None,
            }

            node_id = db.insert(embedding, payload)

            db.create_index("type")
            db.create_index("importance")
            db.create_index("status")

            return node_id

    def create_edge(
        self,
        source_id: int,
        target_id: int,
        relation_type: str,
        content: str = "",
        weight: float = 0.9,
    ) -> None:
        """在两个节点之间创建边"""
        with self._acquire() as db:
            db.link(source_id, target_id, label=relation_type, weight=weight)

    def search_similar(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        expand_depth: int = 1,
    ) -> list[dict[str, Any]]:
        """语义搜索 + 图扩散"""
        with self._acquire() as db:
            results = db.search(
                query_embedding,
                top_k=top_k,
                expand_depth=expand_depth,
                min_score=0.3,
            )

            return [
                {
                    "id": hit.id,
                    "score": hit.score,
                    "payload": hit.payload,
                }
                for hit in results
            ]

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """获取单个节点的详细信息"""
        with self._acquire() as db:
            node = db.get(node_id)
            if node:
                return {
                    "id": node.id,
                    "payload": node.payload,
                    "num_edges": node.num_edges,
                }
            return None

    def _get_all_node_ids(self) -> list[int]:
        """获取数据库中所有节点的 ID 列表（供内部使用）"""
        # type: ignore
        with self._acquire() as db:
            return db.all_node_ids()
