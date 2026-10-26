"""本地 GGUF embedding 模型获取：路径解析 + 断点续传下载 + 完整性校验。

设计要点：
- 下载源顺序（2026-10-26 实测）：ModelScope 优先（国内直连快），HuggingFace 兜底；
  **不要使用 hf-mirror.com**——它对 Qwen/Qwen3-Embedding-0.6B-GGUF 返回 308
  永久重定向回 huggingface.co，等于白绕一圈还多一次 DNS/握手。
- 断点续传：.part 文件已有时带 Range 头续传；服务端不支持 Range（回 200）则从头写。
- 原子落盘：全程写 .part，大小 + sha256 都校验通过才 os.replace 到正式名；
  校验失败立即删 .part——留坏文件会让下次「文件在但内容坏」的误判更难排查。
"""

import hashlib
import logging
import os
import sys
from urllib.parse import quote

import requests

from config import PROJECT_ROOT, Config
from core.trivium_store import EmbeddingUnavailableError

logger = logging.getLogger(__name__)

# 进度打印节奏：TTY 下每 ~4MB 刷新一次；非 TTY 每 25% 记一条日志
_PROGRESS_STEP_BYTES = 4 * 1024 * 1024
# requests 超时：(连接, 读取)。读 60s：CDN 单包慢不等于死链。
_DOWNLOAD_TIMEOUT = (10, 60)


def models_dir() -> str:
    """模型缓存目录：配置了 EMBEDDING_MODELS_DIR 用它，否则 <项目根>/models。"""
    configured = Config.EMBEDDING_MODELS_DIR
    if configured:
        return configured
    return os.path.join(PROJECT_ROOT, "models")


def _target_path() -> str:
    return os.path.join(models_dir(), Config.EMBEDDING_MODEL_FILE)


