# Security Policy

## Built-in security features

Palimpsest scans content **before it is written to the store**. See [`core/secret_scan.py`](core/secret_scan.py) for the implementation:

- **Strong rules** (API keys, tokens, private keys, bearer tokens, SSH keys, …) — a match **rejects the write** and reports which rule fired.
- **Weak rules** (Chinese ID-card numbers, phone numbers) — a match is allowed through but marked with a `secret_hint` flag for later audit.

The rest of the design is privacy-friendly by default: all data stays in a local embedded database, and the default embedding backend is **Ollama**, running locally. Nothing is sent to a cloud embedding endpoint unless you explicitly configure `EMBEDDING_PROVIDER=openai`.

## Reporting a vulnerability

**Do not open a public issue for security problems.** Public issues are visible to everyone; never paste real secrets, keys, tokens, or personal data there.

Please report vulnerabilities through the GitHub Security Advisory ("Private vulnerability reporting"):

- **GitHub Security Advisory:** <https://github.com/JiaY-77/Palimpsest/security/advisories/new>

If you prefer, you can open a **public issue** for *non-sensitive* problems (e.g. a rule that misses a clearly-fictional token type), but keep details generic and redact everything that looks like a real secret.

### What to include in a report

A good report helps us triage quickly:

- **Affected version(s)** — e.g. the git tag (`git describe --tags`) or the version reported by `curl http://localhost:8090/`.
- **Steps to reproduce** — minimal, concrete, and reproducible.
- **Impact** — what an attacker could do, and under which conditions.
- **(Optional) Suggested fix.**

## Response commitment

- **Acknowledgement:** within **48 hours** of a report reaching us (via the Security Advisory channel).
- **Investigation / fix:** typically within **7 days**, depending on severity. Critical issues are prioritized.

We will keep you updated on progress and credit you in the release notes if you wish.

## Security practices for everyone

- **Never commit secrets.** `.env` and `data/` are gitignored. Do not commit `.env`, `.env.example` values that are real keys, API keys, or private keys.
- **Use local embeddings by default** to keep memory private: the default `EMBEDDING_PROVIDER=ollama` runs everything on your machine. Only switch to a cloud provider when you intend it and understand the data leaves your machine.
- **Sanitize before pushing to a public repo.** Before you push, run the repository through `core/secret_scan.py` (or a tool such as git-secrets) and scrub any leaked secrets — public git history is hard to fully delete.
- **Protected branches:** pushes to `main` go through pull requests, and CI runs tests on every PR.