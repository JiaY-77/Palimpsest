"""TriviumDB 存储封装"""

import logging
import time
from typing import Any

import triviumdb
from config import Config
from core.secret_scan import SecretScanError, scan_secret

logger = logging.getLogger(__name__)

# 图谱扩散精馏（2026-08-25 主人挑战 #2）：每节点最多扩散最强 N 条边；弱边阈值
EXPAND_MAX_EDGES_PER_NODE = 20   # 防高节点（500 邻居）全量扩散
EXPAND_MIN_EDGE_WEIGHT = 0.0     # 弱边过滤阈值（默认不启用，可调）


def domain_in_block(node_domain: str, block: str) -> bool:
    """区块匹配（分区块）：domain 是否属于 block。kb 区块兼容 rule（rule 是知识子集）。"""
    d = (node_domain or "general").strip().lower()
    b = (block or "").strip().lower()
    if not b:
        return True
    if d == b:
        return True
    if b == "kb" and d == "rule":
        return True
    return False


def node_domain(payload: dict) -> str:
    """节点区块域：memory 用 character_name，kb_chunk 用 domain，缺省 general。

    注意：无 domain 的节点（general）在带 block 的分区查询中会被彻底忽略
    （既不能当起点也不能当邻居）——「无标签 = 未分类」，隔离场景下丢弃防污染；
    只有全量模式（block 为空）才可见。
    """
    return (payload.get("character_name", "")
            or payload.get("domain", "") or "general").strip().lower()


