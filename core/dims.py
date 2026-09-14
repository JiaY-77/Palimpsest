"""向量维度探测。自 scripts/reindex.py 下沉到 core，消除 core→scripts 反向依赖。"""

import contextlib


def get_db_dim(store):
    """读取库实际维度（从 DB 的 storage_info）。

    返回 (dim: int, error: str | None)。
    """
    import triviumdb
    db = None
    try:
        db = triviumdb.TriviumDB(store.db_path, dim=store.dim)
        info = db.storage_info()
        return info.get("dim"), None
    except Exception as e:  # noqa: BLE001 —— 读维度失败返回错误信息交由体检汇总提示
        return None, f"无法读取数据库维度: {e}"
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()