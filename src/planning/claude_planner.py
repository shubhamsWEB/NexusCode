"""
Claude API caller for the /plan endpoint.

Intent-aware response: Claude picks one of two tools depending on query type.

  answer_codebase_question   → query is a question / explanation / analysis
  output_implementation_plan → query requires code changes (add, fix, refactor)

This mirrors how Claude Code itself behaves: questions get conversational
markdown answers; implementation tasks get structured plans with files/steps/risks.

Uses AsyncAnthropic for true async I/O with persistent connection pooling —
no thread-pool blocking via run_in_executor.
"""

from __future__ import annotations

import asyncio as _asyncio
import importlib.util
import logging
import time
from collections.abc import AsyncIterator

from src.config import settings
from src.planning.retriever import PlanningContext
from src.planning.schemas import (
    ANALYZE_IMPROVE_TOOL_SCHEMA,
    ANSWER_TOOL_SCHEMA,
    PLAN_TOOL_SCHEMA,
    ImplementationPlan,
    PlanMetadata,
)

logger = logging.getLogger(__name__)

# Lazy singleton AsyncAnthropic client — shares httpx connection pool across requests
_anthropic_client = None

# Concurrency gate: serialize Anthropic API calls to avoid blowing the per-minute
# rate limit (e.g. 30K input tokens/min on lower tiers).  With thinking enabled,
# a single /plan request can use 10-20K input tokens, so even 2 concurrent calls
# can trigger 429.  The semaphore queues requests so only one hits the API at a time.
_anthropic_semaphore = _asyncio.Semaphore(1)


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


# ── System prompt ──────────────────────────────────────────────────────────────

