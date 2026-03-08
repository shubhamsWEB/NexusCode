# Deployment Guide

NexusCode runs three processes: the FastAPI server, the RQ worker, and the optional Streamlit dashboard. This page covers local Docker Compose, Railway, and bare-metal production setups.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Public Internet                                             │
│   GitHub push webhook ──────────────────────────────┐      │
│   Claude Desktop / MCP clients ─────────────────┐   │      │
└─────────────────────────────────────────────────│───│──────┘
                                                  │   │
┌──────────────────────────────────────────────── │───│──────┐
│ NexusCode Server                                │   │      │
│                                                 ▼   ▼      │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  FastAPI :8000 — API + MCP SSE + webhook receiver  │  │
│   └──────────────────────┬──────────────────────────────┘  │
│                          │ enqueue                         │
│   ┌───────────────────────▼──────────────────┐            │
│   │  RQ Worker — background indexing jobs    │            │
│   └──────────────────────────────────────────┘            │
│                                                            │
│   ┌──────────────────────┐  ┌──────────────────────────┐  │
│   │  PostgreSQL + pgvector│  │  Redis (job queue)       │  │
│   └──────────────────────┘  └──────────────────────────┘  │
│                                                            │
│   ┌──────────────────────┐                                 │
│   │  Streamlit :8501      │  (optional admin dashboard)   │
│   └──────────────────────┘                                 │
└────────────────────────────────────────────────────────────┘
```

---

## Docker Compose (Local / Staging)

```bash
cp .env.example .env
# Fill in your API keys in .env

docker compose up --build
```

**`docker-compose.yml`** (already in repo) starts:
- `api` — FastAPI on port 8000
- `worker` — RQ indexing worker
- `postgres` — PostgreSQL 15 with pgvector
- `redis` — Redis 7

Services are available at:
- API: `http://localhost:8000`
- Dashboard: `http://localhost:8501` (start separately)
- Docs: `http://localhost:8000/docs`

### Initialize DB on first run

```bash
docker compose exec api python scripts/init_db.py
```

### View logs

```bash
docker compose logs -f api
docker compose logs -f worker
```

---

## Railway (Recommended for Production)

Railway auto-detects the `Procfile` and `railway.toml` in the repo.

### Step 1: Create a Railway project

```bash
npm install -g @railway/cli
railway login
railway init
```

### Step 2: Add services

In the Railway dashboard, add:
- **PostgreSQL** — Railway provides managed Postgres with pgvector support
- **Redis** — Railway provides managed Redis

### Step 3: Set environment variables

In Railway → your service → **Variables**, add all required env vars from [configuration.md](./configuration.md).

Railway sets `DATABASE_URL` and `REDIS_URL` automatically when you link the Postgres and Redis services.

### Step 4: Deploy

```bash
railway up
```

Railway deploys from the `Procfile`:
```
web:    uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
worker: rq worker indexing --url $REDIS_URL
```

### Step 5: Configure webhook URL

After deployment, Railway gives you a public URL (e.g., `https://nexuscode-prod.railway.app`). Set it in `.env` / Railway variables:

```bash
PUBLIC_BASE_URL=https://nexuscode-prod.railway.app
```

Then register a repo — the webhook is auto-created.

---

## Bare Metal / VPS Production Setup

### Prerequisites

```bash
# Ubuntu 22.04
sudo apt install python3.12 python3.12-venv postgresql-15 redis-server nginx

# Install pgvector
sudo apt install postgresql-15-pgvector

# Enable pgvector extension
sudo -u postgres psql -c "CREATE EXTENSION IF NOT EXISTS vector;"

# weasyprint system dependencies (required for PDF generation)
sudo apt install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  libcairo2 libffi-dev libglib2.0-0
```

### Process management with systemd

**`/etc/systemd/system/nexuscode-api.service`**:
```ini
[Unit]
Description=NexusCode API Server
After=network.target postgresql.service redis.service

[Service]
User=nexuscode
WorkingDirectory=/opt/nexuscode
EnvironmentFile=/opt/nexuscode/.env
Environment=PYTHONPATH=.
ExecStart=/opt/nexuscode/.venv/bin/uvicorn src.api.app:app \
  --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/nexuscode-worker.service`**:
