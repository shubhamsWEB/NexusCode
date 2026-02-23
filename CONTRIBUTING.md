# Contributing to NexusCode

Thank you for your interest in contributing! This guide covers everything you need to get started.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you agree to uphold it. Please report unacceptable behaviour to
the maintainers via GitHub Issues.

---

## Ways to Contribute

- **Bug fixes** — open an issue first to confirm it's a bug, then submit a PR
- **Features** — open a feature request issue to discuss before building
- **Documentation** — typos, unclear explanations, missing examples
- **Tests** — expanding coverage for existing or new functionality
- **Performance** — profiling and improvements to indexing / retrieval speed

---

## Development Setup

### Prerequisites

| Tool | Minimum version |
|------|----------------|
| Python | 3.11+ |
| Docker & Docker Compose | 24+ |
| Git | 2.40+ |

### 1. Fork and clone

```bash
git clone https://github.com/<your-fork>/nexusCode_server.git
cd nexusCode_server
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
# Runtime dependencies
pip install -r requirements.txt

# Dev-only dependencies (testing, linting, type-checking)
pip install -r requirements-dev.txt
```

### 4. Install pre-commit hooks

```bash
pre-commit install
```

### 5. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in the required API keys
```

Required keys for development:

| Variable | Where to get it |
|----------|----------------|
| `GITHUB_WEBHOOK_SECRET` | Any random string for local dev |
| `VOYAGE_API_KEY` | [dash.voyageai.com](https://dash.voyageai.com) |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) (planning feature only) |
| `JWT_SECRET` | Any random string for local dev |

### 6. Start infrastructure

```bash
docker compose up postgres redis -d
```

### 7. Initialise the database

```bash
PYTHONPATH=. python scripts/init_db.py
```

### 8. Run the API server

```bash
make dev
# or manually:
PYTHONPATH=. uvicorn src.api.app:app --reload --port 8000
```

---

## Project Structure

```
src/
  api/          # FastAPI routes (search, plan, repos, webhooks)
  github/       # GitHub webhook receiver and fetcher
  pipeline/     # Indexing pipeline (parser → chunker → enricher → embedder)
  planning/     # /plan endpoint — Claude-powered implementation planner
  retrieval/    # Hybrid search, reranking, context assembly
  storage/      # PostgreSQL models, async DB helpers, migrations
  mcp/          # MCP server (5 tools) + OAuth 2.1 auth
  ui/           # Streamlit admin dashboard
scripts/        # CLI utilities (init_db, full_index, simulate_webhook, deploy_check)
tests/          # pytest test suite (no network/DB required)
```

---

## Coding Standards

This project uses:

- **[Ruff](https://docs.astral.sh/ruff/)** — linting and import sorting (replaces flake8, isort)
- **[Mypy](https://mypy.readthedocs.io/)** — static type checking
- **[Black](https://black.readthedocs.io/)** — code formatting (via ruff-format)

All checks run automatically on commit via pre-commit hooks, and in CI on every PR.

### Style rules

- Use `from __future__ import annotations` at the top of every file
- Add type annotations to all function signatures
- Keep functions focused — if a function is over ~60 lines, consider splitting
- Prefer `async def` for all database and I/O operations
- Reference real file paths and line numbers in commit messages and PR descriptions

### Running checks manually

```bash
make lint       # ruff check
make format     # ruff format
make typecheck  # mypy
make test       # pytest
make check      # all of the above
```

---

## Testing

Tests live in `tests/` and use pytest. They run **without any network calls or live database** — all external dependencies are mocked.

```bash
# Run all tests
make test

# With coverage report
make test-cov

# Run a single file
pytest tests/test_chunker.py -v

# Run tests matching a keyword
pytest -k "test_chunker" -v
```

### Writing tests

- Place new tests in `tests/test_<module>.py`
- Mock all I/O — use `unittest.mock.AsyncMock` for async DB calls
- Name tests descriptively: `test_<what>_<condition>_<expected_result>`
- Aim to keep each test under 30 lines

---

## Submitting a Pull Request

1. **Branch** — create a feature branch from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Code** — make your changes following the coding standards above

3. **Test** — ensure all tests pass:
   ```bash
   make check
   ```

4. **Commit** — follow [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat: add rate limiting to /search endpoint
   fix: resolve asyncpg InterfaceError in dashboard
   docs: clarify MCP tool parameters in README
   test: add coverage for chunker edge cases
   refactor: extract stack fingerprint to separate module
   ```

5. **Push and open a PR** against `main`. Fill in the PR template.

6. **CI must pass** — the PR cannot be merged until all GitHub Actions checks are green.

7. **Review** — a maintainer will review within 48 hours. Address feedback and push additional commits to the same branch.

### PR checklist

- [ ] Tests added or updated for the change
- [ ] `make check` passes locally
- [ ] PR description explains *what* and *why* (not just *what*)
- [ ] No secrets or credentials in the diff

---

## Reporting Bugs

Use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.yml).

Include:
- Exact error message and stack trace
- Steps to reproduce (minimal reproduction preferred)
- Environment: OS, Python version, Docker version
- Relevant log output (`docker compose logs app`)

---

## Requesting Features

Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.yml).

Include:
- Problem statement — what are you trying to do?
- Proposed solution — how should it work?
- Alternatives considered
- Any relevant prior art or references

---

## Questions?

Open a [GitHub Discussion](https://github.com/shubhamsWEB/nexusCode_server/discussions) for general questions. Use Issues only for actionable bugs and feature requests.