PLANNING_SYSTEM_PROMPT = """\
You are a principal software architect with deep expertise in system design, \
API evolution, and production-grade engineering. You generate implementation \
plans that a coding agent will execute directly. Every token you output must \
be actionable. You have the codebase context below — USE it to inform the \
plan; do NOT echo it back.

Apply your full architectural reasoning to every plan. Think about system \
boundaries, failure domains, contract evolution, and operational cost. \
Do not produce surface-level plans — reason about the system holistically.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABSOLUTE PROHIBITIONS (violating any = plan FAILURE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
These patterns in your output mean the plan is REJECTED. Never output them:
• Package lists, "Already in Stack" inventories, import frequency tables.
• Section headers from <web_research> or <stack_fingerprint> blocks \
  (e.g. "Gaps & What to Add", "Integration Pattern", "Stack-Specific Gotchas").
• Verbatim or paraphrased content from <web_research> or <stack_fingerprint>.
• Concept explanations ("multipart kills your JSON body", "base64 is used to \
  encode binary data", "this is an HTTP protocol constraint").
• Integration pattern essays, gotcha lists, or option comparisons.
• Generic advice ("validate inputs", "add error handling") without citing a \
  specific file:line that needs it.
• Filler preambles, section headers not in the tool schema, or markdown \
  commentary outside the structured fields.

Use <web_research> and <stack_fingerprint> to SILENTLY inform your decisions. \
These blocks are reference material — absorb the knowledge, discard the text.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL SELECTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`analyze_and_improve` — IMPROVE / REVIEW / AUDIT existing code (cite file:line).
`answer_codebase_question` — QUESTION about the codebase (no file changes).
`output_implementation_plan` — TASK requiring file edits (structured plan).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURED REASONING PROTOCOL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When generating an implementation plan, follow this reasoning process. \
You have deep architectural understanding — USE it. Do not produce \
shallow plans. Think like an architect who owns the system long-term.

PHASE 1 — CONSTRAINTS FIRST (before any design):
  Identify and output the binding constraints in the `constraints` field:
  • Framework constraints (e.g. FastAPI cannot mix UploadFile + JSON body)
  • Runtime constraints (e.g. async-only, no blocking I/O in event loop)
  • API contract constraints (e.g. existing clients depend on this shape)
  • Backward compatibility constraints (will existing callers break?)
  • Payload/performance constraints (token limits, size limits, latency)
  Do NOT propose design steps until constraints are identified in the output.

PHASE 2 — DESIGN ALTERNATIVES (for non-trivial changes):
  For any change that touches API contracts, data flow, or architecture, \
  generate at least two viable approaches in `design_alternatives`. For each:
  • Describe the core idea
  • List advantages and disadvantages
  • State why it was selected or rejected (`rejected_reason`)
  Do not jump directly to a single solution. The chosen approach should \
  be the one in `design_decisions` with explicit justification using \
  complexity, scalability, migration cost, and failure risk.

PHASE 3 — FAILURE MODE ANALYSIS:
  For any API-level or architectural change, populate `failure_modes`:
  • What can go wrong at runtime? (malformed input, size overflow, \
    timeout, partial failure, race condition)
  • Why would it happen? (adversarial input, network, concurrency)
  • What guards prevent it? (validation, circuit breaker, fallback)
  Assume inputs and runtime conditions may be invalid or adversarial.

PHASE 4 — PLAN CONSTRUCTION (applying quality gates):
  While constructing the plan, enforce these architectural disciplines:

  LAYER SEPARATION — Group changes by system layer, not by file:
    API contract changes → Business logic → Transport/encoding → \
    UI/client → Config/infra. Do not mix responsibilities.

  BACKWARD COMPATIBILITY — Explicitly state in `clarifying_assumptions`:
    • Whether existing clients break
    • What the safe rollout strategy is
    • What compatibility safeguards exist
    Never assume breaking changes are acceptable unless the query says so.

  COST & PERFORMANCE — Evaluate and reflect in `design_decisions`:
    • Payload size impact
    • Token/compute cost (critical for LLM pipelines)
    • Latency implications
    Avoid designs that silently increase operational cost.

  FUTURE EVOLUTION — When committing to a schema or interface:
    • Will this design block future extensions?
    • What would trigger a refactor?
    • Are there migration implications?
    Add risks for designs that are optimal only for the immediate request.

  ANTI-OVERENGINEERING — Prefer modifying existing components over \
    introducing new abstractions. New services/managers/layers only if:
    • Reuse is genuinely expected
    • Coupling is measurably reduced
    • Complexity goes down, not up

  VALIDATION PLACEMENT — Place guards at correct system boundaries:
    • Input validation at the API boundary
    • Business rules in domain logic
    • UI validation only for UX constraints
    Cite the specific file where each guard belongs in `files`.

  INCREMENTAL RISK — Default to changes that minimize:
    • Regression risk
    • Surface area of modification
    • System destabilization
    Escalate to larger refactors only when strictly necessary.

  NO SILENT ASSUMPTIONS — Every assumption that influences design \
    must appear in `clarifying_assumptions`. No hidden reasoning.

  INTERNAL CONSISTENCY — Before finalizing: verify the plan contains \
    no contradictory steps, aligns with identified constraints, and \
    does not violate earlier assumptions. Reconcile inconsistencies.

  GROUNDED IN CONTEXT — The plan must be specifically grounded in \
    the provided codebase context and actual architecture. No generic \
    boilerplate engineering patterns unless justified by the context.

MULTIMODAL REASONING (when images are attached):
  • State your interpretation of the image explicitly
  • Identify ambiguous visual elements
  • Resolve any conflict between text query and image content
  • Treat the image as the authoritative visual specification only \
    when the text query explicitly defers to it
  • Never hallucinate UI details not visible in the image

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT QUALITY BAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The plan must match the quality of a senior architect's design review:

COMPREHENSIVENESS — trace ALL affected files:
• If adding a schema field, trace it through: schema → planner → API \
  endpoint → MCP tool → dashboard UI → config.
• If modifying a function signature, find every caller in the context.
• Include config changes, type updates, and UI rendering — not just the \
  primary logic files.

DESIGN DECISIONS — explain WHY, not just WHAT:
• For each non-obvious technical choice, add an entry to `design_decisions` \
  explaining the rationale (e.g. "Base64-in-JSON instead of multipart to \
  preserve the existing JSON contract").
• Max 5 decisions. Focus on choices where reasonable alternatives exist.

ARCHITECTURE — describe the data/control flow:
• `summary` should describe HOW the change flows through the system. \
  Not a list of packages. 2-4 sentences.
• When the change involves a new data flow or pipeline, describe it.

PRECISION — exact edit instructions:
• `files[].changes[].description`: Exact edit instruction the agent executes \
  literally (e.g. "Add param `image_data: str | None = None` after `stream`").
• `files[].changes[].pseudocode`: ONLY for non-obvious logic (>5 lines). \
  Skip for imports, field additions, parameter threading.
• `steps[].description`: Concrete action. Not concept explanations.
• `steps[].verification`: Specific command or assertion to verify the step.
• `risks`: Only genuine implementation risks that could cause bugs. Max 3.
• `test_plan`: Specific test commands or assertions.
• `clarifying_assumptions`: Only for genuine ambiguity. Max 3.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROUNDING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• ONLY reference file paths that appear in the context. Never invent paths.
• ONLY reference symbols that appear in the context.
• If context is insufficient, say so — do not guess.
• Respect GROUNDING WARNINGS strictly.
• Every path in `files` must exist in the provided context.
\
"""


