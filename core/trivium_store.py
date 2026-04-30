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
        self.db: triviumdb.TriviumDB | None = None

    def open(self) -> None:
        """打开数据库连接"""
        self.db = triviumdb.TriviumDB(self.db_path, dim=self.dim)

    def close(self) -> None:
        """关闭数据库连接"""
        if self.db:
            self.db.flush()
            # TriviumDB 的上下文管理器会自动 flush，这里手动调用确保安全

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        """插入记忆节点，返回节点 ID"""
        if not self.db:
            raise RuntimeError("数据库未打开，请先调用 open()")

        payload = {
            "type": node_data.get("type", "unknown"),
            "label": node_data.get("label", ""),
            "content": node_data.get("content", ""),
            "importance": node_data.get("importance", 0.5),
            "status": "active",
            "created_at": None,  # 后续可以加时间戳
        }

        node_id = self.db.insert(embedding, payload)

        # 为 type 和 importance 创建索引（加速后续过滤查询）
        # TriviumDB v0.6.0 支持属性二级索引
        self.db.create_index("type")
        self.db.create_index("importance")
        self.db.create_index("status")

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
        if not self.db:
            raise RuntimeError("数据库未打开")
        self.db.link(source_id, target_id, label=relation_type, weight=weight)

    def search_similar(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        expand_depth: int = 1,
    ) -> list[dict[str, Any]]:
        """语义搜索 + 图扩散"""
        if not self.db:
            raise RuntimeError("数据库未打开")

        results = self.db.search(
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
        if not self.db:
            raise RuntimeError("数据库未打开")

        node = self.db.get(node_id)
        if node:
            return {
                "id": node.id,
                "payload": node.payload,
                "num_edges": node.num_edges,
            }
        return None