class TriviumStore:
    """封装 TriviumDB 操作，提供记忆存储和检索接口"""

    def __init__(self) -> None:
        self.db_path = Config.DB_PATH
        # embedding 维度从配置读取（按 provider 选择：ollama 本地 / openai 兼容云端）
        self.provider = getattr(Config, "EMBEDDING_PROVIDER", "ollama") or "ollama"
        if self.provider == "openai":
            self.dim = Config.EMBEDDING_DIM
        else:
            self.dim = Config.OLLAMA_EMBEDDING_DIM

    def _acquire(self):
        """
        获取数据库连接（仅存在于 with 块内部）
        注意：triviumdb 库没有提供 Python 类型存根 (.pyi)，
        因此手动添加 type: ignore 注释来抑制 Pylance 的类型推断警告。
        """
        # type: ignore
        return triviumdb.TriviumDB(self.db_path, dim=self.dim)  # type: ignore

    def embed_text(self, text: str) -> list[float]:
        """生成文本向量，按 EMBEDDING_PROVIDER 分发（默认本地 ollama，隐私优先）。"""
        if self.provider == "openai":
            return self._embed_openai(text)
        return self._embed_ollama(text)

    def _embed_ollama(self, text: str) -> list[float]:
        """
        通过本地 Ollama 服务生成文本向量
        使用配置的 Ollama embedding 模型（默认 qwen3-embedding:0.6b，1024 维）
        """
        try:
            import requests

            response = requests.post(
                "http://localhost:11434/api/embeddings",
                json={"model": Config.OLLAMA_EMBEDDING_MODEL, "prompt": text},
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

    def _embed_openai(self, text: str) -> list[float]:
        """
        通过 OpenAI 兼容 Embedding API 生成文本向量（Voyage/OpenAI/硅基流动等）。
        需配置 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL。
        """
        if not Config.EMBEDDING_API_KEY:
            print("警告: EMBEDDING_PROVIDER=openai 但未配置 EMBEDDING_API_KEY")
            return [0.0] * self.dim
        try:
            import requests

            url = Config.EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {Config.EMBEDDING_API_KEY}"},
                json={"model": Config.EMBEDDING_MODEL, "input": text},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            # OpenAI 兼容返回: {"data": [{"embedding": [...]}]}
            embedding = data["data"][0]["embedding"]
            if embedding:
                return embedding
            return [0.0] * self.dim
        except Exception as e:
            print(f"云端 Embedding 生成失败: {e}")
            return [0.0] * self.dim

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        """插入记忆节点，返回节点 ID"""
        with self._acquire() as db:
            # 基础 payload
            payload = {
                "type": node_data.get("type", "unknown"),
                "label": node_data.get("label", ""),
                "content": node_data.get("content", ""),
                "importance": node_data.get("importance", 0.5),
                "status": "active",
                "created_at": None,
            }
            # 合并额外字段（如 character_name, user_name）
            extra_fields = {
                k: v
                for k, v in node_data.items()
                if k not in payload and k not in ("label", "content")
            }
            payload.update(extra_fields)

            # 敏感信息扫描：拼接所有字符串字段值扫描，命中则拒绝入库
            scan_text = " ".join(
                str(v) for v in payload.values() if isinstance(v, str)
            )
            secret_hits = scan_secret(scan_text)
            if secret_hits:
                raise SecretScanError(secret_hits)

            node_id = db.insert(embedding, payload)
            db.create_index("type")
            db.create_index("importance")
            db.create_index("status")
            db.create_index("character_name")  # 新增索引，加速过滤
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
        apply_decay: bool = True,
        block: str = "",
    ) -> list[dict[str, Any]]:
        """使用 triviumdb 原生 search API 检索，保留时间衰减与图谱扩散

        - 向量检索：db.search(query, top_k=cand_k, min_score=0.0, expand_depth=0)
          多取候选（top_k×3，至少 10），不设分数下限，由衰减/扩散层决定最终排序
        - expand_depth=0：纯向量 Top-K，不扩散（与旧版一致）；>0 时沿全部边
          扩散邻居（每跳 score × 0.8），去重后重新按 score 排序取 Top-K
        - 时间衰减：非 kb_chunk 节点 有效分 = 余弦分 × importance ×
          MEMORY_DECAY_FACTOR^(距创建天数/30)（只影响排序，不改存储；
          factor=1.0 关闭衰减；apply_decay=False 供 mem_ingest 内部阈值判断保持原样）
        """
        # 1. 原生向量检索拿候选（0.7.6 search API；min_score=0.0 不过滤，
        #    让衰减/扩散层决定最终排序；多取一些候选供后面重排）
        cand_k = max(top_k * 3, 10)
        scored = []
        db = None
        try:
            db = self._acquire()
            hits = db.search(
                query_embedding,
                top_k=cand_k,
                min_score=0.0,
                expand_depth=0,
            )
            # 2. 原生 hits → (score, node) 候选（SearchHit: .id / .payload / .score）
            scored = [
                (float(hit.score), {"id": hit.id, "payload": hit.payload})
                for hit in (hits or [])
            ]
        except Exception as e:
            # 零向量/空库等异常场景兜底：不崩，返回空列表
            logger.warning(f"triviumdb 原生 search 失败，返回空结果: {e}")
            return []
        finally:
            # 关键：0.7.6 的 with 块退出不会释放锁（__exit__ 是空操作），
            # 必须显式 close()，否则后续 _expand_neighbors 再 open 会报
            # "Database locked ... already opened by another process"
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

        # 3. 时间衰减（记忆生命周期）：kb_chunk 知识块不衰减
        if apply_decay:
            decay = getattr(Config, "MEMORY_DECAY_FACTOR", 1.0)
            if decay != 1.0:
                now = time.time()
                for i, (score, node) in enumerate(scored):
                    payload = node.get("payload", {}) or {}
                    if payload.get("type") == "kb_chunk":
                        continue
                    days = _days_since_created(payload.get("created_at"), now)
                    importance = _to_float(payload.get("importance"), 0.5)
                    scored[i] = (score * importance * (decay ** (days / 30.0)), node)

        # 4. 排序、截断
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        # 5. 图谱扩散：expand_depth>0 时沿边把邻居并入候选集（block 非空时只走同区块边）
        if expand_depth > 0:
            top = self._expand_neighbors(top, depth=expand_depth, block=block)

        return [
            {
                "id": node.get("id"),
                "score": score,
                "payload": node.get("payload", {}),
            }
            for score, node in top[:top_k]
        ]

    def _expand_neighbors(self, top: list, depth: int,
                          max_edges_per_node: int | None = None,
                          min_edge_weight: float | None = None,
                          block: str = "") -> list:
        """沿边 BFS 扩散邻居，去重后按 score 重排。

        精馏（2026-08-25 主人挑战 #2 + 分区块）：
        - 每节点只扩散按 weight 降序的最强 max_edges_per_node 条边（默认 20），
          防高节点（如 500 邻居）全量扩散撑爆计算/污染结果；
        - 扩散分数 = 当前分数 × 边 weight（替代旧固定 ×0.8），强边自然靠前；
        - min_edge_weight 可额外过滤弱边（默认 0.0 不启用）；
        - block 非空时只扩散 target 节点 domain 匹配区块的边（图谱分区块，防跨域污染）。
        """
        max_edges = (max_edges_per_node if max_edges_per_node is not None
                     else EXPAND_MAX_EDGES_PER_NODE)
        min_w = (min_edge_weight if min_edge_weight is not None
                 else EXPAND_MIN_EDGE_WEIGHT)
        merged = {node.get("id"): (score, node) for score, node in top}
        seen = set(merged.keys())
        frontier = [(score, node.get("id"), 0) for score, node in top]
        with self._acquire() as db:
            while frontier:
                score, nid, hop = frontier.pop(0)
                if hop >= depth:
                    continue
                edges = list(db.get_edges(nid))
                # 精馏 1：过滤弱边 + 按 weight 降序取最强 max_edges 条
                edges = [e for e in edges
                         if float(getattr(e, "weight", 1.0) or 1.0) >= min_w]
                edges.sort(key=lambda e: float(getattr(e, "weight", 1.0) or 1.0),
                           reverse=True)
                edges = edges[:max_edges]
                for edge in edges:
                    w = float(getattr(edge, "weight", 1.0) or 1.0)
                    nb = edge.target_id
                    if nb in seen:
                        continue
                    # 精馏 3（分区块）：只扩散 target 节点 domain 匹配区块的边
                    if block:
                        nb_node = db.get(nb)
                        if not nb_node:
                            continue
                        nb_payload = nb_node.payload or {}
                        nb_domain = (nb_payload.get("character_name", "")
                                     or nb_payload.get("domain", "") or "general")
                        if not domain_in_block(nb_domain, block):
                            continue
                    else:
                        nb_node = db.get(nb)
                        if not nb_node:
                            continue
                    seen.add(nb)
                    # 精馏 2：扩散分数用边 weight（替代旧固定 ×0.8）
                    nb_score = score * w
                    merged[nb] = (nb_score, {
                        "id": nb_node.id,
                        "payload": nb_node.payload,
                        "num_edges": nb_node.num_edges,
                        "vector": nb_node.vector,
                    })
                    frontier.append((nb_score, nb, hop + 1))
        # 扩散后按 score 降序重排
        return sorted(merged.values(), key=lambda x: x[0], reverse=True)

    def get_edges(self, node_id: int) -> list:
        """获取节点的出边列表（Edge 对象，含 label/target_id/weight）"""
        with self._acquire() as db:
            return db.get_edges(node_id)

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

    def delete_node(self, node_id: int) -> None:
        """删除节点（同时删除所有关联边）"""
        with self._acquire() as db:
            db.delete(node_id)
        logger.info(f"已删除节点 ID={node_id}")

    def update_payload(self, node_id: int, new_payload: dict[str, Any]) -> None:
        """更新节点的 payload 元数据"""
        with self._acquire() as db:
            db.update_payload(id=node_id, payload=new_payload)  # 改为关键字参数
        logger.info(f"已更新节点 ID={node_id} 的 payload")

    def update_vector(self, node_id: int, new_vector: list[float]) -> None:
        """更新节点的向量（维度必须一致）"""
        with self._acquire() as db:
            db.update_vector(vector=new_vector, id=node_id)  # 改为关键字参数
        logger.info(f"已更新节点 ID={node_id} 的向量")

    def _get_all_node_ids(self) -> list[int]:
        """获取数据库中所有节点的 ID 列表（供内部使用）"""
        # type: ignore
        with self._acquire() as db:
            return db.all_node_ids()

    def iter_payloads(self):
        """遍历所有节点，yield (node_id, payload)。

        替代外部重复的 `_get_all_node_ids + get_node + if not node` 样板
        （P0 重构，2026-08-27）。行为与原模式逐字节一致：
        节点缺失跳过；payload 统一归一为 dict。
        """
        for nid in self._get_all_node_ids():
            node = self.get_node(nid)
            if not node:
                continue
            yield nid, node.get("payload", {}) or {}


def _days_since_created(created_at: Any, now: float) -> float:
    """距创建天数；created_at 缺失/非法按 0 天（不衰减）"""
    try:
        ts = float(created_at)
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    return max(0.0, (now - ts) / 86400.0)


def _to_float(value: Any, default: float) -> float:
    """安全转 float，失败用默认值（payload 字段可能为字符串）"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
