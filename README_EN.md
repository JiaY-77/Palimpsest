<div align="center">

# Palimpsest

**A local-first long-term memory system · the cross-session memory backbone for AI assistants**

Pa·limp·sest: *a writing surface that is overwritten again and again while older incisions survive beneath the new text.* Nothing is ever silently lost — new facts supersede old ones through an explicit, traceable **version chain** (`REVISED_BY`), and near-duplicates are passively consolidated.

| | |
|---|---|
| Version | v2.12.0 |
| Python | 3.10+ |
| License | MIT |
| Storage | TriviumDB 0.8.8 (vector + graph + document, embedded) |
| Backends | DeepSeek / Ollama (LLM), Ollama / OpenAI-compatible (embeddings) |
| CI | [![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml) |

**English** | [中文](./README.md)

</div>

---

## Features

Palimpsest is a **local-first embedded long-term memory system** that fuses **semantic vector search, a weighted knowledge graph, and full-text retrieval** into one local database. Its goal is to be the **memory backbone for an AI assistant** — so every conversation's gains are **searchable, linkable, and evolvable** instead of vanishing when the session closes.

This is a list of mechanisms, not a pile of features — every entry traces back to code (see the linked docs).

**Retrieval**

- 🗃️ **Hybrid retrieval.** Semantic vectors (cosine) fused with an FTS5 full-text index (`trigram` tokenizer for Chinese substrings) via RRF or a cascade; hits are labeled by source.
- 🔗 **Knowledge-graph recall.** Nodes connected by weighted edges (`RELATED_TO` / `REVISED_BY` / `CAUSES` / `REFERS_TO`); BFS expansion, scopeable to one domain "block" to prevent cross-domain pollution.
- 🕸️ **Community detection.** Built-in Leiden clustering splits the store into topical clusters.
- ✂️ **Token-efficient by design.** Retrieval returns a **150-char summary + metadata**, never full text; full content is fetched on demand.

**Memory evolution**

- 🔄 **Conflict detection & version chains.** Superseding is **declared explicitly** by the writer (`mem_ingest(replaces=...)`, **regardless of similarity**): the old record is marked `outdated` and linked `REVISED_BY` to its replacement. By default nothing is auto-superseded on similarity alone — similar candidates are only recorded as `related_ids` hints (`CONFLICT_AUTO_SUPERSEDE` restores the old behaviour); type / domain isolation prevents cross-category mistakes.
- 🕰️ **Fact time dimension (bi-temporal).** Records `valid_at` (when the fact became true) and `invalid_at` (when it stopped being true), orthogonal to `created_at`; `mem_fact_history` returns a single fact's full timeline. See [docs/BITEMPORAL.md](docs/BITEMPORAL.md).

**Governance**

- 🧹 **Consolidation & memory stats.** `mem_consolidate` collapses near-duplicates (≥ 0.85, protecting high-value nodes); `mem_stats` reports type / domain / importance / graph / tier distributions.
- ⏫ **Auto-promotion of hot memories.** Retrieval hits are counted (`hit_count`); `promote` raises and tags frequently-used memories (dry-run first, idempotent, reversible).
- ⏳ **Memory lifecycle.** Time decay (`MEMORY_DECAY_FACTOR`, default `0.95`) fades stale memories in ranking without touching storage; `kb_chunk` is exempt.
- 📁 **Task auto-archiving.** Completed tasks are written to markdown archives under the knowledge base, then deleted (dry-run first).
- 🗂️ **Memory tiering (`tier`).** A retrieval-side view, no data movement: by default only the facts tier is returned, keeping the logs tier (`record` / `event` / `git_commit`) out.
- 🛡️ **Pre-write secret scan.** 10 regex rules: a strong-rule match (API keys / tokens / private keys — 8 rules) **rejects the write**, while a weak-rule match (ID cards / phone numbers) is let through and flagged (see [`SECURITY.md`](SECURITY.md)).

**Deployment**

- ✅ **Deployment doctor.** `doctor` checks critical files / storage / FTS / dependencies / Embedding / vector-dimension consistency / runtime path charset, printing an actionable fix per failure.
- 🔐 **Optional API-key auth.** Off by default; setting `PALIMPSEST_API_KEY` requires a Bearer / X-API-Key header on REST.
- 🎯 **Three interfaces, one core.** MCP (stdio and streamable-http), FastAPI REST, and a full CLI reuse the same tools, so behavior never drifts.
- 🧠 **Dual Hermes plugins.** A Memory Provider plus a Context Engine for Hermes, enabled in one command (see the integration example below).

---

## Why it exists · use cases

Most AI apps suffer from three split data capabilities: session memory starting from zero, keyword-only knowledge bases, and memory piles that grow messier over time. Palimpsest solves "retrieve, relate, evolve" with **one local core**, avoiding shuttling data between a vector store, a document store, and a graph store.

A "memory that doesn't disappear" example: you tell the assistant "the service listens on 8090", then a design change says "the port is now 8095". This rewrite is declared explicitly by the writer (`replaces`), so the old memory is not overwritten — it is marked `outdated` and linked via `REVISED_BY` to the new version, so the version chain always shows how the fact evolved. This is Palimpsest: overwritten but never lost, rewritable and traceable.

It fits four scenarios: **long-term companion / personal assistant** (Hermes & co.) auto-recalls each turn, auto-sediments high-signal facts, and distills the graph before compression; **making a knowledge base semantic** (Obsidian) slices and vectorizes years of notes so search returns semantically relevant hits with graph neighbors; **fiction / worldbuilding vaults** ingest whole files and bulk-create edges, with community detection revealing relationships; **memory governance** uses secret scanning, conflict detection, consolidation, time decay and `promote` to keep the store clean.

---

## Memory advantages: why not just another vector store

Most memory layers (LangGraph Store, AutoGen Memory, and similar) answer "can you store it, can you find it". Palimpsest answers one more question on top: **memory changes, and you have to be able to say exactly how.**

| Capability | Common approach | Palimpsest |
|---|---|---|
| **Traceable rewrites** | The new value overwrites the old one; the old is gone | The old record is marked `outdated` and linked back via a `REVISED_BY` chain — **overwritten, never lost** |
| **Facts with a validity period** | Only "when it was written" (`created_at`) | **bi-temporal**: also records "when the fact was true" (`valid_at` / `invalid_at`), so `as_of` can answer "what did it believe three months ago" |
| **No cross-category collisions** | Similar text gets merged | **type + domain isolation** — a user preference cannot swallow a project decision just because the wording is close |
| **A read-only floor** | Similar text auto-supersedes; records get swapped out silently | By default **nothing is auto-superseded** (superseding must be declared via `replaces`); even with the auto mode on, nodes with `type=rule` or `protected=True` are never superseded |
| **Degradation is visible** | One retrieval lane dies; the fallback is silent | Results carry a `channels` field (e.g. `{"fts": "degraded"}`) — **quality loss leaves a trace** |
| **Reversible governance** | Merges and promotions cannot be undone | `consolidate` / `promote` default to dry-run, are idempotent, and can be rolled back precisely |

These mechanisms all answer the same question: **once memory stops being "a record" and becomes "a history", how do you keep it accountable, reversible, and non-destructive.** Full design in [`docs/BITEMPORAL.md`](docs/BITEMPORAL.md) and [`SECURITY.md`](SECURITY.md).

---

## Integration example: using Hermes

Palimpsest is a **general-purpose memory backend** — it speaks standard MCP, so any MCP-capable client can connect (REST and CLI work the same way). Below, **Hermes** serves as the worked example of "auto-recall + session sedimentation":

Palimpsest ships **both plugins** for Hermes' memory provider / context engine slots: a **Memory Provider** (read/write) and a **Context Engine** (pre-compression distillation). Source lives in [`hermes-plugin/`](./hermes-plugin/README.md), with hooks `on_session_end` (session-end distillation) and `on_pre_compress` (graph distillation before compression).

```bash
mkdir -p ~/.hermes/plugins/palimpsest && cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

Once active, every turn automatically queries REST `:8090` for relevant history, heuristically sediments high-signal facts, distils at session end, and distils the graph before compression; it also exposes `palimpsest_search` / `palimpsest_ingest` / `palimpsest_link` / `palimpsest_graph` for the agent to call. Note: the REST service must **stay running** (e.g. `scripts/start_rest.vbs`); auto-sedimentation is **heuristic** — fast, stable, cost-free over clever.

> **Other integration paths**: point an MCP client straight at `http://127.0.0.1:8090/mcp/` (sharing the REST process), or call REST / CLI directly — all three share the same underlying tools, so behavior never drifts.

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

### Hardware requirements

**No discrete GPU required.** The default local embedding model `qwen3-embedding:0.6b` was measured on a **pure CPU** setup at 209 ms end-to-end retrieval (stable mean) and ~92–296 ms per item when indexing — imperceptible for a memory lookup between conversation turns. Full measurements for all three tiers (pure CPU / integrated GPU / discrete GPU), reproduction commands, and how to pin a tier are in [`docs/HARDWARE.md`](docs/HARDWARE.md).

If you would rather not install Ollama at all, take **Path A (cloud key)** — zero extra local dependencies. **The real cost of local deployment is the install step, not hardware performance**, which the measurements show is not a barrier.

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

All configuration is read from environment variables (a `.env` file is loaded automatically via `python-dotenv`); see `.env.example` for a commented template. Below are the essentials to get running — the full 43-key table is in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

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

### MCP tools (21) — `mcp_tools/*`

| Tool | Description |
|---|---|
| `mem_search` | Unified retrieval across memory / knowledge base / both; optional graph-neighbor expansion, domain bias, **soft domain boosting (`domain_boost`)**, block-scoped isolation, **memory tiering (`tier`)** (default `facts` returns only the facts layer, excluding `record`/`event`/`git_commit`; `""` = no filtering) |
| `mem_hybrid_search` | Hybrid FTS5 + vector retrieval; `mode=rrf` (k=60) or `cascade`; also supports `domain_boost` and `tier`; each hit labeled `fts_hit` / `sem_hit` |
| `mem_retrieve` | Semantic retrieval returning a 150-char summary + metadata (never full text) |
| `mem_get_full` | Fetch the full content of a node by ID |
| `mem_ingest` | Write a new memory — conflict detection (superseding is **declared explicitly** by the writer: `replaces="<id>,<id>"` marks old nodes `outdated` and builds the `REVISED_BY` chain), secret scanning, length guards |
| `mem_recent` | Most recent memories (newest first) |
| `tasks_active` | Active task list (`type=task` & `status=active`): filtered by `project` / state set, sorted `doing > blocked > todo` then by last-touched desc; `legacy=true` nodes excluded by default |
| `mem_review` | Periodic recap of the last N days plus governance candidates (high-value upgrades / outdated cleanup / low-value); `tier` (default `facts`) scopes `recent_ingests`: `logs` returns only the logs tier, `""` disables filtering |
| `mem_stats` | Store-wide statistics: type / domain / importance / time / graph distributions + hot nodes; the `tiers` section groups types by retrieval-tier semantics (facts / logs / unclassified) and lists the effective `TIER_FACTS` / `TIER_LOGS` classification; the `write_judgment` section aggregates write-time judgment observations over the last 30 days (observational data stays out of the memory store and never contains memory content) |
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
| `mem_unpack` | Unpack a MemoryHub pack read-only: validate format / version, scan for secrets, produce a human-readable summary; writes nothing |
| `mem_import` | "Naturalize" nodes from a pack into this store; read-only preview by default, only ids in `select` are written, strong-secret nodes need `include_secrets=True` |

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
| `memoryhub` | MemoryHub packs (subcommands `read` / `import`) |
| `memoryhub read PATH` | Unpack a pack read-only (validate + human summary, `--preview-chars`) |
| `memoryhub import PATH` | Naturalize pack nodes into the store (`--select 12,15,31`; empty = preview only; `--include-secrets` allows strong-secret nodes) |
| `tasks active` | Active task list (`--project`, `--states todo,doing,blocked`, `--limit`, `--include-handoff` to include session handoff notes — handoffs are marked automatically on write and are exempt from state filtering when listed) |
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

**Domain attribution semantics**: nodes written without a `domain` (empty or missing) are normalized to the implicit `general` — all domain-filtered reads (`mem_search`, `mem_recent`, block filtering) use this normalized attribution, so **nodes written without a domain are visible under `domain="general"` queries**. `mem_recent(domain="general")` scans payloads with the same normalized attribution; other domains keep the index fast path — both give consistent results. Thought traces from the lifecycle channel get their domain via the optional `domain` field of `POST /lifecycle/post-turn` (omitting it stays implicit `general`).

### REST API — `main.py`, port 8090

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Service info + version + endpoint index |
| `POST` | `/mem/search` | Unified retrieval |
| `POST` | `/mem/ingest` | Write a new memory (conflict detection + secret scan; a `replaces` field declares superseding) |
| `POST` | `/memoryhub/unpack` | Unpack a MemoryHub pack read-only (validate + human summary) |
| `POST` | `/memoryhub/import` | Naturalize pack nodes into the store (`select` empty = preview only; `include_secrets` allows strong-secret nodes) |
| `POST` | `/graph/neighbors` | Graph neighbors of a node |
| `POST` | `/lifecycle/pre-turn` | **Memory strategy + thought preset layer**: decide which memories to recall before each model call; returns text ready to inject into the prompt plus a thinking skeleton picked for the current context (`thought_skeleton`) |

> The full route list is in [`docs/API.md`](docs/API.md). If `PALIMPSEST_API_KEY` is set, every route except `/` requires `Authorization: Bearer <key>` or `X-API-Key: <key>`.

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

`scripts/rest_stress.py` runs an end-to-end load test across six realistic scenarios (high-frequency search / bulk writes / graph linking and diffusion / boundary inputs / mixed read-write load / post-write recall correctness), reporting qps and p50/p95/p99. `eval/` derives queries from real nodes and scores `fts` / `vec` / `rrf` / `cascade` with Recall@K, MRR@K and nDCG@K; every script copies the real store into `eval/.tmp/` and SHA256-verifies it before and after. Supporting tools: `scripts/retrieval_probe.py` / `scripts/prod_entrypoint_check.py` / `scripts/ab_snapshot_*.py`; see [`eval/README.md`](eval/README.md). The full evaluation methodology — how the query set is built, how metrics are defined, how to read the results — plus a summary of findings, including an ablation that raised the memory-layer Recall@5 from 0.263 to 0.684 by switching from multiplicative hard weighting to ε-level additive soft weighting, is in [`docs/RETRIEVAL_QUALITY.md`](docs/RETRIEVAL_QUALITY.md).

---

## Project structure

`main.py` (the REST single writer) / `mcp_server.py` (stdio escape hatch) / `config.py` → `core/` (framework-free shared engine) → `mcp_tools/` (19 tools, shared by MCP/REST/CLI); plus `scripts/` ops tooling, `eval/` offline evaluation, `hermes-plugin/` dual plugins, `tests/`, `docs/`, `data/`. Layering rules and the full tree are in [CONTRIBUTING.md](CONTRIBUTING.md).

**Graph edges:** `scripts/build_structural_edges.py` adds edges from seven sources (same-source grouping `PART_OF` / chunk order `NEXT` / skill affinity `SKILL_RELATED_TO` / novel relations `RELATED_TO` / Markdown links `WIKI_LINK` / session time-chain `FOLLOWS` and name mentions `MENTIONS` / **knowledge references `REFERENCES`** — turning each memory's `linked_from` into memory-to-slice edges). It is **read-only dry-run by default**, printing a predicted impact report; pass `--apply` to write (batches can be reverted precisely with `--rollback`). `build_kb_index.py` / `build_skill_index.py` rebuild edges automatically after reindexing; disable with `PALIMPSEST_BUILD_EDGES=0`.

**Governance views:** besides retrieval panels, `scripts/dashboard.py` offers three read-only views — conflict chain (superseded nodes and their revision chain), decay curve (ranked by time decay × importance), and graph scale (isolation rate and edge-label distribution).

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
