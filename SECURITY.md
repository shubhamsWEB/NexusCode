# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| `main` branch | ✅ |
| Older releases | ❌ — please upgrade |

## Reporting a Vulnerability

**Please do not open a public GitHub Issue for security vulnerabilities.**

If you discover a security vulnerability, please disclose it responsibly:

1. **Email**: Open a [GitHub Security Advisory](https://github.com/shubhamsWEB/nexusCode_server/security/advisories/new) (preferred — keeps it private until fixed)
2. **Response time**: We aim to acknowledge reports within **48 hours** and provide a fix within **14 days** for critical issues
3. **Credit**: Reporters will be credited in the release notes unless they prefer anonymity

### What to include

- Description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept if possible)
- Affected versions
- Any suggested mitigations

---

## Security Design

### Secrets and credentials

- **Never commit** real secrets to git. Use `.env` (gitignored) or your deployment platform's secret manager
- The `.env.example` file contains only placeholder values — never real credentials
- All API keys (`ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `GITHUB_TOKEN`) are loaded exclusively from environment variables via Pydantic settings

### Authentication

- MCP server uses **OAuth 2.1 + PKCE** with short-lived JWTs (`JWT_EXPIRY_HOURS`, default 8)
- Webhook receiver validates **HMAC-SHA256 signatures** for every incoming GitHub event
- No credentials are stored in the database

### Network exposure

- The API server (`0.0.0.0:8000`) should be placed behind a reverse proxy or VPN in production
- The Streamlit dashboard (`0.0.0.0:8501`) is an admin tool — restrict access to trusted networks
- The MCP endpoint (`/mcp`) requires a valid JWT for all tool calls

### Data storage

- Code chunks and embeddings are stored in PostgreSQL — treat your database as sensitive
- No raw source code is transmitted to external services except:
  - **Voyage AI** — receives code chunks for embedding generation
  - **Anthropic** — receives code context for planning queries (only when `/plan` is called)
  - **GitHub API** — fetches repository contents using your GitHub token

### Docker

- The container runs as a non-root user in production images
- Secrets should be injected via Docker secrets or environment variables — never baked into images

---

## Known Limitations

- The planning feature sends code context to the Anthropic API. Review your organisation's data policies before indexing sensitive repositories.
- Voyage AI embeddings are computed server-side; chunks are sent over HTTPS to Voyage AI's API.

---

## Dependency Security

Dependencies are pinned in `requirements.txt`. To check for known vulnerabilities:

```bash
pip install pip-audit
pip-audit -r requirements.txt
```

We use GitHub Dependabot to receive automated alerts for vulnerable dependencies.
