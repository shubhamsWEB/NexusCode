# NexusCode — Codebase Intelligence Platform

## The Problem We're Solving

Every engineering team faces the same silent productivity killer:

> **Developers spend 30–50% of their time just trying to understand the codebase** — reading old code, asking senior engineers questions, waiting for context before they can start building.

When a new ticket arrives, a developer must:
- Manually search across dozens of files to understand what already exists
- Ask teammates "how does X work?" — pulling others out of flow
- Write boilerplate that already exists somewhere in the codebase
- Risk breaking things because they didn't know about a related module

**This is a solved problem at companies like Google, Meta, and GitHub** — they have internal tools that give every engineer instant, deep understanding of the entire codebase. We didn't have that. Until now.

---

## What is NexusCode?

**NexusCode is a Codebase Intelligence Platform** — a centralized, always-on AI brain that knows everything about our codebase and makes that knowledge instantly accessible to any tool, workflow, or team.

Think of it as **a living, searchable knowledge base of the entire codebase**, running on our own infrastructure, under our complete control.

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│               N E X U S C O D E                         │
│         Codebase Intelligence Platform                  │
│                                                         │
│   ┌─────────────┐    ┌─────────────┐   ┌────────────┐  │
│   │   GitHub    │    │  Any Repo   │   │  Any Team  │  │
│   │   Repos     │───▶│   Indexed   │◀──│   Query    │  │
│   └─────────────┘    └─────────────┘   └────────────┘  │
│                                                         │
│         Always up-to-date  ·  Self-hosted  ·  Secure    │
└─────────────────────────────────────────────────────────┘
```

### It is NOT:
- ❌ A GitHub Copilot replacement (it's an infrastructure layer, not an IDE plugin)
- ❌ A third-party SaaS that holds your code (it runs on **our** servers)
- ❌ A one-time snapshot (it **updates automatically** on every git push)

### It IS:
- ✅ An organization-owned, private AI knowledge base of the entire codebase
- ✅ A platform other teams can build their own AI workflows on top of
- ✅ Always in sync with the latest code (live webhook-driven indexing)
- ✅ Production-ready — deployed and indexing real repos today

---

## The Core Architecture (Simple View)

```
                    GITHUB REPOSITORIES
                           │
                    (Push / Webhook)
                           │
                           ▼
              ┌────────────────────────┐
              │   NexusCode Platform   │
              │                        │
              │  1. Parse every file   │
              │  2. Understand code    │  ◀── Tree-sitter AST
              │  3. Create embeddings  │  ◀── AI vector model
              │  4. Store + index      │  ◀── PostgreSQL + pgvector
              └────────────┬───────────┘
                           │
              ┌────────────▼───────────┐
              │    Knowledge API        │
              │   (Always available)    │
              └────────────┬───────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
   JIRA Workflow    Dev Tools         Future Workflows
   (Auto plans)    (Code search)     (Any team can build)
```

When a developer pushes code to GitHub, NexusCode automatically:
1. **Detects the change** via GitHub webhook
2. **Re-parses only changed files** using Tree-sitter (understands Python, JS, TS, Go, Java, Rust, and more)
3. **Creates AI embeddings** of each code chunk with full context
4. **Updates the knowledge base** in seconds

**From push to queryable: under 4 seconds.**

### Automatic Webhook Registration

When you register a new repository through the API or dashboard, NexusCode **automatically creates the GitHub webhook** for you — no manual GitHub settings configuration needed.

```
Register repo via API/Dashboard
        │
        ▼
NexusCode calls GitHub API
(POST /repos/{owner}/{repo}/hooks)
        │
        ├─ Success → Webhook active, push events flow automatically
        │
        └─ Failed (no permissions / localhost) → Clear manual setup
           instructions displayed with pre-filled values