```ini
[Unit]
Description=NexusCode Indexing Worker
After=network.target redis.service

[Service]
User=nexuscode
WorkingDirectory=/opt/nexuscode
EnvironmentFile=/opt/nexuscode/.env
Environment=PYTHONPATH=.
Environment=OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
ExecStart=/opt/nexuscode/.venv/bin/rq worker indexing --url ${REDIS_URL}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable nexuscode-api nexuscode-worker
sudo systemctl start  nexuscode-api nexuscode-worker
```

### Nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name nexuscode.yourcompany.com;

    ssl_certificate     /etc/letsencrypt/live/nexuscode.yourcompany.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexuscode.yourcompany.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # Required for SSE (MCP streaming)
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
    }
}

server {
    listen 80;
    server_name nexuscode.yourcompany.com;
    return 301 https://$host$request_uri;
}
```

> **Important:** The `proxy_buffering off` setting is required for SSE (used by MCP streaming and plan/ask streaming). Without it, SSE connections will stall.

---

## Pre-flight Check

After any deployment:

```bash
PYTHONPATH=. python scripts/deploy_check.py
```

This verifies:
- Database connection and extensions
- Redis connection
- Required environment variables
- At least one LLM API key

---

## Database Migrations

Run after every update that includes new migration files:

```bash
# List migration files
ls src/storage/migrations/

# Apply all (idempotent — safe to re-run)
for f in src/storage/migrations/*.sql; do
  echo "Applying $f..."
  psql $DATABASE_URL -f "$f"
done
```

Current migrations:
| File | Contents |
|---|---|
| `001_init.sql` | Core schema: chunks, symbols, merkle_nodes, repos, webhook_events |
| `003_chat_history.sql` | Chat sessions and plan history |
| `004_indices.sql` | Performance indices on chunks |
| `005_parent_chunks.sql` | Parent-child chunk relationships |
| `006_enriched_fts.sql` | Full-text search index on enriched_content |
| `007_hnsw.sql` | HNSW vector index (replaces ivfflat) |
| `008_external_mcp_servers.sql` | External MCP server registry |
| `009_knowledge_graph.sql` | Knowledge graph nodes and edges |
| `010_workflow_tables.sql` | Workflow definitions, runs, steps, checkpoints |
| `011_agent_roles.sql` | Custom agent role overrides table |
| `012_agent_roles.sql` | Agent role additional fields |
| `013_generated_documents.sql` | PDF document storage (BYTEA) |

---

## Scaling

### Multiple API workers

```bash
# 4 workers — good for high request volume
uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --workers 4
```

### Multiple indexing workers

```bash
# Start additional workers for faster parallel indexing
rq worker indexing --url $REDIS_URL &
rq worker indexing --url $REDIS_URL &
```

### Connection pooling

Increase `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` for high concurrency:
```bash
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40
```

### HNSW tuning

Increase `HNSW_EF_SEARCH` for better recall at the cost of query latency:
```bash
HNSW_EF_SEARCH=80   # better recall
HNSW_EF_SEARCH=20   # faster queries
```

---

## Monitoring

### Health endpoint

```bash
watch -n 5 'curl -s http://localhost:8000/health | python3 -m json.tool'
```

### Recent webhook events

```bash
curl http://localhost:8000/events?limit=10
```

### RQ job status

```bash
rq info --url $REDIS_URL
```

### Index stats by repo

```bash
curl http://localhost:8000/stats/repos
```

---

## Backup

### Database

```bash
pg_dump $DATABASE_URL > nexuscode_backup_$(date +%Y%m%d).sql
```

The database is the only stateful component. Redis only holds job queues (ephemeral). The vector index is rebuilt automatically on restore.

### Restore

```bash
psql $DATABASE_URL < nexuscode_backup_20260301.sql
```

No re-indexing required after restore.
