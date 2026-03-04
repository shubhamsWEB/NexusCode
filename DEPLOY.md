# Deploying NexusCode to Railway

Complete guide to deploy the Codebase Intelligence MCP Server on [Railway](https://railway.com).

## Architecture on Railway

You'll set up **4 services** in one Railway project:

| Service | Config File | Purpose |
|---------|-------------|---------|
| **App** (web) | `railway.toml` | FastAPI API + MCP server |
| **Worker** | `railway-worker.toml` | RQ background indexing jobs |
| **PostgreSQL** | _(Railway managed)_ | Database with pgvector |
| **Redis** | _(Railway managed)_ | Job queue backend |

---

## Step 1 — Create the Project

1. Go to [railway.com/new](https://railway.com/new)
2. Click **"Deploy from GitHub repo"**
3. Select this repository
4. Railway creates the **App** service automatically using `railway.toml`

## Step 2 — Add PostgreSQL

1. In your project, click **"+ New"** → **"Database"** → **"PostgreSQL"**
2. Railway provisions a Postgres instance and auto-injects `DATABASE_URL`

> **pgvector**: Railway's PostgreSQL 16 supports the `vector` extension. The migration script (`001_init.sql`) runs `CREATE EXTENSION IF NOT EXISTS vector;` automatically.

### Link to App & Worker

Railway auto-creates a `DATABASE_URL` reference variable. Make sure it's shared with both the **App** and **Worker** services:

1. Click the Postgres service → **Variables** tab
2. Copy the `DATABASE_URL` reference
3. Add it to the **App** and **Worker** services' variables

> **Important**: The app expects a `postgresql+asyncpg://` prefix. Add a variable in the App service:
> ```
> DATABASE_URL=${{Postgres.DATABASE_URL | replace("postgresql://", "postgresql+asyncpg://")}}
> ```
> The migration script automatically handles this conversion.

## Step 3 — Add Redis

1. Click **"+ New"** → **"Database"** → **"Redis"**
2. Share `REDIS_URL` with both **App** and **Worker** services

## Step 4 — Add the Worker Service

1. Click **"+ New"** → **"GitHub Repo"** → select this same repository
2. In the Worker service settings:
   - Set **Config File Path** to `/railway-worker.toml`
   - Share `DATABASE_URL` and `REDIS_URL` from the database services

## Step 5 — Set Environment Variables

Add these to the **App** service. Variables marked **required** will prevent startup if missing.

### Required

| Variable | Description |
|----------|-------------|
| `GITHUB_WEBHOOK_SECRET` | HMAC secret for webhook verification |
| `VOYAGE_API_KEY` | Voyage AI API key for code embeddings |
| `JWT_SECRET` | Secret for signing internal JWTs |

### Recommended

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key for Ask/Plan features |
| `GITHUB_TOKEN` | GitHub PAT for repo access |
| `PUBLIC_BASE_URL` | Your Railway public URL (e.g. `https://your-app.up.railway.app`) |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_APP_ID` | — | GitHub App ID (alternative to PAT) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | Path to `.pem` file |
| `GITHUB_OAUTH_CLIENT_ID` | — | For MCP OAuth flow |
| `GITHUB_OAUTH_CLIENT_SECRET` | — | For MCP OAuth flow |
| `GITHUB_DEFAULT_BRANCH` | `main` | Default branch to track |
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Claude model for planning |
| `EMBEDDING_MODEL` | `voyage-code-2` | Embedding model |
| `EMBEDDING_DIMENSIONS` | `1536` | Embedding vector size |
| `DB_POOL_SIZE` | `10` | DB connection pool size |
| `DB_MAX_OVERFLOW` | `20` | Max pool overflow |

> **Tip**: Share `DATABASE_URL` and `REDIS_URL` to the Worker service too.

## Step 6 — Deploy

1. Push to your default branch (or trigger a manual deploy)
2. Watch the **App** deploy logs — you should see:
   ```
   Applying 001_init.sql ... OK
   Applying 002_webhook_hook_id.sql ... OK
   ...
   All migrations applied successfully.
   ```
3. The app starts after migrations complete
4. Verify: visit `https://your-app.up.railway.app/health`
   ```json
   {"status": "ok", "repos": 0, "chunks": 0, ...}
   ```

## Step 7 — Post-Deploy Setup

### Register a Repository

```bash
curl -X POST https://your-app.up.railway.app/repos \
  -H "Content-Type: application/json" \
  -d '{"owner": "your-org", "name": "your-repo"}'
```

### Set Up Webhooks

Set `PUBLIC_BASE_URL` to your Railway URL. The app will auto-register GitHub webhooks, or you can manually configure them:

- **Payload URL**: `https://your-app.up.railway.app/webhook`
- **Content type**: `application/json`
- **Secret**: Same as `GITHUB_WEBHOOK_SECRET`
- **Events**: Push events (at minimum)

---

## Troubleshooting

### Migrations fail with "connection refused"

Ensure the Postgres service is linked and `DATABASE_URL` is available. Check the service variables tab.

### Health check fails (deploy times out)

The app loads ML models on startup (cross-encoder reranker), which can take 30–60s on smaller instances. The `healthcheckTimeout` is set to 60s. If you still hit timeouts, try a larger Railway instance.

### Worker not processing jobs

1. Verify `REDIS_URL` is set in the Worker service
2. Check Worker logs for connection errors
3. Ensure the Worker service config path is set to `/railway-worker.toml`

### pgvector extension error

If you see `ERROR: could not open extension control file ... vector.control`, your Railway Postgres version may not include pgvector. Use Railway's PostgreSQL 16 (default) which includes it.

### `DATABASE_URL` prefix mismatch

The app's SQLAlchemy engine requires `postgresql+asyncpg://` but Railway provides `postgresql://`. Use Railway variable references to transform it:
```
DATABASE_URL=${{Postgres.DATABASE_URL | replace("postgresql://", "postgresql+asyncpg://")}}
```
