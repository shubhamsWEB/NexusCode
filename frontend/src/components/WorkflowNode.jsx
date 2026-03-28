import { memo } from "react";
import { Handle, Position } from "@xyflow/react";

const ROLE_COLOR = {
  pm_agent:       "#8b5cf6",
  designer_agent: "#ec4899",
  coder:          "#3b82f6",
  reviewer:       "#f59e0b",
  qa_agent:       "#10b981",
  devops_agent:   "#06b6d4",
  tester:         "#14b8a6",
  planner:        "#6366f1",
  searcher:       "#a855f7",
  supervisor:     "#f97316",
};

const TYPE_ICON = {
  agent:            "🤖",
  human_checkpoint: "🧑‍💻",
  integration:      "🔌",
  router:           "🔀",
};

const STATUS_STYLE = {
  completed: { border: "#22c55e", glow: "#22c55e40", dot: "#22c55e" },
  running:   { border: "#3b82f6", glow: "#3b82f660", dot: "#3b82f6" },
  failed:    { border: "#ef4444", glow: "#ef444440", dot: "#ef4444" },
  pending:   { border: "#334155", glow: "transparent", dot: "#475569" },
};

function WorkflowNode({ data, selected }) {
  const color = ROLE_COLOR[data.role] || "#6366f1";
  const icon = TYPE_ICON[data.type] || "🤖";
  const st = STATUS_STYLE[data.status] || STATUS_STYLE.pending;

  return (
    <div
      style={{
        background: "#1a1a2e",
        border: `2px solid ${selected ? color : st.border}`,
        borderRadius: 10,
        padding: "10px 14px",
        minWidth: 160,
        maxWidth: 220,
        boxShadow: selected ? `0 0 12px ${color}60` : `0 0 8px ${st.glow}`,
        cursor: "grab",
        userSelect: "none",
      }}
    >
      <Handle type="target" position={Position.Top} />

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        <span style={{ fontSize: 14 }}>{icon}</span>
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            textTransform: "uppercase",
            letterSpacing: 1,
            color,
            background: `${color}22`,
            padding: "2px 6px",
            borderRadius: 4,
          }}
        >
          {data.role || data.type || "step"}
        </span>
        {/* Live status dot */}
        <div
          style={{
            marginLeft: "auto",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: st.dot,
            flexShrink: 0,
            ...(data.status === "running" && {
              animation: "pulse 1.2s ease-in-out infinite",
            }),
          }}
        />
      </div>

      {/* Step ID */}
      <div
        style={{
          fontSize: 13,
          fontWeight: 700,
          color: "#e2e8f0",
          marginBottom: 4,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {data.label || data.step_id || "unnamed"}
      </div>

      {/* Task preview */}
      {data.task && (
        <div
          style={{
            fontSize: 11,
            color: "#64748b",
            lineHeight: 1.4,
            overflow: "hidden",
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
          }}
        >
          {data.task}
        </div>
      )}

      {/* Token usage (shown after execution) */}
      {data.tokens_used > 0 && (
        <div
          style={{
            marginTop: 6,
            fontSize: 10,
            color: "#475569",
            fontFamily: "monospace",
          }}
        >
          {data.tokens_used.toLocaleString()} tokens
        </div>
      )}

      <Handle type="source" position={Position.Bottom} />

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </div>
  );
}

export default memo(WorkflowNode);
