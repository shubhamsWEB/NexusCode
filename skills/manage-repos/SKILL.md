---
name: manage-repos
description: Register a new GitHub repository for indexing, trigger full or incremental re-indexing, check per-repo stats, and remove repositories. Use when setting up NexusCode for a new repo, when a repo's index is stale, or when cleaning up a deleted repository.
metadata:
  author: nexuscode
  version: "1.0"
compatibility: Requires NexusCode API at http://localhost:8000 with GITHUB_TOKEN or GITHUB_APP credentials configured.
---

# Manage Repositories Skill

## Register a new repository

```bash
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{
    "owner": "myorg",
    "name": "my-repo",
    "branch": "main",
    "index_now": true
  }'
```

`index_now: true` immediately queues a full indexing job. Omit to register without indexing.

The dashboard equivalent: **📦 Repositories** → "Add Repository".

## Check indexing status

```bash
GET http://localhost:8000/repos
```

Returns all repos with stats: `status`, `active_chunks`, `symbols`, `files`, `last_indexed`.

Possible status values:
- `pending` — registered, not indexed yet
- `indexing` — indexing job is running
- `ready` — index is current
- `error` — last indexing attempt failed

## Trigger re-indexing

```bash
POST http://localhost:8000/repos/{owner}/{name}/index
```

Queues a full re-index job (re-fetches all files from GitHub, re-embeds everything).
Use after: repo branch changes, large refactors, or when search quality degrades.

**Check job progress:**
```bash
GET http://localhost:8000/jobs
```

Returns recent RQ job history with status (`queued`, `started`, `finished`, `failed`).

## Remove a repository

```bash
DELETE http://localhost:8000/repos/{owner}/{name}
```

**Hard-deletes** all data: chunks, symbols, merkle nodes, and the repo row.
This is irreversible. The next `POST /repos` + index will rebuild from scratch.

## Webhook-triggered indexing (incremental)

Once a repo is registered, configure a GitHub push webhook to keep it current:

1. Expose your server publicly (ngrok, Railway, etc.)
2. Set the webhook URL to: `https://your-server.com/webhook`
3. Content type: `application/json`
4. Secret: your `GITHUB_WEBHOOK_SECRET` value
5. Event: **Push** only

On each push, NexusCode automatically diffs changed files using Merkle trees
and only re-indexes what changed (typically < 1s per push).

Dashboard wizard: **🔗 Webhook Setup** → step-by-step guide.

## View configuration

```bash
GET http://localhost:8000/config
```

Returns masked values of all env vars grouped by section (GitHub, DB, Redis, embeddings, MCP auth).

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| `status: error` on repo | GitHub token invalid or repo private | Update `GITHUB_TOKEN` in `.env`, restart |
| `active_chunks: 0` | Index job queued but worker not running | Start RQ worker |
| Search returns no results | VOYAGE_API_KEY invalid | Update key in `.env`, re-index |
| Webhook not triggering | Wrong secret or URL | Run **Step 4 — Test Ping** in Webhook Setup wizard |
| Index stale after large PR | Webhook missed or failed | Manually trigger `POST /repos/{owner}/{name}/index` |
