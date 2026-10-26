"""向量空间指纹守卫（2026-10-26 T2）
====================================

解决的问题：库里的向量是「哪个空间」算出来的？换 provider / 模型 / 维度 /
pooling 后旧向量与新向量混用，会让检索排序静默漂移（即便语义近似，如
ollama 与 local 实测 cos 0.9994，混用仍会漂移）。

做法：库内存一条运维记录（type=record, source=embedding-fingerprint），
记录当前向量空间的指纹；启动自检 / doctor / reindex 开跑前比对，给出
可执行修复（reindex --force），不在写入路径拦截（指纹不一致只告警 +
一键修复，不让整个服务不可用）。

space_id 不含引擎版本：每次 pip install --upgrade 都会变指纹 → 告警
变成狼来了。引擎版本单独记在 engine 字段（用于排查），比对时按不同
严重度处理（compare 的 soft_warnings，不算失败）。

向量占位选**全 0**（而非真实 embedding）：这条 record 是运维元数据，
不参与语义检索（全零向量 cos=0 不会进 top-k）；若用真实 embedding，
write_fingerprint 自身会触发 embed——reindex 收尾时 embed 恰好不可用
就得降级，白增一条失败路径。占位让指纹写入零外部依赖。
"""

import hashlib
import logging
import os
from datetime import datetime

from config import Config

logger = logging.getLogger(__name__)

# record 节点的唯一标识（payload.source 字段；检索/统计按它过滤）
FINGERPRINT_SOURCE = "embedding-fingerprint"

# 可直接复制的修复命令（compare 的 hint 必须包含它）
FIX_CMD = "python scripts/palimpsest_cli.py reindex --force"


def compute_space_id(provider: str, model: str, dim: int, pooling: str) -> str:
    """向量空间指纹 = sha256(provider|model|dim|pooling)[:16]。

    同输入必同输出；任一构成字段变化都会产生不同 space_id。
    """
    raw = f"{provider}|{model}|{dim}|{pooling}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def detect_engine_version(store) -> str:
    """探测 embedding 引擎版本（仅用于排查展示，不参与 space_id）。

    local → llama_cpp:<llama-cpp-python 版本>；ollama → ollama:<服务版本>；
    其余 provider（openai 云端）无本地引擎概念 → "unknown"。
    任何取不到的情形一律返回 "unknown"（compare 对它不告警，避免噪音）。
    """
    provider = getattr(store, "provider", "") or ""
    try:
        if provider == "local":
            import importlib.metadata

            ver = importlib.metadata.version("llama-cpp-python")
            return f"llama_cpp:{ver}" if ver else "unknown"
        if provider == "ollama":
            import requests

            url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/") + "/api/version"
            resp = requests.get(url, timeout=2)
            resp.raise_for_status()
            ver = str((resp.json() or {}).get("version") or "").strip()
            return f"ollama:{ver}" if ver else "unknown"
    except Exception as e:  # noqa: BLE001 —— 版本探测失败只降级为 unknown，绝不抛出
        logger.debug("引擎版本探测失败（provider=%s）: %s", provider, e)
    return "unknown"


def current_fingerprint(store) -> dict:
    """按 Config + store.dim 计算当前的向量空间指纹（含 engine 版本）。"""
    provider = getattr(store, "provider", None) or getattr(Config, "EMBEDDING_PROVIDER", "local") or "local"
    if provider == "local":
        # 只取文件名：模型缓存目录搬家（EMBEDDING_MODEL_PATH 换盘）不该误判成换了空间
        model = os.path.basename(str(Config.EMBEDDING_MODEL_FILE))
        pooling = "last"  # decoder-only 因果 LM 只能用 LAST pooling（见 trivium_store._embed_local）
    elif provider == "openai":
        model = str(Config.EMBEDDING_MODEL)
        pooling = "n/a"
    else:  # ollama
        model = str(Config.OLLAMA_EMBEDDING_MODEL)
        pooling = "n/a"
    dim = int(store.dim)
    return {
        "space_id": compute_space_id(provider, model, dim, pooling),
        "provider": provider,
        "model": model,
        "dim": dim,
        "pooling": pooling,
        "engine": detect_engine_version(store),
    }


