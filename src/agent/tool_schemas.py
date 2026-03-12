"""
Anthropic-format tool schemas for all NexusCode internal tools.

These schemas are given directly to Claude during inference so it understands
exactly what each tool does, what it returns, when to use it, and how to call it.
Rich descriptions are critical: Claude decides which tool to call (and with what
input) based entirely on what is written here.

Tool categories:
  RETRIEVAL_TOOL_SCHEMAS     — core 4: search/symbol/callers/file  (always included)
  EXTENDED_TOOL_SCHEMAS      — 3 higher-order: agent-context/plan/ask
  ALL_INTERNAL_TOOL_SCHEMAS  — all 7 combined
  ASK_RETRIEVAL_TOOL_SCHEMAS — trimmed set for Ask Mode (drops get_file_context)
  THINK_TOOL_SCHEMA          — meta-loop self-evaluation scratchpad (injected by AgentLoop)
"""

from __future__ import annotations

# ── Tool 1: search_codebase ────────────────────────────────────────────────────

SEARCH_CODEBASE_SCHEMA: dict = {
    "name": "search_codebase",
    "description": (
        "Hybrid semantic + keyword search over all indexed code, merged with RRF "
        "(Reciprocal Rank Fusion) and re-ranked by a cross-encoder. "
        "The PRIMARY discovery tool — call this first for any unfamiliar code path.\n\n"
        "HOW IT WORKS:\n"
        "1. Embeds the query with voyage-code-2 (1536-dim) for semantic similarity.\n"
        "2. Runs tsvector keyword search in parallel across all chunks.\n"
        "3. Merges both result sets using RRF so the best matches from each rank surface.\n"
        "4. Re-ranks merged results with a cross-encoder for final precision.\n\n"
        "RETURNS (JSON):\n"
        "  results[]:   array of code chunks, each with file (path), symbol (name),\n"
        "               kind (function/class/method/variable), lines (start-end),\n"
        "               language, score (0-1), preview (first 400 chars of source).\n"
        "  context:     pre-assembled, token-budget-optimised string of all chunks\n"
        "               ready to read — often the most useful part of the result.\n"
        "  results_count: total hits found.\n"
        "  tokens_used: approximate token count of the assembled context.\n\n"
        "WHEN TO USE:\n"
        "  • Start EVERY investigation here — explore before calling other tools.\n"
        "  • Topic/concept queries: 'authentication flow', 'error handling middleware'.\n"
        "  • Identifier queries: 'JWTMiddleware', 'UserService.create'.\n"
        "  • Error string searches: '401 unauthorized', 'connection refused'.\n"
        "  • Call multiple times with different phrasings to get full coverage.\n\n"
        "GOOD QUERY EXAMPLES:\n"
        "  'JWT token validation and expiry'\n"
        "  'webhook HMAC signature verification'\n"
        "  'how does payment processing work'\n"
        "  'database connection pool setup'\n\n"
        "TIPS:\n"
        "  • Specific multi-word phrases >> vague single words.\n"
        "    'user authentication flow' beats 'auth'.\n"
        "  • Use top_k=10-15 for broad exploration; 3-5 for targeted lookup.\n"
        "  • Use mode='keyword' when searching for exact identifiers or error strings.\n"
        "  • Always read the full `context` field — it contains the actual source code."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language description OR a specific identifier/error string to search for. "
                    "Be as specific as possible. Examples: "
                    "'JWT token validation', 'UserService.create', 'webhook HMAC verification', "
                    "'ECONNREFUSED database pool'. "
                    "Specific multi-word phrases surface better results than single vague words."
                ),
            },
            "language": {
                "type": "string",
                "description": (
                    "Optional: filter results to a single programming language. "
                    "Values: python, typescript, javascript, go, rust, java, ruby, cpp, c, "
                    "kotlin, swift, php, scala, etc. Omit to search all languages."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": (
                    "Maximum number of code chunks to return (1-15, default 5). "
                    "Use 10-15 for broad exploration or when the codebase is large. "
                    "Use 3-5 for targeted single-symbol lookups."
                ),
                "default": 5,
            },
            "mode": {
                "type": "string",
                "description": (
                    "Search strategy (default 'hybrid'): "
                    "'hybrid'   — semantic + keyword merged with RRF (best for most queries). "
                    "'semantic' — pure vector similarity (best for concepts/descriptions). "
                    "'keyword'  — pure tsvector/text match (best for exact identifiers or error strings)."
                ),
                "default": "hybrid",
                "enum": ["hybrid", "semantic", "keyword"],
            },
        },
        "required": ["query"],
    },
}

