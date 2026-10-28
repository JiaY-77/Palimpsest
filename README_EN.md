<div align="center">

# Palimpsest

**All You Need Is Just Memory.** — What you need is a memory of your own. The session ends; the memory stays.

**Local-first · vectors / full-text / graph / time in one kernel · MCP + REST + CLI share one set of tools**

| | |
|---|---|
| Version | v2.17.0 |
| Python | 3.10+ |
| License | MIT |
| Storage | TriviumDB 0.8.10 (vector + graph + document, embedded) |
| Backends | DeepSeek / Ollama (LLM), built-in local / Ollama / OpenAI-compatible (embeddings) |
| CI | [![CI](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml/badge.svg)](https://github.com/JiaY-77/Palimpsest/actions/workflows/ci.yml) |

[中文](./README.md) | **English**

</div>

> *Palimpsest*: a writing surface that is overwritten again and again while older incisions survive beneath the new text. The same image, applied to memory: **new facts supersede old ones, but the old trace is never truly lost** — every rewrite is joined by a queryable **version chain** (`REVISED_BY`).

---

## What it is

A **local memory store**: what an AI session learns becomes nodes in one database on your machine, and any later session can find it again **by meaning**, not by keyword.

- **Runs without an API key.** Storage, retrieval, the graph and version chains happen entirely locally; an LLM is only used when generating reports and can be left unconfigured.
- **Zero external dependencies.** A built-in vector engine (`llama-cpp-python` + a bundled GGUF model) runs fully offline once installed; Ollama is an optional speed-up path, not a requirement.
- **One process is the single writer.** REST on `:8090` is the only entry point — MCP / CLI / scripts all go through it, so no second writer can corrupt the store.
- **The data is one directory.** `.db` + `.vec` / `.gidx` / `.pidx` sidecars plus `fts.db` under `data/`: back all of it up and you can move the store.

---

## Install

**5–10 minutes end to end, no API key needed.** The embedding model is ~610 MB and downloads on first run only.

### 1. Clone and install dependencies

```bash
python --version                  # 3.10+ required
git clone https://github.com/JiaY-77/Palimpsest.git
cd Palimpsest
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

> ⚠️ This project is **not published on PyPI**. `pip install palimpsest` installs an unrelated package uploaded in 2023 — it cannot start.

**Success criterion:** `pip install` exits 0 and `python -c "import fastapi, triviumdb"` prints nothing.

### 2. The vector engine (the default is already decided)

With `EMBEDDING_PROVIDER` unset, it auto-detects: a cloud `EMBEDDING_API_KEY` means `openai`, otherwise it uses the **built-in `local` engine (recommended, zero external dependencies)**.

```bash
cp .env.example .env

# llama-cpp-python has no prebuilt wheel on PyPI, so a plain install compiles from
# source (needs CMake + MSVC and mostly fails). Use the official CPU wheel index:
pip install "llama-cpp-python>=0.3.36,<0.4" \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

The first start downloads `Qwen3-Embedding-0.6B` Q8_0 (~610 MB; ModelScope by default — reachable from mainland China, with HuggingFace fallback), sha256-verified and resumable. **Once only, fully offline afterwards.**

**Already running Ollama?** Reuse it and skip the download (optional, not required):

```bash
ollama pull qwen3-embedding:0.6b
echo "EMBEDDING_PROVIDER=ollama" >> .env
```

**Success criterion:** the `pip install` exits 0 with no CMake / MSVC error (the model is fetched on first start, step 3).

### 3. Start the service

```bash
python scripts/palimpsest_cli.py doctor      # deployment check: prints the fix for every failed item
python -m uvicorn main:app --host 127.0.0.1 --port 8090
```

> **Never run multiple workers / instances**: triviumdb opens the store files in exclusive write mode, so a second write connection fails with `Database locked`, and a failed concurrent write can pollute the file group without self-healing. Conflicts are loud (the CLI raises `DatabaseBusyError`, REST returns `503`), not a silent downgrade.

On Windows, `scripts/start_rest.vbs` launches it in a hidden window (logs to `scripts/start_rest.log`).

**Success criterion:** every item of the deployment check is OK and the service stays up.

### 4. Verify (all three must pass)

```bash
# (1) the service is alive
curl -s http://127.0.0.1:8090/
# returns {"service":"Palimpsest","version":"v2.17.0","endpoints":[...]}

# (2) the health check is all green
python scripts/palimpsest_cli.py doctor

# (3) write and read work — this checks semantic recall itself
python scripts/palimpsest_cli.py ingest "install smoke test"
python scripts/palimpsest_cli.py search "装好了吗"
# returns {"results":[{"id":1,"type":"memory","score":0.4815,"summary":"install smoke test",...}]}
```

(3) is the real acceptance test: **written in English, queried in Chinese, not one word in common — and it still hits.** If it only succeeds through keyword matching, the vector lane is not connected.

### 5. Plug it into your agent

MCP clients (recommended — shares the REST process):

```json
{ "mcpServers": { "palimpsest": { "url": "http://127.0.0.1:8090/mcp/" } } }
```

Hermes (two plugins: Memory Provider + Context Engine):

```bash
mkdir -p ~/.hermes/plugins/palimpsest && cp hermes-plugin/* ~/.hermes/plugins/palimpsest/
hermes plugins enable palimpsest
hermes config set memory.provider palimpsest
hermes config set context.engine palimpsest-graph
```

**Success criterion:** your agent's tool list shows `palimpsest_search` / `palimpsest_ingest`, and a query returns your own memories.

> The integration surface is standard protocol: MCP (stdio / streamable-http), REST and the CLI all point at the same process and the same underlying tools, so **several clients can share one memory store**. The multi-writer consistency spec is still in design; for now keep a single primary writer.
>
> **All three embedding backends, hardware requirements, swapping models → [`docs/INSTALL.md`](docs/INSTALL.md)**

---

## What it solves

| Symptom | How it is solved | Run it yourself |
|---|---|---|
| A new session forgets what you talked about | High-signal points are written into the local store; a new session recalls them **semantically**, not by keyword | `mem_search "the architecture we discussed"` |
| Memory piles up, several versions of one thing | Superseding is **declared by the writer** (`replaces`); the old version is marked `outdated` and linked through a `REVISED_BY` chain — never silently overwritten | `mem_version_history <id>` |
| A pile of Markdown the AI cannot read through | Sliced into the store **by headings** (a heading is a semantic boundary a human drew), with `[[wikilinks]]` turned into graph edges | `build_kb_index.py` then `kb "TERM"` |
| It forgets *why* a decision was made | Nodes carry type / importance / time dimensions; reasoning is stored as `thought` nodes | `mem_fact_history <id>` |
| Memory lives on someone else's server | Everything stays in the local `data/` directory; back up the whole file group to move it | `python scripts/backup_db.py --keep 7` |
| Context blows up after many turns | Retrieval returns a **150-char summary + metadata** by default; full text is fetched on demand | watch the size of a `mem_retrieve` response |

**One design floor:** no node is ever superseded **because it looks similar**. Similarity produces only relevance hints (`related_ids`); whether to supersede is decided by the party holding the full context — "I like apples" and "I like apple pie" are highly similar and are not the same thing at all.

**Day-to-day usage** (what to record, how to grade `importance`, when to supersede) is in **[`docs/USAGE.md`](docs/USAGE.md)** (Chinese).

---

## Why not the others

| | md folders | Pure vector stores | Cloud-hosted memory | **Palimpsest** |
|---|---|---|---|---|
| What it stores | files | vectors | vectors / text | nodes + weighted edges + vectors + time |
| Ranking | none | yes | yes | yes (importance / time decay / hit counts) |
| Conflict governance | none | none | partial | **explicit superseding + `REVISED_BY` version chains** |
| Semantic recall | none (grep) | yes | yes | yes (vectors + FTS5 + RRF fusion) |
| Relations | none | none | partial | **weighted graph + BFS diffusion + Leiden communities** |
| Version traceability | none | none | rare | **bi-temporal (`valid_at` / `invalid_at`)** |
| Data ownership | yours | yours | the platform's | **yours** |
| Install cost | zero | medium | zero (needs an account) | **medium (Python + embedding model)** |

**Three things we do not dodge:**

1. **Installing is more work than an md folder.** On that point the md approach wins — granted. Where it starts to break is right after the install: once files reach the hundreds to thousands range, ranking, conflict governance, semantic recall and version chains all break together.
2. **Single machine, single process.** One store allows exactly one writing process at a time; it is not designed for sharing across machines.
3. **The multi-writer consistency spec is not done yet.** For now, keep a single primary writer.

> Mechanism inventory, the item-by-item comparison with LangGraph Store / AutoGen Memory, where Markdown sits, use cases → **[`docs/DESIGN.md`](docs/DESIGN.md)** (Chinese)

---

## When it breaks

Run the health check first — it prints the matching fix command for every failed item:

```bash
python scripts/palimpsest_cli.py doctor
```

| Symptom | Cause | Fix |
|---|---|---|
| The `Embedding` check fails | the built-in model is missing / corrupt, or Ollama is not running, or the cloud key is unset | delete the model file and re-download; `ollama pull qwen3-embedding:0.6b`; or fill in `EMBEDDING_API_KEY` |
| `Database locked` / REST `503` | a second process opened the same store | keep exactly one writing process; stop the old REST / CLI first |
| Vector dimension mismatch | the embedding model or provider was switched | `reindex --force --yes` re-embeds everything and writes the new fingerprint |
| `pip install llama-cpp-python` fails to build | it went down the compile-from-source path | use the official CPU wheel index (see Install, step 2) |
| The port is taken | 8090 is already listened on by another process | switch `uvicorn --port`, or find the occupying process first |

**REST is the single writer.** The CLI / dashboard / all scripts go through REST; stdio `mcp_server.py` is used only in pure-MCP setups that do not run REST (the escape hatch), because it opens the store files directly in exclusive write mode.

Still stuck → open an issue: **[new issue](https://github.com/JiaY-77/Palimpsest/issues/new/choose)**. Bring these three and fixes get turned around ten times faster:

1. **The full `doctor` output** (secrets are masked; still give it one look for real keys before pasting)
2. **OS / Python version / exactly which step you are stuck at** (referencing the numbered Install steps)
3. **One reproducible command** (a single one is enough — paste the whole call, not "it does not run")

- Bug: [`bug.yml`](.github/ISSUE_TEMPLATE/bug.yml) — Feature request: [`feature.yml`](.github/ISSUE_TEMPLATE/feature.yml)
- Security vulnerabilities: do **not** open a public issue, see [`SECURITY.md`](SECURITY.md)
- Want to change code: start with [`CONTRIBUTING.md`](CONTRIBUTING.md) (dev environment / adding tools / test requirements / commit conventions / PR process)
- Merge gates: `pytest` + `ruff check` + `ruff format --check` + `mypy` + `scripts/readme_check.py --strict`; all five green before landing

---

## Configuration

All configuration is read from environment variables (a `.env` file is loaded automatically via `python-dotenv`); see `.env.example` for a commented template. Most users only need these:

| Variable | Default | Description |
|---|---|---|
| `REST_PORT` | `8090` | Port for the FastAPI REST service |
| `DB_PATH` | `data/mh_memory.db` | Path to the embedded TriviumDB database |
| `EMBEDDING_PROVIDER` | *(empty = auto-detect)* | Embedding backend: empty auto-detects (cloud key → `openai`, otherwise `local`), or explicitly `local` / `ollama` / `openai` |
| `DEEPSEEK_API_KEY` | *(empty)* | Used for report generation; optional |
| `KNOWLEDGE_DIR` | *(optional)* | Root of the knowledge base (Obsidian `.md` files) to index |

The full 62-key table (defaults and activation conditions) is in [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) (Chinese).

---

## Swapping models / re-embedding

Switching the embedding model or provider changes the vector space, so you **must re-embed the full store**. The store keeps a **vector-space fingerprint** (a hash of `provider` / `model` / `dim` / `pooling`); the startup self-check, `doctor` and `reindex` all compare against it — on a mismatch `doctor` fails that item and prints the fix command. After changing the model or provider, run `reindex --force --yes`: it drops the resume checkpoint, skips the fingerprint prompt, re-embeds everything and writes the new fingerprint when done. `--yes` is required because `--force` does not skip the final `reindex N nodes? [y/N]` prompt (non-interactive callers would hit `EOF when reading a line`); the command opens the store directly, so stop the REST service first. Full sequence in [`docs/INSTALL.md`](docs/INSTALL.md).

---

## Documentation map

The README only covers what you need to get running. Everything else lives in `docs/` — **when adding content, find its place in this table instead of growing the README**. Most reference docs are in Chinese; the code, API and configuration keys are the same either way.

| What you want | Where |
|---|---|
| Install (three embedding backends, hardware, swapping models) | [docs/INSTALL.md](docs/INSTALL.md) |
| Command reference (21 MCP tools / 25 CLI subcommands / 31 REST routes / blocks / index rules) | [docs/REFERENCE.md](docs/REFERENCE.md) |
| Which tool to use when | [docs/USAGE.md](docs/USAGE.md) |
| Environment variables (full 62-key table) | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Design (mechanism inventory, comparison, Markdown positioning, use cases) | [docs/DESIGN.md](docs/DESIGN.md) |
| Tests / evaluation / repo layout / dev conventions | [docs/TESTING.md](docs/TESTING.md) |
| Changing code (layering, adding tools, gates) | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Hermes integration (two plugins, seven steps) | [docs/HERMES_INTEGRATION.md](docs/HERMES_INTEGRATION.md) |
| Obsidian vault integration | [docs/OBSIDIAN.md](docs/OBSIDIAN.md) |
| Bi-temporal / security / memory packs / full API | [BITEMPORAL](docs/BITEMPORAL.md) · [SECURITY](SECURITY.md) · [MEMORYHUB](docs/MEMORYHUB.md) · [API](docs/API.md) |
| Retrieval quality and measurements | [RETRIEVAL_QUALITY](docs/RETRIEVAL_QUALITY.md) · [CONFIDENCE](docs/CONFIDENCE.md) |
| Release process / history / deprecations | [RELEASING](docs/RELEASING.md) · [CHANGELOG](CHANGELOG.md) · [DEPRECATIONS](docs/DEPRECATIONS.md) |

---

## License

[MIT](LICENSE) © JiaY-77
