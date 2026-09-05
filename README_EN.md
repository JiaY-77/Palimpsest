<div align="center">

# Palimpsest

**English** | [中文](./README.md)

**A local-first long-term memory system** that stores, retrieves, and evolves an AI assistant's memories through semantic vector search, a weighted knowledge graph, and full-text retrieval — all in a single embedded database.

Pa·limp·sest: *a writing surface that is overwritten again and again while older incisions survive beneath the new text.* Palimpsest applies the same idea to memory: nothing is ever silently lost — new facts supersede old ones through an explicit, traceable **version chain** (`REVISED_BY`), and near-duplicates are passively consolidated instead of cluttering the store.

| | |
|---|---|
| Version | v1.1.1 |
| Python | 3.10+ |
| License | MIT |
| Storage | TriviumDB 0.8.5 (vector + graph + document, embedded) |
| Backends | DeepSeek / Ollama (LLM), Ollama / OpenAI-compatible (embeddings) |
| CI | [![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml) |

</div>

---

## Features

- **Hybrid retrieval.** Semantic vector search (cosine) fused with an FTS5 full-text index (`trigram` tokenizer for Chinese substring matching) via Reciprocal Rank Fusion (RRF) or a cascade (FTS coarse-filter → vector re-rank). Every hit is labeled with its source — `fts_hit` / `sem_hit`.
- **Knowledge-graph recall.** Nodes are connected by **weighted edges** (`RELATED_TO`, `REVISED_BY`, `CAUSES`, `REFERS_TO`). Retrieval expands along edges with BFS: per-node expansion is pruned to the strongest edges, weak edges are filtered, and diffusion can be scoped to a single domain "block" to prevent cross-domain pollution.
- **Community detection.** Built-in Leiden clustering splits the store into topical clusters (project groups, character-relationship groups, …) — answering "what circles exist in my memory?"
- **Conflict detection & version chains.** Every write is compared against similar existing memories. High similarity (score > 0.75) means the same fact was superseded: the old record is marked `outdated` and linked to its replacement with a `REVISED_BY` edge. Medium similarity only records `related_ids` (no false marking); type and domain isolation prevent cross-category mistakes.
- **Pre-write secret scan.** Before anything is stored, content is scanned against **10 regular-expression rules** (API keys, tokens, private keys, card/phone numbers, bearer tokens, …). A match **rejects the write** and reports which rule fired.
- **Consolidation & memory stats.** `mem_consolidate` collapses near-duplicate memory pairs (similarity ≥ 0.85, protecting `importance ≥ 0.8`); `mem_stats` reports store-wide distributions (type / domain / importance / time / graph / hot spots).
- **Auto-promotion of hot memories.** Retrieval hits are counted (`hit_count`); `promote` surfaces frequently-used memories — raises importance and tags them (dry-run first, idempotent, reversible) as candidates for human-reviewed knowledge-base promotion.
- **Memory lifecycle.** Time decay weighting (`MEMORY_DECAY_FACTOR`, default 0.95 /month) fades stale memories in ranking without touching storage; `kb_chunk` knowledge slices are exempt; `outdated` versions no longer pollute ordinary retrieval (traceable on demand).
- **Task auto-archiving.** Completed task nodes are moved out of the hot store into markdown archives under the knowledge base, then deleted — dry-run first, `apply` to commit.
- **Startup self-check.** `startup-check` verifies critical files, storage initialization, and the FTS index, emitting a structured report and a non-zero exit code on failure.
- **Token-efficient by design.** Retrieval returns a **150-character summary plus metadata** instead of full text; full content is fetched on demand.
- **Optional API-key auth.** Off by default (localhost direct access); setting `PALIMPSEST_API_KEY` requires a Bearer / X-API-Key header on all REST routes except `/` — for LAN / trusted-network deployments.
- **Three interfaces, one core.** MCP (stdio) for agent tooling, a FastAPI REST service, and a full CLI — all reuse the same underlying tools, so behavior never drifts.
- **Swap Hermes memory with two plugins.** Replace Hermes' memory layer with Palimpsest entirely: a Memory Provider (semantic recall + auto-sedimentation) plus a Context Engine (graph distillation before compression) — enabled with one command each, memory survives across sessions.

---

## Architecture

```
                         ┌──────────────────────────────────────┐
                         │              Clients                 │
                         │   MCP        REST         CLI        │
                         │  (stdio)   (:8090)      (scripts/    │
                         │             │           palimpsest)  │
                         └──────────┬───┴───────────┬───────────┘
                                    │               │
                    ┌───────────────▼───────────────▼───────────┐
                    │                 Palimpsest                │
                    │                                           │
                    │   mcp_server.py    main.py  (FastAPI)     │
                    │   mcp_tools/*   (16 tools, shared)        │
                    │                                           │
                    │   ┌───────────────────────────────────┐   │
                    │   │             core/                 │   │
                    │   │  trivium_store   ·   conflict     │   │
                    │   │  consolidator    ·   stats        │   │
                    │   │  promoter        ·   reporting    │   │
                    │   │  fts_index       ·   secret_scan  │   │
                    │   │  startup_check   ·   task_archive │   │
                    │   │  utils           ·   version      │   │
                    │   └───────────────┬───────────────────┘   │
                    └───────────────────┼───────────────────────┘
                                        │
       ┌────────────────────────────────┼──────────────────────────────┐
       │                    ┌───────────▼───────────┐    ┌─────────────▼──────┐
       │                    │     TriviumDB 0.8.5   │    │   SQLite FTS5      │
       │                    │  vector + graph + doc │    │   fts.db (trigram) │
       │                    └───────────┬───────────┘    └────────────────────┘
       │                                │  embeddings
       └────────────────────────────────┴──────────────────────────────────
                      Ollama (qwen3-embedding:0.6b, 1024-d)
                               or OpenAI-compatible API
```

Three independently usable entry points share one `mcp_tools` layer and one `core` store:

1. **MCP server** (`mcp_server.py`) — exposes 16 tools over stdio for any Model Context Protocol client.
2. **REST service** (`main.py`) — FastAPI on port 8090, with paginated JSON export and an optional API-key auth mode.
3. **CLI** (`scripts/palimpsest_cli.py`) — subcommands for scripting, cron, and operations without a server.

The dashboard (`scripts/dashboard.py`, port 8010) provides a lightweight human-facing view of the store.

---

## Use cases

### Use case 1 · Long-term companion / personal-assistant memory across sessions (Hermes & co.)

Once Palimpsest is wired up as an agent's memory backend, every turn automatically recalls the relevant history, auto-sediments high-signal facts, and distills the round into highlights at session end; right before context compression, the graph layer distills once more, weaving scattered fragments into a searchable network. Closing the conversation doesn't matter — next time we meet, it still remembers and recalls.

### Use case 2 · Making a knowledge base semantic (Obsidian users)

Turn years of Obsidian notes into semantically searchable assets: `build_kb_index.py` scans every `.md`, slices by Markdown headings, vectorizes the chunks, and keeps `[[wikilinks]]` intact. Search is no longer keyword roulette — it returns semantically relevant hits with graph neighbors attached.

### Use case 3 · Fiction / worldbuilding setting vaults (novelists, game designers)

`build_novel_index.py` ingests a local creative vault (character cards, worldbuilding, relationship documents) whole-file into `domain=novel` nodes; `link_novel_relations.py` bulk-creates edges from a relationship list (mentor-student / bloodline / faction / rivalry). Combined with community detection and graph queries, relationships between settings become visible at a glance. Creative data stays local — never pushed to a public repo.

### Use case 4 · Memory governance (anti-pollution / anti-bloat / traceable)

Your store won't get messier over time: a pre-write secret scan blocks keys, conflict detection + version chains make every rewrite traceable, consolidation collapses near-duplicates into one node, time decay fades stale facts, and stats + promote surface the memories that earn their place. Memory is an asset, not a landfill.

---

## For Hermes users: turn it into your memory plugin

Hermes exposes a memory provider / context engine slot, and Palimpsest ships **both plugins**: a **Memory Provider** (read/write) and a **Context Engine** (pre-compression distillation). The plugin source lives in the repo at [`hermes-plugin/`](./hermes-plugin/README.md) — including `plugin.yaml` (`kind=standalone`) and two hooks: `on_session_end` (session-end highlight distillation) and `on_pre_compress` (graph distillation before compression).

Deploy (copy the plugin into Hermes' plugin directory, then activate one line per item):

```bash
# 1. Copy the plugin into Hermes' plugin directory (default ~/.hermes/plugins/)
mkdir -p ~/.hermes/plugins/palimpsest
cp hermes-plugin/* ~/.hermes/plugins/palimpsest/

# 2. Activate (one line per item)
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

Once active, every round of conversation does this automatically:

- **Auto recall** — each turn queries `:8090` REST and retrieves the relevant history (`memory.provider=palimpsest`).
- **Auto sedimentation** — high-signal facts are written into memory automatically, via heuristics, no LLM involved.
- **Session-end distillation** — `on_session_end` condenses the round into structured memory.
- **Pre-compress graph distillation** — `on_pre_compress` distills graph highlights for the compression stage (`context.engine=palimpsest-graph`).
- **Memory tools** — `palimpsest_search` / `palimpsest_ingest` / `palimpsest_link` / `palimpsest_graph` / `palimpsest_router`, etc., for the agent to call proactively.

Two notes:

- The REST service (`:8090`) must **stay running** (e.g. `scripts/start_rest.vbs` at login).
- Auto-sedimentation is **heuristic** (similarity / importance thresholds), not an LLM judgment — it favors fast, stable, cost-free over clever.

---

## Obsidian users: how we read your vault (even if you don't use Palimpsest)

> This section is about the *approach*, not an ad — even if you never use Palimpsest, you can replicate this pipeline with any toolchain.

We don't treat a vault as "files" but as a **source of knowledge**. Reading takes five steps:

1. **The vault directory is the source** — recursively scan every `.md` under `KNOWLEDGE_DIR` (skipping config dirs like `.obsidian`); each note is one document.
2. **Frontmatter parsing** — read the YAML frontmatter and use its `tags` to drive rule detection: notes tagged `rule` are classified as rule-domain (`domain=rule`), the rest go to `kb`.
3. **Smart slicing by Markdown headings** — split on `##` / `###` into 300–800 char chunks, keeping `[[wikilinks]]` verbatim so cross-note context survives.
4. **Vectorize into the store** — each slice is embedded and stored as a `kb_chunk` node, becoming a semantically searchable asset.
5. **Rule weighting** — rule-domain slices get a **×1.3 weight** at retrieval, so "how it should be done" rules surface above ordinary knowledge.

**Want to build it yourself?** The skeleton is simple: one vector store (sqlite-vec, chroma, …) plus one embedding service is enough. The real design points are three:

- **Chunk granularity** — too coarse hurts precision, too fine loses context.
- **Rule detection** — use frontmatter / path / naming conventions to separate "rules to follow" from ordinary notes.
- **Wikilink preservation** — let `[[A]]⇄[[B]]` relationships enter retrieval results instead of lying inert in the body text.

A working implementation of this approach is `scripts/build_kb_index.py` (full `--full` / incremental by default; incremental mode diffs `mtime` and only rebuilds changed files).

---

## Quick Start

### Install

```bash
# 1. Python 3.10+ required
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. Dependencies
pip install -r requirements.txt

# 3. Local embeddings (default provider) — needs Ollama running
ollama pull qwen3-embedding:0.6b
```

### Configure

```bash
cp .env.example .env
# edit .env:
#   - LLM_BACKEND=deepseek   → set DEEPSEEK_API_KEY
#   - LLM_BACKEND=ollama      → keep OLLAMA_* defaults
#   - EMBEDDING_PROVIDER=ollama  (local, default) or openai (cloud, needs EMBEDDING_API_KEY)
```

> **Note:** Changing the embedding provider changes the vector space. You must fully rebuild the knowledge-base index afterwards (`python scripts/build_kb_index.py`).

### Run

```bash
# Optional pre-flight check
python scripts/palimpsest_cli.py startup-check
```

> **First-run self-check**: `startup-check` runs checks on key files / storage / FTS / dependencies /
> Embedding service. If the Embedding check fails: for the default local Ollama, make sure Ollama is
> running and `ollama pull qwen3-embedding:0.6b`; if you use cloud `EMBEDDING_PROVIDER=openai`, set
> `EMBEDDING_API_KEY` in `.env`.

```bash
# REST API (:8090)
python -m uvicorn main:app --host 127.0.0.1 --port 8090

# MCP server (stdio — add to any MCP client)
python mcp_server.py

# CLI (example)
python scripts/palimpsest_cli.py search "what changed in the architecture?"

# Dashboard (:8010)
python scripts/dashboard.py

# Index your knowledge base (Obsidian .md files under KNOWLEDGE_DIR)
python scripts/build_kb_index.py
```

On Windows, `scripts/start_rest.vbs` launches the REST service in a hidden window (e.g. at login) and logs to `scripts/start_rest.log`.

**MCP client integration** (generic MCP servers config):

```json
{
  "mcpServers": {
    "palimpsest": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/Palimpsest"
    }
  }
}
```

---

## Configuration

All configuration is read from environment variables (a `.env` file is loaded automatically via `python-dotenv`). See `.env.example` for a commented template.

| Variable | Default | Description |
|---|---|---|
| `REST_PORT` | `8090` | Port for the FastAPI REST service |
| `DASHBOARD_PORT` | `8010` | Port for the dashboard service |
| `DB_PATH` | `data/mh_memory.db` | Path to the embedded TriviumDB database |
| `PALIMPSEST_API_KEY` | *(empty = off)* | Optional REST auth; when set, every route except `/` requires a Bearer / X-API-Key header |
| `LLM_BACKEND` | `deepseek` | LLM backend: `deepseek` or `ollama` |
| `DEEPSEEK_API_KEY` | *(empty)* | API key for the DeepSeek API |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API base URL |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek model identifier |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible base URL |
| `OLLAMA_MODEL` | `deepseek-r1:7b` | Ollama chat model used as the LLM |
| `EMBEDDING_PROVIDER` | `ollama` | Embedding backend: `ollama` (local, private) or `openai` (OpenAI-compatible cloud, e.g. Voyage / SiliconFlow) |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Local Ollama embedding model |
| `OLLAMA_EMBEDDING_BASE_URL` | `http://localhost:11434` | Ollama native embedding API root (decoupled from the LLM's `/v1` URL) |
| `OLLAMA_EMBEDDING_DIM` | `1024` | Embedding dimension (local backend) |
| `EMBEDDING_API_KEY` | *(empty)* | API key for the cloud embedding endpoint |
| `EMBEDDING_BASE_URL` | `https://api.voyageai.com/v1` | Cloud embedding base URL (any OpenAI-compatible endpoint) |
| `EMBEDDING_MODEL` | `voyage-3` | Cloud embedding model |
| `EMBEDDING_DIM` | `1024` | Embedding dimension (cloud backend) |
| `MEMORY_DECAY_FACTOR` | `0.95` | Monthly memory decay used in ranking (`score × importance × factor^(days/30)`); `1.0` disables decay; `kb_chunk` nodes never decay |
| `RULE_RETRIEVAL_WEIGHT` | `1.3` | Score multiplier for rule-domain knowledge slices |
| `DOMAIN_BIAS_WEIGHT` | `1.15` | Extra weight for domain-biased retrieval |
| `EXPAND_MAX_EDGES_PER_NODE` | `20` | Max strongest edges diffused per node during graph expansion |
| `EXPAND_MIN_EDGE_WEIGHT` | `0.0` | Weak-edge pruning threshold during expansion (0 disables) |
| `RRF_K` | `60.0` | RRF constant k for hybrid retrieval (single-side hits still count) |
| `L1_MAX_SIZE` | `5120` | Max bytes of the external L1 memory file (MEMORY.md) read into cache; larger files are skipped |
| `MEM_INGEST_MAX_LENGTH` | `50000` | Max characters of a single memory `content`; longer writes are rejected |
| `KNOWLEDGE_DIR` | *(optional)* | Root of the knowledge base (Obsidian `.md` files) to index |
| `HERMES_MEMORY_FILE` | *(empty)* | Optional path to an external plain-text memory file used as an additional memory source; leave empty to disable |

---

## Usage

### MCP tools (16) — `mcp_tools/*`

| Tool | Description |
|---|---|
| `mem_search` | Unified retrieval across memory / knowledge base / both; optional graph-neighbor expansion, domain bias, block-scoped isolation |
| `mem_hybrid_search` | Hybrid FTS5 + vector retrieval; `mode=rrf` (k=60) or `cascade`; each hit labeled `fts_hit` / `sem_hit` |
| `mem_retrieve` | Semantic retrieval returning a 150-char summary + metadata (never full text) |
| `mem_get_full` | Fetch the full content of a node by ID |
| `mem_ingest` | Write a new memory — with conflict detection, `REVISED_BY` version chaining, secret scanning, and length guards |
| `mem_recent` | Most recent memories (newest first) |
| `mem_review` | Periodic recap of the last N days plus governance candidates (high-value upgrades / outdated cleanup / low-value) |
| `mem_stats` | Store-wide statistics: type / domain / importance / time / graph distributions + hot nodes |
| `mem_version_history` | Walk the `REVISED_BY` chain to show how a fact evolved |
| `mem_consolidate` | Near-duplicate detection; dry-run preview or apply merge |
| `mem_communities` | Leiden community detection: cluster the store into topical groups |
| `kb_index` | Index knowledge-base `.md` files into `kb_chunk` nodes (vectorized) |
| `kb_search` | Semantic search over indexed knowledge chunks |
| `graph_neighbors` | BFS over the knowledge graph from a node (relation filter, depth 1–3, weak-edge filter) |
| `mem_link` | Manually create graph edges (`RELATED_TO` / `CAUSES` / `REFERS_TO`; bidirectional by default) |
| `router_query` | Query rule-domain knowledge slices and extract model/configuration recommendations |

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
| `review` | Periodic recap of the last N days |
| `stats` | Store-wide statistics (totals / domains / importance / time / graph) |
| `kb "QUERY"` | Semantic search over knowledge chunks |
| `consolidate` | Preview merges; `--apply` executes (`--threshold 0.85`, `--max-importance 0.8`) |
| `promote` | Hot-memory promotion candidates; `--apply` raises importance and tags (`--days`, `--min-hits`) |
| `ingest-git` | Index recent git commits as `git_commit` nodes (idempotent) |
| `fts-rebuild` | Rebuild the full FTS5 index |
| `fts-search "QUERY"` | Raw FTS5 search (trigram substring) |
| `startup-check` | Run the startup self-check (exit code 1 on failure) |
| `task-archive` | Archive completed task nodes; `--apply` writes markdown and deletes the node |

Examples:

```bash
python scripts/palimpsest_cli.py ingest "the service listens on 8090" --domain work --importance 0.6
python scripts/palimpsest_cli.py search "8090 port" --neighbors
python scripts/palimpsest_cli.py stats
python scripts/palimpsest_cli.py promote            # preview hot-memory candidates
python scripts/palimpsest_cli.py consolidate        # preview merge candidates
python scripts/palimpsest_cli.py consolidate --apply # execute merges
```

### Blocks

`block` is the "domain-group" concept: the graph is isolated per block, and diffusion retrieval only follows edges inside the same block, preventing cross-domain pollution. Built-in generic blocks: `task` (tasks), `kb` (knowledge base), `hermes` (the assistant's own memory), `novel` (fiction / worldbuilding settings), `general` (unclassified fallback); `rule` is a subset of `kb` (rule slices live in the `kb` block). Any of your own `domain` values can be used as a block (e.g. `--block myproject`). Leaving `--block` empty runs in full mode.

Node ownership is expressed by the `payload.domain` field. Specify a block at write time via `--domain X` or `mem_ingest(domain=...)`; `kb`-type nodes are set automatically by the knowledge-base indexer (`kb` / `rule`).

### REST API — `main.py`, port 8090

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Service info + version + endpoint index |
| `GET` | `/export` | Export memories as a paginated JSON snapshot (default 100/page, max 500) |
| `GET` | `/summary` | Human-readable memory summary (events / character states / plans) |
| `GET` | `/memory/{id}` | Read a single node's full payload |
| `POST` | `/report` | Generate an LLM analysis report over the current store |
| `DELETE` | `/memory/{id}` | Delete a memory node (FTS index synced) |
| `PUT` | `/memory/{id}` | Update a node's payload (**merge semantics**: only supplied fields change, the rest are kept; FTS synced) |
| `PATCH` | `/memory/{id}` | Partial update of a node (same merge semantics, REST-precise) |
| `PATCH` | `/memory/{id}/vector` | Update a node's vector (dimension must match) |
| `POST` | `/mem/search` | Unified retrieval |
| `POST` | `/mem/hybrid-search` | FTS5 + vector hybrid retrieval |
| `POST` | `/mem/ingest` | Write a new memory (conflict detection + secret scan) |
| `POST` | `/mem/link` | Create a graph edge |
| `POST` | `/mem/stats` | Store-wide statistics |
| `POST` | `/graph/neighbors` | Graph neighbors of a node |
| `POST` | `/graph/communities` | Leiden community detection |
| `POST` | `/mem/router` | Task-routing query |

> If `PALIMPSEST_API_KEY` is set, every route except `/` requires `Authorization: Bearer <key>` or `X-API-Key: <key>`.

Example:

```bash
curl -X POST http://127.0.0.1:8090/mem/search \
  -H "Content-Type: application/json" \
  -d '{"query": "architecture", "scope": "all", "top_k": 5}'
```

---

## Tests

```bash
# from the repo root
python -m pytest tests/ -v
```

The suite covers the core loop: write → `mem_search` hit → `mem_get_full` round trip; graph edges → `graph_neighbors` / `mem_communities`; secret scan rejecting key-bearing content; FTS-side hit marking in hybrid retrieval; conflict detection / version chains and outdated filtering semantics; `consolidate` / `promote` dry-run and idempotency; PUT/PATCH partial updates preserving fields; concurrency and failure paths (dirty payloads, embedding unavailable, …).

`tests/conftest.py` redirects `DB_PATH` to a **temporary database** (and isolates the knowledge base) before anything else is imported — the suite never touches a production store, and it runs green with a deterministic fake embedder, no live Ollama needed.

---

## Project structure

```
Palimpsest/
├── README.md                     # Chinese (primary)
├── README_EN.md                  # English
├── LICENSE
├── .env.example                  # commented config template
├── .gitignore
├── requirements.txt
├── config.py                     # env-driven configuration
├── main.py                       # FastAPI REST entry (:8090)
├── mcp_server.py                 # MCP stdio entry (FastMCP)
├── dashboard.html
├── core/                         # shared engine, framework-free
│   ├── trivium_store.py          #   TriviumDB wrapper (vector + graph + doc)
│   ├── conflict.py               #   conflict detection / version chains
│   ├── consolidator.py           #   near-duplicate consolidation
│   ├── stats.py                  #   store-wide statistics (mem_stats core)
│   ├── promoter.py               #   hot-memory promotion (hit_count → promote)
│   ├── fts_index.py              #   FTS5 full-text index (trigram)
│   ├── reporting.py              #   LLM memory reports
│   ├── secret_scan.py            #   pre-write secret scan (10 rules)
│   ├── startup_check.py          #   startup self-check
│   ├── task_archive.py           #   task auto-archiving
│   ├── utils.py
│   └── version.py                #   version from git tag (falls back to dev)
├── mcp_tools/                    # 16 MCP tools (shared across MCP/REST/CLI)
│   ├── __init__.py
│   ├── _common.py                #   shared store / mcp / serialization helpers
│   ├── memory.py                 #   mem_* tools
│   ├── kb.py                     #   kb_index / kb_search
│   ├── graph.py                  #   graph_neighbors / mem_link / mem_communities
│   ├── routing.py                #   router_query
│   ├── consolidate_tool.py       #   mem_consolidate
│   └── stats_tool.py             #   mem_stats
├── scripts/                      # ops tooling
│   ├── palimpsest_cli.py         #   CLI (search/ingest/…/stats/promote)
│   ├── dashboard.py              #   monitoring dashboard (:8010)
│   ├── build_kb_index.py         #   knowledge-base chunking & vectorization
│   ├── build_novel_index.py      #   fiction vault whole-file import (--source)
│   ├── link_novel_relations.py   #   fiction relationship bulk edge creation
│   ├── sync_rules.py             #   rule notes → routing decision tree
│   ├── check_kb_consistency.py   #   knowledge-base vs database consistency
│   ├── check_fts_consistency.py  #   FTS content-level reconciliation
│   ├── export_all_data.py        #   read-only JSON backup export
│   ├── graph_edges.py            #   persisted knowledge-graph edges
│   ├── migrate_domain.py         #   legacy field migration (character_name → domain)
│   ├── rebuild_db.py             #   rebuild the database from an export snapshot
│   ├── start_rest.vbs            #   Windows hidden-window REST launcher
│   └── tdb_stress/               #   TriviumDB stress tests
├── hermes-plugin/                # Hermes dual plugins (Memory Provider + Context Engine)
├── tests/                        # pytest (isolated conftest + fake embedder, offline-green)
└── data/                         # runtime database (gitignored)
    ├── mh_memory.db              #   primary TriviumDB store
    └── fts.db                    #   FTS5 full-text index
```

---

## Development

- **Virtual env:** one per checkout (`python -m venv venv`) + `pip install -r requirements.txt`.
- **Adding a tool:** register it in `mcp_tools/` with the shared `@mcp.tool()` decorator — it instantly appears across the MCP service, the REST layer, and the CLI.
- **Adding a core module:** keep `core/` free of FastAPI/MCP; consume it via `mcp_tools/` and `main.py`. Version is derived from git tags by `core/version.py`.
- **Changed the schema?** Rebuild the FTS index (`fts-rebuild`) and the knowledge-base index (`build_kb_index.py`); export / rebuild helpers live in `scripts/`.
- **Tests:** stay isolated — never point tests at the production database.

Run the tests before opening a PR:

```bash
python -m pytest tests/ -v
```

Releases follow [Semantic Versioning](https://semver.org/), see [RELEASING.md](docs/RELEASING.md) for the process and [CHANGELOG.md](CHANGELOG.md) for history.

---

## License

[MIT](LICENSE) © JiaY-77