# ── Tool 2: get_symbol ─────────────────────────────────────────────────────────

GET_SYMBOL_SCHEMA: dict = {
    "name": "get_symbol",
    "description": (
        "Look up a specific function, class, method, or variable by name — "
        "the equivalent of IDE 'Go to Definition'. "
        "Returns the symbol's exact file location, full signature, docstring, "
        "line range, and export status.\n\n"
        "HOW IT WORKS:\n"
        "  Uses trigram similarity (pg_trgm) for fuzzy matching PLUS ILIKE for partial "
        "  matching, so partial or abbreviated names resolve to the best match. "
        "  Returns up to 10 symbols ordered by match score.\n\n"
        "RETURNS (JSON):\n"
        "  symbols[]:   array of matching symbols, each with:\n"
        "    name:           exact symbol name in the source.\n"
        "    qualified_name: fully-qualified name (e.g. 'UserService.authenticate').\n"
        "    kind:           function / class / method / variable / constant / interface.\n"
        "    file:           repo-relative file path.\n"
        "    repo:           'owner/repo' it belongs to.\n"
        "    lines:          'start-end' line range.\n"
        "    signature:      full function/class signature.\n"
        "    docstring:      first 200 chars of the docstring/JSDoc comment.\n"
        "    is_exported:    whether the symbol is public/exported.\n"
        "    match_score:    0-1 fuzzy similarity score.\n"
        "  count: total symbols found.\n\n"
        "WHEN TO USE:\n"
        "  • You already know (or suspect) the name of what you're looking for.\n"
        "  • You want the exact definition location, signature, or docstring.\n"
        "  • After search_codebase returns a symbol name, use this to get full details.\n"
        "  • Use before find_callers — confirm the symbol exists first.\n\n"
        "FUZZY MATCHING EXAMPLES:\n"
        "  'auth'         → finds authenticate, AuthMiddleware, authorization\n"
        "  'UserSvc'      → finds UserService\n"
        "  'JWTMiddleware'→ finds exact match + any fuzzy neighbors\n\n"
        "WHEN NOT TO USE:\n"
        "  • Exploring unknown code — use search_codebase instead.\n"
        "  • When you only have a description — use search_codebase instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Symbol name to look up. Can be: "
                    "exact ('authenticate'), "
                    "qualified ('UserService.authenticate'), "
                    "partial ('auth' matches authenticate/AuthMiddleware), "
                    "or abbreviated ('JWTMid' fuzzy-matches JWTMiddleware). "
                    "Class.method format resolves to the method definition."
                ),
            },
        },
        "required": ["name"],
    },
}

# ── Tool 3: find_callers ───────────────────────────────────────────────────────