def read_fingerprint(store) -> dict | None:
    """读取库内的指纹 record；不存在返回 None。

    全库遍历定位（与 doctor 第 7 项同口径）：source 是自由 payload 字段，
    triviumdb 等值索引无法表达，且本检查只在启动/体检/reindex 收尾触发，
    不在热路径。
    """
    for _nid, payload in store.iter_payloads():
        if isinstance(payload, dict) and payload.get("source") == FINGERPRINT_SOURCE:
            return dict(payload)
    return None


def write_fingerprint(store, fp: dict) -> None:
    """把指纹写成库内一条运维 record（upsert：已有则更新，保证至多一条）。

    向量用全 0 占位（理由见模块 docstring）；写入失败向上抛，由调用方
    决定降级方式（reindex 收尾会 try/except，不让指纹写失败拖垮重嵌结果）。
    """
    now = datetime.now().astimezone()
    payload = {
        "type": "record",
        "source": FINGERPRINT_SOURCE,
        "label": FINGERPRINT_SOURCE,
        "domain": "system",
        "content": (
            f"向量空间指纹：provider={fp['provider']} model={fp['model']} dim={fp['dim']} "
            f"pooling={fp['pooling']} space_id={fp['space_id']} （写入于 {now:%Y-%m-%d %H:%M}）"
        ),
        "space_id": fp["space_id"],
        "provider": fp["provider"],
        "model": fp["model"],
        "dim": fp["dim"],
        "pooling": fp["pooling"],
        "engine": fp.get("engine", "unknown"),
        "recorded_at": now.isoformat(timespec="seconds"),
    }

    existing = [
        nid for nid, p in store.iter_payloads() if isinstance(p, dict) and p.get("source") == FINGERPRINT_SOURCE
    ]
    if existing:
        # update_payload 是合并语义：payload 键集合固定，旧值全部被覆盖
        store.update_payload(existing[0], payload)
        for extra in existing[1:]:  # 历史残留清到只留一条（不变量：至多一条）
            try:
                store.delete_node(extra)
            except Exception as e:  # noqa: BLE001 —— 残留清理失败仅告警，不影响主记录
                logger.warning("清理多余指纹记录 ID=%s 失败: %s", extra, e)
    else:
        store.insert_node(payload, [0.0] * int(store.dim))


def compare(stored: dict | None, current: dict) -> dict:
    """比对库内指纹与当前指纹，返回 {"ok", "reason", "hint", "soft_warnings"}。

    分级判据：
      - stored 为空        → ok=False（存量库升级或从未重算过），hint 给一键修复
      - space_id 相同      → ok=True；engine 不同（且双方都非 unknown）只记软告警
                             （引擎升级不改变量空间，不算失败，狼来了要防）
      - space_id 不同      → ok=False，reason 逐字段列出 provider/model/dim/pooling 差异
    """
    if not stored:
        return {
            "ok": False,
            "reason": "库内未记录向量空间指纹（存量库升级或从未重算过）",
            "hint": f"执行一次全量重算并写入指纹: {FIX_CMD}",
            "soft_warnings": [],
        }

    if stored.get("space_id") == current.get("space_id"):
        old_engine = stored.get("engine")
        new_engine = current.get("engine")
        both_known = old_engine not in (None, "unknown") and new_engine not in (None, "unknown")
        if old_engine != new_engine and both_known:
            return {
                "ok": True,
                "reason": "",
                "hint": "",
                "soft_warnings": [
                    f"引擎版本已从 {old_engine} 变为 {new_engine}（向量空间未变）。"
                    f"若检索质量变差，可执行 {FIX_CMD} 重算"
                ],
            }
        return {"ok": True, "reason": "", "hint": "", "soft_warnings": []}

    diffs = [
        f"{field}: {stored.get(field)!r} → {current.get(field)!r}"
        for field in ("provider", "model", "dim", "pooling")
        if stored.get(field) != current.get(field)
    ]
    reason = (
        f"向量空间不一致（{'; '.join(diffs)}）: "
        f"库内 space_id={stored.get('space_id')}，当前 space_id={current.get('space_id')}"
    )
    return {
        "ok": False,
        "reason": reason,
        "hint": f"接受当前向量空间并全量重算: {FIX_CMD}",
        "soft_warnings": [],
    }
