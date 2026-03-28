import { useState, useCallback, useRef, useEffect } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  Panel,
  MarkerType,
} from "@xyflow/react";
import jsyaml from "js-yaml";
import WorkflowNode from "./components/WorkflowNode.jsx";
import StepForm from "./components/StepForm.jsx";
import YamlPanel from "./components/YamlPanel.jsx";

// ── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "/workflows";

const STEP_PALETTE = [
  { type: "agent", role: "pm_agent",       label: "PM Agent",       color: "#8b5cf6" },
  { type: "agent", role: "designer_agent", label: "Designer",       color: "#ec4899" },
  { type: "agent", role: "coder",          label: "Coder",          color: "#3b82f6" },
  { type: "agent", role: "reviewer",       label: "Reviewer",       color: "#f59e0b" },
  { type: "agent", role: "qa_agent",       label: "QA Agent",       color: "#10b981" },
  { type: "agent", role: "devops_agent",   label: "DevOps",         color: "#06b6d4" },
  { type: "agent", role: "planner",        label: "Planner",        color: "#6366f1" },
  { type: "agent", role: "searcher",       label: "Searcher",       color: "#a855f7" },
  { type: "human_checkpoint", role: null,  label: "Human Review",   color: "#fbbf24" },
  { type: "integration", role: null,       label: "Integration",    color: "#34d399" },
];

const NODE_TYPES = { workflow: WorkflowNode };

let _nodeId = 1;
const newId = () => `step_${_nodeId++}`;

// ── Graph → YAML ──────────────────────────────────────────────────────────────

function graphToYaml(name, nodes, edges) {
  const steps = nodes.map((n) => {
    const d = n.data;
    const step = { id: d.step_id || n.id, type: d.type || "agent" };
    if (d.role) step.role = d.role;
    if (d.task) step.task = d.task;
    if (d.max_iterations) step.max_iterations = parseInt(d.max_iterations);
    if (d.timeout_hours) step.timeout_hours = parseInt(d.timeout_hours);
    if (d.action) step.action = d.action;
    if (d.on_timeout) step.on_timeout = d.on_timeout;

    // Build routes from outgoing edges
    const outEdges = edges.filter((e) => e.source === n.id);
    if (outEdges.length > 0) {
      step.routes = outEdges.map((e) => {
        const r = {};
        const targetNode = nodes.find((x) => x.id === e.target);
        if (e.label) r.condition = e.label;
        r.goto = targetNode?.data?.step_id || e.target;
        return r;
      });
    }
    return step;
  });

  const wf = { name: name || "unnamed-workflow", steps };
  return jsyaml.dump(wf, { lineWidth: 100, noRefs: true });
}

// ── YAML → Graph ──────────────────────────────────────────────────────────────