FIND_CALLERS_SCHEMA: dict = {
    "name": "find_callers",
    "description": (
        "Find every place in the codebase that calls a given function or method. "
        "Supports multi-hop BFS traversal to trace the full call chain. "
        "Essential for blast-radius analysis: 'what breaks if I change this signature?'\n\n"
        "HOW IT WORKS:\n"
        "  1. Keyword-searches the indexed codebase for the symbol name.\n"
        "  2. Filters out definition lines (def/class/fn/const/export …).\n"
        "  3. For depth>1: takes the caller functions from hop N as the new search "
        "     targets for hop N+1 (BFS), avoiding already-visited symbols.\n"
        "  Each hop can reveal up to 5 call-site lines per matching chunk.\n\n"
        "RETURNS (JSON):\n"
        "  symbol:         the symbol you searched for.\n"
        "  total_callers:  total call-site entries across all hops.\n"
        "  hops[]:         one entry per traversal depth, each with:\n"
        "    hop:              hop number (1 = direct callers).\n"
        "    targets_searched: symbols searched in this hop.\n"
        "    callers[]:        array of caller entries, each with:\n"
        "      file:           file containing the call site.\n"
        "      symbol_context: the containing function/method name.\n"
        "      lines:          line range of the containing chunk.\n"
        "      call_sites[]:   specific lines where the symbol is called.\n"
        "      calls:          which symbol triggered this entry.\n\n"
        "WHEN TO USE:\n"
        "  • Before changing a function's signature — find all callers first.\n"
        "  • Understanding how/where a utility or service is used.\n"
        "  • Tracing data flow upstream from a known point.\n"
        "  • Assessing test coverage impact of a change.\n\n"
        "DEPTH GUIDE:\n"
        "  depth=1 (default): direct callers only — who calls 'authenticate'?\n"
        "  depth=2:           callers of callers — who triggers the code that calls it?\n"
        "  depth=3:           three hops — full upstream blast radius (expensive).\n\n"
        "EXAMPLE:\n"
        "  {\"symbol\": \"validate_token\"}           → all places that call validate_token\n"
        "  {\"symbol\": \"PaymentService.charge\", \"depth\": 2}  → call chain 2 hops deep"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Name of the function, method, or symbol to find callers of. "
                    "Examples: 'authenticate', 'validate_token', 'PaymentService.charge'. "
                    "Partial names work (keyword match). "
                    "Use qualified names (Class.method) to narrow results."
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    "How many call-graph hops to traverse (1-2, default 1). "
                    "depth=1: direct callers only. "
                    "depth=2: callers of callers (BFS, may be slow on large codebases). "
                    "Start with depth=1 and increase only if you need the full chain."
                ),
                "default": 1,
            },
        },
        "required": ["symbol"],
    },
}

# ── Tool 4: get_file_context ───────────────────────────────────────────────────

GET_FILE_CONTEXT_SCHEMA: dict = {
    "name": "get_file_context",
    "description": (
        "Get the complete structural map of a source file: all symbols it defines, "
        "all modules it imports, and which other files import it (reverse dependencies). "
        "Answers: 'what does this file contain and how does it fit into the codebase?'\n\n"
        "HOW IT WORKS:\n"
        "  Queries the symbols table for all definitions in the file, "
        "  the chunks table for import metadata and commit info, "
        "  and performs a reverse-dependency search across all chunk imports arrays.\n\n"
        "RETURNS (JSON):\n"
        "  file:         resolved file path (as indexed).\n"
        "  language:     detected programming language.\n"
        "  last_commit:  7-char git SHA of the most recent commit touching this file.\n"
        "  commit_author:author of that commit.\n"
        "  imports[]:    modules/packages imported by this file.\n"
        "  symbols[]:    all defined symbols, each with:\n"
        "    name, qualified_name, kind, lines, signature, docstring (first 200 chars),\n"
        "    is_exported.\n"
        "  imported_by[]:files that import this file ('owner/repo:path' format).\n"
        "  chunk_count:  number of indexed chunks in this file.\n\n"
        "WHEN TO USE:\n"
        "  • After search_codebase returns a file — use this to see its full structure.\n"
        "  • Before editing a file — understand all its exports and dependents.\n"
        "  • To check what a file imports (its dependencies).\n"
        "  • To find all files that would be affected if this file's API changes.\n\n"
        "PATH MATCHING:\n"
        "  Partial paths are supported — 'app.py' matches 'src/api/app.py'.\n"
        "  If multiple files match, the most likely match is returned.\n"
        "  Use the exact path from search_codebase results for precise lookup.\n\n"
        "EXAMPLES:\n"
        "  {\"path\": \"src/api/app.py\"}              → full structure of app.py\n"
        "  {\"path\": \"pipeline.py\"}                 → matches src/pipeline/pipeline.py\n"
        "  {\"path\": \"auth.ts\", \"include_deps\": false} → symbols only, skip reverse deps"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "File path relative to the repository root. "
                    "Partial paths are fine and will be matched with ILIKE. "
                    "Examples: 'src/api/app.py', 'webhook.py', 'app/shopify.server.ts'. "
                    "Do NOT use absolute paths. "
                    "Prefer using paths from search_codebase or get_symbol results."
                ),
            },
            "include_deps": {
                "type": "boolean",
                "description": (
                    "Whether to include the reverse dependency list (which files import "
                    "this file). Default true. Set false to skip the reverse-dep query "
                    "and get a faster response when you only need the file's own symbols."
                ),
                "default": True,
            },
        },
        "required": ["path"],
    },
}

