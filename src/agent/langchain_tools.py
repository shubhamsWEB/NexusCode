"""
NexusCode tools wrapped as LangChain BaseTool subclasses.

Wrapping strategy:
  - Every tool's _arun() delegates to the existing tool_executor.execute_tool()
    or integrations dispatcher.dispatch_tool() — zero logic duplication.
  - Tools appear as structured spans in LangSmith traces.
  - Compatible with LangGraph ToolNode for future graph-native tool execution.
  - Integration tool _arun() methods fetch credentials transparently — the LLM
    never sees tokens (same isolation guarantee as the existing dispatch path).

Usage:
    from src.agent.langchain_tools import NEXUSCODE_LC_TOOLS, get_lc_tools_for_role

    tools = get_lc_tools_for_role("pm_agent")
    # Pass to LangGraph ToolNode or use directly in chains
"""

from __future__ import annotations

import json
from typing import Any, Type

from pydantic import BaseModel, Field

try:
    from langchain_core.tools import BaseTool
    _LC_AVAILABLE = True
except ImportError:
    _LC_AVAILABLE = False
    # Provide a no-op stub so imports don't break if langchain-core isn't installed
    class BaseTool:  # type: ignore[no-redef]
        name: str = ""
        description: str = ""
        args_schema: Any = None
        def _run(self, *args, **kwargs): raise NotImplementedError
        async def _arun(self, *args, **kwargs): raise NotImplementedError

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


# ── Shared async dispatch helper ───────────────────────────────────────────────

async def _exec(name: str, inp: dict, repo_owner: str | None = None, repo_name: str | None = None) -> str:
    """Delegate to the existing tool executor and return a JSON string."""
    from src.agent.tool_executor import execute_tool
    return await execute_tool(name, inp, repo_owner=repo_owner, repo_name=repo_name)


async def _dispatch(name: str, params: dict) -> str:
    """Delegate to integration dispatcher and return a JSON string."""
    from src.integrations.dispatcher import dispatch_tool
    result = await dispatch_tool(name, params)
    return json.dumps(result) if not isinstance(result, str) else result


# ── Input schemas ──────────────────────────────────────────────────────────────

class SearchCodebaseInput(BaseModel):
    query: str = Field(description="Natural-language or identifier query")
    language: str | None = Field(default=None, description="Filter by language (python, typescript, etc.)")
    top_k: int = Field(default=5, ge=1, le=15, description="Max chunks to return")
    mode: str = Field(default="hybrid", description="hybrid | semantic | keyword")

class GetSymbolInput(BaseModel):
    name: str = Field(description="Symbol name to look up (exact, partial, or qualified)")

class FindCallersInput(BaseModel):
    symbol: str = Field(description="Function/method name to trace callers of")
    depth: int = Field(default=1, ge=1, le=2, description="Call-graph hops (1=direct, 2=callers of callers)")

class GetFileContextInput(BaseModel):
    path: str = Field(description="File path (partial OK, e.g. 'app.py' matches 'src/api/app.py')")
    include_deps: bool = Field(default=True, description="Include reverse dependency list")

class GetSemanticContextInput(BaseModel):
    symbols: list[str] = Field(description="Symbol names to get semantic relationships for")
    concept: str | None = Field(default=None, description="Optional concept filter (e.g. 'authentication')")

class GetAgentContextInput(BaseModel):
    task: str = Field(description="Coding task description — drives semantic search")
    focal_files: list[str] | None = Field(default=None, description="Files you're actively editing (max 5)")
    token_budget: int = Field(default=8000, description="Max tokens to include (default 8000, max 32000)")

class PlanImplementationInput(BaseModel):
    query: str = Field(description="Bug report, feature request, or refactoring task to plan")
    web_research: bool = Field(default=True, description="Search web for best practices first")

class AskCodebaseInput(BaseModel):
    question: str = Field(description="Natural-language question about the codebase")

class GeneratePdfInput(BaseModel):
    content: str = Field(description="Full markdown document content")
    title: str = Field(description="Document title")
    filename: str | None = Field(default=None, description="Output filename without .pdf")
    metadata: dict | None = Field(default=None, description="Optional key-value metadata")

