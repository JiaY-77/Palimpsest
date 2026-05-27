"""TriviumDB 存储封装"""

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
        self.dim = 1024

    def _acquire(self):
        """
        获取数据库连接（仅存在于 with 块内部）
        注意：triviumdb 库没有提供 Python 类型存根 (.pyi)，
        因此手动添加 type: ignore 注释来抑制 Pylance 的类型推断警告。
        """
        # type: ignore
        return triviumdb.TriviumDB(self.db_path, dim=self.dim)  # type: ignore

    def embed_text(self, text: str) -> list[float]:
        """
        通过本地 Ollama 服务生成文本向量
        使用 bge-m3 模型，输出 1024 维
        """
        import requests

        try:
            response = requests.post(
                "http://localhost:11434/api/embeddings",
                json={"model": "bge-m3", "prompt": text},
                timeout=30,
            )
            response.raise_for_status()
            embedding = response.json().get("embedding")
            if embedding:
                return embedding
            else:
                print("警告: Ollama 返回的 embedding 为空")
                return [0.0] * self.dim
        except Exception as e:
            print(f"Ollama Embedding 生成失败: {e}")
            return [0.0] * self.dim

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
        """手动遍历所有节点，计算余弦相似度并返回 Top-K"""
        import numpy as np

        # 1. 获取所有节点ID
        ids = self._get_all_node_ids()
        if not ids:
            return []

        # 2. 逐个获取节点，手动计算相似度
        scored = []
        qv = np.array(query_embedding)
        for nid in ids:
            node_data = self.get_node(nid)
            if not node_data or "vector" not in node_data:
                continue
            db_vec = np.array(node_data["vector"])
            # 防止零向量
            norm = np.linalg.norm(db_vec)
            if norm == 0:
                continue
            score = float(np.dot(qv, db_vec) / (np.linalg.norm(qv) * norm))
            scored.append((score, node_data))

        # 3. 排序、截断、返回
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "id": node.get("id"),
                "score": score,
                "payload": node.get("payload", {}),
            }
            for score, node in scored[:top_k]
        ]

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """获取单个节点的详细信息（包含向量）"""
        with self._acquire() as db:
            node = db.get(node_id)
            if node:
                return {
                    "id": node.id,
                    "payload": node.payload,
                    "num_edges": node.num_edges,
                    "vector": node.vector,  # 新增：返回向量
                }
            return None

    def _get_all_node_ids(self) -> list[int]:
        """获取数据库中所有节点的 ID 列表（供内部使用）"""
        # type: ignore
        with self._acquire() as db:
            return db.all_node_ids()