# ── Tool 5: get_agent_context ──────────────────────────────────────────────────

GET_AGENT_CONTEXT_SCHEMA: dict = {
    "name": "get_agent_context",
    "description": (
        "Pre-assembles the most relevant codebase context for a specific coding task "
        "in a single call. Combines focal file chunks + semantic search + reranking, "
        "all within a configurable token budget.\n\n"
        "Use this at the START of a complex task (before you begin planning or reasoning) "
        "to get everything relevant in one shot, deduplicated and ranked by relevance.\n\n"
        "HOW IT WORKS:\n"
        "  1. Fetches ALL chunks from focal_files (highest priority — always included).\n"
        "  2. Embeds the task description and runs semantic search for related chunks.\n"
        "  3. Reranks search results; focal file chunks retain top priority.\n"
        "  4. Assembles combined results within the token_budget, deduplicating.\n\n"
        "RETURNS (JSON):\n"
        "  task:           the task description you provided.\n"
        "  focal_files[]:  the focal files you listed.\n"
        "  context_text:   fully assembled, readable context — the main output.\n"
        "  chunks_used:    number of chunks included in context_text.\n"
        "  tokens_used:    total tokens in context_text.\n"
        "  retrieval_log:  summary of the retrieval pipeline steps.\n\n"
        "WHEN TO USE:\n"
        "  • At the very start of any implementation task before writing code.\n"
        "  • When you know which files you'll be editing (pass them as focal_files).\n"
        "  • When you need a single rich context block instead of running multiple searches.\n"
        "  • As a shortcut that replaces: search_codebase + get_file_context + merging.\n\n"
        "WHEN NOT TO USE:\n"
        "  • For quick single-symbol lookups — use get_symbol.\n"
        "  • When exploring unknown territory — use search_codebase first to orient.\n\n"
        "EXAMPLES:\n"
        "  {\"task\": \"Add rate limiting to the authentication endpoint\",\n"
        "   \"focal_files\": [\"src/api/auth.py\", \"src/middleware/rate_limiter.py\"]}\n"
        "  {\"task\": \"Fix the webhook HMAC verification bug\",\n"
        "   \"focal_files\": [\"src/github/webhook.py\"], \"token_budget\": 12000}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Natural-language description of the coding task you are about to perform. "
                    "Be specific — this drives semantic search for related code. "
                    "Examples: 'Add rate limiting to the auth endpoint', "
                    "'Fix JWT expiry handling in the token validator', "
                    "'Refactor the payment service to use the new Stripe SDK'."
                ),
            },
            "focal_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of file paths you are actively working on. "
                    "Their chunks are fetched first with highest priority, ensuring "
                    "they always appear in the assembled context. "
                    "Use paths from search_codebase or get_symbol results. "
                    "Cap at 5 files for best results."
                ),
            },
            "token_budget": {
                "type": "integer",
                "description": (
                    "Maximum tokens to include in context_text (default 8000, max 32000). "
                    "Increase for complex tasks spanning many files. "
                    "Decrease if you need a quick overview. "
                    "Tokens are allocated greedily: focal files first, then search results."
                ),
                "default": 8000,
            },
        },
        "required": ["task"],
    },
}

