import { useState } from "react";

const layers = [
  {
    id: "triggers",
    label: "TRIGGER LAYER",
    color: "#6366f1",
    items: [
      { icon: "🔔", label: "GitHub Webhook", desc: "PR opened, push, issue created" },
      { icon: "⏰", label: "APScheduler", desc: "Cron jobs (nightly audit, etc.)" },
      { icon: "🌐", label: "REST API", desc: "Manual or programmatic trigger" },
      { icon: "🔗", label: "n8n / Zapier", desc: "External workflow calls POST /run" },
    ],
  },
  {
    id: "registry",
    label: "WORKFLOW REGISTRY",
    color: "#8b5cf6",
    items: [
      { icon: "📄", label: "YAML DSL", desc: "Human-readable workflow definitions" },
      { icon: "🗄️", label: "PostgreSQL", desc: "Versioned workflow storage" },
      { icon: "✅", label: "DAG Validator", desc: "Step dependency + cycle detection" },
      { icon: "🎯", label: "Trigger Router", desc: "Matches events to workflows" },
    ],
  },
  {
    id: "executor",
    label: "DAG EXECUTOR  (extends RQ)",
    color: "#a855f7",
    items: [
      { icon: "⚡", label: "Parallel Branches", desc: "Independent steps run concurrently" },
      { icon: "🔄", label: "Retry + Backoff", desc: "Per-step retry policies" },
      { icon: "🧑‍💻", label: "Human Checkpoints", desc: "Pause, get approval, resume" },
      { icon: "📊", label: "State Machine", desc: "PENDING→RUNNING→DONE/FAILED" },
    ],
  },
  {
    id: "agents",
    label: "MULTI-AGENT ROUTER",
    color: "#ec4899",
    items: [
      { icon: "🧠", label: "Supervisor", desc: "Decomposes task, assigns sub-agents" },
      { icon: "🔍", label: "Searcher", desc: "Deep codebase navigation specialist" },
      { icon: "📐", label: "Planner", desc: "Implementation planning specialist" },
      { icon: "👁️", label: "Reviewer", desc: "Security + performance specialist" },
      { icon: "✍️", label: "Coder", desc: "Code generation specialist" },
      { icon: "🧪", label: "Tester", desc: "Test generation specialist" },
    ],
  },
  {
    id: "agentloop",
    label: "EXISTING AgentLoop + ToolExecutor + MCP Bridge  (UNCHANGED)",
    color: "#14b8a6",
    items: [
      { icon: "🔎", label: "search_codebase", desc: "Semantic + keyword hybrid search" },
      { icon: "🔣", label: "get_symbol", desc: "Fuzzy symbol / definition lookup" },
      { icon: "📞", label: "find_callers", desc: "Call graph traversal (3-hop)" },
      { icon: "🗺️", label: "plan_implementation", desc: "7-phase planning retrieval" },
      { icon: "❓", label: "ask_codebase", desc: "Mentor-tone Q&A agent" },
      { icon: "🌉", label: "MCP Bridge", desc: "External tool servers" },
    ],
  },
  {
    id: "store",
    label: "EXECUTION CONTEXT STORE  (4 new Postgres tables)",
    color: "#0ea5e9",
    items: [
      { icon: "📋", label: "workflow_definitions", desc: "YAML + trigger config" },
      { icon: "▶️", label: "workflow_runs", desc: "Status, payload, cost" },
      { icon: "🪜", label: "step_executions", desc: "Per-step I/O, tokens, timing" },
      { icon: "🙋", label: "human_checkpoints", desc: "Approval prompts + responses" },
    ],
  },
];

const useCases = [
  {
    icon: "🔍",
    title: "Automated PR Review",
    trigger: "github.pull_request.opened",
    steps: ["fetch_diff", "understand_changes", "security_review ∥ perf_review", "synthesize", "human_approval?", "post_comment"],
    color: "#6366f1",
  },
  {
    icon: "🌙",
    title: "Nightly Codebase Audit",
    trigger: "cron: 0 2 * * *",
    steps: ["scan_tech_debt", "find_security_patterns", "check_dead_code", "generate_digest", "slack.send"],
    color: "#8b5cf6",
  },
  {
    icon: "💡",
    title: "Issue → Implementation Plan",
    trigger: "github.issue.labeled(needs-plan)",
    steps: ["parse_issue", "search_related_code", "plan_implementation", "post_to_issue"],
    color: "#a855f7",
  },
  {
    icon: "⚠️",
    title: "Breaking Change Detector",
    trigger: "github.push (main branch)",
    steps: ["get_changed_symbols", "find_callers (3-hop)", "flag_risky_callers", "create_gh_check"],
    color: "#ec4899",
  },
  {
    icon: "🚀",
    title: "New Repo Onboarding",
    trigger: "repos.register",
    steps: ["auto_index", "generate_arch_doc", "create_symbol_map", "post_wiki"],
    color: "#14b8a6",
  },
];

