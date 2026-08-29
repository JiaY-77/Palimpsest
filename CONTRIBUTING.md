# Contributing to Palimpsest

> Palimpsest is a local-first long-term memory system that stores, retrieves, and evolves an AI assistant's memories through semantic vector search, a weighted knowledge graph, and full-text retrieval — all in a single embedded database.

Thanks for taking the time to contribute. Whether you are fixing a bug, adding a tool, improving docs, or reviewing an issue, your help matters. Please also read the [Code of Conduct](CODE_OF_CONDUCT.md); all maintainers and contributors are expected to follow it.

**Table of contents**

- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Adding a New Tool](#adding-a-new-tool)
- [Testing](#testing)
- [Commit Conventions](#commit-conventions)
- [Pull Requests](#pull-requests)
- [Releases](#releases)

---

## Development Setup

Requires **Python 3.10+** and [Ollama](https://ollama.com) (for local embeddings, the default backend).

```bash
# 1. Clone
git clone https://github.com/JiaY-77/Palimpsest.git
cd Palimpsest

# 2. Virtual environment (one per checkout)
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Dependencies (runtime + development/test)
pip install -r requirements.txt -r requirements-dev.txt

# 4. Local embedding model — needs Ollama running first
ollama pull qwen3-embedding:0.6b

# 5. Run the test suite (should be all green — currently 51 tests)
python -m pytest tests/ -v
```

Optional but recommended before first run:

```bash
cp .env.example .env
python scripts/palimpsest_cli.py startup-check
```

> **Note:** `tests/conftest.py` redirects `DB_PATH` to a temporary database before anything is imported, so you do not need (and should not) touch `data/mh_memory.db` while developing.

---

## Project Structure

```
Palimpsest/
├── core/          # shared engine — no FastAPI / MCP dependencies
│   ├── trivium_store.py   # store wrapper (vector + graph + document)
│   ├── conflict.py        # conflict detection / REVISED_BY version chains
│   ├── consolidator.py    # near-duplicate consolidation
│   ├── fts_index.py       # FTS5 full-text index
│   ├── secret_scan.py     # pre-write secret scanning
│   ├── startup_check.py   # startup self-check
│   └── task_archive.py    # completed-task auto-archiving
├── mcp_tools/     # 14 MCP tools — shared by MCP / REST / CLI
├── scripts/       # operational tooling (CLI, dashboard, KB indexing)
├── tests/         # pytest suite (conftest.py isolates the DB)
└── data/          # runtime databases (gitignored — never commit)
```

Three interfaces (MCP stdio, FastAPI REST on `:8090`, CLI) all reuse the same `mcp_tools` layer and the same `core` store, so behavior never drifts.

Keep the layering clean when you add code:

- `core/` must stay **free of FastAPI / MCP imports** — it is pure storage and algorithm logic, consumed through `mcp_tools/` and `main.py`.
- `mcp_tools/` is the tool layer where new capabilities are registered.
- `scripts/` is for operational, human-facing workflows.

---

## Adding a New Tool

New capabilities are exposed as MCP tools so that MCP, REST, and the CLI pick them up from a single registration point.

1. Pick the right module in `mcp_tools/` (or create a new one) — `memory.py` (`mem_*`), `kb.py` (`kb_index` / `kb_search`), `graph.py` (`graph_neighbors` / `mem_link`), `routing.py` (`router_query`).
2. Write a plain Python function that receives a shared `store` (from `mcp_tools/_common.py`), keep docstrings in English, and keep side effects explicit.
3. Register it with the shared decorator, e.g.:

   ```python
   from mcp_tools._common import mcp

   @mcp.tool()
   def my_new_tool(query: str, top_k: int = 5) -> dict:
       """One-line English description of what the tool does."""
       ...
   ```

   Once registered with `@mcp.tool()`, the tool **automatically appears in the MCP server, the REST layer, and the CLI** — no further wiring needed.
4. If the tool should be reachable via a friendly CLI subcommand, add the binding in `scripts/palimpsest_cli.py`; add the REST endpoint in `main.py` for direct HTTP access.
5. Add tests (see below) and run the full suite.

If you add a **core module** instead: keep `core/` framework-free, consume it from `mcp_tools/` / `main.py`, and make sure the module is exported where appropriate. Version numbers come from git tags via `core/version.py` (`git describe --tags`) — do not hardcode versions.

If your change touches the storage schema or the FTS index, rebuild both:

```bash
python scripts/palimpsest_cli.py fts-rebuild
python scripts/build_kb_index.py
```

---

## Testing

The suite currently has **51 tests** (smoke + core algorithm unit tests + transaction tests):

```bash
python -m pytest tests/ -v
```

Ground rules:

- **Any new or modified feature must include tests**, and the full suite must pass before you open a PR.
- **Keep tests isolated.** `tests/conftest.py` points `DB_PATH` at a temporary database and isolates the knowledge base. Never point a test at `data/mh_memory.db`.
- Prefer asserting on behavior (return values, storage state) over implementation details, and keep the tests fast — they run on CI for every PR across Python 3.10 / 3.11 / 3.12.

---

## Commit Conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/) with **English descriptions** (`feat` / `fix` / `refactor` / `docs` / `chore` / `perf` / `ci` / `build` / `test`). See `git log` for recent examples.

```text
feat(mcp_tools): add mem_link for manual graph edge creation
fix(conflict): prevent version chaining across domains
docs(readme): document the blocks concept
refactor(core): split consolidate into internal helpers
perf(store): single-connection iteration
```

- Keep commits focused — one logical change per commit.
- Use an optional scope to indicate the area (`core`, `mcp_tools`, `cli`, `docs`, …).
- Never commit secrets: `.env`, API keys, or private keys. `.env` and `data/` are gitignored — keep it that way.

---

## Pull Requests

1. **Fork** the repository and create your feature branch from `main`:

   ```bash
   git checkout -b feat/my-change
   ```

2. **Commit** your work following the conventions above.
3. Push and open a pull request via GitHub.
4. In the PR description, made clear:
   - What the change does and why (link to the issue if one exists).
   - **Tests run** (e.g. `python -m pytest tests/ -v` → "51 passed") and any tests added.
   - Screenshots or example output if your change affects user-visible behavior (CLI / REST / dashboard).

5. **CI must be green.** Every PR runs the full test suite on Python 3.10 / 3.11 / 3.12 — if a job fails, fix it and re-push before requesting review.

A maintainer will review your PR; please be patient and responsive to feedback. Small, well-tested PRs review fastest.

---

## Releases

Releases follow [Semantic Versioning](https://semver.org/). The release checklist (version decision, changelog update, tagging, GitHub Release) lives in **[docs/RELEASING.md](docs/RELEASING.md)** (currently in Chinese). Key rules:

- Tests must be all green and CI must pass before any release.
- Sanitization audit must pass before release — no secrets in the repository history.
- Never reuse a version tag already published.

---

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE) © JiaY-77.