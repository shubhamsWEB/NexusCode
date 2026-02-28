# Connecting GitHub Repositories

NexusCode supports two ways to authenticate with GitHub and two ways to keep indexes fresh. This page covers all of them.

---

## Authentication Methods

### Option A — Personal Access Token (PAT)

The fastest way to get started. Suitable for personal projects or single-team setups.

1. Go to **GitHub → Settings → Developer Settings → Personal access tokens → Tokens (classic)**
2. Click **Generate new token (classic)**
3. Select scopes:
   - `repo` — read private repositories
   - `read:org` — if indexing organization repos
4. Copy the token and add to `.env`:

```bash
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

**Limitations:** The token is tied to your personal account. Use a GitHub App for production.

---

### Option B — GitHub App (Recommended for Production)

A GitHub App can be installed on any organization and works without a personal account.

**Step 1: Create the GitHub App**

1. Go to **GitHub → Settings → Developer Settings → GitHub Apps → New GitHub App**
2. Fill in:
   - **App name**: `nexuscode-prod` (or any name)
   - **Homepage URL**: your server's public URL
   - **Webhook URL**: `https://your-server.com/webhook`
   - **Webhook secret**: a random string (save this as `GITHUB_WEBHOOK_SECRET`)
3. Set **Repository permissions**:
   - Contents: **Read**
   - Metadata: **Read**
   - Webhooks: **Read & Write** (to auto-register)
4. Set **Subscribe to events**: `Push`
5. Click **Create GitHub App**

**Step 2: Generate a private key**

On the app's settings page, scroll to **Private keys** → **Generate a private key**. Save the `.pem` file.

**Step 3: Install the app on your org/repos**

Go to the app's **Install App** tab and install it on your organization or select specific repos.

**Step 4: Configure environment**

```bash
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/nexuscode.pem
# Do NOT set GITHUB_TOKEN when using GitHub App
```

---

## Registering a Repository

### Via REST API

```bash
# Minimal — triggers indexing immediately
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{
    "owner": "your-org",
    "name":  "your-repo",
    "index_now": true
  }'

# With options
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{
    "owner":      "your-org",
    "name":       "your-repo",
    "branch":     "main",
    "index_now":  true
  }'
```

### Via Dashboard

Open `http://localhost:8501`, go to the **Repos** tab, and click **Register New Repo**.

### List registered repos

```bash
curl http://localhost:8000/repos
```

### Trigger a full re-index

```bash
curl -X POST http://localhost:8000/repos/your-org/your-repo/index
```

### Delete a repo (removes all data)

```bash
curl -X DELETE http://localhost:8000/repos/your-org/your-repo
```

---

## GitHub Webhooks (Live Updates)

Webhooks let NexusCode automatically re-index changed files within seconds of every push. Without webhooks, you need to manually trigger re-indexing.

### How it works

```
git push → GitHub → POST /webhook → HMAC verify → RQ queue → incremental index
```

Only the changed files are re-indexed. Unchanged files are skipped via Merkle tree comparison. End-to-end latency from push to queryable: **2–4 seconds**.

---

### Setting Up Webhooks

#### Method 1: Auto-registration (easiest)

If your server is publicly accessible, NexusCode can register webhooks automatically when you add a repo.

Set in `.env`:
```bash
PUBLIC_BASE_URL=https://your-server.com
```

Then register a repo with `index_now: true` — the webhook is created automatically.

#### Method 2: Manual setup in GitHub

1. Go to your repo → **Settings → Webhooks → Add webhook**
2. Fill in:
   - **Payload URL**: `https://your-server.com/webhook`
   - **Content type**: `application/json`
   - **Secret**: the value of your `GITHUB_WEBHOOK_SECRET`
   - **Events**: select **Just the push event**
3. Click **Add webhook**

GitHub will send a ping — check your server logs for `webhook: received push event`.

#### Method 3: Test locally with the simulation script

```bash
# Simulate a push webhook (requires server running)
PYTHONPATH=. python scripts/simulate_webhook.py \
  --owner your-org \
  --repo  your-repo \
  --file  src/main.py

# Simulate a file deletion
PYTHONPATH=. python scripts/simulate_webhook.py \
  --owner  your-org \
  --repo   your-repo \
  --file   src/old_file.py \
  --delete
```

---

### Webhook Security (HMAC Verification)

Every incoming webhook request is verified using HMAC-SHA256. Requests with invalid or missing signatures are rejected with HTTP 401.

```bash
# Set a strong random secret
GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
```

The same value must be set in both GitHub's webhook configuration and your `.env`.

---

### Monitoring Webhook Events

```bash
# Recent events via API
curl "http://localhost:8000/events?limit=20"

# Filter by repo
curl "http://localhost:8000/events?repo_owner=your-org&repo_name=your-repo"
```

Events have statuses: `queued → processing → done` (or `error`).

Check the Dashboard → **Events** tab for a visual view with error messages.

---

## Supported File Types

NexusCode indexes these extensions by default:

```
.py  .ts  .tsx  .js  .jsx  .java  .go  .rs  .cpp  .c  .cs  .rb
.swift  .kt  .json  .md  .yaml  .yml  .html  .css  .scss  .sh
.sql  .xml  .toml
```

Ignored by default: `node_modules`, `.git`, `__pycache__`, `dist`, `build`, `*.min.js`, test files.

Customize via environment:
```bash
SUPPORTED_EXTENSIONS=.py,.ts,.go,.rs
IGNORE_PATTERNS=node_modules,.git,vendor/,fixtures/
```

---

## Monorepo Support

For monorepos, register the repo once. Queries can be scoped by path prefix:

```bash
# Search only in the payments service subtree
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "billing logic", "repo": "your-org/monorepo"}'
```

Path-prefix filtering is supported in all retrieval endpoints and MCP tools via the `repo` parameter.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401` from GitHub | Wrong `GITHUB_TOKEN` or token missing `repo` scope |
| Webhook shows `pending` | Check `GITHUB_WEBHOOK_SECRET` matches on both sides |
| Files not re-indexed | Worker not running — start `rq worker indexing` |
| Webhook times out | GitHub expects a response within 10s — the server queues async, it should respond immediately |
| Token expired | GitHub Apps generate short-lived tokens automatically — check `GITHUB_APP_PRIVATE_KEY_PATH` |
