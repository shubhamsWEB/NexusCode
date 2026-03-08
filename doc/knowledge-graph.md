# Knowledge Graph

NexusCode builds an interactive knowledge graph of your codebase — visualizing how files
import each other, which symbols are defined where, and which functions call others.

---

## What the Graph Shows

The knowledge graph has two types of nodes and four types of edges:

**Nodes:**
| Type | Color | Description |
|------|-------|-------------|
| `file` | by language | A source file in the repository |
| `symbol` | by kind | A function, class, or variable |

**File node colors by language:**
- Python: Blue (`#4B8BBE`)
- TypeScript/JavaScript: Yellow (`#F7DF1E`)
- Go: Cyan (`#00ADD8`)
- Rust: Orange (`#FF4500`)
- Java: Red-orange (`#FF6B35`)

**Symbol node colors by kind:**
- `function`: Green (`#28A745`)
- `class`: Purple (`#6F42C1`)
- `method`: Teal (`#17A2B8`)
- `variable/constant`: Grey (`#6C757D`)

**Edges:**
| Type | Direction | Meaning |
|------|-----------|---------|
| `imports` | A → B | File A imports File B |
| `defines` | file → symbol | File defines this symbol |
| `contains` | parent → child | Class contains method |
| `calls` | A → B | Function A calls Function B |

---

## Building the Graph

### Via Dashboard

1. Open **🕸️ Knowledge Graph** in the sidebar
2. Select a repository
3. Click **Build Graph** (runs synchronously, ~30 seconds for large repos)
4. Use the **View** dropdown (Files, Symbols, All)
5. Interact: drag nodes, hover for details, click to inspect

### Via API

```bash
# Build the graph (sync, up to 30 seconds)
curl -X POST http://localhost:8000/graph/myorg/myrepo/build

# Get graph data
curl "http://localhost:8000/graph/myorg/myrepo?view=all&max_nodes=200"
```

**Response:**
```json
{
  "nodes": [
    {
      "id": "src/auth/service.py",
      "label": "auth/service.py",
      "type": "file",
      "color": "#4B8BBE",
      "size": 15
    },
    {
      "id": "sym:AuthService.authenticate",
      "label": "authenticate",
      "type": "symbol",
      "color": "#28A745",
      "size": 10
    }
  ],
  "edges": [
    {
      "source": "src/auth/service.py",
      "target": "src/models/user.py",
      "type": "imports",
      "confidence": 1.0
    }
  ],
  "stats": {"node_count": 45, "edge_count": 120},
  "built_at": "2026-03-08T14:30:00Z"
}
```

---

## View Modes

| Mode | URL param | Shows |
|------|-----------|-------|
| Files only | `view=files` | File-to-file import relationships |
| Symbols only | `view=symbols` | Symbol definitions and calls |
| All | `view=all` (default) | Everything |

**Limit node count** for large repos:
```bash
GET /graph/myorg/myrepo?view=files&max_nodes=100
```

---

## Use Cases

**Finding orphaned files:**
Files with no edges (no imports, not imported) may be dead code.

**Understanding blast radius:**
Click a symbol to see what imports it — helps estimate change impact.

**Tracing import chains:**
Follow `imports` edges from an entry point to understand the dependency tree.

**Identifying hotspot files:**
Large nodes (high degree) are central to the codebase — changes there affect everything.

---

## Graph API Integration

The graph data format is compatible with D3.js, vis.js, and other graph libraries.
You can build custom visualizations by consuming `GET /graph/{owner}/{name}` from your
own frontend.

**Example D3.js integration:**
```javascript
const { nodes, edges } = await fetch('/graph/myorg/myrepo').then(r => r.json());
// Use nodes[].id, nodes[].color, edges[].source, edges[].target
// with d3.forceSimulation() for a force-directed layout
```
