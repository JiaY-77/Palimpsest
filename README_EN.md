<div align="center">

# Palimpsest

**A local-first long-term memory system · the cross-session memory backbone for AI assistants**

Pa·limp·sest: *a writing surface that is overwritten again and again while older incisions survive beneath the new text.* Nothing is ever silently lost — new facts supersede old ones through an explicit, traceable **version chain** (`REVISED_BY`), and near-duplicates are passively consolidated.

| | |
|---|---|
| Version | v2.6.0 |
| Python | 3.10+ |
| License | MIT |
| Storage | TriviumDB 0.8.8 (vector + graph + document, embedded) |
| Backends | DeepSeek / Ollama (LLM), Ollama / OpenAI-compatible (embeddings) |
| CI | [![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml) |

**English** | [中文](./README.md)

</div>

---

## Features

Palimpsest is a **local-first embedded long-term memory system** that fuses **semantic vector search, a weighted knowledge graph, and full-text retrieval** into one local database. Its goal is to be the **memory backbone for an AI assistant** — so every conversation's gains are **searchable, linkable, and evolvable** instead of vanishing when the session closes:

- 🗃️ **Hybrid retrieval.** Semantic vectors (cosine) fused with an FTS5 full-text index (`trigram` tokenizer for Chinese substrings) via RRF or a cascade; hits are labeled `fts_hit` / `sem_hit`.
- 🔗 **Knowledge-graph recall.** Nodes connected by **weighted edges** (`RELATED_TO` / `REVISED_BY` / `CAUSES` / `REFERS_TO`); BFS expansion prunes to the strongest edges, filters weak edges, and can be scoped to one domain "block" to prevent cross-domain pollution.
- 🕸️ **Community detection.** Built-in Leiden clustering splits the store into topical clusters — answering "what circles exist in my memory?"
- 🔄 **Conflict detection & version chains.** High similarity (score > 0.75) means the same fact was superseded: the old record is marked `outdated` and linked `REVISED_BY` to its replacement; medium similarity only records `related_ids`; type / domain isolation prevents cross-category mistakes.
- 🕰️ **Fact time dimension (bi-temporal).** Fact-type memories record `valid_at` (world time: when the fact became true) on write, and gain `invalid_at` (no longer true) plus `expired_at` (marked historical by the system) when superseded — orthogonal to `created_at` (recording time). Surfaced as `meta.times` in search results, and `mem_fact_history` returns a single fact's full timeline and supersede relations. See [docs/BITEMPORAL.md](docs/BITEMPORAL.md).
- 🛡️ **Pre-write secret scan.** 10 regex rules: a **strong-rule** match (API keys / tokens / private keys / SSH keys / bearer tokens — 8 rules) **rejects the write**, while a **weak-rule** match (ID cards / phone numbers — 2 rules) is **let through and flagged `secret_hint`** for audit (see [`SECURITY.md`](SECURITY.md)).
- 🧹 **Consolidation & memory stats.** `mem_consolidate` collapses near-duplicates (≥ 0.85, protecting high-value nodes); `mem_stats` reports type / domain / importance / time / graph / hot spots / tier distributions.
- ⏫ **Auto-promotion of hot memories.** Retrieval hits are counted (`hit_count`); `promote` raises and tags frequently-used memories (dry-run first, idempotent, reversible).
- ⏳ **Memory lifecycle.** Time decay (`MEMORY_DECAY_FACTOR`) fades stale memories in ranking without touching storage; `kb_chunk` is exempt; `outdated` versions leave ordinary retrieval.
- 📁 **Task auto-archiving.** Completed tasks are written to markdown archives under the knowledge base, then deleted — dry-run first, `apply` to commit.
- ✅ **Deployment doctor.** `doctor` checks critical files / storage / FTS / dependencies / Embedding reachability / vector-dimension consistency / runtime path charset, printing an actionable fix per failure (`--json` for machines).
- ✂️ **Token-efficient by design.** Retrieval returns a **150-char summary + metadata**, never full text; full content is fetched on demand.
- 🗂️ **Memory tiering (`tier`).** A retrieval-side view, no data movement: by default only the facts tier (`memory` / `correction` / `decision` / `plan` / `task`, …) is returned, keeping the logs tier (`record` / `event` / `git_commit`) out; `tier="logs"` returns only logs, `tier=""` restores the full pool.
- 🔐 **Optional API-key auth.** Off by default; setting `PALIMPSEST_API_KEY` requires a Bearer / X-API-Key header on REST — for LAN / trusted-network deployments.
- 🎯 **Three interfaces, one core.** MCP (stdio and streamable-http), FastAPI REST, and a full CLI reuse the same tools, so behavior never drifts; REST also exposes an MCP endpoint at `/mcp` so a client can share the process.
- 🧠 **Swap Hermes memory with two plugins.** A Memory Provider (semantic recall + auto-sedimentation) plus a Context Engine (graph distillation before compression) — one command each, memory survives across sessions.

---

## Why it exists · use cases

Most AI apps suffer from three split data capabilities: session memory starting from zero, keyword-only knowledge bases, and memory piles that grow messier over time. Palimpsest solves "retrieve, relate, evolve" with **one local core**, avoiding shuttling data between a vector store, a document store, and a graph store.

A "memory that doesn't disappear" example: you tell the assistant "the service listens on 8090", then a design change says "the port is now 8095". The old memory is not overwritten — it is marked `outdated` and linked via `REVISED_BY` to the new version, so the version chain always shows how the fact evolved. This is Palimpsest: overwritten but never lost, rewritable and traceable.

It fits four scenarios: **long-term companion / personal assistant** (Hermes & co.) auto-recalls each turn, auto-sediments high-signal facts, and distills the graph before compression; **making a knowledge base semantic** (Obsidian) slices and vectorizes years of notes so search returns semantically relevant hits with graph neighbors; **fiction / worldbuilding vaults** ingest whole files and bulk-create edges, with community detection revealing relationships; **memory governance** uses secret scanning, conflict detection, consolidation, time decay and `promote` to keep the store clean.

---

## For Hermes users: turn it into your memory plugin

Palimpsest ships **both plugins** for Hermes' memory provider / context engine slots: a **Memory Provider** (read/write) and a **Context Engine** (pre-compression distillation). Source lives in [`hermes-plugin/`](./hermes-plugin/README.md), with hooks `on_session_end` (session-end distillation) and `on_pre_compress` (graph distillation before compression).

```bash
mkdir -p ~/.hermes/plugins/palimpsest && cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

Once active, every turn automatically queries REST `:8090` for relevant history, heuristically sediments high-signal facts, distils at session end, and distils the graph before compression; it also exposes `palimpsest_search` / `palimpsest_ingest` / `palimpsest_link` / `palimpsest_graph` for the agent to call. Note: the REST service must **stay running** (e.g. `scripts/start_rest.vbs`); auto-sedimentation is **heuristic** — fast, stable, cost-free over clever.

---

## Obsidian users: how we read your vault

We treat a vault not as "files" but as a **source of knowledge**: recursively scan every `.md` under `KNOWLEDGE_DIR`, slice by Markdown headings while keeping `[[wikilinks]]` intact, then vectorize the chunks into `kb_chunk` nodes for semantic search. Even if you never use Palimpsest you can replicate this pipeline with any toolchain; a working implementation is `scripts/build_kb_index.py`. Full approach in [`docs/OBSIDIAN.md`](docs/OBSIDIAN.md).

---

## Quick Start

### Install (common)

```bash
# Python 3.10+ required
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Path A: Cloud API key — fastest start (no Ollama)

```bash
cp .env.example .env
# Edit .env: fill in your cloud embedding key + LLM key
#    EMBEDDING_API_KEY=your_key          # leave empty or delete → local Ollama
#    EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM  per your provider
#    DEEPSEEK_API_KEY=your_key           (required when LLM_BACKEND=deepseek)
```

### Path B: Local Ollama (privacy-first, data never leaves your machine)

```bash
# 1. Install and start Ollama (https://ollama.com); 2. Pull the embedding model
ollama pull qwen3-embedding:0.6b
# 3. Copy .env.example → .env and set DEEPSEEK_API_KEY (or LLM_BACKEND=ollama for full-local)
cp .env.example .env
```

> **Both paths:** leaving `EMBEDDING_PROVIDER` empty auto-detects (valid `EMBEDDING_API_KEY` → cloud, otherwise local; set `openai` / `ollama` to force). Changing the provider changes the vector space — **you must rebuild the knowledge-base index** (see [Swapping models](#swapping-models--re-embedding)).

### Run

```bash
# Recommended: deployment doctor (prints the fix for every failing check); startup-check is its subset
python scripts/palimpsest_cli.py doctor
python scripts/palimpsest_cli.py startup-check

# REST API (:8090) — the single writer; every integration goes through it
python -m uvicorn main:app --host 127.0.0.1 --port 8090

# CLI example / dashboard (:8010) / index the knowledge base
python scripts/palimpsest_cli.py search "what changed in the architecture?"
python scripts/dashboard.py
python scripts/build_kb_index.py
```

> **Integrations and the single-process constraint (important)**
>
> Prefer **REST's `/mcp`**: point your MCP client at `http://127.0.0.1:8090/mcp/`, sharing the REST process; the CLI / dashboard / scripts also go through REST. stdio `mcp_server.py` is an **escape hatch** (not a peer option), only for a **no-REST** pure-MCP setup: the database is opened in exclusive mode, a second connection fails with `Database locked: ...`, and a failed concurrent write **corrupts the file group** without self-healing. So REST must stay single-process (no `uvicorn --workers N`), **only one process should access a database**, and the conflict is loud — the CLI raises `DatabaseBusyError`, REST returns `503`.
>
> If the Embedding check fails: start Ollama and `ollama pull qwen3-embedding:0.6b`, or set `EMBEDDING_API_KEY` in `.env` for cloud.

On Windows, `scripts/start_rest.vbs` launches REST in a hidden window (e.g. at login), logging to `scripts/start_rest.log`.

**MCP client integration** (recommended: HTTP, sharing the REST process):

```json
{ "mcpServers": { "palimpsest": { "url": "http://127.0.0.1:8090/mcp/" } } }
```

**Backup & restore**: `python scripts/backup_db.py --keep 7` performs a full-group cold backup + read-back check (keeps 7 by default). Backups must be **whole-group** (`.db` / `.vec` / `.gidx` / `.pidx` / `.flush_ok` / `.pld.*` / `.wal`, one generation); the script verifies by reading back and exits non-zero on failure.

---

## Configuration

All configuration is read from environment variables (a `.env` file is loaded automatically via `python-dotenv`); see `.env.example` for a commented template. Below are the essentials to get running — the full 36-key table is in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

| Variable | Default | Description |
|---|---|---|
| `REST_PORT` | `8090` | Port for the FastAPI REST service |
| `DASHBOARD_PORT` | `8010` | Port for the dashboard service |
| `DB_PATH` | `data/mh_memory.db` | Path to the embedded TriviumDB database |
| `PALIMPSEST_API_KEY` | *(empty = off)* | Optional REST auth; when set, every route except `/` requires Bearer / X-API-Key |
| `LLM_BACKEND` | `deepseek` | LLM backend: `deepseek` or `ollama` |
| `DEEPSEEK_API_KEY` | *(empty)* | DeepSeek API key (required when `LLM_BACKEND=deepseek`) |
| `OLLAMA_MODEL` | `deepseek-r1:7b` | Ollama chat model when `LLM_BACKEND=ollama` |
| `EMBEDDING_PROVIDER` | *(empty = auto-detect)* | Embedding backend: empty auto-detects, or `ollama` / `openai` |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Local Ollama embedding model |
| `EMBEDDING_API_KEY` | *(empty)* | Cloud embedding endpoint key (setting it routes to `openai`) |
| `KNOWLEDGE_DIR` | *(optional)* | Root of the knowledge base (Obsidian `.md` files) to index |

---

## Swapping models / re-embedding

Changing the embedding model changes the vector space, so you **must re-embed the full store**. The complete sequence (`reindex --check` / `--dry-run` / `--yes`) and the dimension-change rebuild flow are in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

---

## Custom index rules

The scan scope, per-node `kind`, chunking strategy, and payload `domain` of `build_novel_index.py` / `build_kb_index.py` come from a **declarative JSON rule set** (built-in defaults, zero config to start), and can be passed with `--rules`:

```bash
python scripts/build_novel_index.py --source <vault-path> --rules rules.json
python scripts/build_kb_index.py --rules rules.json
```

**Loading priority**: `--rules <path>` (a missing path errors out) > `<vault root>/<legacy filename>` (the novel script also honors `.palimpsest-novel-index.json`) > `<vault root>/.palimpsest-index.json` > built-in defaults; a legacy `.palimpsest-index.yaml` is **not parsed**, only warned about. Top-level keys are `kind_map` / `default_kind` / `chunk_strategy` / `min_chunk_len` / `max_chunk_len` / `domain` / `include` / `exclude` / `require_frontmatter_id`; unknown keys only warn, invalid values error. Each `kind_map` rule supports `dir_prefix` / `filename` / `glob` (combinable = AND), first match wins, none → `default_kind` (never silently folded; unmatched paths show up in `unmatched_paths`). Complete example: [`examples/index-rules.example.json`](examples/index-rules.example.json).

---

## Usage

### MCP tools (19) — `mcp_tools/*`

| Tool | Description |
|---|---|
| `mem_search` | Unified retrieval across memory / knowledge base / both; optional graph-neighbor expansion, domain bias, **soft domain boosting (`domain_boost`)**, block-scoped isolation, **memory tiering (`tier`)** (default `facts` returns only the facts layer, excluding `record`/`event`/`git_commit`; `""` = no filtering) |
| `mem_hybrid_search` | Hybrid FTS5 + vector retrieval; `mode=rrf` (k=60) or `cascade`; also supports `domain_boost` and `tier`; each hit labeled `fts_hit` / `sem_hit` |
| `mem_retrieve` | Semantic retrieval returning a 150-char summary + metadata (never full text) |
| `mem_get_full` | Fetch the full content of a node by ID |
| `mem_ingest` | Write a new memory — with conflict detection, `REVISED_BY` version chaining, secret scanning, and length guards |
| `mem_recent` | Most recent memories (newest first) |
| `tasks_active` | Active task list (`type=task` & `status=active`): filtered by `project` / state set, sorted `doing > blocked > todo` then by last-touched desc; `legacy=true` nodes excluded by default |
| `mem_review` | Periodic recap of the last N days plus governance candidates (high-value upgrades / outdated cleanup / low-value); `tier` (default `facts`) scopes `recent_ingests`: `logs` returns only the logs tier, `""` disables filtering |
| `mem_stats` | Store-wide statistics: type / domain / importance / time / graph distributions + hot nodes; the `tiers` section groups types by retrieval-tier semantics (facts / logs / unclassified) and lists the effective `TIER_FACTS` / `TIER_LOGS` classification |
| `mem_version_history` | Walk the `REVISED_BY` chain to show how a fact evolved |
| `mem_fact_history` | **Timeline of a single fact**: its bi-temporal fields (`valid_at` / `invalid_at` / `expired_at` / `created_at`) plus supersede relations (what it superseded / what superseded it) |
| `mem_consolidate` | Near-duplicate detection; dry-run preview or apply merge |
| `mem_communities` | Leiden community detection: cluster the store into topical groups |
| `kb_index` | Index knowledge-base `.md` files into `kb_chunk` nodes (vectorized) |
| `kb_search` | Semantic search over indexed knowledge chunks |
| `skill_search` | Semantic search over indexed Hermes skills (`skill_chunk` nodes) — returns name / description / category / source_path |
| `graph_neighbors` | BFS over the knowledge graph from a node (relation filter, depth 1–3, weak-edge filter) |
| `mem_link` | Manually create graph edges (`RELATED_TO` / `CAUSES` / `REFERS_TO`; bidirectional by default) |
| `mem_unlink` | Delete a graph edge (idempotent: returns `deleted=false` when absent; cleans up stray `REVISED_BY` edges) |

### CLI — `scripts/palimpsest_cli.py`

| Command | Description |
|---|---|
| `search "QUERY"` | Unified retrieval (`--scope all\|memory\|kb`, `--neighbors`, `--block`) |
| `hybrid-search "QUERY"` | FTS5 + vector hybrid retrieval (`--mode rrf\|cascade`) |
| `ingest "CONTENT"` | Write a memory (`--importance 0.5`, `--type memory`, `--domain`) |
| `link --source N --target N` | Create a graph edge (`--relation`, `--one-way`) |
| `index` | Scan and index the knowledge base |
| `graph --id N` | Graph neighbors of a node (`--depth`, `--relation`, `--min-weight`) |
| `recent` | Most recent memories (`--limit`, `--domain`) |
| `tasks` | Task-state registry (subcommands `active` / `backfill`) |
| `tasks active` | Active task list (`--project`, `--states todo,doing,blocked`, `--limit`) |
| `tasks backfill` | Backfill `task_state` on existing task nodes (dry-run by default; `--apply` writes) |
| `review` | Periodic recap of the last N days (`--tier facts\|logs\|''` scopes recent_ingests) |
| `stats` | Store-wide statistics (totals / domains / importance / time / graph) |
| `kb "QUERY"` | Semantic search over knowledge chunks |
| `consolidate` | Preview merges; `--apply` executes (`--threshold 0.85`, `--max-importance 0.8`) |
| `promote` | Hot-memory promotion candidates; `--apply` raises importance and tags (`--days`, `--min-hits`) |
| `ingest-git` | Index recent git commits as `git_commit` nodes (idempotent) |
| `fts-rebuild` | Rebuild the full FTS5 index |
| `fts-search "QUERY"` | Raw FTS5 search (trigram substring) |
| `doctor` | Deployment health check: critical files / storage / FTS / dependencies / Embedding / vector-dimension consistency / runtime path charset, with an actionable fix per failure (`--json`) |
| `startup-check` | Run the startup self-check (lightweight subset of `doctor`; exit code 1 on failure) |
| `task-archive` | Archive completed task nodes; `--apply` writes markdown and deletes the node |
| `reindex` | Re-embed the whole store after switching embedding models (`--check` health check, `--dry-run` preview) |

### Blocks

`block` is the "domain-group" concept: the graph is isolated per block, and diffusion retrieval only follows edges inside the same block, preventing cross-domain pollution. Built-ins: `task` / `kb` / `hermes` / `novel` / `general`; any of your own `domain` values can be a block (e.g. `--block myproject`), and an empty `--block` runs in full mode. Node ownership is expressed by `payload.domain`: set it at write time via `--domain X` or `mem_ingest(domain=...)`; `kb` nodes are set automatically by the indexer.

### REST API — `main.py`, port 8090

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Service info + version + endpoint index |
| `POST` | `/mem/search` | Unified retrieval |
| `POST` | `/mem/ingest` | Write a new memory (conflict detection + secret scan) |
| `POST` | `/graph/neighbors` | Graph neighbors of a node |
| `POST` | `/lifecycle/pre-turn` | **Memory strategy**: decide which memories to recall before each model call; returns text ready to inject into the prompt |

> The full 26 routes are in [`docs/API.md`](docs/API.md). If `PALIMPSEST_API_KEY` is set, every route except `/` requires `Authorization: Bearer <key>` or `X-API-Key: <key>`.

---

## Tests & evaluation

```bash
python -m pytest tests/ -v                                 # from the repo root
DB_PATH=/tmp/stress.db python -m uvicorn main:app --port 8091
python scripts/rest_stress.py --base http://127.0.0.1:8091 --seeds 200 --out report.json
venv/Scripts/python.exe eval/gen_eval_set.py --dry-run     # needs DEEPSEEK_API_KEY
venv/Scripts/python.exe eval/run_eval.py
```

The suite covers the core loop: write → `mem_search` hit → `mem_get_full` round trip; graph edges → `graph_neighbors` / `mem_communities`; secret scan rejecting key-bearing content; FTS-side hit marking in hybrid retrieval; conflict detection / version chains and outdated semantics; `consolidate` / `promote` dry-run and idempotency; PUT/PATCH partial updates preserving fields; concurrency and failure paths. `tests/conftest.py` redirects `DB_PATH` to a temporary database before anything is imported, so the suite never touches a production store and runs green with a deterministic fake embedder, no live Ollama needed.

`scripts/rest_stress.py` runs an end-to-end load test across six realistic scenarios (high-frequency search / bulk writes / graph linking and diffusion / boundary inputs / mixed read-write load / post-write recall correctness), reporting qps and p50/p95/p99. `eval/` derives queries from real nodes and scores `fts` / `vec` / `rrf` / `cascade` with Recall@K, MRR@K and nDCG@K; every script copies the real store into `eval/.tmp/` and SHA256-verifies it before and after. Supporting tools: `scripts/retrieval_probe.py` / `scripts/prod_entrypoint_check.py` / `scripts/ab_snapshot_*.py`; see [`eval/README.md`](eval/README.md).

---

## Project structure

`main.py` (the REST single writer) / `mcp_server.py` (stdio escape hatch) / `config.py` → `core/` (framework-free shared engine) → `mcp_tools/` (18 tools, shared by MCP/REST/CLI); plus `scripts/` ops tooling, `eval/` offline evaluation, `hermes-plugin/` dual plugins, `tests/`, `docs/`, `data/`. Layering rules and the full tree are in [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Development

- **Virtual env:** one per checkout (`python -m venv venv`) + `pip install -r requirements.txt`.
- **Adding a tool:** register it in `mcp_tools/` with the shared `@mcp.tool()` decorator — it instantly appears across the MCP service, the REST layer, and the CLI.
- **Adding a core module:** keep `core/` free of FastAPI/MCP; consume it via `mcp_tools/` and `main.py`.
- **Changed the schema?** Rebuild the FTS index (`fts-rebuild`) and the knowledge-base index (`build_kb_index.py`).
- **Tests:** stay isolated — never point tests at the production database.

Run the quality gates before opening a PR (they map one-to-one onto the CI `lint` / `typecheck` / `test` jobs):

```bash
python -m pytest tests/ -q                         # tests (CI runs 3.10 / 3.11 / 3.12)
ruff check .                                       # lint (rules pinned in pyproject.toml)
mypy                                               # type check (core/ for now)
python -m pytest --cov=core --cov=mcp_tools -q     # coverage baseline
```

Documentation-vs-code consistency is checked by `scripts/readme_check.py` (MCP tool list / CLI subcommands / REST routes / config keys and their documented defaults / file references / inline-code pairing); CI runs it with `--strict` in the `docs` job: `python scripts/readme_check.py --strict`.

Releases follow [Semantic Versioning](https://semver.org/): process in [RELEASING.md](docs/RELEASING.md), history in [CHANGELOG.md](CHANGELOG.md), deprecations in [DEPRECATIONS.md](docs/DEPRECATIONS.md).

---

## License

[MIT](LICENSE) © JiaY-77