# Integration input schemas
class JiraGetIssueInput(BaseModel):
    issue_key: str = Field(description="Jira issue key, e.g. PROJ-123")
    org_id: str = Field(default="default")

class JiraSearchInput(BaseModel):
    jql: str = Field(description="JQL query string")
    max_results: int = Field(default=20)
    org_id: str = Field(default="default")

class JiraCreateIssueInput(BaseModel):
    project_key: str
    summary: str
    description: str = ""
    issue_type: str = "Story"
    priority: str | None = None
    labels: list[str] | None = None
    org_id: str = "default"

class JiraUpdateIssueInput(BaseModel):
    issue_key: str
    summary: str | None = None
    description: str | None = None
    status: str | None = None
    comment: str | None = None
    org_id: str = "default"

class SlackSendMessageInput(BaseModel):
    channel: str = Field(description="Channel name or ID")
    text: str = Field(description="Message text")
    thread_ts: str | None = None
    org_id: str = "default"

class SlackGetHistoryInput(BaseModel):
    channel: str
    limit: int = 20
    org_id: str = "default"

class SlackListChannelsInput(BaseModel):
    org_id: str = "default"

class GithubCreatePRInput(BaseModel):
    owner: str; repo: str; title: str; body: str; head: str
    base: str = "main"; draft: bool = False; org_id: str = "default"

class GithubGetPRInput(BaseModel):
    owner: str; repo: str; pr_number: int; org_id: str = "default"

class GithubAddCommentInput(BaseModel):
    owner: str; repo: str; pr_number: int; body: str; org_id: str = "default"

class GithubGetDiffInput(BaseModel):
    owner: str; repo: str; pr_number: int; org_id: str = "default"

class GithubCreateIssueInput(BaseModel):
    owner: str; repo: str; title: str; body: str = ""
    labels: list[str] | None = None; assignees: list[str] | None = None
    org_id: str = "default"

class FigmaGetFileInput(BaseModel):
    file_key_or_url: str; depth: int = 2; org_id: str = "default"

class FigmaGetComponentInput(BaseModel):
    file_key_or_url: str; node_id: str; org_id: str = "default"

class FigmaGetComponentsInput(BaseModel):
    file_key_or_url: str; org_id: str = "default"

class FigmaGetStylesInput(BaseModel):
    file_key_or_url: str; org_id: str = "default"

class NotionGetPageInput(BaseModel):
    page_id: str; org_id: str = "default"

class NotionCreatePageInput(BaseModel):
    parent_id: str; title: str; content: str = ""
    parent_type: str = "page"; org_id: str = "default"

class NotionUpdatePageInput(BaseModel):
    page_id: str; title: str | None = None; content: str | None = None
    org_id: str = "default"

class NotionSearchInput(BaseModel):
    query: str; org_id: str = "default"


# ── Internal tool classes ──────────────────────────────────────────────────────

class SearchCodebaseTool(BaseTool):
    name: str = "search_codebase"
    description: str = (
        "Hybrid semantic + keyword search over all indexed code. "
        "The PRIMARY discovery tool — call this first for any unfamiliar code path. "
        "Returns code chunks with file paths, line numbers, and assembled context."
    )
    args_schema: Type[BaseModel] = SearchCodebaseInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, query: str, language: str | None = None, top_k: int = 5, mode: str = "hybrid") -> str:
        return await _exec("search_codebase", {"query": query, "language": language, "top_k": top_k, "mode": mode})


class GetSymbolTool(BaseTool):
    name: str = "get_symbol"
    description: str = (
        "Look up a specific function, class, method, or variable by name. "
        "Returns exact file location, signature, docstring, and line range. "
        "Supports fuzzy matching — partial names work."
    )
    args_schema: Type[BaseModel] = GetSymbolInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, name: str) -> str:
        return await _exec("get_symbol", {"name": name})


class FindCallersTool(BaseTool):
    name: str = "find_callers"
    description: str = (
        "Find every place in the codebase that calls a given function or method. "
        "Supports multi-hop BFS traversal. Essential for blast-radius analysis."
    )
    args_schema: Type[BaseModel] = FindCallersInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, symbol: str, depth: int = 1) -> str:
        return await _exec("find_callers", {"symbol": symbol, "depth": depth})


