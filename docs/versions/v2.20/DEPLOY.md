# 自托管部署（Docker）

> 目标：clone 完到跑起来，一条 `docker compose up`。
> 源码安装路径见 [`INSTALL.md`](INSTALL.md)。**本项目没有发布到 PyPI**，
> `pip install palimpsest` 装到的是另一个无关的包；本镜像从源码构建，没有这个问题。

---

## 前置要求

| 项 | 要求 | 说明 |
|---|---|---|
| Docker / Compose | Docker 24+，**docker compose v2**（`docker compose version` 能跑） | compose 文件用了 `deploy.resources.limits`，老版 docker-compose（v1，2023 年已停维护）会静默忽略内存限制 |
| 内存 | **推荐 ≥ 4GB**；2GB 无余量 | 实测（内置 embedding，`n_batch=512`，v2.18.0 生产进程）：空载 WorkingSet 581MB / Private 1.9GB；首次 `mem_search` 触发模型懒加载后 WorkingSet 904MB；常驻 1.3GB；峰值 WorkingSet **1.6GB**。Private（进程承诺内存）常态约 **1.9GB**——2GB 机器即使不 OOM 也没有余量，紧盯的是提交量不是驻留量。把 `EMBEDDING_LOCAL_N_BATCH` 调到 2048 峰值约 2.5GB（不要调大：它是分块池化的块长上限，调大只涨内存、不涨质量；调小也不建议，见下方边界第 3 条） |
| 磁盘 | ≥ 10GB | 镜像 ~1.5GB + GGUF 610MB + 首次建库的索引文件余量 |
| 端口 | 8090 空闲 | 被占用改 compose 里的 `ports` 映射（如 `"18090:8090"`） |

**注意：TriviumDB 要求 Python <3.13** —— 镜像已锁 `python:3.12-slim`，自行改基础镜像时勿用 3.13/3.14。

---

## 三步启动

```bash
# 1. clone
git clone https://github.com/JiaY-77/Palimpsest.git && cd Palimpsest

# 2. 配置（公网必填 PALIMPSEST_API_KEY；纯本机试跑可留空）
cp .env.example .env

# 3. 构建并启动（首次构建预置 610MB 向量模型，带 sha256 校验）
docker compose up -d --build

# 验证
curl -s http://127.0.0.1:8090/health
# {"status":"ok","version":"...","db":"ok"}     # 新库首写之前 db 可能是 "degraded"，属正常
```

容器卫生检查：`docker compose ps`（health 显示 healthy）、`docker compose logs -f palimpsest`。

接入 agent：MCP 指向 `http://127.0.0.1:8090/mcp/`，与源码部署完全相同（见 README「接到你的 agent」）。

---

## 公网安全

**必须做两件事：设 API key + 走 TLS 反代。**

1. 生成强 key 并写进 `.env`：

```bash
openssl rand -hex 32
# .env 里：
#   PALIMPSEST_API_KEY=上面生成的值
```

2. 客户端怎么带 key（两种等价）：

```bash
curl -H "Authorization: Bearer <key>" https://你的域名/mem/search -X POST ...
curl -H "X-API-Key: <key>" https://你的域名/mem/search -X POST ...
```

3. 免鉴权路径只有 `/` 和 `/health`（存活探针不带 key 也要能探，这是刻意的）；
   `/docs` 等一切业务与调试路径都受保护。

> API key 只做校验，不做传输加密。明文 HTTP 传公网等于没有 key——必须配 TLS（下一节）。
> key 不验证大小写强度；泄露了就换一个重启（记忆都在卷里，key 与数据无关）。

---

## 反向代理 + TLS

### Caddy（自动签发证书，最省事）

```caddyfile
# /etc/caddy/Caddyfile
memory.example.com {
    reverse_proxy 127.0.0.1:8090
}
```

```bash
docker compose up -d   # 或 systemctl reload caddy
```

### nginx 对照（证书手动放）

