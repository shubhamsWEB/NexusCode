const ROLES = [
  "pm_agent", "designer_agent", "coder", "reviewer",
  "qa_agent", "devops_agent", "tester", "planner", "searcher", "supervisor",
];

const TYPES = ["agent", "human_checkpoint", "integration", "router"];

export default function StepForm({ node, onChange, onDelete }) {
  if (!node) {
    return (
      <div style={{ padding: 16, color: "#475569", fontSize: 13 }}>
        Click a node to edit its properties.
      </div>
    );
  }

  const d = node.data;

  const update = (key, value) => onChange({ ...d, [key]: value });

  const field = (label, key, type = "text", opts = null) => (
    <div style={{ marginBottom: 14 }}>
      <label style={{ fontSize: 11, color: "#64748b", display: "block", marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.8 }}>
        {label}
      </label>
      {opts ? (
        <select
          value={d[key] || ""}
          onChange={(e) => update(key, e.target.value)}
          style={inputStyle}
        >
          <option value="">— none —</option>
          {opts.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : type === "textarea" ? (
        <textarea
          value={d[key] || ""}
          onChange={(e) => update(key, e.target.value)}
          rows={3}
          style={{ ...inputStyle, resize: "vertical" }}
        />
      ) : (
        <input
          type={type}
          value={d[key] || ""}
          onChange={(e) => update(key, e.target.value)}
          style={inputStyle}
        />
      )}
    </div>
  );

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: "#e2e8f0" }}>Edit Step</span>
        <button onClick={onDelete} style={btnDanger}>Delete</button>
      </div>

      {field("Step ID", "step_id")}
      {field("Type", "type", "text", TYPES)}
      {field("Agent Role", "role", "text", ROLES)}
      {field("Task Description", "task", "textarea")}
      {field("Max Iterations", "max_iterations", "number")}
      {field("Timeout (hours)", "timeout_hours", "number")}

      {d.type === "integration" && field("Integration Action", "action")}
      {d.type === "human_checkpoint" && field("On Timeout", "on_timeout", "text", ["skip", "fail", "escalate"])}
    </div>
  );
}

const inputStyle = {
  width: "100%",
  background: "#0f0f1a",
  border: "1px solid #334155",
  borderRadius: 6,
  padding: "6px 10px",
  color: "#e2e8f0",
  fontSize: 12,
  outline: "none",
  fontFamily: "inherit",
};

const btnDanger = {
  background: "#450a0a",
  border: "1px solid #ef4444",
  color: "#ef4444",
  borderRadius: 6,
  padding: "4px 10px",
  cursor: "pointer",
  fontSize: 11,
};