class GetFileContextTool(BaseTool):
    name: str = "get_file_context"
    description: str = (
        "Get the complete structural map of a source file: all symbols it defines, "
        "imports, and which other files import it."
    )
    args_schema: Type[BaseModel] = GetFileContextInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, path: str, include_deps: bool = True) -> str:
        return await _exec("get_file_context", {"path": path, "include_deps": include_deps})


class GetSemanticContextTool(BaseTool):
    name: str = "get_semantic_context"
    description: str = (
        "Retrieve LLM-extracted semantic architecture relationships for symbols. "
        "Returns facts the call graph can't show: 'AuthService validates JWT tokens'."
    )
    args_schema: Type[BaseModel] = GetSemanticContextInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, symbols: list[str], concept: str | None = None) -> str:
        return await _exec("get_semantic_context", {"symbols": symbols, "concept": concept})


class GetAgentContextTool(BaseTool):
    name: str = "get_agent_context"
    description: str = (
        "Pre-assembles the most relevant codebase context for a specific coding task in one call. "
        "Combines focal file chunks + semantic search + reranking within a token budget."
    )
    args_schema: Type[BaseModel] = GetAgentContextInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, task: str, focal_files: list[str] | None = None, token_budget: int = 8000) -> str:
        return await _exec("get_agent_context", {"task": task, "focal_files": focal_files, "token_budget": token_budget})


class PlanImplementationTool(BaseTool):
    name: str = "plan_implementation"
    description: str = (
        "Generate a complete, grounded implementation plan for a coding task. "
        "Combines web research with live codebase context. Heavyweight — runs its own agent loop."
    )
    args_schema: Type[BaseModel] = PlanImplementationInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, query: str, web_research: bool = True) -> str:
        return await _exec("plan_implementation", {"query": query, "web_research": web_research})


class AskCodebaseTool(BaseTool):
    name: str = "ask_codebase"
    description: str = (
        "Answer a natural-language question about the codebase in a mentor tone. "
        "Traces data flows, clarifies architecture, points to real file locations with citations."
    )
    args_schema: Type[BaseModel] = AskCodebaseInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, question: str) -> str:
        return await _exec("ask_codebase", {"question": question})


class GeneratePdfTool(BaseTool):
    name: str = "generate_pdf"
    description: str = (
        "Convert markdown content to a PDF document stored server-side. "
        "Returns a download URL."
    )
    args_schema: Type[BaseModel] = GeneratePdfInput

    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, content: str, title: str, filename: str | None = None, metadata: dict | None = None) -> str:
        return await _exec("generate_pdf", {"content": content, "title": title, "filename": filename, "metadata": metadata})


# ── Integration tool classes ───────────────────────────────────────────────────

class JiraGetIssueTool(BaseTool):
    name: str = "jira_get_issue"
    description: str = "Fetch a Jira issue by key (e.g. PROJ-123). Returns summary, status, assignee, description."
    args_schema: Type[BaseModel] = JiraGetIssueInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, issue_key: str, org_id: str = "default") -> str:
        return await _dispatch("jira_get_issue", {"issue_key": issue_key, "org_id": org_id})

class JiraSearchTool(BaseTool):
    name: str = "jira_search_issues"
    description: str = "Search Jira issues using JQL. Returns list of matching issues."
    args_schema: Type[BaseModel] = JiraSearchInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, jql: str, max_results: int = 20, org_id: str = "default") -> str:
        return await _dispatch("jira_search_issues", {"jql": jql, "max_results": max_results, "org_id": org_id})

class JiraCreateIssueTool(BaseTool):
    name: str = "jira_create_issue"
    description: str = "Create a new Jira issue. Returns the issue key and URL."
    args_schema: Type[BaseModel] = JiraCreateIssueInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, **kwargs) -> str:
        return await _dispatch("jira_create_issue", kwargs)

class JiraUpdateIssueTool(BaseTool):
    name: str = "jira_update_issue"
    description: str = "Update a Jira issue — change summary, description, status, or add a comment."
    args_schema: Type[BaseModel] = JiraUpdateIssueInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, **kwargs) -> str:
        return await _dispatch("jira_update_issue", kwargs)