```nginx
server {
    listen 443 ssl http2;
    server_name memory.example.com;

    ssl_certificate     /etc/letsencrypt/live/memory.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/memory.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

反代不应加缓存/剪请求头之外的东西；超时给足 120s（首启建索引、报告生成等都可能上百毫秒到秒级，不是超时的坏事，只是别默认 5s）。

---

## 边界（不美化，选型前先读完）

1. **单实例。** 库文件由 triviumdb 以独占写模式打开：同一份数据目录挂两个容器（或容器内多 worker）会直接报 `Database locked`、REST 返 503——不会静默写坏，但也**不支持水平扩展**。要「多机」就各机各库，不要共享一个卷。
2. **必须持久卷。** `./data:/app/data` 映射了库文件组：`.db` + `.vec` / `.gidx` / `.pidx` / `.pld.<generation>` / `.wal` / `.flush_ok`（可能还有 `.lock`）。**删卷 = 删记忆**；备份必须整组拷（见下节），只拷 `.db` 会得到「读得动、写不动」的残废快照。
3. **内存不是可选项，2GB 装不下（2026-10-29 实测）。** 进程内 embedding 是**懒加载**的：空载 WorkingSet 581MB，第一次检索才把 639MB 的 Q8 模型读进来，之后常驻约 1.3GB、峰值 1.6GB，且空闲不回落（实测空闲 45 秒无释放）。而真正卡机器的是 **Private（进程承诺内存）常态约 1.9GB**——2GB 机器连提交量都占满，OS 一紧就换页或 OOM，所以安全线是 4GB。真要在 2GB 上跑，**唯一推荐的路是把 embedding 换成进程外后端**（宿主机 llama-server / Ollama / 云端 key；换代要重嵌全库，且该配置下的实测数字尚未采集，不要按本地数值推算）。**不建议靠调小 `EMBEDDING_LOCAL_N_BATCH` 硬挤**：它同时是分块池化的块长上限，调小意味着块长跟着变小、已有向量全部失效、必须整库重嵌（2026-10-30 实测：2900 节点按 128 token 重切，一小时仍未跑完），而 639MB 的模型本身是硬地板，省下来的只是计算批缓冲那一段——代价和收益不成比例。
4. **容器内嵌 embedding 默认走本地 CPU，GPU 加速需显式开启。** 容器内默认（`EMBEDDING_LOCAL_N_GPU_LAYERS=0`，镜像随附的 llama-cpp-python 是 CPU wheel）仍是纯 CPU。想要 GPU：宿主机装支持 GPU 的 llama-cpp-python wheel 并设 `EMBEDDING_LOCAL_N_GPU_LAYERS` > 0（此时 `llama_supports_gpu_offload()` 必须为 True，wheel 仍是 CPU-only 版本时会直接报错、启动自检也可能拦下，不会静默降级成纯 CPU 跑）。要快也可以走 `EMBEDDING_PROVIDER=ollama`（宿主机 Ollama）或云端 key，但换向量空间必须重嵌全库（见下「升级」）。

---

## 备份与恢复

**备份（在容器内执行，跑进卷里）：**

```bash
docker compose exec palimpsest python scripts/backup_db.py --keep 7
# 产出在卷内：./data/backups/backup_<时间戳>/（宿主机可直接看到）
```

脚本会拷**整文件组**（同 generation 的 `.db` / `.vec` / `.gidx` / `.pidx` / `.flush_ok` / `.pld.*` / `.wal`，并附带同目录的 `fts.db` 全文索引及其 `-wal` / `-shm` 侧车）并回读校验（含 FTS 行数比对：源 `fts.db` 与副本必须行数一致；源库没有 `fts.db` 则跳过说明，不算失败），校验不过报错退出。拿不准就多留几份。

**恢复：**

```bash
docker compose down                    # 停掉写者
rm -rf ./data.broken && mv ./data ./data.broken   # 保留坏现场，别直接删
mv ./data/backups/backup_<时间戳> ./data          # 整组放回：目录里就是 .db + 全部侧车文件 + fts.db
docker compose up -d
```

> 只换 `.db` 不还侧车 → 报「拒绝不完整的 .tdb/.vec generation」。报这个错就说明你不是恢复了整组。
> 想搬家：把整个卷目录（含 `backups/`）拷到新机器同路径即可，数据就是一个目录。

**恢复后校验：**

```bash
docker compose exec palimpsest python scripts/palimpsest_cli.py doctor
```

若 doctor 报「FTS 索引缺失 / 明显滞后」，重建索引（不是数据丢失，主库完好）：

```bash
docker compose exec palimpsest python scripts/check_fts_consistency.py --repair
```

---

## 升级流程

```bash
cd Palimpsest && git pull
docker compose build          # 模型层走 BuildKit 缓存，命中则不再下 610MB
docker compose up -d          # 换新镜像，数据卷不动
curl -s http://127.0.0.1:8090/health
```

注意事项：

- **换 embedding 模型 / provider = 换向量空间**，必须重嵌全库：库内有向量空间指纹，不一致时 `/doctor` 该项 FAIL 并直接给出修复命令（`reindex --force --yes`，经 REST 执行，无需停容器）。
- **triviumdb 版本变更**：`requirements.txt` 固定该版本是有原因的（见文件内注释——历史上有过「0.8.11 后写坏无法自救」的事故）。升级后先 `docker compose exec palimpsest python scripts/palimpsest_cli.py doctor` 全绿再放流量；**并确认旧的备份还在**（库格式变更可能不可回滚）。
- 版本号在镜像里显示为 `dev`（构建上下文不含 `.git`），`/health` 的 `version` 字段同理——功能不受影响。

---

## 故障排查

| 症状 | 原因 | 修法 |
|---|---|---|
| 首启后 embedding 下载慢 / 失败 | ModelScope 侧不稳或出口网络走海外 | `docker compose logs -f palimpsest` 看下载日志；`.env` 设 `EMBEDDING_DOWNLOAD_SOURCE=huggingface` 换源；或本地下载好 GGUF（sha256 必须等于 `06507c7b...` 全串，见 `config.py`）放进 `./models`，并设 `EMBEDDING_MODEL_PATH=/app/models/<文件名>`，同时 compose 改 `PRELOAD_EMBEDDING_MODEL: "0"`（跳过构建期下载） |
| 重启后报「拒绝不完整的 .tdb/.vec generation」（或 triviumdb 定义过的 `RecoveryRequiredError` / `generation mismatch` 一族） | 上一次进程被强杀（`docker kill` / OOM / 宿主断电），留了待恢复的 WAL 或 generation 不齐 | 先直接 `docker compose up -d` 再试一次——triviumdb 重启会自己做 WAL 恢复；仍报错才走上方「备份与恢复」，用最近一份**整组**备份回放。常态 `docker stop` 是 SIGTERM 优雅关闭，不该走到这一步；若是 OOM，回看内存边界并调小 `EMBEDDING_LOCAL_N_BATCH` |
| healthcheck 一直 unhealthy | ① 端口没起来（看 `docker compose logs`）；② 镜像内监听地址被改过；③ start_period 之前本来就允许失败，超 180s 仍失败才是真问题 | `docker exec palimpsest python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/health').status)"` 在容器内自探；8090 被宿主占用则改 `ports` 左侧端口 |
| 设了 `PALIMPSEST_API_KEY`，客户端 401 | 带头方式不对（Bearer 或 X-API-Key 之外的写法） / key 尾部多了空格引号 / 走的路径不在免鉴权白名单 | `curl -H "Authorization: Bearer <key>" http://…/` 应回 200；`/` 与 `/health` 免 key 是设计行为；`.dockerignore` 排除了 `.env`，改 key 必须 `docker compose up -d` 重建容器才生效（`restart` 不会重读 `.env`） |
| `docker compose up` 报 `data` 目录权限 | bind mount 目录在宿主上不属于 UID 10001 | `sudo chown -R 10001:10001 ./data` |