const newComponents = [
  { path: "src/workflows/registry.py", desc: "CRUD for workflow definitions" },
  { path: "src/workflows/parser.py", desc: "YAML DSL parser + validator" },
  { path: "src/workflows/executor.py", desc: "DAG executor with parallel branches" },
  { path: "src/workflows/context.py", desc: "Inter-step data passing + templating" },
  { path: "src/agent/roles.py", desc: "Specialized agent role prompts" },
  { path: "src/agent/router.py", desc: "Multi-agent supervisor/spawner" },
  { path: "src/events/bus.py", desc: "Redis pub/sub event bus" },
  { path: "src/scheduler/", desc: "APScheduler cron integration" },
  { path: "src/api/workflows.py", desc: "6 new REST endpoints" },
  { path: "src/ui/_pages/workflows.py", desc: "Streamlit workflow designer + monitor" },
];

export default function App() {
  const [activeLayer, setActiveLayer] = useState(null);
  const [activeUseCase, setActiveUseCase] = useState(null);
  const [tab, setTab] = useState("arch");

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", background: "#0f0f1a", minHeight: "100vh", color: "#e2e8f0", padding: "24px" }}>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Header */}
        <div style={{ marginBottom: 32, borderBottom: "1px solid #1e293b", paddingBottom: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
            <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#6366f1", boxShadow: "0 0 10px #6366f1" }} />
            <span style={{ color: "#64748b", fontSize: 12, letterSpacing: 2, textTransform: "uppercase" }}>NexusCode · Architecture Proposal</span>
          </div>
          <h1 style={{ fontSize: 28, fontWeight: 700, margin: 0, background: "linear-gradient(135deg, #6366f1, #ec4899)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>
            Codebase Automation Engine
          </h1>
          <p style={{ color: "#64748b", marginTop: 8, fontSize: 14 }}>
            Native multi-agent workflow orchestration — every step has codebase intelligence baked in
          </p>
        </div>

        {/* Tabs */}
        <div style={{ display: "flex", gap: 4, marginBottom: 28 }}>
          {[["arch", "Architecture"], ["usecases", "Use Cases"], ["build", "What to Build"]].map(([id, label]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              style={{
                padding: "8px 18px", borderRadius: 8, border: "none", cursor: "pointer", fontSize: 13, fontWeight: 600,
                background: tab === id ? "#6366f1" : "#1e293b",
                color: tab === id ? "white" : "#64748b",
                transition: "all 0.15s",
              }}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Architecture Tab */}
        {tab === "arch" && (
          <div>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              {layers.map((layer, i) => (
                <div key={layer.id}>
                  <div
                    onClick={() => setActiveLayer(activeLayer === layer.id ? null : layer.id)}
                    style={{
                      background: activeLayer === layer.id ? `${layer.color}22` : "#1a1a2e",
                      border: `1px solid ${activeLayer === layer.id ? layer.color : "#1e293b"}`,
                      borderRadius: 10, padding: "12px 16px", cursor: "pointer",
                      transition: "all 0.2s",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                        <div style={{ width: 3, height: 24, borderRadius: 2, background: layer.color }} />
                        <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: 1.5, textTransform: "uppercase", color: layer.color }}>
                          {layer.label}
                        </span>
                      </div>
                      <span style={{ color: "#64748b", fontSize: 18, lineHeight: 1 }}>{activeLayer === layer.id ? "−" : "+"}</span>
                    </div>

                    {activeLayer === layer.id && (
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 10, marginTop: 14 }}>
                        {layer.items.map((item) => (
                          <div
                            key={item.label}
                            style={{
                              background: "#0f0f1a", border: `1px solid ${layer.color}44`, borderRadius: 8,
                              padding: "10px 12px",
                            }}
                          >
                            <div style={{ fontSize: 16, marginBottom: 4 }}>{item.icon}</div>
                            <div style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0", marginBottom: 3 }}>{item.label}</div>
                            <div style={{ fontSize: 11, color: "#64748b", lineHeight: 1.4 }}>{item.desc}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  {i < layers.length - 1 && (
                    <div style={{ display: "flex", justifyContent: "center", padding: "2px 0" }}>
                      <div style={{ width: 2, height: 14, background: "#1e293b" }} />
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* vs n8n callout */}
            <div style={{ marginTop: 28, background: "#1a1a2e", border: "1px solid #1e293b", borderRadius: 12, padding: 20 }}>
              <h3 style={{ margin: "0 0 14px", fontSize: 14, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 1 }}>Why Not n8n?</h3>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={{ background: "#0f0f1a", border: "1px solid #ef444422", borderRadius: 8, padding: 14 }}>
                  <div style={{ color: "#ef4444", fontSize: 12, fontWeight: 700, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>n8n as the brain ✗</div>
                  {["Separate Node.js service = infra complexity", "Steps are HTTP black boxes — no codebase context", "State lives in n8n, not your Postgres", "Generic connectors, not code-aware", "Visual builder adds latency to dev loops"].map(p => (
                    <div key={p} style={{ fontSize: 12, color: "#94a3b8", marginBottom: 6, display: "flex", gap: 6, alignItems: "flex-start" }}>
                      <span style={{ color: "#ef4444", flexShrink: 0 }}>✗</span>{p}
                    </div>
                  ))}
                </div>
                <div style={{ background: "#0f0f1a", border: "1px solid #22c55e22", borderRadius: 8, padding: 14 }}>
                  <div style={{ color: "#22c55e", fontSize: 12, fontWeight: 700, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>n8n as a trigger ✓</div>
                  {["n8n handles Slack/Jira/Calendar integrations", "Sends POST /workflows/{id}/run to NexusCode", "NexusCode owns the intelligence layer", "Best of both: 400+ connectors + codebase brain", "No duplicate state — everything in your Postgres"].map(p => (
                    <div key={p} style={{ fontSize: 12, color: "#94a3b8", marginBottom: 6, display: "flex", gap: 6, alignItems: "flex-start" }}>
                      <span style={{ color: "#22c55e", flexShrink: 0 }}>✓</span>{p}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Use Cases Tab */}
        {tab === "usecases" && (
          <div>
            <p style={{ color: "#64748b", fontSize: 13, marginBottom: 20 }}>
              Click any use case to see its workflow steps. These are Day-1 templates — shippable workflows out of the box.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 14 }}>
              {useCases.map((uc) => (
                <div
                  key={uc.title}
                  onClick={() => setActiveUseCase(activeUseCase === uc.title ? null : uc.title)}
                  style={{
                    background: activeUseCase === uc.title ? `${uc.color}18` : "#1a1a2e",
                    border: `1px solid ${activeUseCase === uc.title ? uc.color : "#1e293b"}`,
                    borderRadius: 12, padding: 18, cursor: "pointer", transition: "all 0.2s",
                  }}
                >
                  <div style={{ fontSize: 24, marginBottom: 10 }}>{uc.icon}</div>
                  <div style={{ fontSize: 15, fontWeight: 700, color: "#e2e8f0", marginBottom: 6 }}>{uc.title}</div>
                  <div style={{ fontSize: 11, color: uc.color, fontFamily: "monospace", marginBottom: 12, background: `${uc.color}18`, padding: "4px 8px", borderRadius: 4, display: "inline-block" }}>
                    trigger: {uc.trigger}
                  </div>

                  {activeUseCase === uc.title && (
                    <div style={{ marginTop: 12 }}>
                      <div style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Steps</div>
                      {uc.steps.map((step, i) => (
                        <div key={step} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                          <div style={{ width: 20, height: 20, borderRadius: "50%", background: `${uc.color}33`, border: `1px solid ${uc.color}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, fontWeight: 700, color: uc.color, flexShrink: 0 }}>
                            {i + 1}
                          </div>
                          <div style={{ fontSize: 12, color: "#94a3b8", fontFamily: "monospace" }}>{step}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* YAML preview */}
            <div style={{ marginTop: 28, background: "#1a1a2e", border: "1px solid #1e293b", borderRadius: 12, padding: 20 }}>
              <div style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: 1.5, marginBottom: 14 }}>Sample YAML — PR Review Workflow</div>
              <pre style={{ fontSize: 12, color: "#a5f3fc", lineHeight: 1.6, margin: 0, overflowX: "auto", fontFamily: "monospace" }}>{`name: automated-pr-review
trigger:
  type: webhook
  filter:
    event: github.pull_request.opened

steps:
  - id: understand_changes
    type: agent
    role: searcher
    task: "Analyze diff, find related symbols"
    tools: [search_codebase, get_symbol, find_callers]

  - id: security_review
    type: agent
    role: reviewer
    depends_on: [understand_changes]
    parallel_with: [performance_review]

  - id: performance_review
    type: agent
    role: reviewer
    depends_on: [understand_changes]
    parallel_with: [security_review]

  - id: synthesize
    type: agent
    role: planner
    depends_on: [security_review, performance_review]

  - id: human_approval
    type: human_checkpoint
    timeout_hours: 4
    on_timeout: skip

  - id: post_comment
    type: action
    action: github.post_pr_comment`}</pre>
            </div>
          </div>
        )}

        {/* What to Build Tab */}
        {tab === "build" && (
          <div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 20 }}>
              {[
                { sprint: "Sprint 1", label: "Foundation", color: "#6366f1", items: ["workflow_definitions, workflow_runs, step_executions tables", "WorkflowRegistry CRUD (src/workflows/registry.py)", "YAML parser + DAG validator (src/workflows/parser.py)", "Sequential DAG executor (src/workflows/executor.py)", "POST/GET /workflows + POST /workflows/{id}/run", "Streamlit: workflow list + manual trigger UI"] },
                { sprint: "Sprint 2", label: "Multi-Agent", color: "#a855f7", items: ["Agent role definitions + system prompts (src/agent/roles.py)", "Multi-Agent Router / supervisor (src/agent/router.py)", "Parallel step execution (asyncio.gather)", "Inter-step context passing + Jinja2 templating", "Token usage ledger + cost tracking per step"] },
                { sprint: "Sprint 3", label: "Triggers & Events", color: "#ec4899", items: ["Redis pub/sub event bus (src/events/bus.py)", "APScheduler cron integration (src/scheduler/)", "GitHub webhook → event bus routing", "human_checkpoints table + API + Streamlit notifications", "SSE streaming endpoint for live workflow progress"] },
                { sprint: "Sprint 4", label: "Polish & MCP", color: "#14b8a6", items: ["4 new MCP tools: create/run/status/list workflows", "Streamlit DAG visualizer (Gantt-style step timeline)", "Built-in templates: PR review, nightly audit, security scan", "n8n integration guide + sample n8n template", "AGENTS.md update + full API docs"] },
              ].map((s) => (
                <div key={s.sprint} style={{ background: "#1a1a2e", border: `1px solid ${s.color}44`, borderRadius: 12, padding: 18 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
                    <div style={{ background: s.color, borderRadius: 6, padding: "4px 10px", fontSize: 11, fontWeight: 700 }}>{s.sprint}</div>
                    <div style={{ fontSize: 15, fontWeight: 700, color: "#e2e8f0" }}>{s.label}</div>
                  </div>
                  {s.items.map((item) => (
                    <div key={item} style={{ fontSize: 12, color: "#94a3b8", marginBottom: 8, display: "flex", gap: 8, alignItems: "flex-start", lineHeight: 1.4 }}>
                      <span style={{ color: s.color, flexShrink: 0 }}>→</span>{item}
                    </div>
                  ))}
                </div>
              ))}
            </div>

            <div style={{ background: "#1a1a2e", border: "1px solid #1e293b", borderRadius: 12, padding: 20 }}>
              <div style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: 1.5, marginBottom: 14 }}>New Files to Create</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                {newComponents.map((c) => (
                  <div key={c.path} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                    <span style={{ color: "#6366f1", fontFamily: "monospace", fontSize: 11, flexShrink: 0 }}>{c.path}</span>
                    <span style={{ color: "#64748b", fontSize: 11 }}>— {c.desc}</span>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ marginTop: 16, background: "#0f1a10", border: "1px solid #22c55e33", borderRadius: 12, padding: 18 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#22c55e", marginBottom: 10 }}>The Defensible Moat</div>
              <p style={{ fontSize: 13, color: "#94a3b8", margin: 0, lineHeight: 1.6 }}>
                Every step in a NexusCode workflow gets <strong style={{ color: "#e2e8f0" }}>semantic codebase context automatically injected</strong> — vector search,
                symbol graphs, AST structure, call chains, import graphs, git history. No generic orchestrator
                (n8n, Temporal, Airflow, Prefect) can do this. This is the unique, defensible thing that makes
                NexusCode workflows fundamentally more intelligent than anything else on the market.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