class SlackSendMessageTool(BaseTool):
    name: str = "slack_send_message"
    description: str = "Post a message to a Slack channel. Returns the message timestamp."
    args_schema: Type[BaseModel] = SlackSendMessageInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, channel: str, text: str, thread_ts: str | None = None, org_id: str = "default") -> str:
        return await _dispatch("slack_send_message", {"channel": channel, "text": text, "thread_ts": thread_ts, "org_id": org_id})

class SlackGetHistoryTool(BaseTool):
    name: str = "slack_get_channel_history"
    description: str = "Fetch recent messages from a Slack channel."
    args_schema: Type[BaseModel] = SlackGetHistoryInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, channel: str, limit: int = 20, org_id: str = "default") -> str:
        return await _dispatch("slack_get_channel_history", {"channel": channel, "limit": limit, "org_id": org_id})

class SlackListChannelsTool(BaseTool):
    name: str = "slack_list_channels"
    description: str = "List available Slack channels in the workspace."
    args_schema: Type[BaseModel] = SlackListChannelsInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, org_id: str = "default") -> str:
        return await _dispatch("slack_list_channels", {"org_id": org_id})

class GithubCreatePRTool(BaseTool):
    name: str = "github_create_pr"
    description: str = "Create a GitHub pull request. Returns PR URL and number."
    args_schema: Type[BaseModel] = GithubCreatePRInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, **kwargs) -> str:
        return await _dispatch("github_create_pr", kwargs)

class GithubGetPRTool(BaseTool):
    name: str = "github_get_pr"
    description: str = "Get details of a GitHub pull request."
    args_schema: Type[BaseModel] = GithubGetPRInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, owner: str, repo: str, pr_number: int, org_id: str = "default") -> str:
        return await _dispatch("github_get_pr", {"owner": owner, "repo": repo, "pr_number": pr_number, "org_id": org_id})

class GithubAddCommentTool(BaseTool):
    name: str = "github_add_pr_comment"
    description: str = "Add a review comment to a GitHub pull request."
    args_schema: Type[BaseModel] = GithubAddCommentInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, owner: str, repo: str, pr_number: int, body: str, org_id: str = "default") -> str:
        return await _dispatch("github_add_pr_comment", {"owner": owner, "repo": repo, "pr_number": pr_number, "body": body, "org_id": org_id})

class GithubGetDiffTool(BaseTool):
    name: str = "github_get_pr_diff"
    description: str = "Get the unified diff of a GitHub pull request."
    args_schema: Type[BaseModel] = GithubGetDiffInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, owner: str, repo: str, pr_number: int, org_id: str = "default") -> str:
        return await _dispatch("github_get_pr_diff", {"owner": owner, "repo": repo, "pr_number": pr_number, "org_id": org_id})

class GithubCreateIssueTool(BaseTool):
    name: str = "github_create_issue"
    description: str = "Create a GitHub issue. Returns the issue URL and number."
    args_schema: Type[BaseModel] = GithubCreateIssueInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, **kwargs) -> str:
        return await _dispatch("github_create_issue", kwargs)

class FigmaGetFileTool(BaseTool):
    name: str = "figma_get_file"
    description: str = "Get a Figma file's structure and components."
    args_schema: Type[BaseModel] = FigmaGetFileInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, file_key_or_url: str, depth: int = 2, org_id: str = "default") -> str:
        return await _dispatch("figma_get_file", {"file_key_or_url": file_key_or_url, "depth": depth, "org_id": org_id})

class FigmaGetComponentTool(BaseTool):
    name: str = "figma_get_component"
    description: str = "Get a specific Figma component node by ID."
    args_schema: Type[BaseModel] = FigmaGetComponentInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, file_key_or_url: str, node_id: str, org_id: str = "default") -> str:
        return await _dispatch("figma_get_component", {"file_key_or_url": file_key_or_url, "node_id": node_id, "org_id": org_id})

class FigmaGetComponentsTool(BaseTool):
    name: str = "figma_get_components"
    description: str = "List all components in a Figma file."
    args_schema: Type[BaseModel] = FigmaGetComponentsInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, file_key_or_url: str, org_id: str = "default") -> str:
        return await _dispatch("figma_get_components", {"file_key_or_url": file_key_or_url, "org_id": org_id})