```

- Set `PUBLIC_BASE_URL` in `.env` to your server's public address (ngrok, Railway, etc.)
- Token needs `admin:repo_hook` scope (classic PAT) or **Webhooks: Read & Write** (fine-grained token)
- If auto-registration fails, the API and dashboard show step-by-step manual setup instructions
- Webhooks can also be managed per-repo via the dashboard: register, check status, or remove

---

## The Technology Stack (For Technical Audience)

| Layer | Technology | Why |
|---|---|---|
| Code Understanding | Tree-sitter (12 languages) | Industry-standard AST parser, same as VS Code |
| AI Embeddings | voyage-code-2 (1536 dims) | Best-in-class code embedding model |
| Vector Search | PostgreSQL + pgvector | Semantic similarity — finds code by meaning |
| Lexical Search | pg_trgm + tsvector | Exact keyword matching |
| Combined Ranking | Reciprocal Rank Fusion (RRF) | Gets the best of both search types |
| Reranking | Cross-encoder ML model | Re-scores results for precision |
| Live Indexing | Redis + RQ worker | Background job queue, non-blocking |
| API | FastAPI (Python) | Async, production-grade REST + SSE |
| Protocol | Model Context Protocol (MCP) | Anthropic's standard for AI tool integration |
| Self-hosted | Docker Compose → Railway | Runs on our own infrastructure |

---

## What Makes It Different: We Own the Knowledge


```
┌─────────────────────────────────────────────────────────────┐
│                     TRADITIONAL APPROACH                     │
│                                                             │
│   Dev asks ChatGPT/Copilot → Code goes to third party →    │
│   Generic answer with no knowledge of OUR codebase         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                     NEXUSCODE APPROACH                       │
│                                                             │
│   Dev asks NexusCode → Query hits OUR knowledge base →     │
│   Precise answer with 100% context of OUR actual code       │
│   No code leaves our infrastructure                         │
└─────────────────────────────────────────────────────────────┘
```

- **Code never leaves our servers** — embeddings are stored in our own PostgreSQL
- **AI model calls use only our API keys** — we control cost and access
- **Every team gets the same knowledge** — no siloed understanding
- **Access is token-gated** — we control who can query what

---

## The Vision: A Central AI Platform

NexusCode is not just a search tool. It is an **AI orchestration foundation**.

```
                    ┌─────────────────────────┐
                    │   NexusCode Platform      │
                    │   (Codebase Brain)        │
                    └────────────┬────────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
          ▼                      ▼                      ▼
  ┌───────────────┐    ┌────────────────┐    ┌──────────────────┐
  │ JIRA Workflow │    │  Dev Tooling   │    │  Future Workflows │
  │               │    │                │    │                  │
  │ Auto-generate │    │ Code Search    │    │  PR Review Agent │
  │ impl. plans   │    │ Symbol Lookup  │    │  Onboarding Bot  │
  │ from tickets  │    │ File Context   │    │  Slack Q&A Bot   │
  │               │    │ Caller Graph   │    │  Doc Generator   │
  └───────────────┘    └────────────────┘    └──────────────────┘
         ↑
   Already built
```

Any team in the organization can:
1. Get an **API access token** from NexusCode
2. Use the **Knowledge API** to query codebase context
3. Build their own **agent, workflow, or tool** on top of it
4. Without needing to understand how the indexing works

**NexusCode becomes the AI backbone of the engineering organization.**

---

## Already Live: 6 Knowledge API Tools

NexusCode already exposes 6 tools via a standard AI protocol (MCP):

| Tool | What It Does | Example Use |
|---|---|---|
| `search_codebase` | Semantic + keyword search | "Find all places where JWT tokens are validated" |
| `get_symbol` | Look up any function/class | "Show me the `AuthService` class definition" |
| `find_callers` | Who calls this function? | "What calls `send_email()`?" |
| `get_file_context` | Full structure of a file | "What does `pipeline.py` contain?" |
| `get_agent_context` | Pre-assembled context for a task | "Give me everything relevant to add a new payment method" |
| `plan_implementation` | Generate implementation plans | "How do I add rate limiting to the API?" |

---

## Use Case Already Built: Implementation Planning

**The problem:** Developers start a ticket and spend the first few hours just understanding the codebase well enough to write the plan.

**NexusCode's solution:** Ask the platform what to build and how.

```
Developer types:
"Add rate limiting to the search API endpoint —
 max 100 requests per minute per token"

