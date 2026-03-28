import { useState } from "react";

export default function YamlPanel({ yaml, onImport, workflowName, onNameChange }) {
  const [importText, setImportText] = useState("");
  const [mode, setMode] = useState("export"); // "export" | "import"

  return (
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 14 }}>
        <label style={labelStyle}>Workflow Name</label>
        <input
          value={workflowName}
          onChange={(e) => onNameChange(e.target.value)}
          style={inputStyle}
          placeholder="my-workflow"
        />
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        {["export", "import"].map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            style={{
              ...tabBtn,
              background: mode === m ? "#6366f1" : "#1e293b",
              color: mode === m ? "white" : "#64748b",
            }}
          >
            {m === "export" ? "YAML Preview" : "Import YAML"}
          </button>
        ))}
      </div>

      {mode === "export" ? (
        <>
          <textarea
            readOnly
            value={yaml}
            style={{ ...inputStyle, height: 320, resize: "vertical", fontFamily: "monospace", fontSize: 11, lineHeight: 1.5 }}
          />
          <button
            onClick={() => navigator.clipboard?.writeText(yaml)}
            style={{ ...btnPrimary, marginTop: 8, width: "100%" }}
          >
            Copy YAML
          </button>
          <button
            onClick={() => {
              const blob = new Blob([yaml], { type: "text/yaml" });
              const a = document.createElement("a");
              a.href = URL.createObjectURL(blob);
              a.download = `${workflowName || "workflow"}.yaml`;
              a.click();
            }}
            style={{ ...btnSecondary, marginTop: 6, width: "100%" }}
          >
            Download .yaml
          </button>
        </>
      ) : (
        <>
          <textarea
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            placeholder="Paste YAML here..."
            style={{ ...inputStyle, height: 280, resize: "vertical", fontFamily: "monospace", fontSize: 11, lineHeight: 1.5 }}
          />
          <button
            onClick={() => { onImport(importText); setImportText(""); setMode("export"); }}
            style={{ ...btnPrimary, marginTop: 8, width: "100%" }}
          >
            Import & Visualize
          </button>
        </>
      )}
    </div>
  );
}

const labelStyle = { fontSize: 11, color: "#64748b", display: "block", marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.8 };
const inputStyle = { width: "100%", background: "#0f0f1a", border: "1px solid #334155", borderRadius: 6, padding: "6px 10px", color: "#e2e8f0", fontSize: 12, outline: "none", fontFamily: "inherit", display: "block" };
const btnPrimary = { background: "#4f46e5", border: "none", color: "white", borderRadius: 6, padding: "7px 14px", cursor: "pointer", fontSize: 12, fontWeight: 600 };
const btnSecondary = { background: "#1e293b", border: "1px solid #334155", color: "#94a3b8", borderRadius: 6, padding: "7px 14px", cursor: "pointer", fontSize: 12 };
const tabBtn = { border: "none", borderRadius: 6, padding: "5px 12px", cursor: "pointer", fontSize: 11, fontWeight: 600 };