class FigmaGetStylesTool(BaseTool):
    name: str = "figma_get_styles"
    description: str = "Get design tokens (colors, typography, effects) from a Figma file."
    args_schema: Type[BaseModel] = FigmaGetStylesInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, file_key_or_url: str, org_id: str = "default") -> str:
        return await _dispatch("figma_get_styles", {"file_key_or_url": file_key_or_url, "org_id": org_id})

class NotionGetPageTool(BaseTool):
    name: str = "notion_get_page"
    description: str = "Read a Notion page by ID. Returns title and content blocks."
    args_schema: Type[BaseModel] = NotionGetPageInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, page_id: str, org_id: str = "default") -> str:
        return await _dispatch("notion_get_page", {"page_id": page_id, "org_id": org_id})

class NotionCreatePageTool(BaseTool):
    name: str = "notion_create_page"
    description: str = "Create a new Notion page under a parent page or database."
    args_schema: Type[BaseModel] = NotionCreatePageInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, **kwargs) -> str:
        return await _dispatch("notion_create_page", kwargs)

class NotionUpdatePageTool(BaseTool):
    name: str = "notion_update_page"
    description: str = "Update the title or content of an existing Notion page."
    args_schema: Type[BaseModel] = NotionUpdatePageInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, **kwargs) -> str:
        return await _dispatch("notion_update_page", kwargs)

class NotionSearchTool(BaseTool):
    name: str = "notion_search"
    description: str = "Search Notion pages and databases by query string."
    args_schema: Type[BaseModel] = NotionSearchInput
    def _run(self, **kwargs): raise NotImplementedError("Use async")
    async def _arun(self, query: str, org_id: str = "default") -> str:
        return await _dispatch("notion_search", {"query": query, "org_id": org_id})


# ── Registries ─────────────────────────────────────────────────────────────────

# All internal NexusCode retrieval tools
INTERNAL_LC_TOOLS: list[BaseTool] = [
    SearchCodebaseTool(),
    GetSymbolTool(),
    FindCallersTool(),
    GetFileContextTool(),
    GetSemanticContextTool(),
    GetAgentContextTool(),
    PlanImplementationTool(),
    AskCodebaseTool(),
    GeneratePdfTool(),
]

# All integration tools
INTEGRATION_LC_TOOLS: list[BaseTool] = [
    JiraGetIssueTool(), JiraSearchTool(), JiraCreateIssueTool(), JiraUpdateIssueTool(),
    SlackSendMessageTool(), SlackGetHistoryTool(), SlackListChannelsTool(),
    GithubCreatePRTool(), GithubGetPRTool(), GithubAddCommentTool(),
    GithubGetDiffTool(), GithubCreateIssueTool(),
    FigmaGetFileTool(), FigmaGetComponentTool(), FigmaGetComponentsTool(), FigmaGetStylesTool(),
    NotionGetPageTool(), NotionCreatePageTool(), NotionUpdatePageTool(), NotionSearchTool(),
]

# Combined
NEXUSCODE_LC_TOOLS: list[BaseTool] = INTERNAL_LC_TOOLS + INTEGRATION_LC_TOOLS

# Quick name→tool index
_TOOL_BY_NAME: dict[str, BaseTool] = {t.name: t for t in NEXUSCODE_LC_TOOLS}


def get_lc_tools_for_role(role: str) -> list[BaseTool]:
    """
    Return the LangChain BaseTool instances allowed for a given role.
    Mirrors the existing get_tools_for_role() in integrations/registry.py
    but returns BaseTool objects instead of JSON schema dicts.
    """
    from src.agent.roles import _ROLES
    from src.agent.enterprise_roles import ENTERPRISE_ROLES
    from src.integrations.registry import _ROLE_TOOLS

    # Gather tool names: role default_tools + integration tools
    base = _ROLES.get(role) or ENTERPRISE_ROLES.get(role) or {}
    tool_names: set[str] = set(base.get("default_tools") or [])
    tool_names.update(_ROLE_TOOLS.get(role) or [])

    return [_TOOL_BY_NAME[n] for n in tool_names if n in _TOOL_BY_NAME]


def get_lc_tool(name: str) -> BaseTool | None:
    """Return a single LangChain tool by name."""
    return _TOOL_BY_NAME.get(name)
