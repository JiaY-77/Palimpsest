"""向量维度探测。自 scripts/reindex.py 下沉到 core，消除 core→scripts 反向依赖。"""

import contextlib
import re

# 异常信息里拿不到真实维度时的兜底候选（按常见 embedding 维度降序）。
_COMMON_DIMS = (1024, 1536, 768, 512, 3072, 2560, 2048, 384, 256, 128, 64)


def _probe(path: str, dim: int):
    """用指定维度试开库（只探测，不触发 QuIVer 构图）。"""
    import triviumdb

    return triviumdb.TriviumDB(path, dim=dim, auto_build_quiver=False)


def get_db_dim(store):
    """读取库实际维度。

    返回 (dim: int, error: str | None)。

    TriviumDB 0.9.0 起在构造时严格校验 ``dim`` 与库内实际维度（旧版忽略入参、
    以库内 storage_info() 为准），所以不能再用「随便传 store.dim 打开再读
    storage_info」的老姿势——那会在维度不符的库上直接抛错。改为两段式：
    先用 store.dim 试开；失败时从异常信息里取「期望 N」重试一次；仍拿不到
    真实维度就按常见维度逐个试，全部失败才返回错误。
    """
    from core.trivium_store import try_acquire_existing

    # 单例进程内优先复用共享连接（issue #167）：本进程已持有该库时，裸开
    # 第二条连接必被连接级排他拒绝。尚无共享连接时才裸开（独立 CLI 场景不变）。
    shared = None
    with contextlib.suppress(Exception):
        shared = try_acquire_existing(store.db_path)
    if shared is not None:
        try:
            return shared.storage_info().get("dim"), None
        except Exception as e:  # noqa: BLE001 —— 读维度失败返回错误信息交由体检汇总提示
            return None, f"无法读取数据库维度: {e}"

    candidates: list[int] = []
    if getattr(store, "dim", None):
        candidates.append(int(store.dim))
    errors: list[str] = []

    idx = 0
    while idx < len(candidates):
        dim = candidates[idx]
        idx += 1
        db = None
        try:
            db = _probe(store.db_path, dim)
            return db.storage_info().get("dim"), None
        except Exception as e:  # noqa: BLE001 —— 维度不符时按异常里的期望值重试
            errors.append(str(e))
            m = re.search(r"期望\s*(\d+)", str(e))
            if m:
                want = int(m.group(1))
                if want not in candidates:
                    candidates.append(want)
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    for dim in _COMMON_DIMS:
        if dim in candidates:
            continue
        db = None
        try:
            db = _probe(store.db_path, dim)
            return db.storage_info().get("dim"), None
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    return None, f"无法读取数据库维度: {errors[0] if errors else '未知错误'}"