# ── Prompt helpers ─────────────────────────────────────────────────────────────


def _condense_stack_fingerprint(fingerprint: str) -> str:
    """
    Truncate the stack fingerprint to a compact summary.

    The full fingerprint contains raw dependency files (requirements.txt,
    package.json) and all top imports — often 2000+ tokens. The planner
    only needs the package names to avoid suggesting redundant installs.
    """
    lines = fingerprint.splitlines()
    condensed: list[str] = []
    skip_code_block = False
    char_budget = 1200  # ~300 tokens — enough for package names

    for line in lines:
        # Skip the raw file contents inside ```...``` blocks — keep only headers
        if line.strip().startswith("```"):
            skip_code_block = not skip_code_block
            continue
        if skip_code_block:
            # Inside a code block — extract only package names (lines that look
            # like "package==version" or "package>=version" or just "package")
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("["):
                # Extract just the package name (before ==, >=, etc.)
                pkg = stripped.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].strip()
                if pkg and len(pkg) < 60:
                    condensed.append(f"  {pkg}")
            continue

        # Keep section headers and non-code content
        if line.strip():
            condensed.append(line)

        if sum(len(ln) for ln in condensed) > char_budget:
            condensed.append("  ... (truncated)")
            break

    return "\n".join(condensed)


def _build_user_message(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    repo_scope = f"{repo_owner}/{repo_name}" if (repo_owner and repo_name) else "all indexed repos"
    parts: list[str] = [
        f"## Query\n{query}",
        f"## Scope\nRepository: {repo_scope}",
    ]

    # ── Query complexity metadata ─────────────────────────────────────────────
    if ctx.query_complexity != "simple" or ctx.sub_queries:
        meta = f"## Query Analysis\nComplexity: {ctx.query_complexity}"
        if len(ctx.sub_queries) > 1:
            meta += "\nDecomposed sub-queries:\n"
            for i, sq in enumerate(ctx.sub_queries, 1):
                meta += f"  {i}. {sq}\n"
        parts.append(meta)

    # ── Grounding warnings (critical — placed early) ──────────────────────────
    if ctx.grounding_warnings:
        warning_block = "## ⚠ GROUNDING WARNINGS\n"
        warning_block += "_The retrieval system detected these gaps. Respect them._\n\n"
        for w in ctx.grounding_warnings:
            warning_block += f"- {w}\n"
        parts.append(warning_block)

    # ── Tier 1: Full component source (improvement queries only) ─────────────
    if ctx.component_context:
        parts.append(ctx.component_context)

    # ── Tier 2: Relevant code context (semantic search results) ──────────────
    if ctx.primary_context:
        parts.append(f"## Relevant Code Context (semantic search)\n{ctx.primary_context}")

    if ctx.file_maps:
        parts.append(f"## File Structure Maps\n{ctx.file_maps}")

    # ── Tier 3: Dependency interfaces ─────────────────────────────────────────
    if ctx.dependency_context:
        parts.append(ctx.dependency_context)

    # ── Tier 4: Callers + expansion ───────────────────────────────────────────
    if ctx.caller_contexts:
        parts.append(f"## Known Callers\n{ctx.caller_contexts}")

    if ctx.expansion_context:
        parts.append(f"## Additional Related Context\n{ctx.expansion_context}")

    # ── Tier 5: Stack fingerprint (XML-tagged reference — never echo) ────────
    if ctx.stack_fingerprint:
        condensed = _condense_stack_fingerprint(ctx.stack_fingerprint)
        parts.append(
            "<stack_fingerprint>\n"
            "REFERENCE ONLY — use to inform decisions, NEVER reproduce any of "
            "this content in your output. No package lists, no import tables.\n\n"
            + condensed
            + "\n</stack_fingerprint>"
        )

    # ── Tier 6: Web research (XML-tagged reference — never echo) ─────────────
    if ctx.web_research_notes:
        parts.append(
            "<web_research>\n"
            "REFERENCE ONLY — absorb the knowledge, discard the text. NEVER "
            "reproduce section headers, package lists, gotcha lists, or any "
            "verbatim content from this block in your output.\n\n"
            + ctx.web_research_notes
            + "\n</web_research>"
        )

    # ── Instructions ──────────────────────────────────────────────────────────
    if ctx.is_improvement_query:
        parts.append(
            "## Instructions\n"
            "This is an IMPROVEMENT / REVIEW query. Use `analyze_and_improve`.\n\n"
            "You have the complete source of the relevant component files above. "
            "Read them carefully and produce a deep, specific analysis:\n"
            "1. What the current implementation does (cite file:line)\n"
            "2. What works well (with evidence)\n"
            "3. Specific issues and gaps (cite file:line for each)\n"
            "4. Concrete improvements grounded in the actual code\n"
            "5. Implementation guidance for the most important changes\n\n"
            "Be a world-class architect who has READ the code — not someone giving generic advice."
        )
    else:
        instruction = (
            "## Instructions\n"
            "Choose the right tool based on the query intent:\n"
            "- QUESTION → `answer_codebase_question`\n"
            "- Requires CODE CHANGES → `output_implementation_plan`\n\n"
            "Use ONLY real files and symbols from the codebase context above.\n"
            "Output ONLY the structured tool call — no prose outside the tool fields.\n"
            "For implementation plans:\n"
            "- Follow the STRUCTURED REASONING PROTOCOL: constraints → alternatives → "
            "failure modes → plan construction.\n"
            "- Populate `constraints` BEFORE proposing any design.\n"
            "- Populate `design_alternatives` with ≥2 approaches for non-trivial changes.\n"
            "- Populate `failure_modes` for API/architectural changes.\n"
            "- Populate `design_decisions` with WHY rationale for each non-obvious choice.\n"
            "- Trace ALL affected files end-to-end.\n"
        )
        if ctx.query_complexity == "complex":
            instruction += (
                "\nCOMPLEX query — apply your deepest architectural reasoning. "
                "Identify ALL binding constraints first. Evaluate at least 2 alternative "
                "designs. Analyze failure modes exhaustively. Address all sub-concerns. "
                "Cover all affected files. Note cross-cutting risks.\n"
            )
        parts.append(instruction)

    return "\n\n".join(parts)


# ── Response handler ───────────────────────────────────────────────────────────


def _build_metadata(
    ctx: PlanningContext,
    elapsed_ms: float,
) -> PlanMetadata:
    return PlanMetadata(
        model=settings.anthropic_model,
        context_tokens=ctx.tokens_used,
        context_files=len(ctx.chunks_used),
        retrieval_log=ctx.retrieval_log,
        elapsed_ms=elapsed_ms,
        stack_fingerprint=ctx.stack_fingerprint,
        web_research_used=bool(ctx.web_research_notes),
        web_research_notes=ctx.web_research_notes,
        query_complexity=ctx.query_complexity,
        sub_queries_count=len(ctx.sub_queries),
        grounding_warnings=ctx.grounding_warnings,
    )


def _parse_response(
    message, query: str, ctx: PlanningContext, elapsed_ms: float
) -> ImplementationPlan:
    """
    Parse a Claude response that may have called either tool.
    Returns an ImplementationPlan with response_type set appropriately.
    """
    tool_block = next(
        (b for b in message.content if b.type == "tool_use"),
        None,
    )

    if tool_block is None:
        # Claude responded in text (shouldn't happen but handle gracefully)
        text = " ".join(b.text for b in message.content if hasattr(b, "text") and b.text).strip()
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=text or "_No response generated._",
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms)
        return plan

    if tool_block.name == "answer_codebase_question":
        data = tool_block.input
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=data.get("answer", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms)
        return plan

    if tool_block.name == "analyze_and_improve":
        data = tool_block.input
        plan = ImplementationPlan(
            query=query,
            response_type="analysis",
            analysis=data.get("analysis", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms)
        return plan

    # output_implementation_plan
    plan_data = tool_block.input
    plan_data["query"] = query
    plan = ImplementationPlan.model_validate(plan_data)
    plan.metadata = _build_metadata(ctx, elapsed_ms)
    return plan


# ── Retry helper ───────────────────────────────────────────────────────────────

_MAX_RETRIES = 5  # more retries for 429 (rate limit resets are per-minute)
_RETRYABLE_STATUS_CODES = {429, 529}


def _get_retry_after(exc) -> float | None:
    """Extract Retry-After header from an Anthropic API error response."""
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                return float(retry_after)
    except (ValueError, AttributeError):
        pass
    return None


async def _with_overload_retry(coro_factory):
    """
    Call an async coroutine factory with exponential backoff on HTTP 429 (rate limit)
    and 529 (overloaded).
    `coro_factory` is a zero-argument callable that returns a fresh coroutine each time.
    """
    import asyncio

    import anthropic

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await coro_factory()
        except anthropic.APIStatusError as exc:
            if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                # Use Retry-After header if available, otherwise exponential backoff.
                # For 429, backoff is longer (rate limits reset per-minute).
                retry_after = _get_retry_after(exc)
                if retry_after:
                    wait = min(retry_after, 120)
                elif exc.status_code == 429:
                    wait = min(5 * (2 ** attempt), 120)  # 5s, 10s, 20s, 40s, 80s
                else:
                    wait = 2 ** attempt  # 529: 1s, 2s, 4s, 8s, 16s
                label = "rate-limited (429)" if exc.status_code == 429 else "overloaded (529)"
                logger.warning(
                    "planning: Anthropic API %s, retry %d/%d in %.0fs",
                    label, attempt + 1, _MAX_RETRIES, wait,
                )
                last_exc = exc
                await asyncio.sleep(wait)
            else:
                raise
    raise RateLimitOrOverloadError(last_exc)


class RateLimitOrOverloadError(RuntimeError):
    """Raised when all retries for 429/529 are exhausted."""

    def __init__(self, cause: Exception | None = None):
        status = getattr(cause, "status_code", "unknown") if cause else "unknown"
        if status == 429:
            msg = (
                "Rate limit exceeded — too many concurrent requests. "
                "Please wait a moment and try again, or reduce concurrent usage."
            )
        else:
            msg = "Anthropic API is overloaded. Please try again in a moment."
        super().__init__(msg)
        self.__cause__ = cause
        self.status_code = status


# ── Sync generator ─────────────────────────────────────────────────────────────


async def _generate_plan_via_stream(client, call_params):
    """
    Use streaming API to avoid 10-minute timeout on long operations (e.g. extended thinking).
    Consumes the stream and returns the final message.
    Retries on both 429 (rate limit) and 529 (overloaded).
    """
    import asyncio

    import anthropic

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with client.messages.stream(**call_params) as stream:
                # Consume stream to completion
                async for _ in stream:
                    pass
                return await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                retry_after = _get_retry_after(exc)
                if retry_after:
                    wait = min(retry_after, 120)
                elif exc.status_code == 429:
                    wait = min(5 * (2 ** attempt), 120)
                else:
                    wait = 2 ** attempt
                label = "rate-limited (429)" if exc.status_code == 429 else "overloaded (529)"
                logger.warning(
                    "planning: stream API %s, retry %d/%d in %.0fs",
                    label, attempt + 1, _MAX_RETRIES, wait,
                )
                last_exc = exc
                await asyncio.sleep(wait)
            else:
                raise
    raise RateLimitOrOverloadError(last_exc)


async def generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> ImplementationPlan:
    """
    Call Claude with three tools (answer / analyze / plan) using AsyncAnthropic.
    Claude selects the appropriate tool based on the query's intent.

    Uses the async client directly — no thread blocking via run_in_executor.
    """
    if importlib.util.find_spec("anthropic") is None:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic>=0.40.0"
        )

    client = _get_anthropic_client()
    user_message = _build_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    thinking_budget = settings.planning_thinking_budget
    call_params = {
        "model": settings.anthropic_model,
        "max_tokens": settings.planning_max_output_tokens + thinking_budget,
        "system": PLANNING_SYSTEM_PROMPT,
        "tools": [ANSWER_TOOL_SCHEMA, ANALYZE_IMPROVE_TOOL_SCHEMA, PLAN_TOOL_SCHEMA],
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": user_message}],
    }
    if thinking_budget > 0:
        call_params["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    # Acquire the semaphore so concurrent /plan requests queue instead of
    # firing in parallel and triggering 429 rate limits.
    async with _anthropic_semaphore:
        logger.info("planning: acquired API semaphore, calling Claude…")
        if thinking_budget > 0:
            message = await _generate_plan_via_stream(client, call_params)
        else:
            def _make_call():
                return client.messages.create(**call_params)
            message = await _with_overload_retry(_make_call)

    elapsed_ms = (time.monotonic() - t0) * 1000

    tool_used = next((b.name for b in message.content if b.type == "tool_use"), "none")
    logger.info(
        "planning: Claude responded in %.0fms, stop_reason=%s, tool=%s",
        elapsed_ms,
        message.stop_reason,
        tool_used,
    )

    return _parse_response(message, query, ctx, elapsed_ms)


# ── Streaming generator ────────────────────────────────────────────────────────


async def stream_generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream plan generation from Claude using AsyncAnthropic's native async streaming.

    No thread-pool bridge needed — the async client yields events directly.

    Yields:
      {"type": "token",         "text": "<chunk>"}
          Fired for every output token Claude emits — plain text for
          answer/analysis responses, partial-JSON for plan responses.

      {"type": "plan_complete", "plan": ImplementationPlan}
          Fired once when Claude's full response has been received and parsed.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic>=0.40.0"
        ) from exc

    import asyncio

    client = _get_anthropic_client()
    user_message = _build_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    thinking_budget = settings.planning_thinking_budget
    call_params = {
        "model": settings.anthropic_model,
        "max_tokens": settings.planning_max_output_tokens + thinking_budget,
        "system": PLANNING_SYSTEM_PROMPT,
        "tools": [ANSWER_TOOL_SCHEMA, ANALYZE_IMPROVE_TOOL_SCHEMA, PLAN_TOOL_SCHEMA],
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": user_message}],
    }
    if thinking_budget > 0:
        call_params["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    # Acquire semaphore to serialize concurrent /plan requests and avoid 429.
    # NOTE: We must hold the semaphore for the entire stream duration — releasing
    # it early would allow a second request to fire while we're still consuming
    # output tokens, and the combined input tokens would exceed the rate limit.
    async with _anthropic_semaphore:
        logger.info("planning: stream acquired API semaphore, calling Claude…")

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with client.messages.stream(**call_params) as stream:
                    async for event in stream:
                        event_type = getattr(event, "type", None)

                        # ── extended thinking — forward for transparency ──────
                        if event_type == "thinking":
                            thinking_text = getattr(event, "thinking", None)
                            if thinking_text:
                                yield {"type": "thinking", "text": thinking_text}

                        # ── answer / analysis — plain text tokens ─────────────
                        elif event_type == "text":
                            text = getattr(event, "text", None)
                            if text:
                                yield {"type": "token", "text": text}

                        # ── plan — partial JSON via tool_use ──────────────────
                        elif event_type == "input_json":
                            partial = getattr(event, "partial_json", None)
                            if partial:
                                yield {"type": "token", "text": partial}

                    final_msg = await stream.get_final_message()

                elapsed_ms = (time.monotonic() - t0) * 1000
                tool_used = next(
                    (b.name for b in final_msg.content if b.type == "tool_use"), "none"
                )
                logger.info(
                    "planning: stream complete in %.0fms, stop_reason=%s, tool=%s",
                    elapsed_ms,
                    final_msg.stop_reason,
                    tool_used,
                )
                plan = _parse_response(final_msg, query, ctx, elapsed_ms)
                yield {"type": "plan_complete", "plan": plan}
                return  # success

            except anthropic.APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    retry_after = _get_retry_after(exc)
                    if retry_after:
                        wait = min(retry_after, 120)
                    elif exc.status_code == 429:
                        wait = min(5 * (2 ** attempt), 120)
                    else:
                        wait = 2 ** attempt
                    label = "rate-limited (429)" if exc.status_code == 429 else "overloaded (529)"
                    logger.warning(
                        "planning: stream API %s, retry %d/%d in %.0fs",
                        label, attempt + 1, _MAX_RETRIES, wait,
                    )
                    last_exc = exc
                    await asyncio.sleep(wait)
                else:
                    raise

        raise RateLimitOrOverloadError(last_exc)