# ── Tool 6: plan_implementation ───────────────────────────────────────────────

PLAN_IMPLEMENTATION_SCHEMA: dict = {
    "name": "plan_implementation",
    "description": (
        "Generate a complete, grounded implementation plan for a coding task. "
        "Combines web research (best practices, library recommendations) with "
        "live codebase context (actual files, symbols, callers) to produce a "
        "Cursor-style structured plan. This is a HEAVYWEIGHT tool — it runs its "
        "own internal agent loop and web search, returning in 10-30 seconds.\n\n"
        "HOW IT WORKS:\n"
        "  1. Web research — searches for the best library, pattern, and current "
        "     best practices relevant to the task (Anthropic models only).\n"
        "  2. Codebase retrieval — runs a 7-phase pipeline: embed → search → rerank "
        "     → file maps → caller graphs → web grounding → final assembly.\n"
        "  3. LLM planning — generates a structured plan grounded in both sources.\n\n"
        "RETURNS (Markdown string):\n"
        "  # Implementation Plan\n"
        "  ## Summary         — high-level approach\n"
        "  ## Assumptions     — clarifying assumptions made\n"
        "  ## Files to Change — exact file paths with action (MODIFY/CREATE/DELETE) "
        "                       and per-symbol changes with pseudocode\n"
        "  ## Execution Steps — ordered steps with dependencies, files, and verifications\n"
        "  ## Risks           — severity-tagged risks with mitigations\n"
        "  ## Test Plan       — concrete test scenarios\n\n"
        "WHEN TO USE:\n"
        "  • Before starting any non-trivial feature, bug fix, or refactoring.\n"
        "  • When you need to know EXACTLY which files and symbols to change.\n"
        "  • When best-practice research + codebase grounding are both needed.\n"
        "  • For code review: 'is this the right approach for our codebase?'\n\n"
        "WHEN NOT TO USE:\n"
        "  • Simple single-file changes — use search_codebase + get_symbol instead.\n"
        "  • Quick questions about how code works — use ask_codebase instead.\n\n"
        "NOTE: This tool is compute-intensive. Call it once with a clear, complete "
        "task description rather than iteratively with vague queries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Bug report, feature request, or refactoring task to plan (min 10 chars). "
                    "Be as specific as possible — include error messages, affected features, "
                    "and constraints. Examples: "
                    "'Add Redis-based rate limiting to POST /auth/login — max 5 attempts per IP per minute', "
                    "'Fix N+1 query in UserService.list_users that causes timeouts for orgs > 1000 users', "
                    "'Refactor authentication middleware to support OAuth2 in addition to JWT'."
                ),
            },
            "web_research": {
                "type": "boolean",
                "description": (
                    "Whether to search the web for best practices before generating the plan "
                    "(default true). Set false for faster results when best practices are known "
                    "or web access is unnecessary (e.g. internal refactoring)."
                ),
                "default": True,
            },
        },
        "required": ["query"],
    },
}

# ── Tool 7: ask_codebase ───────────────────────────────────────────────────────

