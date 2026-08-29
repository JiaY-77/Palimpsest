<div align="center">

# Palimpsest

**English** | [中文](./README.md)

**A local-first long-term memory system** that stores, retrieves, and evolves an AI assistant's memories through semantic vector search, a weighted knowledge graph, and full-text retrieval — all in a single embedded database.

Pa·limp·sest: *a writing surface that is overwritten again and again while older incisions survive beneath the new text.* Palimpsest applies the same idea to memory: nothing is ever silently lost — new facts supersede old ones through an explicit, traceable **version chain** (`REVISED_BY`), and near-duplicates are passively consolidated instead of cluttering the store.

| | |
|---|---|
| Version | v2.2.0 |
| Python | 3.10+ |
| License | MIT |
| Storage | TriviumDB 0.8.2 (vector + graph + document, embedded) |
| Backends | DeepSeek / Ollama (LLM), Ollama / OpenAI-compatible (embeddings) |
| CI | [![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml) |

</div>

---

## Features

- **Hybrid retrieval.** Semantic vector search (cosine) fused with an FTS5 full-text index (`trigram` tokenizer for Chinese substring matching) via Reciprocal Rank Fusion (RRF) or a cascade (FTS coarse-filter → vector re-rank). Every hit is labeled with its source — `fts_hit` / `sem_hit`.
- **Knowledge-graph recall.** Nodes are connected by **directed, weighted edges** (`RELATED_TO`, `REVISED_BY`, with `CAUSES` / `REFERS_TO` reserved). Retrieval expands along edges with BFS: per-node expansion is pruned to the strongest edges, weak edges are filtered, and diffusion can be scoped to a single domain "block" to prevent cross-domain pollution.
- **Conflict detection & version chains.** Every write is compared against similar existing memories. A superseded record is marked `outdated` and linked to its replacement with a `REVISED_BY` edge, preserving a browsable history of how a fact evolved. Multi-layer guards (type whitelist, type isolation, domain isolation) prevent false marking.
- **Pre-write secret scan.** Before anything is stored, content is scanned against **10 regular-expression rules** (API keys, tokens, private keys, card/phone numbers, bearer tokens, …). A match **rejects the write** and reports which rule fired.
- **Capacity consolidation.** `mem_consolidate` finds near-duplicate `memory` node pairs (similarity ≥ 0.85), protecting high-value memories (`importance ≥ 0.8`). `dry_run` previews candidates; `apply` merges them into a new node and chains the old ones via `REVISED_BY`.
- **Memory lifecycle.** Time decay weighting (`MEMORY_DECAY_FACTOR`, default 0.95 /month) fades stale memories in ranking without touching storage; `kb_chunk` knowledge slices are exempt.
- **Task auto-archiving.** Completed task nodes are moved out of the hot store into markdown archives under the knowledge base (`05_任务归档/`), then deleted — dry-run first, `apply` to commit.
- **Startup self-check.** `startup-check` verifies critical files, storage initialization, and the FTS index, emitting a structured report and a non-zero exit code on failure.
- **Token-efficient by design.** Retrieval returns a **150-character summary plus metadata** instead of full text; full content is fetched on demand.
- **Three interfaces, one core.** MCP (stdio) for agent tooling, a FastAPI REST service, and a full CLI — all reuse the same underlying tools, so behavior never drifts.

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
                    │   mcp_tools/*   (14 tools, shared)        │
                    │                                           │
                    │   ┌───────────────────────────────────┐   │
                    │   │             core/                 │   │
                    │   │  trivium_store   ·   conflict     │   │
                    │   │  consolidator    ·   reporting    │   │
                    │   │  fts_index       ·   secret_scan  │   │
                    │   │  startup_check   ·   task_archive │   │
                    │   │  utils           ·   version      │   │
                    │   └───────────────┬───────────────────┘   │
                    └───────────────────┼───────────────────────┘
                                        │
       ┌────────────────────────────────┼──────────────────────────────┐
       │                    ┌───────────▼───────────┐    ┌─────────────▼──────┐
       │                    │     TriviumDB 0.8.2   │    │   SQLite FTS5      │
       │                    │  vector + graph + doc │    │   fts.db (trigram) │
       │                    └───────────┬───────────┘    └────────────────────┘
       │                                │  embeddings
       └────────────────────────────────┴──────────────────────────────────
                      Ollama (qwen3-embedding:0.6b, 1024-d)
                               or OpenAI-compatible API
```

Three independently usable entry points share one `mcp_tools` layer and one `core` store:

1. **MCP server** (`mcp_server.py`) — exposes 14 tools over stdio for any Model Context Protocol client.
2. **REST service** (`main.py`) — FastAPI on port 8090, including a startup self-check and a JSON memory export for backups.
3. **CLI** (`scripts/palimpsest_cli.py`) — 15 subcommands for scripting, cron, and operations without a server.

The dashboard (`scripts/dashboard.py`, port 8010) provides a lightweight human-facing view of the store.

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

> **First-run self-check**：`startup-check` runs 5 checks (key files / storage / FTS / dependencies /
> Embedding service). If the Embedding check fails: for the default local Ollama, make sure Ollama is
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

All configuration is read from environment variables (a `.env` file is loaded automatically via `python-dotenv`).

| Variable | Default | Description |
|---|---|---|
| `REST_PORT` | `8090` | Port for the FastAPI REST service |
| `DASHBOARD_PORT` | `8010` | Port for the dashboard service |
| `DB_PATH` | `data/mh_memory.db` | Path to the embedded TriviumDB database |
| `LLM_BACKEND` | `deepseek` | LLM backend: `deepseek` or `ollama` |
| `DEEPSEEK_API_KEY` | *(empty)* | API key for the DeepSeek API |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API base URL |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek model identifier |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible base URL |
| `OLLAMA_MODEL` | `deepseek-r1:7b` | Ollama chat model used as the LLM |
| `EMBEDDING_PROVIDER` | `ollama` | Embedding backend: `ollama` (local, private) or `openai` (OpenAI-compatible cloud, e.g. Voyage) |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Local Ollama embedding model |
| `OLLAMA_EMBEDDING_DIM` | `1024` | Embedding dimension (local backend) |
| `EMBEDDING_API_KEY` | *(empty)* | API key for the cloud embedding endpoint |
| `EMBEDDING_BASE_URL` | `https://api.voyageai.com/v1` | Cloud embedding base URL (any OpenAI-compatible endpoint) |
| `EMBEDDING_MODEL` | `voyage-3` | Cloud embedding model |
| `EMBEDDING_DIM` | `1024` | Embedding dimension (cloud backend) |
| `MEMORY_DECAY_FACTOR` | `0.95` | Monthly memory decay used in ranking (`score × importance × factor^(days/30)`); `1.0` disables decay; `kb_chunk` nodes never decay |
| `RULE_RETRIEVAL_WEIGHT` | `1.3` | Score multiplier for rule-domain knowledge slices |
| `DOMAIN_BIAS_WEIGHT` | `1.15` | Extra weight for domain-biased retrieval |
| `KNOWLEDGE_DIR` | *(optional)* | Root of the knowledge base (Obsidian `.md` files) to index |
| `HERMES_MEMORY_FILE` | *(empty)* | Optional path to an external plain-text memory file used as an additional memory source; leave empty to disable |

See `.env.example` for a commented template.

---

## Usage

### MCP tools (14) — `mcp_tools/*`

| Tool | Description |
|---|---|
| `mem_search` | Unified retrieval across memory / knowledge base / both; optional graph-neighbor expansion, domain bias, block-scoped diffusion |
| `mem_hybrid_search` | Hybrid FTS5 + vector retrieval; `mode=rrf` (k=60) or `cascade`; each hit labeled `fts_hit` / `sem_hit` |
| `mem_retrieve` | Semantic retrieval returning a 150-char summary + metadata (never full text) |
| `mem_get_full` | Fetch the full content of a node by ID |
| `mem_ingest` | Write a new memory — with conflict detection, `REVISED_BY` version chaining, and secret scanning |
| `mem_recent` | Most recent memories (newest first) |
| `mem_review` | Periodic recap of the last N days plus governance candidates |
| `mem_version_history` | Walk the `REVISED_BY` chain to show how a fact evolved |
| `mem_consolidate` | Near-duplicate detection; dry-run preview or apply merge |
| `kb_index` | Index knowledge-base `.md` files into `kb_chunk` nodes (vectorized) |
| `kb_search` | Semantic search over indexed knowledge chunks |
| `graph_neighbors` | BFS over the knowledge graph from a node (relation filter, depth 1–3) |
| `mem_link` | Manually create graph edges (`RELATED_TO` / `CAUSES` / `REFERS_TO`; bidirectional by default) |
| `router_query` | Query rule-domain knowledge slices and extract model/configuration recommendations |

### CLI (15) — `scripts/palimpsest_cli.py`

| Command | Description |
|---|---|
| `search "QUERY"` | Unified retrieval (`--scope all\|memory\|kb`, `--neighbors`, `--block`) |
| `hybrid-search "QUERY"` | FTS5 + vector hybrid retrieval (`--mode rrf\|cascade`) |
| `ingest "CONTENT"` | Write a new memory (`--importance 0.5`, `--type memory`, `--domain`) |
| `link --source N --target N` | Create a graph edge (`--relation`, `--one-way`) |
| `index` | Scan and index the knowledge base |
| `graph --id N` | Graph neighbors of a node (`--depth`, `--relation`, `--min-weight`) |
| `recent` | Recent memories (`--limit`, `--domain`) |
| `review` | Period review over the last N days |
| `kb "QUERY"` | Semantic search over knowledge chunks |
| `consolidate` | Consolidation preview; `--apply` merges (`--threshold 0.85`, `--max-importance 0.8`) |
| `ingest-git` | Index recent git commits as `git_commit` nodes (idempotent) |
| `fts-rebuild` | Rebuild the full FTS5 index |
| `fts-search "QUERY"` | Raw FTS5 search (trigram substring) |
| `startup-check` | Run the startup self-check (exit code 1 on failure) |
| `task-archive` | Archive completed tasks; `--apply` writes markdown and deletes nodes |

Examples:

```bash
python scripts/palimpsest_cli.py ingest "The service listens on port 8090" --domain work --importance 0.6
python scripts/palimpsest_cli.py search "port 8090" --neighbors
python scripts/palimpsest_cli.py fts-search "8090"
python scripts/palimpsest_cli.py consolidate              # preview
python scripts/palimpsest_cli.py consolidate --apply      # merge
```

### Blocks

A `block` is a domain-grouping concept: the graph is partitioned by block, so diffusion retrieval only walks edges within the same block, preventing cross-domain pollution. Built-in blocks are `task`, `kb` (knowledge base), `hermes` (the assistant's own memory), and `general` (an uncategorized fallback); `rule` is a subset of `kb` (rule slices, grouped under the `kb` block). You can also use your own `domain` as a block (e.g. `--block myproject`). Leaving `--block` empty switches to full-scope retrieval.

### REST API — `main.py`, port 8090

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Service info + version + endpoint index |
| `GET` | `/export` | Export all memories as a compact JSON snapshot (by importance) |
| `GET` | `/summary` | Human-readable memory summary (events / character state / plans) |
| `POST` | `/report` | Generate a LLM-based analysis report from the current store |
| `DELETE` | `/memory/{id}` | Delete a memory node (FTS index kept in sync) |
| `PUT` | `/memory/{id}` | Update a node's payload/metadata |
| `PATCH` | `/memory/{id}/vector` | Update a node's vector (dimension must match) |
| `POST` | `/mem/search` | Unified retrieval |
| `POST` | `/mem/hybrid-search` | Hybrid FTS5 + vector retrieval |
| `POST` | `/mem/ingest` | Write a new memory (with conflict detection + secret scan) |
| `POST` | `/mem/link` | Create a graph edge |
| `POST` | `/graph/neighbors` | Graph neighbors of a node |
| `POST` | `/mem/router` | Task-routing query |

Example:

```bash
curl -X POST http://127.0.0.1:8090/mem/search \
  -H "Content-Type: application/json" \
  -d '{"query": "architecture", "scope": "all", "top_k": 5}'
```

---

## Testing

```bash
# from the repository root
python -m pytest tests/ -v
```

The suite is 6 smoke tests covering the core loop:

- ingest → `mem_search` hit → `mem_get_full` full-text roundtrip
- graph edge creation → `graph_neighbors`
- secret scan **rejects** content containing an API key (and reports the matching rule)
- hybrid retrieval — FTS side (trigram substring) hit marked `fts_hit`
- `consolidate` dry-run surfaces candidates without mutating nodes
- `task_archive` dry-run vs apply (archival markdown + node deletion)

`tests/conftest.py` redirects `DB_PATH` to a **temporary database** (and isolates the knowledge base) before anything is imported — the suite never touches `data/mh_memory.db`.

---

## Project Structure

```
Palimpsest/
├── README.md
├── LICENSE
├── .env.example
├── .gitignore
├── requirements.txt
├── config.py                     # environment-driven configuration
├── main.py                       # FastAPI REST entry point (:8090)
├── mcp_server.py                 # MCP stdio entry point (FastMCP)
├── dashboard.html
├── core/                         # shared engine, no framework dependencies
│   ├── trivium_store.py          #   TriviumDB wrapper (vector+graph+document)
│   ├── conflict.py               #   conflict detection / version chaining
│   ├── consolidator.py           #   near-duplicate memory consolidation
│   ├── fts_index.py              #   FTS5 full-text index (trigram, fts.db)
│   ├── reporting.py              #   LLM-generated memory reports
│   ├── secret_scan.py            #   pre-write secret scanning (10 rules)
│   ├── startup_check.py          #   startup self-check
│   ├── task_archive.py           #   completed-task auto-archiving
│   ├── utils.py
│   └── version.py                #   version from git tags (fallback: dev)
├── mcp_tools/                    # 14 MCP tools (shared by MCP/REST/CLI)
│   ├── __init__.py
│   ├── _common.py                #   shared store / mcp / serialization helpers
│   ├── memory.py                 #   mem_* tools
│   ├── kb.py                     #   kb_index / kb_search
│   ├── graph.py                  #   graph_neighbors / mem_link
│   ├── routing.py                #   router_query
│   └── consolidate_tool.py       #   mem_consolidate
├── scripts/                      # operational tooling
│   ├── palimpsest_cli.py         #   15-command CLI
│   ├── dashboard.py              #   dashboard service (:8010)
│   ├── build_kb_index.py         #   knowledge-base chunking & vectorization
│   ├── sync_rules.py             #   rule notes → model-routing decision tree
│   ├── check_kb_consistency.py   #   KB vs DB consistency checks
│   ├── export_all_data.py        #   read-only JSON backup export
│   ├── graph_edges.py            #   persistent knowledge-graph edges
│   ├── rebuild_db.py             #   rebuild DB from an export snapshot
│   ├── start_rest.vbs            #   Windows hidden-window REST launcher
│   └── tdb_stress/               #   TriviumDB stress tests
│       ├── tdb_stress.py
│       └── tdb_stress2.py
├── tests/
│   ├── conftest.py               # temporary-DB isolation
│   └── test_smoke.py             # 6 smoke tests
└── data/                         # runtime databases (gitignored)
    ├── mh_memory.db              #   main TriviumDB store
    └── fts.db                    #   FTS5 full-text index
```

---

## Development

- **Virtual environment:** create one per checkout (`python -m venv venv`) and `pip install -r requirements.txt`.
- **Adding a tool:** register it inside `mcp_tools/` with the shared `@mcp.tool()` decorator — it becomes immediately available to the MCP server, the REST layer, and the CLI.
- **Adding a core module:** keep `core/` free of FastAPI/MCP imports; consume it through `mcp_tools/` and `main.py`. `versions` are discovered from git tags (`git describe --tags`) by `core/version.py`.
- **Breaking a schema?** rebuild the FTS index (`fts-search`... `fts-rebuild`) and the knowledge-base index (`build_kb_index.py`); export/rebuild tooling lives in `scripts/`.
- **Tests:** keep them isolated — never point a test at `data/mh_memory.db`.

Before opening a pull request, run the smoke suite:

```bash
python -m pytest tests/ -v
```

---

## License

[MIT](LICENSE) © JiaY-77
