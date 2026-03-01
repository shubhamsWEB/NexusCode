"""
LLM caller for the /plan endpoint.

Intent-aware response: the LLM picks one of three tools depending on query type.

  answer_codebase_question   -> query is a question / explanation / analysis
  analyze_and_improve        -> improvement / review / audit query
  output_implementation_plan -> query requires code changes (add, fix, refactor)

Uses the provider abstraction layer (src.llm) to support multiple LLM backends
(Anthropic, OpenAI, Grok) with per-request model selection.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from src.config import settings
from src.llm import get_provider
from src.llm.tool_converter import from_anthropic_schema
from src.llm.types import LLMResponse, LLMStreamEvent
from src.planning.retriever import PlanningContext
from src.planning.schemas import (
    ANALYZE_IMPROVE_TOOL_SCHEMA,
    ANSWER_TOOL_SCHEMA,
    PLAN_TOOL_SCHEMA,
    ImplementationPlan,
    PlanMetadata,
    SPARCSummary,
)
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# Convert Anthropic-format tool schemas to provider-agnostic LLMToolSchema
_TOOLS = [
    from_anthropic_schema(ANSWER_TOOL_SCHEMA),
    from_anthropic_schema(ANALYZE_IMPROVE_TOOL_SCHEMA),
    from_anthropic_schema(PLAN_TOOL_SCHEMA),
]

# ── System prompt ──────────────────────────────────────────────────────────────

PLANNING_SYSTEM_PROMPT = """\
You are a principal software architect with deep expertise in system design, \
API evolution, and production-grade engineering. You generate implementation \
plans that a coding agent will execute directly. Every token you output must \
be actionable. You have the codebase context below - USE it to inform the \
plan; do NOT echo it back.

Apply your full architectural reasoning to every plan. Think about system \
boundaries, failure domains, contract evolution, and operational cost. \
Do not produce surface-level plans - reason about the system holistically.

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
These blocks are reference material - absorb the knowledge, discard the text.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL SELECTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`analyze_and_improve` - IMPROVE / REVIEW / AUDIT existing code (cite file:line).
`answer_codebase_question` - QUESTION about the codebase (no file changes).
`output_implementation_plan` - TASK requiring file edits (structured plan).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURED REASONING PROTOCOL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When generating an implementation plan, follow this reasoning process. \
You have deep architectural understanding - USE it. Do not produce \
shallow plans. Think like an architect who owns the system long-term.

PHASE 1 - CONSTRAINTS FIRST (before any design):
  Identify and output the binding constraints in the `constraints` field:
  • Framework constraints (e.g. FastAPI cannot mix UploadFile + JSON body)
  • Runtime constraints (e.g. async-only, no blocking I/O in event loop)
  • API contract constraints (e.g. existing clients depend on this shape)
  • Backward compatibility constraints (will existing callers break?)
  • Payload/performance constraints (token limits, size limits, latency)
  Do NOT propose design steps until constraints are identified in the output.

PHASE 2 - DESIGN ALTERNATIVES (for non-trivial changes):
  For any change that touches API contracts, data flow, or architecture, \
  generate at least two viable approaches in `design_alternatives`. For each:
  • Describe the core idea
  • List advantages and disadvantages
  • State why it was selected or rejected (`rejected_reason`)
  Do not jump directly to a single solution. The chosen approach should \
  be the one in `design_decisions` with explicit justification using \
  complexity, scalability, migration cost, and failure risk.

PHASE 3 - FAILURE MODE ANALYSIS:
  For any API-level or architectural change, populate `failure_modes`:
  • What can go wrong at runtime? (malformed input, size overflow, \
    timeout, partial failure, race condition)
  • Why would it happen? (adversarial input, network, concurrency)
  • What guards prevent it? (validation, circuit breaker, fallback)
  Assume inputs and runtime conditions may be invalid or adversarial.