NexusCode responds in ~3 seconds with:
┌──────────────────────────────────────────────────┐
│  Implementation Plan                             │
│                                                  │
│  Files to modify:                                │
│    1. src/api/app.py          (add middleware)   │
│    2. src/mcp/auth.py         (extract token ID) │
│    3. requirements.txt        (add slowapi)      │
│                                                  │
│  Step 1: Install slowapi rate limiting library   │
│  Step 2: Create rate limiter with Redis backend  │
│  Step 3: Apply limiter decorator to /search      │
│  Step 4: Add 429 error handler                   │
│                                                  │
│  Risks: Redis must be running (already is)       │
│  Test: curl -X POST /search 101 times            │
└──────────────────────────────────────────────────┘
```

The plan is grounded in **our actual code** — not a generic template.

---

## The JIRA Integration Use Case

### The Vision

> **A developer labels a JIRA ticket → an implementation plan appears as a comment on the ticket — automatically, in seconds.**

This is the first full end-to-end AI workflow built on NexusCode. Here is exactly how it works:

---

### The Full Flow — Step by Step

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  STEP 1: Developer labels JIRA ticket                               │
│                                                                     │
│  ┌──────────────────────────────┐                                   │
│  │  JIRA Ticket: NXC-247        │                                   │
│  │  Title: Add payment retry    │                                   │
│  │  Labels: [ready-for-plan] ◀──┼── Developer adds this label      │
│  └──────────────────────────────┘                                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  STEP 2: JIRA fires a webhook → NexusCode receives it               │
│                                                                     │
│  JIRA Webhook ──────────────────▶  NexusCode API                   │
│  {                                 /jira/webhook                    │
│    event: "issue_updated",                                          │
│    issue_key: "NXC-247",                                            │
│    label_added: "ready-for-plan"                                    │
│  }                                                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  STEP 3: NexusCode fetches the full JIRA ticket                     │
│                                                                     │
│  NexusCode calls JIRA API and pulls:                                │
│  ┌────────────────────────────────────────────────────┐             │
│  │  Title:               Add payment retry logic      │             │
│  │  Description:         When a payment fails...      │             │
│  │  Acceptance Criteria: • Retry max 3 times          │             │
│  │                       • Exponential backoff         │             │
│  │                       • Alert after 3 failures     │             │
│  │  Story Points:        5                            │             │
│  │  Components:          payments, notifications      │             │
│  └────────────────────────────────────────────────────┘             │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  STEP 4: Expand ticket into a structured requirement                │
│                                                                     │
│  NexusCode's AI expands the raw ticket into a clean requirement     │
│  document that the Planning Mode can fully understand:              │
│                                                                     │
│  ┌────────────────────────────────────────────────────┐             │
│  │  Feature: Payment Retry with Exponential Backoff   │             │
│  │                                                    │             │
│  │  Context:                                          │             │
│  │  Currently, failed payments are not retried.       │             │
│  │  This causes revenue loss on transient failures.   │             │
│  │                                                    │             │
│  │  Requirements:                                     │             │
│  │  1. On payment failure, retry up to 3 times        │             │
│  │  2. Use exponential backoff (1s, 2s, 4s delays)    │             │
│  │  3. After 3 failures, trigger alert notification   │             │
│  │  4. Store retry count and status in the DB         │             │
│  │                                                    │             │
│  │  Acceptance Criteria: [from ticket]                │             │
│  └────────────────────────────────────────────────────┘             │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  STEP 5: Planning Mode generates the implementation plan            │
│                                                                     │
│  NexusCode queries its own knowledge base to understand:            │
│  • Where does payment logic live in OUR codebase?                   │
│  • What does the existing payment service look like?                │
│  • What notification system do we already use?                      │
│  • What DB models are involved?                                     │
│                                                                     │
│  Then generates a precise, codebase-aware plan:                     │
│  ┌────────────────────────────────────────────────────┐             │
│  │  Implementation Plan — NXC-247                     │             │
│  │                                                    │             │
│  │  Files to modify:                                  │             │
│  │   • src/payments/service.py  (add retry logic)    │             │
│  │   • src/payments/models.py   (add retry_count)    │             │
│  │   • src/notifications/alerts.py (add payment_fail)│             │
│  │   • migrations/004_payment_retry.sql              │             │
│  │                                                    │             │
│  │  Step-by-step with exact code locations            │             │
│  │  Risks, edge cases, and test plan included         │             │
│  └────────────────────────────────────────────────────┘             │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  STEP 6: Plan posted back to JIRA as a comment                      │
│                                                                     │
│  NexusCode calls JIRA API → adds comment to NXC-247:               │
│                                                                     │
│  ┌──────────────────────────────────────────────────┐               │
│  │  NexusCode AI  [just now]                        │               │
│  │                                                  │               │
│  │  Implementation plan generated for NXC-247:      │               │
│  │                                                  │               │
│  │  **Files to modify:** ...                        │               │
│  │  **Step 1:** ...                                 │               │
│  │  **Step 2:** ...                                 │               │
│  │  **Risks:** ...                                  │               │
│  │  **Test Plan:** ...                              │               │
│  │                                                  │               │
│  │  _Generated by NexusCode in 4.2 seconds_         │               │
│  └──────────────────────────────────────────────────┘               │
│                                                                     │
│  Developer opens the ticket → the plan is already there.            │
│  They can start coding immediately.                                 │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### End Result

```
Without NexusCode:                    With NexusCode:

Ticket assigned                       Ticket assigned
     │                                      │
     ▼                                      ▼