ASK_CODEBASE_SCHEMA: dict = {
    "name": "ask_codebase",
    "description": (
        "Answer a natural-language question about the codebase in a mentor tone. "
        "Unlike plan_implementation (which outputs a change plan), this tool "
        "explains how existing code works — tracing data flows, clarifying "
        "architecture, pointing to real file locations with citations. "
        "This is a HEAVYWEIGHT tool — it runs its own internal agent loop.\n\n"
        "HOW IT WORKS:\n"
        "  Runs an internal AgentLoop that: searches the codebase, looks up symbols, "
        "  traces callers, reads file structures, then synthesises a cited markdown answer.\n\n"
        "RETURNS (Markdown string):\n"
        "  • Direct 1-2 sentence answer first (no preamble).\n"
        "  • Prose explanation citing real file paths and line numbers.\n"
        "  • Fenced code blocks for key snippets from the actual source.\n"
        "  • 2-3 concrete follow-up questions grounded in the code found.\n"
        "  • A reference list of all cited files.\n\n"
        "WHEN TO USE:\n"
        "  • 'How does the webhook processing pipeline work?'\n"
        "  • 'Where is authentication handled and which files are involved?'\n"
        "  • 'What does the reranker do and when is it called?'\n"
        "  • 'Explain the chunking algorithm step by step.'\n"
        "  • Any question that starts with How/What/Where/Why about existing code.\n\n"
        "WHEN NOT TO USE:\n"
        "  • Planning a change — use plan_implementation.\n"
        "  • Looking up a specific symbol — use get_symbol (much faster).\n"
        "  • Searching for a code pattern — use search_codebase (much faster).\n\n"
        "NOTE: This tool runs an internal agent loop (10-20s). Prefer search_codebase "
        "or get_symbol for simple factual lookups."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "Preferred field: natural-language question about the codebase "
                    "(min 5 chars). Examples: "
                    "'How does the webhook HMAC verification work?', "
                    "'Where is user authentication handled and how does the JWT flow work?', "
                    "'What is the RRF merge algorithm and where is it implemented?', "
                    "'Explain how the embedding pipeline indexes a new file'."
                ),
            },
            "query": {
                "type": "string",
                "description": "Compatibility alias for 'question'.",
            },
            "text": {
                "type": "string",
                "description": "Secondary compatibility alias for 'question'.",
            },
        },
    },
}


# ── Tool 5 (retrieval): get_semantic_context ──────────────────────────────────

GET_SEMANTIC_CONTEXT_SCHEMA: dict = {
    "name": "get_semantic_context",
    "description": (
        "Retrieve LLM-extracted semantic architecture relationships for symbols.\n"
        "Returns facts the call graph can't show: 'AuthService validates JWT tokens',\n"
        "'PaymentFlow coordinates StripeClient'.\n\n"
        "WHEN TO USE:\n"
        "  • After finding key symbols — understand their architectural role.\n"
        "  • Cross-cutting questions: 'what relates to authentication?'\n"
        "  • Before planning changes — know a module's semantic dependencies.\n"
        "RETURNS: formatted relationship graph for the requested symbols."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Symbol names or qualified names to look up semantic relationships for.",
            },
            "concept": {
                "type": "string",
                "description": (
                    "Optional concept filter — only return relationships matching this concept "
                    "(e.g. 'authentication', 'caching', 'validation')."
                ),
            },
        },
        "required": ["symbols"],
    },
}


# ── Tool 8: generate_pdf ──────────────────────────────────────────────────────

GENERATE_PDF_SCHEMA: dict = {
    "name": "generate_pdf",
    "description": (
        "Convert markdown content to a professional PDF document and store it for download. "
        "Call this AFTER you have written the complete document content in markdown. "
        "The PDF is stored server-side and a download URL is returned immediately.\n\n"
        "RETURNS (JSON):\n"
        "  doc_id:       UUID of the stored document.\n"
        "  download_url: GET /documents/{doc_id}/download — link to stream the PDF.\n"
        "  filename:     Final filename with .pdf extension.\n"
        "  size_bytes:   PDF file size.\n\n"
        "WHEN TO USE:\n"
        "  • After compile_rca_doc — generate a downloadable RCA report PDF.\n"
        "  • After any step that produces a long-form document meant to be shared.\n"
        "  • When asked explicitly to 'generate a PDF' or 'export as PDF'.\n\n"
        "NOTE: Include the download_url in your final answer so the user can access it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Full markdown document content.",
            },
            "title": {
                "type": "string",
                "description": "Document title shown in the PDF header. E.g. 'RCA: Payment Service Timeout'.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Output filename without .pdf extension "
                    "(e.g. 'rca-payment-service-2026-03-08'). Defaults to slugified title."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional key-value pairs embedded in the PDF: "
                    "service, severity, environment, author, etc."
                ),
            },
        },
        "required": ["content", "title"],
    },
}


