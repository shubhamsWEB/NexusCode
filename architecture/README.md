# NexusCode — Architecture Documentation

This directory contains deep technical documentation for every component of NexusCode.
Start with **overview.md** for the big picture, then dive into specific subsystems.

---

## Index

| Document | What it covers |
|----------|----------------|
| [overview.md](./overview.md) | System components, technology stack, high-level data flow |
| [indexing-pipeline.md](./indexing-pipeline.md) | GitHub → Webhook → Parser → Embedder → PostgreSQL |
| [retrieval-pipeline.md](./retrieval-pipeline.md) | Hybrid search, RRF, cross-encoder reranking, context assembly |
| [agent-system.md](./agent-system.md) | AgentLoop, gate system, tool execution, roles |
| [workflow-engine.md](./workflow-engine.md) | YAML DSL, DAG execution, parallel waves, human checkpoints |
| [database-schema.md](./database-schema.md) | All tables, columns, indexes, relationships, migrations |
| [llm-providers.md](./llm-providers.md) | Multi-LLM abstraction, supported models, routing, retries |
| [mcp-integration.md](./mcp-integration.md) | MCP protocol, tool registration, external server bridge |
| [how-to-use.md](./how-to-use.md) | End-to-end step-by-step guide from zero to productive |

---

## Quick Architecture Summary

```
┌──────────────────────────────────────────────────────────┐
│                    CLIENTS / CONSUMERS                    │
│  Claude Code  ·  Claude Desktop  ·  Cursor  ·  REST API  │
└───────────────────────────┬──────────────────────────────┘
                            │ MCP + REST
┌───────────────────────────▼──────────────────────────────┐
│                    NexusCode Server                       │
│  FastAPI (8000)  ·  MCP SSE (/mcp)  ·  Streamlit (8501)  │
│                                                           │
│  Planning Mode ──► AgentLoop ──► Tools ──► Retrieval      │
│  Ask Mode      ──► AgentLoop ──► Tools ──► Retrieval      │
│  Workflows     ──► DAG Executor ──► AgentLoop             │
└──────┬────────────────────────────────────┬──────────────┘
       │                                    │
┌──────▼──────┐                   ┌─────────▼────────┐
│ PostgreSQL  │                   │     Redis         │
│ + pgvector  │                   │  RQ job queue     │
│ (storage +  │                   │  pub/sub events   │
│  HNSW idx)  │                   │  embedding cache  │
└─────────────┘                   └──────────────────┘
```

See [overview.md](./overview.md) for the full component diagram.