PHASE 4 - PLAN CONSTRUCTION (applying quality gates):
  While constructing the plan, enforce these architectural disciplines:

  LAYER SEPARATION - Group changes by system layer, not by file:
    API contract changes -> Business logic -> Transport/encoding -> \
    UI/client -> Config/infra. Do not mix responsibilities.

  BACKWARD COMPATIBILITY - Explicitly state in `clarifying_assumptions`:
    • Whether existing clients break
    • What the safe rollout strategy is
    • What compatibility safeguards exist
    Never assume breaking changes are acceptable unless the query says so.

  COST & PERFORMANCE - Evaluate and reflect in `design_decisions`:
    • Payload size impact
    • Token/compute cost (critical for LLM pipelines)
    • Latency implications
    Avoid designs that silently increase operational cost.

  FUTURE EVOLUTION - When committing to a schema or interface:
    • Will this design block future extensions?
    • What would trigger a refactor?
    • Are there migration implications?
    Add risks for designs that are optimal only for the immediate request.

  ANTI-OVERENGINEERING - Prefer modifying existing components over \
    introducing new abstractions. New services/managers/layers only if:
    • Reuse is genuinely expected
    • Coupling is measurably reduced
    • Complexity goes down, not up

  VALIDATION PLACEMENT - Place guards at correct system boundaries:
    • Input validation at the API boundary
    • Business rules in domain logic
    • UI validation only for UX constraints
    Cite the specific file where each guard belongs in `files`.

  INCREMENTAL RISK - Default to changes that minimize:
    • Regression risk
    • Surface area of modification
    • System destabilization
    Escalate to larger refactors only when strictly necessary.

  NO SILENT ASSUMPTIONS - Every assumption that influences design \
    must appear in `clarifying_assumptions`. No hidden reasoning.

  INTERNAL CONSISTENCY - Before finalizing: verify the plan contains \
    no contradictory steps, aligns with identified constraints, and \
    does not violate earlier assumptions. Reconcile inconsistencies.

  GROUNDED IN CONTEXT - The plan must be specifically grounded in \
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
SPARC METHODOLOGY (populate sparc_summary for all implementation plans)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The above reasoning protocol maps to SPARC phases. Capture each in sparc_summary:
S - SPECIFICATION: What exactly needs to be built. 1-2 sentences. Requirements + acceptance criteria.
P - PSEUDOCODE: Non-trivial algorithmic logic in pseudocode. Skip for simple changes.
A - ARCHITECTURE: How the change flows through the existing system. Maps to `summary`.
R - REFINEMENT: Edge cases, trade-offs, failure modes addressed. Maps to design_alternatives + failure_modes.
C - COMPLETION: How to verify done. Specific tests and checks. Maps to test_plan.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT QUALITY BAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The plan must match the quality of a senior architect's design review:

COMPREHENSIVENESS - trace ALL affected files:
• If adding a schema field, trace it through: schema -> planner -> API \
  endpoint -> MCP tool -> dashboard UI -> config.
• If modifying a function signature, find every caller in the context.
• Include config changes, type updates, and UI rendering - not just the \
  primary logic files.

DESIGN DECISIONS - explain WHY, not just WHAT:
• For each non-obvious technical choice, add an entry to `design_decisions` \
  explaining the rationale (e.g. "Base64-in-JSON instead of multipart to \
  preserve the existing JSON contract").
• Max 5 decisions. Focus on choices where reasonable alternatives exist.

ARCHITECTURE - describe the data/control flow:
• `summary` should describe HOW the change flows through the system. \
  Not a list of packages. 2-4 sentences.
• When the change involves a new data flow or pipeline, describe it.