# ── Think tool (meta-loop self-evaluation) ────────────────────────────────────
# Based on Anthropic's "think" tool pattern (engineering blog, 2025):
# A side-effect-free scratchpad that lets Claude explicitly reason about whether
# it has enough context to answer before deciding to search more or call the final
# answer tool. Shown to improve complex multi-step task consistency on τ-bench.
# Injected by AgentLoop into the retrieval tools list (not a domain retrieval tool).

THINK_TOOL_SCHEMA: dict = {
    "name": "think",
    "description": (
        "A side-effect-free scratchpad for explicit self-evaluation. "
        "Use this to reason out loud about what you've found and whether it's "
        "sufficient before deciding to search more or call the final answer tool. "
        "Your thought is echoed back so you can refer to it in future turns.\n\n"
        "IMPORTANT: Thinking is NOT the same as answering. After thinking, you MUST still\n"
        "call the answer tool to actually deliver your response.\n\n"
        "WHEN TO USE:\n"
        "  • After 2+ searches — ask yourself: 'Do I have enough to answer well?'\n"
        "  • Before repeating a similar query — check if you already have the answer.\n"
        "  • When you're unsure whether a new search would reveal anything new.\n"
        "  • Before calling the final answer tool — confirm no critical gap remains.\n\n"
        "GOOD THOUGHT EXAMPLES:\n"
        "  'I found the JWT validation logic in auth.py lines 45-89, the middleware "
        "   in middleware/jwt.py, and the token generation in utils/token.py. "
        "   I have the complete auth flow. I can answer now.'\n"
        "  'I found authentication but haven't seen how sessions are managed after "
        "   login. Let me search for session handling before answering.'\n"
        "  'My last two searches returned the same auth.py file. Searching more "
        "   for this topic will likely yield diminishing returns. Time to answer.'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "thought": {
                "type": "string",
                "description": (
                    "Your explicit reasoning covering: "
                    "(1) what relevant code/context you have found so far, "
                    "(2) whether it is sufficient to answer the question comprehensively, "
                    "(3) what specific information is still missing, if anything."
                ),
            },
        },
        "required": ["thought"],
    },
}


# ── Exported schema lists ──────────────────────────────────────────────────────

# Core retrieval tools — included in every agent loop by default
RETRIEVAL_TOOL_SCHEMAS: list[dict] = [
    SEARCH_CODEBASE_SCHEMA,
    GET_SYMBOL_SCHEMA,
    FIND_CALLERS_SCHEMA,
    GET_FILE_CONTEXT_SCHEMA,
    GET_SEMANTIC_CONTEXT_SCHEMA,
]

# 3 higher-order tools — only included when a role explicitly requests them
EXTENDED_TOOL_SCHEMAS: list[dict] = [
    GET_AGENT_CONTEXT_SCHEMA,
    PLAN_IMPLEMENTATION_SCHEMA,
    ASK_CODEBASE_SCHEMA,
]

# All internal tools combined (includes generate_pdf)
ALL_INTERNAL_TOOL_SCHEMAS: list[dict] = RETRIEVAL_TOOL_SCHEMAS + EXTENDED_TOOL_SCHEMAS + [GENERATE_PDF_SCHEMA]

# Trimmed set for Ask Mode — drops get_file_context to save ~300 tokens/turn
ASK_RETRIEVAL_TOOL_SCHEMAS: list[dict] = [
    SEARCH_CODEBASE_SCHEMA,
    GET_SYMBOL_SCHEMA,
    FIND_CALLERS_SCHEMA,
    GET_SEMANTIC_CONTEXT_SCHEMA,
]
