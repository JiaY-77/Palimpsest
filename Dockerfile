# syntax=docker/dockerfile:1
# Palimpsest 自托管镜像（单实例）。用法、公网安全与运维边界见 docs/DEPLOY.md。

FROM python:3.12-slim

# 预置档位：1 = 构建时把 GGUF 下到 /app/models（镜像 +610MB，起容器即有模型）；
# 0 = 跳过下载（镜像最小，模型改为运行时下载或挂卷）。
# 本 ARG 同时是缓存键：改档位会重建模型层。
ARG PRELOAD_EMBEDDING_MODEL=1

# 下载基准，与 config.py 的同名常量保持一致（改动时两边同步，勿漂移）。
# 基础镜像必须 <3.13：triviumdb==0.9.0b2 的 requires_python 是 <3.13,>=3.9。
ARG EMBEDDING_MODEL_REPO=Qwen/Qwen3-Embedding-0.6B-GGUF
ARG EMBEDDING_MODEL_FILE=Qwen3-Embedding-0.6B-Q8_0.gguf
ARG EMBEDDING_MODEL_SHA256=06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439
ARG EMBEDDING_MODEL_SIZE=639150592

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---- 第 1 层：依赖（requirements 与 llama-cpp-python 独立索引）----
# llama-cpp-python 在 PyPI 无预编译 wheel，必须走官方 CPU wheel 索引（与 docs/INSTALL.md 同参数）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "llama-cpp-python>=0.3.36,<0.4" \
       --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

# ---- 第 2 层：源码（.dockerignore 已排除 .git/data/models/venv/.env 等）----
COPY . .

# ---- 第 3 层：非 root 用户；data/（库文件组）与 models/（GGUF）归它 ----
# UID/GID 固定 10001：宿主机上对挂卷目录 chown 10001:10001 即可授权（见 docs/DEPLOY.md）。
RUN groupadd -g 10001 palimpsest \
    && useradd -u 10001 -g palimpsest -s /usr/sbin/nologin palimpsest \
    && mkdir -p /app/data /app/models \
    && chown -R palimpsest:palimpsest /app/data /app/models

# ---- 第 4 层：模型预置（PRELOAD_EMBEDDING_MODEL=0 时整层跳过）----
# 复用运行时下载逻辑（config.py 基准、ModelScope→HuggingFace 回退、sha256+字节数校验、坏文件不留）。
# BuildKit 缓存挂载跨构建保留 GGUF：增量重建（git pull 后 compose build）命中缓存则不再下 610MB。
RUN --mount=type=cache,id=palimpsest-embedding-model,target=/var/cache/palimpsest-model \
    if [ "$PRELOAD_EMBEDDING_MODEL" = "1" ]; then \
      EMBEDDING_MODELS_DIR=/var/cache/palimpsest-model \
        python -c "from config import Config; from core.model_fetch import _target_path, download_model; download_model(_target_path(), source=Config.EMBEDDING_DOWNLOAD_SOURCE)" ; \
      cp -n "/var/cache/palimpsest-model/$EMBEDDING_MODEL_FILE" /app/models/ ; \
      chown -R palimpsest:palimpsest /app/models ; \
    fi

# ---- 运行配置 ----
USER palimpsest
EXPOSE 8090

# uvicorn 以 exec 形式成为 PID 1，收 SIGTERM 优雅关闭；强杀才会留 WAL 待恢复。
STOPSIGNAL SIGTERM

# 与 compose 同参数：探 /health（始终 200，故 retry 判死的只有「进程没在听」这一种情况）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=6 \
    CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4); assert json.load(r)['status']=='ok'"]

# REST 是唯一写者（单进程单 worker，禁止 --workers N）；绑 0.0.0.0 否则容器外访问不到。
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