PRECISION - exact edit instructions:
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
• If context is insufficient, say so - do not guess.
• Respect GROUNDING WARNINGS strictly.
• Every path in `files` must exist in the provided context.
\
"""


# ── Prompt helpers ─────────────────────────────────────────────────────────────


def _condense_stack_fingerprint(fingerprint: str) -> str:
    """
    Truncate the stack fingerprint to a compact summary.

    The full fingerprint contains raw dependency files (requirements.txt,
    package.json) and all top imports - often 2000+ tokens. The planner
    only needs the package names to avoid suggesting redundant installs.
    """
    lines = fingerprint.splitlines()
    condensed: list[str] = []
    skip_code_block = False
    char_budget = 1200  # ~300 tokens - enough for package names

    for line in lines:
        # Skip the raw file contents inside ```...``` blocks - keep only headers
        if line.strip().startswith("```"):
            skip_code_block = not skip_code_block
            continue
        if skip_code_block:
            # Inside a code block - extract only package names (lines that look
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

    # Enumerate the exact file paths present in the retrieved context.
    # This prevents the LLM from citing invented paths or training-data knowledge.
    if ctx.chunks_used:
        files_in_ctx = sorted({c["file"] for c in ctx.chunks_used})
        file_list = "\n".join(f"- `{f}`" for f in files_in_ctx)
        parts.append(
            "## Files Available in Retrieved Context\n"
            "You MUST cite ONLY the exact paths listed below. "
            "Do NOT reference any other file path, even if you know it from training data:\n"
            f"{file_list}"
        )

    # ── Query complexity metadata ─────────────────────────────────────────────
    if ctx.query_complexity != "simple" or ctx.sub_queries:
        meta = f"## Query Analysis\nComplexity: {ctx.query_complexity}"
        if len(ctx.sub_queries) > 1:
            meta += "\nDecomposed sub-queries:\n"
            for i, sq in enumerate(ctx.sub_queries, 1):
                meta += f"  {i}. {sq}\n"
        parts.append(meta)

    # ── Grounding warnings (critical - placed early) ──────────────────────────
    if ctx.grounding_warnings:
        critical_types = ("MISSING_PATH:", "MISSING_SYMBOL:", "NO_RESULTS:")
        critical_warnings = [w for w in ctx.grounding_warnings if w.startswith(critical_types)]
        advisory_warnings = [w for w in ctx.grounding_warnings if not w.startswith(critical_types)]

        if critical_warnings:
            critical_block = "## 🚨 CRITICAL: REQUIRED CONTENT IS NOT INDEXED\n"
            critical_block += (
                "The following files/symbols are NOT in the index. "
                "Do NOT reference them or fill in details from pretraining knowledge. "
                "The plan must clearly state that these files need to be indexed first:\n\n"
            )
            for w in critical_warnings:
                critical_block += f"- {w}\n"
            parts.append(critical_block)

        if advisory_warnings:
            warning_block = "## ⚠ GROUNDING WARNINGS\n"
            warning_block += "_The retrieval system detected these gaps. Respect them._\n\n"
            for w in advisory_warnings:
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

    # ── Tier 5: Stack fingerprint (XML-tagged reference - never echo) ────────
    if ctx.stack_fingerprint:
        condensed = _condense_stack_fingerprint(ctx.stack_fingerprint)
        parts.append(
            "<stack_fingerprint>\n"
            "REFERENCE ONLY - use to inform decisions, NEVER reproduce any of "
            "this content in your output. No package lists, no import tables.\n\n"
            + condensed
            + "\n</stack_fingerprint>"
        )

    # ── Tier 6: Web research (XML-tagged reference - never echo) ─────────────
    if ctx.web_research_notes:
        parts.append(
            "<web_research>\n"
            "REFERENCE ONLY - absorb the knowledge, discard the text. NEVER "
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
            "Be a world-class architect who has READ the code - not someone giving generic advice."
        )
    else:
        instruction = (
            "## Instructions\n"
            "Choose the right tool based on the query intent:\n"
            "- QUESTION -> `answer_codebase_question`\n"
            "- Requires CODE CHANGES -> `output_implementation_plan`\n\n"
            "Use ONLY real files and symbols from the codebase context above.\n"
            "Output ONLY the structured tool call - no prose outside the tool fields.\n"
            "For implementation plans:\n"
            "- Follow the STRUCTURED REASONING PROTOCOL: constraints -> alternatives -> "
            "failure modes -> plan construction.\n"
            "- Populate `constraints` BEFORE proposing any design.\n"
            "- Populate `design_alternatives` with >=2 approaches for non-trivial changes.\n"
            "- Populate `failure_modes` for API/architectural changes.\n"
            "- Populate `design_decisions` with WHY rationale for each non-obvious choice.\n"
            "- Trace ALL affected files end-to-end.\n"
        )
        if ctx.query_complexity == "complex":
            instruction += (
                "\nCOMPLEX query - apply your deepest architectural reasoning. "
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
    model: str,
) -> PlanMetadata:
    return PlanMetadata(
        model=model,
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
        quality_score=ctx.quality_score,
    )


def _parse_response(
    response: LLMResponse,
    query: str,
    ctx: PlanningContext,
    elapsed_ms: float,
    model: str,
) -> ImplementationPlan:
    """
    Parse a unified LLMResponse that may contain a tool call.
    Returns an ImplementationPlan with response_type set appropriately.
    """
    if not response.tool_calls:
        # LLM responded in text (shouldn't happen but handle gracefully)
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=response.text_content or "_No response generated._",
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms, model)
        return plan

    tool_call = response.tool_calls[0]

    if tool_call.name == "answer_codebase_question":
        data = tool_call.input
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=data.get("answer", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms, model)
        return plan

    if tool_call.name == "analyze_and_improve":
        data = tool_call.input
        plan = ImplementationPlan(
            query=query,
            response_type="analysis",
            analysis=data.get("analysis", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms, model)
        return plan

    # output_implementation_plan
    plan_data = tool_call.input
    plan_data["query"] = query
    plan = ImplementationPlan.model_validate(plan_data)
    plan.metadata = _build_metadata(ctx, elapsed_ms, model)

    # Parse SPARC summary if provided
    sparc_data = plan_data.get("sparc_summary")
    if sparc_data:
        plan.sparc = SPARCSummary.model_validate(sparc_data)

    return plan


# ── Re-export for backward compatibility ──────────────────────────────────────

_MAX_RETRIES = 5
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


# ── Sync generator ─────────────────────────────────────────────────────────────


async def generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    model: str | None = None,
) -> ImplementationPlan:
    """
    Call the LLM with three tools (answer / analyze / plan).
    The LLM selects the appropriate tool based on the query's intent.

    Args:
        model: Override model name. If None, uses settings.default_model.
    """
    effective_model = model or settings.default_model
    provider = get_provider(effective_model)

    user_message = _build_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    thinking_budget = settings.planning_thinking_budget if provider.supports_thinking else 0

    response = await provider.generate(
        model=effective_model,
        system=PLANNING_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        tools=_TOOLS,
        tool_choice="auto",
        max_tokens=settings.planning_max_output_tokens,
        thinking_budget=thinking_budget,
        temperature=0 if thinking_budget == 0 else 1,
    )

    elapsed_ms = (time.monotonic() - t0) * 1000

    tool_name = response.tool_calls[0].name if response.tool_calls else "none"
    logger.info(
        "planning: %s responded in %.0fms, stop_reason=%s, tool=%s",
        effective_model,
        elapsed_ms,
        response.stop_reason,
        tool_name,
    )

    return _parse_response(response, query, ctx, elapsed_ms, effective_model)


# ── Streaming generator ────────────────────────────────────────────────────────


async def stream_generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream plan generation from the LLM.

    Yields:
      {"type": "token",         "text": "<chunk>"}
      {"type": "thinking",      "text": "<chunk>"}
      {"type": "plan_complete", "plan": ImplementationPlan}
    """
    effective_model = model or settings.default_model
    provider = get_provider(effective_model)

    user_message = _build_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    thinking_budget = settings.planning_thinking_budget if provider.supports_thinking else 0

    async for event in provider.stream(
        model=effective_model,
        system=PLANNING_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        tools=_TOOLS,
        tool_choice="auto",
        max_tokens=settings.planning_max_output_tokens,
        thinking_budget=thinking_budget,
        temperature=0 if thinking_budget == 0 else 1,
    ):
        if isinstance(event, LLMStreamEvent):
            if event.type == "thinking":
                yield {"type": "thinking", "text": event.text}
            elif event.type in ("text", "input_json"):
                yield {"type": "token", "text": event.text}
        elif isinstance(event, LLMResponse):
            # Final response
            elapsed_ms = (time.monotonic() - t0) * 1000
            tool_name = event.tool_calls[0].name if event.tool_calls else "none"
            logger.info(
                "planning: stream complete in %.0fms, stop_reason=%s, tool=%s",
                elapsed_ms,
                event.stop_reason,
                tool_name,
            )
            plan = _parse_response(event, query, ctx, elapsed_ms, effective_model)
            yield {"type": "plan_complete", "plan": plan}
