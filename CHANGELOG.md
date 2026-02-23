# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- Intent-aware planning responses: `analyze_and_improve` (deep review), `answer_codebase_question` (Q&A), `output_implementation_plan` (implementation tasks)
- Stack fingerprint extraction — web research now knows what packages are already installed before searching for gaps
- Component-aware full retrieval for improvement queries — loads complete source of relevant files instead of 10 semantic fragments
- `GET /events`, `GET /stats/repos`, `GET /stats/recent-files`, `GET /stats/chunk-distribution` API endpoints
- RQ worker health warning in repos dashboard when jobs are stalled
- Repo status lifecycle fix — repos now transition to `ready` after full index completes

### Fixed
- `asyncio.run()` + `AsyncSessionLocal` conflicts in Streamlit dashboard (replaced with HTTP API calls)
- `response_type: plan` incorrectly returned for improvement/review queries
- Web research returning irrelevant generic suggestions for internal system queries

---

## [0.1.0] — 2026-02-22

### Added
- **Infrastructure**: Docker Compose stack (PostgreSQL + pgvector, Redis, API, Worker, Dashboard)
- **GitHub integration**: Webhook receiver with HMAC-SHA256 verification, REST API fetcher
- **Indexing pipeline**: Tree-sitter AST parsing (12+ languages), recursive chunker, scope-chain enricher, voyage-code-2 embeddings
- **Hybrid retrieval**: Semantic vector search + pg_trgm keyword search, RRF merge, cross-encoder reranking
- **MCP server**: 5 tools — `search_codebase`, `get_symbol`, `find_callers`, `get_file_context`, `get_agent_context`
- **Planning mode**: Claude-powered `POST /plan` endpoint with web research + codebase context
- **Admin dashboard**: Streamlit UI with health monitoring, repo management, query tester, webhook setup wizard, planning mode
- **OAuth 2.1 + PKCE** authentication for MCP endpoint
- **Merkle-based incremental indexing** — only re-indexes changed files on push events
- **Railway deployment** config (Procfile, railway.toml)

[Unreleased]: https://github.com/shubhamsWEB/nexusCode_server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/shubhamsWEB/nexusCode_server/releases/tag/v0.1.0