def _sha256_of(path: str) -> str:
    """流式读 4MB 块算 sha256（不整文件入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_PROGRESS_STEP_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_urls(source: str) -> list[str]:
    """按 source 生成候选 URL 列表（顺序即尝试顺序）。"""
    repo, file = Config.EMBEDDING_MODEL_REPO, Config.EMBEDDING_MODEL_FILE
    ms = f"https://modelscope.cn/models/{quote(repo, safe='/')}/resolve/master/{quote(file)}"
    hf = f"https://huggingface.co/{quote(repo, safe='/')}/resolve/main/{quote(file)}"
    if source == "modelscope":
        return [ms]
    if source == "huggingface":
        return [hf]
    return [ms, hf]  # auto：ModelScope 优先


def _check_size_and_hash(path: str) -> None:
    """大小 + sha256 双校验；不符抛 EmbeddingUnavailableError（含期望/实际值）。"""
    actual_size = os.path.getsize(path)
    if actual_size != Config.EMBEDDING_MODEL_SIZE:
        raise EmbeddingUnavailableError(
            f"embedding 模型文件大小不符：{path}\n期望 {Config.EMBEDDING_MODEL_SIZE} B，实际 {actual_size} B。"
        )
    digest = _sha256_of(path)
    if digest != Config.EMBEDDING_MODEL_SHA256:
        raise EmbeddingUnavailableError(
            f"embedding 模型 sha256 校验失败：{path}\n期望 {Config.EMBEDDING_MODEL_SHA256}，实际 {digest}。"
        )


def _discard(path: str) -> None:
    """删除损坏/残缺文件；删不掉记警告（不让清理失败掩盖主错误）。"""
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning("删除损坏文件失败（请手动清理）：%s（%s）", path, exc)


def _report_progress(done: int, progress: bool, _state: dict | None = None) -> None:
    """进度反馈：TTY 用 \\r 覆盖同行每 ~4MB 一次；非 TTY 每 25% 一条日志。"""
    if _state is None:
        _state = {}
    total = Config.EMBEDDING_MODEL_SIZE
    pct = min(int(done * 100 / max(total, 1)), 100)
    if progress and sys.stderr.isatty():
        if done >= _state.get("next", 0) or done >= total:
            _state["next"] = (done // _PROGRESS_STEP_BYTES + 1) * _PROGRESS_STEP_BYTES
            sys.stderr.write(f"\r下载中 {pct:5.1f}%（{done:,} / {total:,} B）")
            if done >= total:
                sys.stderr.write("\n")
        return
    # 非 TTY：按 25% 里程碑记日志（含 0% 与 100%）
    milestone = _state.get("milestone", 0)
    is_last = done >= total
    at_first = pct < 25 and milestone == 0
    if pct >= milestone and (is_last or at_first):
        logger.info("embedding 模型下载进度：%d%%（%d / %d B）", pct, done, total)
        if is_last:
            _state["milestone"] = 101
        else:
            _state["milestone"] = (pct // 25) * 25 + 25


def _download_one(url: str, part: str, progress: bool) -> bool:
    """从单个 URL 下载到 .part（含断点续传）。成功返回 True，失败返回 False。"""
    resume_from = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    if resume_from:
        logger.info("检测到 .part 文件（%d B），尝试断点续传", resume_from)
    state: dict = {}  # 进度打印的跨 chunk 状态（本次下载内有效）
    try:
        with requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT, headers=headers, allow_redirects=True) as resp:
            resp.raise_for_status()
            # 服务端跳过 Range 回 200 → 从头写；206 → 追加续传
            if resume_from and resp.status_code == 206:
                mode, done = "ab", resume_from
            else:
                mode, done = "wb", 0
            with open(part, mode) as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    _report_progress(done, progress, state)
        return True
    except (requests.RequestException, OSError) as exc:
        # 连接失败 / 4xx / 5xx / 磁盘写错：记日志换下一个源
        logger.warning("下载源失败 %s：%s", url, exc)
        return False


def download_model(dest: str, *, source: str = "auto", progress: bool = True) -> str:
    """断点续传下载 + sha256 校验 + 原子落盘（先写 .part，校验通过后 os.replace）。

    多源依次尝试（source="auto" 时 ModelScope → HuggingFace），第一个成功即止；
    全部失败抛 EmbeddingUnavailableError，文案列出尝试过的源。
    """
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError as exc:
        raise EmbeddingUnavailableError(f"无法创建模型缓存目录 {os.path.dirname(dest)}：{exc}") from exc

    part = dest + ".part"
    tried: list[str] = []
    for url in _download_urls(source):
        tried.append(url)
        if _download_one(url, part, progress=progress):
            try:
                _check_size_and_hash(part)
            except EmbeddingUnavailableError:
                _discard(part)  # 坏文件绝不留：清掉再抛
                raise
            try:
                os.replace(part, dest)
            except OSError as exc:
                _discard(part)
                raise EmbeddingUnavailableError(f"模型文件落盘失败（{part} → {dest}）：{exc}") from exc
            logger.info("embedding 模型就绪：%s", dest)
            return dest
        # 单源失败：清掉残缺 .part 再换下一个源（防止把 A 源的残件续传给 B 源）
        if os.path.isfile(part):
            _discard(part)
    raise EmbeddingUnavailableError(
        "embedding 模型下载失败，尝试过以下源（均失败）：\n  "
        + "\n  ".join(tried)
        + "\n请检查网络或设 EMBEDDING_DOWNLOAD_SOURCE 换源；"
        "也可手动下载后设 EMBEDDING_MODEL_PATH 指向文件。"
    )


def resolve_model_path() -> str:
    """返回可用的本地 GGUF 绝对路径；缺失时按配置决定下载或报错。"""
    configured = Config.EMBEDDING_MODEL_PATH or ""
    if configured:
        if os.path.isfile(configured):
            return configured
        raise EmbeddingUnavailableError(f"EMBEDDING_MODEL_PATH 指向的模型文件不存在：{configured}")

    dest = _target_path()
    if os.path.isfile(dest) and os.path.getsize(dest) == Config.EMBEDDING_MODEL_SIZE:
        return dest

    if Config.EMBEDDING_AUTO_DOWNLOAD:
        return download_model(dest, source=Config.EMBEDDING_DOWNLOAD_SOURCE)

    raise EmbeddingUnavailableError(
        f"本地 embedding 模型不存在：{dest}。请任选其一：\n"
        "  1. 设 EMBEDDING_MODEL_PATH 为本地 GGUF 模型的绝对路径；\n"
        "  2. 设 EMBEDDING_AUTO_DOWNLOAD=1 允许自动下载。"
    )