function yamlToGraph(yamlText) {
  const parsed = jsyaml.load(yamlText);
  if (!parsed?.steps) throw new Error("Invalid YAML: missing 'steps'");

  const stepList = parsed.steps;
  const nodeMap = {};

  // Compute layout: nodes with only sequential flow go in a single column.
  // Nodes that are route targets of a previous node get positioned to the side.
  // Pre-compute which steps are ONLY reachable as loop-back targets
  // (their source step comes later in the list) — position them to the left
  const loopBackTargets = new Set();
  stepList.forEach((step, i) => {
    (step.routes || []).forEach((r) => {
      if (!r.goto || r.goto === "END") return;
      const targetIdx = stepList.findIndex((s) => s.id === r.goto);
      if (targetIdx < i) loopBackTargets.add(r.goto); // target is earlier → loop-back
    });
  });

  const nodes = stepList.map((step, i) => {
    const id = `rf_${i}_${step.id || newId()}`;
    nodeMap[step.id] = id;
    // Loop-back nodes offset to the left so the edge arc is visible
    const xOffset = loopBackTargets.has(step.id) ? 80 : 300;
    return {
      id,
      type: "workflow",
      position: { x: xOffset, y: i * 180 + 60 },
      data: {
        step_id: step.id,
        label: step.id,
        type: step.type || "agent",
        role: step.role || null,
        task: step.task || "",
        max_iterations: step.max_iterations || null,
        timeout_hours: step.timeout_hours || null,
        action: step.action || null,
        on_timeout: step.on_timeout || null,
        status: "pending",
        tokens_used: 0,
      },
    };
  });

  // Track which steps already have an explicit incoming edge so we don't
  // add a duplicate implicit sequential edge.
  const hasIncomingEdge = new Set();

  const edges = [];
  stepList.forEach((step, i) => {
    const srcId = nodeMap[step.id];

    if (step.routes) {
      // Conditional routing — draw one labeled edge per route
      step.routes.forEach((r, ri) => {
        if (!r.goto || r.goto === "END") return;
        const tgtId = nodeMap[r.goto];
        if (!tgtId) return;
        hasIncomingEdge.add(r.goto);
        edges.push({
          id: `e_${step.id}_${r.goto}_${ri}`,
          source: srcId,
          target: tgtId,
          label: r.condition || "default",
          markerEnd: { type: MarkerType.ArrowClosed, color: "#6366f1" },
          style: { stroke: r.condition ? "#6366f1" : "#475569", strokeDasharray: r.condition ? "5,3" : "none" },
          labelStyle: { fontSize: 10, fill: "#94a3b8" },
          labelBgStyle: { fill: "#1e293b", fillOpacity: 0.9 },
        });
      });

      // Also draw sequential edge to the NEXT step if it wouldn't already
      // be covered by a route (so the "happy path" is always visible)
      const nextStep = stepList[i + 1];
      if (nextStep) {
        const nextId = nodeMap[nextStep.id];
        const alreadyCovered = step.routes.some(
          (r) => r.goto === nextStep.id && !r.condition
        );
        if (!alreadyCovered && nextId && !hasIncomingEdge.has(nextStep.id)) {
          hasIncomingEdge.add(nextStep.id);
          edges.push({
            id: `e_seq_${step.id}_${nextStep.id}`,
            source: srcId,
            target: nextId,
            markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
            style: { stroke: "#475569" },
          });
        }
      }
    } else if (step.depends_on) {
      // Explicit dependency — draw edge(s) from each declared dependency
      const deps = Array.isArray(step.depends_on) ? step.depends_on : [step.depends_on];
      deps.forEach((dep) => {
        const depId = nodeMap[dep];
        if (!depId) return;
        hasIncomingEdge.add(step.id);
        edges.push({
          id: `e_dep_${dep}_${step.id}`,
          source: depId,
          target: srcId,
          markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
          style: { stroke: "#475569" },
        });
      });
    } else if (i > 0) {
      // No routes, no depends_on — draw implicit sequential edge from previous step
      const prevStep = stepList[i - 1];
      const prevId = nodeMap[prevStep?.id];
      if (prevId && !hasIncomingEdge.has(step.id)) {
        hasIncomingEdge.add(step.id);
        edges.push({
          id: `e_seq_${prevStep.id}_${step.id}`,
          source: prevId,
          target: srcId,
          markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
          style: { stroke: "#475569" },
        });
      }
    }
  });

  return { nodes, edges, name: parsed.name || "" };
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedNode, setSelectedNode] = useState(null);
  const [workflowName, setWorkflowName] = useState("my-workflow");
  const [rightPanel, setRightPanel] = useState("yaml"); // "yaml" | "step"
  const [runId, setRunId] = useState(null);
  const [runStatus, setRunStatus] = useState(null);
  const [statusMsg, setStatusMsg] = useState("");
  const [workflows, setWorkflows] = useState([]);
  const [showSaveModal, setShowSaveModal] = useState(false);
  const sseRef = useRef(null);

  // Load workflow list on mount
  useEffect(() => {
    fetch(`${API_BASE}`)
      .then((r) => r.json())
      .then(setWorkflows)
      .catch(() => {});
  }, []);

  // Live YAML
  const yaml = graphToYaml(workflowName, nodes, edges);

  // Connect nodes
  const onConnect = useCallback(
    (params) =>
      setEdges((eds) =>
        addEdge(
          {
            ...params,
            label: "",
            markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
            style: { stroke: "#475569" },
          },
          eds
        )
      ),
    [setEdges]
  );

  // Drop new node from palette
  const onDragOver = useCallback((e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (e) => {
      e.preventDefault();
      const raw = e.dataTransfer.getData("application/nexus-step");
      if (!raw) return;
      const stepData = JSON.parse(raw);
      const bounds = e.currentTarget.getBoundingClientRect();
      const position = { x: e.clientX - bounds.left - 80, y: e.clientY - bounds.top - 30 };
      const id = newId();
      setNodes((nds) => [
        ...nds,
        {
          id,
          type: "workflow",
          position,
          data: {
            step_id: id,
            label: stepData.label || id,
            type: stepData.type,
            role: stepData.role,
            task: "",
            status: "pending",
            tokens_used: 0,
          },
        },
      ]);
    },
    [setNodes]
  );

  // Select node
  const onNodeClick = useCallback(
    (_, node) => {
      setSelectedNode(node);
      setRightPanel("step");
    },
    []
  );

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  // Update node data from form
  const onNodeDataChange = useCallback(
    (newData) => {
      setNodes((nds) =>
        nds.map((n) =>
          n.id === selectedNode.id ? { ...n, data: { ...n.data, ...newData } } : n
        )
      );
      setSelectedNode((prev) => ({ ...prev, data: { ...prev.data, ...newData } }));
    },
    [selectedNode, setNodes]
  );

  const onDeleteNode = useCallback(() => {
    if (!selectedNode) return;
    setNodes((nds) => nds.filter((n) => n.id !== selectedNode.id));
    setEdges((eds) => eds.filter((e) => e.source !== selectedNode.id && e.target !== selectedNode.id));
    setSelectedNode(null);
    setRightPanel("yaml");
  }, [selectedNode, setNodes, setEdges]);

  // Import YAML
  const onImport = useCallback(
    (yamlText) => {
      try {
        const { nodes: n, edges: e, name } = yamlToGraph(yamlText);
        setNodes(n);
        setEdges(e);
        if (name) setWorkflowName(name);
      } catch (err) {
        alert(`Import failed: ${err.message}`);
      }
    },
    [setNodes, setEdges]
  );

  // Save to API
  const onSave = useCallback(async () => {
    setStatusMsg("Saving...");
    try {
      const resp = await fetch(API_BASE, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: workflowName, yaml_definition: yaml, description: "" }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setWorkflows((prev) => {
        const idx = prev.findIndex((w) => w.name === workflowName);
        return idx >= 0 ? prev.map((w, i) => (i === idx ? data : w)) : [...prev, data];
      });
      setStatusMsg("Saved!");
      setShowSaveModal(false);
      setTimeout(() => setStatusMsg(""), 2000);
    } catch (err) {
      setStatusMsg(`Save failed: ${err.message}`);
    }
  }, [workflowName, yaml]);

  // Run workflow with live SSE updates
  const onRun = useCallback(async () => {
    if (!workflowName) { alert("Set a workflow name first"); return; }

    // Find workflow id by name
    const wf = workflows.find((w) => w.name === workflowName);
    if (!wf) {
      setStatusMsg("Workflow not saved yet — save first");
      return;
    }

    setStatusMsg("Starting run...");
    setRunStatus("pending");

    // Reset all node statuses
    setNodes((nds) => nds.map((n) => ({ ...n, data: { ...n.data, status: "pending", tokens_used: 0 } })));

    try {
      const resp = await fetch(`${API_BASE}/${wf.id}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: {} }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setRunId(data.run_id);
      setStatusMsg(`Run started: ${data.run_id}`);

      // Open SSE stream
      if (sseRef.current) sseRef.current.close();
      const es = new EventSource(`${API_BASE}/runs/${data.run_id}/stream`);
      sseRef.current = es;

      es.onmessage = (e) => {
        const evt = JSON.parse(e.data);
        if (evt.type === "step_started") {
          setNodes((nds) =>
            nds.map((n) =>
              n.data.step_id === evt.step_id ? { ...n, data: { ...n.data, status: "running" } } : n
            )
          );
          setStatusMsg(`Running: ${evt.step_id}`);
        } else if (evt.type === "step_complete") {
          setNodes((nds) =>
            nds.map((n) =>
              n.data.step_id === evt.step_id
                ? { ...n, data: { ...n.data, status: "completed", tokens_used: evt.tokens || 0 } }
                : n
            )
          );
        } else if (evt.type === "step_failed") {
          setNodes((nds) =>
            nds.map((n) =>
              n.data.step_id === evt.step_id ? { ...n, data: { ...n.data, status: "failed" } } : n
            )
          );
        } else if (evt.type === "workflow_complete") {
          setRunStatus("completed");
          setStatusMsg("Run complete");
          es.close();
        } else if (evt.type === "workflow_error") {
          setRunStatus("failed");
          setStatusMsg(`Run failed: ${evt.error}`);
          es.close();
        }
      };
      es.onerror = () => { setStatusMsg("Stream disconnected"); es.close(); };
    } catch (err) {
      setStatusMsg(`Run failed: ${err.message}`);
      setRunStatus("failed");
    }
  }, [workflowName, workflows, setNodes]);

  // Load an existing workflow from API
  const onLoadWorkflow = useCallback(
    async (wf) => {
      try {
        const resp = await fetch(`${API_BASE}/${wf.id}`);
        const data = await resp.json();
        onImport(data.yaml_definition);
        setWorkflowName(data.name);
      } catch (err) {
        alert(`Load failed: ${err.message}`);
      }
    },
    [onImport]
  );

  return (
    <div style={{ display: "flex", height: "100vh", background: "#0f0f1a" }}>
      {/* ── Left sidebar: palette + saved workflows ─────────────────────── */}
      <div
        style={{
          width: 200,
          flexShrink: 0,
          background: "#111827",
          borderRight: "1px solid #1e293b",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Logo */}
        <div style={{ padding: "14px 16px", borderBottom: "1px solid #1e293b" }}>
          <div style={{ fontSize: 13, fontWeight: 700, background: "linear-gradient(135deg,#6366f1,#ec4899)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>
            NexusCode
          </div>
          <div style={{ fontSize: 10, color: "#475569", marginTop: 2 }}>Workflow Builder</div>
        </div>

        {/* Step palette */}
        <div style={{ padding: "10px 12px", borderBottom: "1px solid #1e293b" }}>
          <div style={{ fontSize: 10, color: "#475569", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>Drag to canvas</div>
          {STEP_PALETTE.map((item) => (
            <div
              key={`${item.type}_${item.role}`}
              draggable
              onDragStart={(e) => e.dataTransfer.setData("application/nexus-step", JSON.stringify(item))}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "6px 8px",
                borderRadius: 6,
                cursor: "grab",
                marginBottom: 4,
                background: "#1e293b",
                border: "1px solid #334155",
                transition: "border-color 0.15s",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.borderColor = item.color)}
              onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#334155")}
            >
              <div style={{ width: 8, height: 8, borderRadius: 2, background: item.color, flexShrink: 0 }} />
              <span style={{ fontSize: 11, color: "#cbd5e1" }}>{item.label}</span>
            </div>
          ))}
        </div>

        {/* Saved workflows */}
        <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px" }}>
          <div style={{ fontSize: 10, color: "#475569", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
            Saved workflows
          </div>
          {workflows.length === 0 ? (
            <div style={{ fontSize: 11, color: "#334155" }}>None yet</div>
          ) : (
            workflows.map((wf) => (
              <button
                key={wf.id}
                onClick={() => onLoadWorkflow(wf)}
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  background: "none",
                  border: "1px solid #1e293b",
                  borderRadius: 6,
                  padding: "5px 8px",
                  color: "#94a3b8",
                  fontSize: 11,
                  cursor: "pointer",
                  marginBottom: 4,
                }}
                onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#6366f1")}
                onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#1e293b")}
              >
                {wf.name}
              </button>
            ))
          )}
        </div>
      </div>

      {/* ── Center: ReactFlow canvas ─────────────────────────────────────── */}
      <div style={{ flex: 1, position: "relative" }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          onDrop={onDrop}
          onDragOver={onDragOver}
          nodeTypes={NODE_TYPES}
          fitView
          deleteKeyCode="Delete"
        >
          <Background color="#1e293b" gap={20} />
          <Controls />
          <MiniMap
            nodeColor={(n) => {
              const ROLE_COLOR = { pm_agent: "#8b5cf6", designer_agent: "#ec4899", coder: "#3b82f6", reviewer: "#f59e0b", qa_agent: "#10b981", devops_agent: "#06b6d4" };
              return ROLE_COLOR[n.data?.role] || "#6366f1";
            }}
            maskColor="#0f0f1a88"
          />

          {/* Top toolbar */}
          <Panel position="top-center">
            <div style={{ display: "flex", gap: 8, background: "#111827", border: "1px solid #1e293b", borderRadius: 10, padding: "8px 12px", alignItems: "center" }}>
              <input
                value={workflowName}
                onChange={(e) => setWorkflowName(e.target.value)}
                placeholder="workflow-name"
                style={{ background: "transparent", border: "none", color: "#e2e8f0", fontSize: 13, fontWeight: 600, outline: "none", width: 180 }}
              />
              <div style={{ width: 1, height: 18, background: "#1e293b" }} />
              <button onClick={onSave} style={btnPrimary}>Save</button>
              <button
                onClick={onRun}
                disabled={runStatus === "pending" || runStatus === "running"}
                style={{ ...btnGreen, opacity: (runStatus === "pending" || runStatus === "running") ? 0.5 : 1 }}
              >
                {runStatus === "running" ? "Running…" : "Run"}
              </button>
              {statusMsg && (
                <span style={{ fontSize: 11, color: "#64748b", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {statusMsg}
                </span>
              )}
            </div>
          </Panel>

          {/* Empty canvas hint */}
          {nodes.length === 0 && (
            <Panel position="top-left" style={{ marginTop: 60, marginLeft: 20 }}>
              <div style={{ color: "#334155", fontSize: 13, pointerEvents: "none" }}>
                Drag steps from the left panel to build a workflow
              </div>
            </Panel>
          )}
        </ReactFlow>
      </div>

      {/* ── Right sidebar: step form or yaml panel ───────────────────────── */}
      <div
        style={{
          width: 280,
          flexShrink: 0,
          background: "#111827",
          borderLeft: "1px solid #1e293b",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Tab switcher */}
        <div style={{ display: "flex", borderBottom: "1px solid #1e293b" }}>
          {[["yaml", "YAML"], ["step", "Step"]].map(([id, label]) => (
            <button
              key={id}
              onClick={() => setRightPanel(id)}
              style={{
                flex: 1,
                padding: "10px 0",
                background: rightPanel === id ? "#1e293b" : "transparent",
                border: "none",
                color: rightPanel === id ? "#e2e8f0" : "#475569",
                fontSize: 11,
                fontWeight: 700,
                cursor: "pointer",
                letterSpacing: 0.8,
                textTransform: "uppercase",
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <div style={{ flex: 1, overflowY: "auto" }}>
          {rightPanel === "yaml" ? (
            <YamlPanel
              yaml={yaml}
              onImport={onImport}
              workflowName={workflowName}
              onNameChange={setWorkflowName}
            />
          ) : (
            <StepForm
              node={selectedNode}
              onChange={onNodeDataChange}
              onDelete={onDeleteNode}
            />
          )}
        </div>

        {/* Run status footer */}
        {runId && (
          <div style={{ padding: "8px 12px", borderTop: "1px solid #1e293b", fontSize: 11 }}>
            <div style={{ color: "#475569" }}>Run ID</div>
            <div style={{ color: "#94a3b8", fontFamily: "monospace", fontSize: 10, wordBreak: "break-all" }}>{runId}</div>
            <div style={{ marginTop: 4 }}>
              <span
                style={{
                  padding: "2px 8px",
                  borderRadius: 4,
                  fontSize: 10,
                  fontWeight: 700,
                  background: runStatus === "completed" ? "#14532d" : runStatus === "failed" ? "#450a0a" : "#1e3a5f",
                  color: runStatus === "completed" ? "#22c55e" : runStatus === "failed" ? "#ef4444" : "#60a5fa",
                }}
              >
                {runStatus || "pending"}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const btnPrimary = { background: "#4f46e5", border: "none", color: "white", borderRadius: 6, padding: "5px 12px", cursor: "pointer", fontSize: 12, fontWeight: 600 };
const btnGreen = { background: "#14532d", border: "1px solid #22c55e", color: "#22c55e", borderRadius: 6, padding: "5px 12px", cursor: "pointer", fontSize: 12, fontWeight: 600 };