Developer reads code        ──▶      Implementation plan is
(1-2 hours)                          already in the ticket
     │                                      │
     ▼                                      ▼
Developer writes plan                 Developer reads plan
(30-60 mins)                          (5 mins)
     │                                      │
     ▼                                      ▼
Developer starts coding               Developer starts coding

Total: 2-3 hours of overhead          Total: 5 minutes of overhead
```

**Estimated time saved: 2-3 hours per ticket, per developer.**

---

## Impact & Business Value

### For Developers
- Start coding faster — the plan is already there when you pick up a ticket
- Reduce "who knows this code?" interruptions
- Onboard to a new service in minutes, not days

### For Tech Leads / Architects
- Consistent, codebase-aware implementation plans
- Reduce review cycles caused by developers going in the wrong direction
- Junior devs can produce senior-quality plans

### For Engineering Management
- Measurable productivity gain per sprint
- Reduced dependency on tribal knowledge (bus factor)
- No code leaves the organization's infrastructure

### For Company
- Proprietary AI infrastructure — not dependent on any vendor's product staying available
- Foundation to build multiple teams' AI workflows from a single platform
- Competitive advantage: engineering teams move faster with AI that knows OUR code

---

## What Has Been Built (POC Status)

This is a working proof of concept with all core infrastructure in place:

| Component | Status |
|---|---|
| GitHub webhook receiver (live indexing) | ✅ Production ready |
| AST parsing (12 languages) | ✅ Production ready |
| AI embeddings pipeline | ✅ Production ready |
| Hybrid search (semantic + keyword) | ✅ Production ready |
| ML reranking for precision | ✅ Production ready |
| Implementation planning (via Claude) | ✅ Production ready |
| REST API with JWT auth | ✅ Production ready |
| MCP protocol (standard AI tool interface) | ✅ Production ready |
| Admin dashboard | ✅ Production ready |
| Docker / Railway deployment | ✅ Production ready |
| 64 automated tests (all passing) | ✅ Production ready |
| Auto webhook registration on repo add | ✅ Production ready |
| **JIRA integration** | 🔲 Next to build (blueprint ready) |

---

## What Can Be Built Next

Because the platform is running and the API is stable, any team can start building:

### Near Term (Low Effort, High Impact)
- **JIRA Workflow** (described above) — highest priority
- **Slack bot** — "Hey @nexuscode, how does auth work?" in any Slack channel
- **PR review context** — automatically add context about changed files to every pull request

### Medium Term
- **Onboarding assistant** — new team members ask questions, get real answers from actual code
- **Test generation** — given a function, generate relevant unit tests
- **Dependency impact analysis** — "If I change this function, what else breaks?"

### Longer Term (The Full Vision)
- **Multi-repo intelligence** — understand relationships across all our microservices
- **Architecture Q&A** — "How does data flow from the frontend to the database for checkout?"
- **Security scanning** — "Find all SQL queries that aren't parameterized"
- **Custom agents per team** — each team builds the AI workflow that fits their process

---

## Why Now?

- **AI models are ready** — the underlying models (Claude, voyage-code-2) are production quality
- **The infrastructure is proven** — vector databases, MCP, and async pipelines all work at scale
- **We control it** — unlike GitHub Copilot or ChatGPT, no code leaves our environment
- **The POC works today** — this is not a proposal, it is a running system

---

## What We Need

To take NexusCode from POC to organization-wide platform:

1. **Index more repos** — add every active GitHub repository to NexusCode (self-service via admin dashboard)
2. **Build the JIRA integration** — the highest-impact first workflow (2-3 sprint effort)
3. **Dedicated infrastructure** — move from local to a permanent server (Railway or AWS)
4. **Team API access** — issue tokens to teams who want to build their own workflows

The codebase intelligence foundation is already built. Everything else is an application layer on top.

---

## Summary

| | |
|---|---|
| **What is NexusCode?** | An organization-owned AI brain that knows our entire codebase |
| **How does it work?** | Indexes GitHub repos automatically, searches by meaning, generates plans |
| **Who built it?** | Internal — we own and control 100% of it |
| **What's already live?** | Full indexing pipeline, hybrid search, planning API, admin dashboard |
| **First big workflow?** | JIRA → auto implementation plan → comment back on ticket |
| **Bigger vision?** | Every team's AI workflows powered by a shared codebase knowledge platform |
| **What's needed?** | Index more repos + build JIRA integration + production infrastructure |

---

*NexusCode — Built internally. Owned entirely. Available to every team.*

*Questions? Contact: Shubham Agrawal*
