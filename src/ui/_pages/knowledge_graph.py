"""
Knowledge Graph — interactive per-repo visualization.

Layout
------
  Top bar : repo selector · view toggle · max-nodes slider · build button
  Stats   : colored chips — node count, per-edge-type counts, last built
  Canvas  : pyvis vis.js graph (700 px, full width) with injected legend overlay
  Below   : Node Explorer (searchable, sortable) | Edge Breakdown table
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from src.ui.helpers import api_get, api_post, time_ago

# ── Color constants ────────────────────────────────────────────────────────────

_LANG_COLORS: dict[str, str] = {
    "python":     "#3572A5",
    "typescript": "#3178C6",
    "tsx":        "#3178C6",
    "javascript": "#F7DF1E",
    "java":       "#B07219",
    "go":         "#00ADD8",
    "rust":       "#CE422B",
    "ruby":       "#CC342D",
    "cpp":        "#F34B7D",
    "c":          "#555555",
    "markdown":   "#083fa1",
    "json":       "#40a070",
}
_LANG_DEFAULT = "#6E7681"

_KIND_COLORS: dict[str, str] = {
    "class":    "#3FB950",   # green
    "function": "#79C0FF",   # blue
    "method":   "#D2A8FF",   # purple
}
_KIND_DEFAULT = "#8B949E"

# edge_type → (hex color, dash setting, line width)
_EDGE_STYLES: dict[str, tuple[str, Any, float]] = {
    "imports":  ("#FF7B72", [6, 4], 1.5),  # red dashed
    "defines":  ("#4ECDC4", False,  1.2),  # teal solid thin
    "contains": ("#79C0FF", [2, 4], 1.2),  # blue dotted
    "calls":    ("#FFA657", False,  2.2),  # orange solid thick
}

_NODE_SHAPES: dict[str, str] = {
    "file":     "box",
    "class":    "diamond",
    "function": "dot",
    "method":   "triangle",
}


# ── Tooltip HTML builders ──────────────────────────────────────────────────────

def _file_tooltip(node: dict, degree: int) -> str:
    lang  = node.get("language", "unknown")
    color = _LANG_COLORS.get(lang, _LANG_DEFAULT)
    path  = node["id"]
    label = node["label"]
    return textwrap.dedent(f"""
        <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;
             padding:12px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
             sans-serif;max-width:300px;box-shadow:0 8px 24px rgba(0,0,0,.7)">
          <div style="font-size:13px;font-weight:600;color:#e6edf3;margin-bottom:10px;
               display:flex;align-items:center;gap:7px">
            <span style="width:9px;height:9px;border-radius:2px;background:{color};
                 display:inline-block;flex-shrink:0"></span>
            {label}
          </div>
          <table style="font-size:11px;border-collapse:collapse;width:100%">
            <tr><td style="color:#7d8590;padding:2px 8px 2px 0;white-space:nowrap">Path</td>
                <td style="color:#cdd9e5;font-family:monospace;word-break:break-all">{path}</td></tr>
            <tr><td style="color:#7d8590;padding:2px 8px 2px 0">Language</td>
                <td style="color:{color};font-weight:600;text-transform:capitalize">{lang}</td></tr>
            <tr><td style="color:#7d8590;padding:2px 8px 2px 0">Connections</td>
                <td style="color:#f0883e;font-weight:700">{degree}</td></tr>
          </table>
        </div>
    """).strip()


def _symbol_tooltip(node: dict, degree: int) -> str:
    kind  = node.get("kind", "function")
    color = _KIND_COLORS.get(kind, _KIND_DEFAULT)
    icons = {"class": "◆", "function": "ƒ", "method": "⚡"}
    icon  = icons.get(kind, "•")
    fp    = node.get("file_path", "")
    return textwrap.dedent(f"""
        <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;
             padding:12px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
             sans-serif;max-width:320px;box-shadow:0 8px 24px rgba(0,0,0,.7)">
          <div style="font-size:13px;font-weight:600;color:#e6edf3;margin-bottom:10px;
               display:flex;align-items:center;gap:7px">
            <span style="color:{color};font-size:15px">{icon}</span> {node["label"]}
          </div>
          <table style="font-size:11px;border-collapse:collapse;width:100%">
            <tr><td style="color:#7d8590;padding:2px 8px 2px 0;white-space:nowrap">Full name</td>
                <td style="color:#cdd9e5;font-family:monospace;word-break:break-all">{node["id"]}</td></tr>
            <tr><td style="color:#7d8590;padding:2px 8px 2px 0">Kind</td>
                <td style="color:{color};font-weight:600;text-transform:capitalize">{kind}</td></tr>
            {"" if not fp else f'<tr><td style="color:#7d8590;padding:2px 8px 2px 0">File</td><td style="color:#58a6ff;font-family:monospace;word-break:break-all">{fp}</td></tr>'}
            <tr><td style="color:#7d8590;padding:2px 8px 2px 0">Connections</td>
                <td style="color:#f0883e;font-weight:700">{degree}</td></tr>
          </table>
        </div>
    """).strip()


# ── Pyvis graph builder ────────────────────────────────────────────────────────

_VIS_OPTIONS = """{
  "nodes": {
    "borderWidth": 1,
    "borderWidthSelected": 3,
    "shadow": {"enabled": true, "color": "rgba(0,0,0,0.55)", "size": 10, "x": 2, "y": 3},
    "font": {
      "size": 11,
      "face": "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace",
      "color": "#e6edf3",
      "strokeWidth": 3,
      "strokeColor": "#0d1117"
    },
    "scaling": {"min": 12, "max": 38, "label": {"enabled": true, "min": 9, "max": 13}}
  },
  "edges": {
    "selectionWidth": 3,
    "hoverWidth": 2.5,
    "shadow": {"enabled": false},
    "smooth": {"enabled": true, "type": "dynamic"},
    "font": {"size": 9, "color": "#8b949e", "strokeWidth": 0, "align": "middle"},
    "arrows": {"to": {"enabled": true, "scaleFactor": 0.45, "type": "arrow"}}
  },
  "physics": {
    "enabled": true,
    "solver": "barnesHut",
    "barnesHut": {
      "gravitationalConstant": -14000,
      "centralGravity": 0.25,
      "springLength": 170,
      "springConstant": 0.04,
      "damping": 0.12,
      "avoidOverlap": 0.5
    },
    "stabilization": {"enabled": true, "iterations": 280, "updateInterval": 20, "fit": true},
    "maxVelocity": 50,
    "minVelocity": 0.08,
    "timestep": 0.5
  },
  "interaction": {
    "hover": true,
    "hoverConnectedEdges": true,
    "navigationButtons": false,
    "keyboard": {"enabled": true, "bindToWindow": false},
    "tooltipDelay": 80,
    "multiselect": false,
    "dragNodes": true,
    "dragView": true,
    "zoomView": true,
    "zoomSpeed": 0.8
  },
  "layout": {"randomSeed": 42}
}"""


def _build_graph_html(
    nodes: list[dict],
    edges: list[dict],
    view_param: str,
    height: int,
) -> str:
    """Build a fully styled pyvis HTML page with injected legend + CSS."""
    from pyvis.network import Network

    net = Network(
        height=f"{height}px",
        width="100%",
        bgcolor="#0d1117",
        font_color="#e6edf3",
        notebook=False,
        directed=True,
    )
    net.set_options(_VIS_OPTIONS)

    # Degree map for sizing
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1

    tooltip_data: dict[str, str] = {}

    for node in nodes:
        nid     = node["id"]
        ntype   = node["type"]
        kind    = node.get("kind", ntype)  # for symbols
        deg     = degree.get(nid, 1)
        size    = max(12, min(38, 12 + deg * 4))

        if ntype == "file":
            color = _LANG_COLORS.get(node.get("language", ""), _LANG_DEFAULT)
            shape = "box"
            tooltip = _file_tooltip(node, deg)
        else:
            color = _KIND_COLORS.get(kind, _KIND_DEFAULT)
            shape = _NODE_SHAPES.get(kind, "dot")
            tooltip = _symbol_tooltip(node, deg)

        tooltip_data[nid] = tooltip

        net.add_node(
            nid,
            label=node["label"],
            color={
                "background": color,
                "border": _darken(color, 0.6),
                "highlight": {"background": _lighten(color, 1.3), "border": color},
                "hover":     {"background": _lighten(color, 1.2), "border": color},
            },
            size=size,
            shape=shape,
            font={"color": "#e6edf3", "strokeColor": "#0d1117", "strokeWidth": 3},
        )

    for edge in edges:
        etype = edge.get("type", "defines")
        color, dashes, width = _EDGE_STYLES.get(etype, ("#8B949E", False, 1.5))
        net.add_edge(
            edge["source"],
            edge["target"],
            color={"color": color, "highlight": color, "hover": color, "opacity": 0.8},
            width=width,
            dashes=dashes,
        )

    html = net.generate_html(local=True)

    # ── Inject custom CSS ──────────────────────────────────────────────────────
    custom_css = """
<style>
* { box-sizing: border-box; }
body { margin: 0; padding: 0; background: #0d1117; overflow: hidden; }
#mynetwork {
  border-radius: 12px;
  border: 1px solid rgba(48,54,61,0.8);
  background: #0d1117 !important;
}
.vis-network:focus { outline: none; }
/* Custom tooltip: remove vis default box, our nodes carry HTML */
.vis-tooltip {
  background: transparent !important;
  border: none !important;
  padding: 0 !important;
  box-shadow: none !important;
  max-width: 340px !important;
  pointer-events: none;
}
/* Navigation buttons */
.vis-navigation .vis-button { filter: brightness(0.7) invert(0.8); }
.vis-navigation .vis-button:hover { filter: brightness(0.9) invert(0.8); }
</style>
"""
    # ── Build legend HTML ──────────────────────────────────────────────────────
    if view_param == "files":
        legend_items = "".join(
            f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:5px">'
            f'<span style="width:10px;height:10px;border-radius:2px;background:{c};'
            f'display:inline-block;flex-shrink:0"></span>'
            f'<span style="font-size:11px;color:#cdd9e5">{lang}</span></div>'
            for lang, c in _LANG_COLORS.items()
            if any(n.get("language") == lang for n in nodes)
        )
    else:
        legend_items = "".join(
            f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:5px">'
            f'<span style="width:10px;height:10px;border-radius:50%;background:{c};'
            f'display:inline-block;flex-shrink:0"></span>'
            f'<span style="font-size:11px;color:#cdd9e5;text-transform:capitalize">{k}</span></div>'
            for k, c in _KIND_COLORS.items()
        )

    edge_legend = "".join(
        f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:4px">'
        f'<svg width="22" height="6" style="flex-shrink:0">'
        f'<line x1="0" y1="3" x2="22" y2="3" stroke="{c}" stroke-width="{w}" '
        f'{"stroke-dasharray=\'4,3\'" if d else ""}/></svg>'
        f'<span style="font-size:11px;color:#8b949e;text-transform:capitalize">{et}</span></div>'
        for et, (c, d, w) in _EDGE_STYLES.items()
        if any(e.get("type") == et for e in edges)
    )

    legend_html = f"""
<div id="kg-legend" style="
  position:absolute;top:14px;left:14px;z-index:300;
  background:rgba(13,17,23,0.92);backdrop-filter:blur(10px);
  border:1px solid rgba(48,54,61,0.9);border-radius:12px;
  padding:14px 16px;min-width:130px;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:10px;font-weight:700;color:#6e7681;
       text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">
    {'Files' if view_param == 'files' else 'Symbols'}
  </div>
  {legend_items}
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(48,54,61,.7)">
    <div style="font-size:10px;font-weight:700;color:#6e7681;
         text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Edges</div>
    {edge_legend}
  </div>
</div>
"""

    # ── Physics status overlay ─────────────────────────────────────────────────
    controls_html = """
<div id="kg-controls" style="
  position:absolute;bottom:14px;right:14px;z-index:300;display:flex;gap:8px">
  <button onclick="togglePhysics()" id="phys-btn" style="
    background:rgba(13,17,23,0.88);backdrop-filter:blur(8px);
    border:1px solid rgba(48,54,61,0.8);border-radius:8px;
    color:#8b949e;font-size:11px;padding:5px 12px;cursor:pointer;
    font-family:-apple-system,sans-serif;transition:color .15s">
    ⏸ Freeze
  </button>
  <button onclick="fitNetwork()" style="
    background:rgba(13,17,23,0.88);backdrop-filter:blur(8px);
    border:1px solid rgba(48,54,61,0.8);border-radius:8px;
    color:#8b949e;font-size:11px;padding:5px 12px;cursor:pointer;
    font-family:-apple-system,sans-serif">
    ⊞ Fit
  </button>
</div>
<script>
var physicsOn = true;
function togglePhysics() {
  physicsOn = !physicsOn;
  network.setOptions({physics: {enabled: physicsOn}});
  document.getElementById('phys-btn').textContent = physicsOn ? '⏸ Freeze' : '▶ Unfreeze';
}
function fitNetwork() { network.fit({animation: {duration: 500, easingFunction: 'easeInOutQuad'}}); }
</script>
"""

    # ── Stabilizing indicator ──────────────────────────────────────────────────
    stab_html = """
<div id="kg-loading" style="
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:400;
  background:rgba(13,17,23,0.88);backdrop-filter:blur(8px);
  border:1px solid rgba(48,54,61,0.8);border-radius:10px;
  padding:12px 22px;color:#8b949e;font-size:12px;
  font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  display:flex;align-items:center;gap:10px;pointer-events:none">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3fb950"
       stroke-width="2" style="animation:spin 1s linear infinite">
    <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83
             M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
  </svg>
  Stabilizing layout…
</div>
<style>
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
</style>
<script>
// Hide loading overlay once network stabilizes
document.addEventListener('DOMContentLoaded', function() {
  var attempts = 0;
  var check = setInterval(function() {
    attempts++;
    if (typeof network !== 'undefined') {
      network.on('stabilized', function() {
        var el = document.getElementById('kg-loading');
        if (el) el.style.display = 'none';
      });
      clearInterval(check);
    }
    if (attempts > 50) clearInterval(check);
  }, 100);
  // Fallback: always hide after 4s
  setTimeout(function() {
    var el = document.getElementById('kg-loading');
    if (el) el.style.display = 'none';
  }, 4000);
});
</script>
"""

    # ── Custom tooltip JS ──────────────────────────────────────────────────────
    # vis.js v9 uses textContent (not innerHTML) for node title attrs, so we
    # skip title= on nodes and drive tooltips via network.on('hoverNode').
    # json.dumps escapes < > & → unescape after so the browser JS sees real HTML.
    _tooltip_json = json.dumps(tooltip_data)
    _tooltip_json = (
        _tooltip_json
        .replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u0026", "&")
    )
    tooltip_html = f"""
<div id="custom-tooltip" style="
  position:fixed;z-index:9999;pointer-events:none;display:none;
  max-width:340px"></div>
<script>
(function(){{
  var tooltipData = {_tooltip_json};
  var _el = document.getElementById('custom-tooltip');
  var _ready = false;
  function _initTooltips() {{
    if (_ready || typeof network === 'undefined') return;
    _ready = true;
    network.on('hoverNode', function(params) {{
      var html = tooltipData[String(params.node)];
      if (!html) return;
      _el.innerHTML = html;
      _el.style.display = 'block';
      var canvas = document.getElementById('mynetwork');
      var rect = canvas.getBoundingClientRect();
      var x = rect.left + params.pointer.DOM.x + 18;
      var y = rect.top + params.pointer.DOM.y - 10;
      _el.style.left = Math.min(x, window.innerWidth - 360) + 'px';
      _el.style.top = Math.max(10, Math.min(y, window.innerHeight - 220)) + 'px';
    }});
    network.on('blurNode', function() {{ _el.style.display = 'none'; }});
    network.on('click', function() {{ _el.style.display = 'none'; }});
    network.on('dragStart', function() {{ _el.style.display = 'none'; }});
  }}
  var _att = 0;
  var _chk = setInterval(function() {{
    _att++;
    _initTooltips();
    if (_ready || _att > 60) clearInterval(_chk);
  }}, 100);
}})();
</script>
"""

    # ── Inject everything ──────────────────────────────────────────────────────
    html = html.replace("</head>", custom_css + "</head>")
    html = html.replace("<body>", f"<body>{legend_html}{stab_html}")
    html = html.replace("</body>", tooltip_html + controls_html + "</body>")

    return html


# ── Color math helpers ─────────────────────────────────────────────────────────

def _darken(hex_color: str, factor: float = 0.7) -> str:
    r, g, b = _parse_hex(hex_color)
    return _to_hex(int(r * factor), int(g * factor), int(b * factor))


def _lighten(hex_color: str, factor: float = 1.3) -> str:
    r, g, b = _parse_hex(hex_color)
    return _to_hex(min(255, int(r * factor)), min(255, int(g * factor)), min(255, int(b * factor)))


def _parse_hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


# ── Stats chips ────────────────────────────────────────────────────────────────

def _stats_html(nodes: list, edges: list, built_at: str | None) -> str:
    edge_counts: dict[str, int] = {}
    for e in edges:
        edge_counts[e["type"]] = edge_counts.get(e["type"], 0) + 1

    node_counts: dict[str, int] = {}
    for n in nodes:
        k = n.get("kind") if n["type"] == "symbol" else "file"
        node_counts[k] = node_counts.get(k, 0) + 1

    def chip(color: str, label: str, value: int | str, bg_opacity: str = "18") -> str:
        r, g, b = _parse_hex(color)
        return (
            f'<span style="display:inline-flex;align-items:center;gap:6px;'
            f'padding:5px 12px;border-radius:20px;font-size:12px;font-weight:500;'
            f'background:rgba({r},{g},{b},0.{bg_opacity});'
            f'border:1px solid rgba({r},{g},{b},0.35);color:#e6edf3;margin:2px 3px">'
            f'<span style="width:7px;height:7px;border-radius:50%;'
            f'background:{color};display:inline-block"></span>'
            f'{label} <b style="color:{color}">{value}</b></span>'
        )

    chips = ""

    # Node chips
    for kind, cnt in sorted(node_counts.items()):
        c = _LANG_DEFAULT if kind == "file" else _KIND_COLORS.get(kind, _KIND_DEFAULT)
        chips += chip(c, kind, cnt)

    # Separator
    chips += '<span style="color:#30363d;margin:0 4px;font-size:18px">|</span>'

    # Edge chips
    for etype, cnt in sorted(edge_counts.items()):
        c, *_ = _EDGE_STYLES.get(etype, ("#8b949e", False, 1.5))
        chips += chip(c, etype, cnt)

    # Last built
    ago = time_ago(built_at) if built_at else "never"
    chips += (
        f'<span style="display:inline-flex;align-items:center;gap:5px;'
        f'padding:5px 12px;border-radius:20px;font-size:12px;'
        f'color:#6e7681;border:1px solid #21262d;margin:2px 3px 2px 8px">'
        f'🕐 built {ago}</span>'
    )

    return (
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
        f'padding:8px 2px 4px;gap:0">{chips}</div>'
    )


# ── Node explorer below the graph ─────────────────────────────────────────────

def _render_node_explorer(nodes: list, edges: list) -> None:
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1

    search = st.text_input(
        "🔍 Search nodes",
        placeholder="type to filter by name or path…",
        key="kg_node_search",
        label_visibility="collapsed",
    )

    # Sort by degree desc
    sorted_nodes = sorted(nodes, key=lambda n: degree.get(n["id"], 0), reverse=True)

    if search:
        q = search.lower()
        sorted_nodes = [n for n in sorted_nodes if q in n["id"].lower()]

    if not sorted_nodes:
        st.caption("No nodes match your search.")
        return

    rows = []
    for n in sorted_nodes[:60]:
        ntype = n["type"]
        kind  = n.get("kind", "") if ntype == "symbol" else ""
        tag   = kind or "file"
        color = (
            _LANG_COLORS.get(n.get("language", ""), _LANG_DEFAULT)
            if ntype == "file"
            else _KIND_COLORS.get(kind, _KIND_DEFAULT)
        )
        badge = (
            f'<span style="display:inline-block;padding:1px 7px;border-radius:10px;'
            f'font-size:10px;font-weight:600;text-transform:capitalize;'
            f'background:{color}22;color:{color};border:1px solid {color}44">'
            f'{tag}</span>'
        )
        deg = degree.get(n["id"], 0)
        rows.append(
            f'<tr style="border-bottom:1px solid #21262d">'
            f'<td style="padding:7px 10px 7px 4px;color:#cdd9e5;font-family:monospace;'
            f'font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
            f'{n["id"]}</td>'
            f'<td style="padding:7px 10px;white-space:nowrap">{badge}</td>'
            f'<td style="padding:7px 6px;color:#f0883e;font-weight:700;font-size:12px;'
            f'text-align:right">{deg}</td>'
            f'</tr>'
        )

    table = (
        '<table style="width:100%;border-collapse:collapse;font-family:-apple-system,sans-serif">'
        '<thead><tr style="border-bottom:2px solid #30363d">'
        '<th style="padding:6px 10px 6px 4px;text-align:left;font-size:11px;'
        'color:#6e7681;font-weight:600;text-transform:uppercase;letter-spacing:.06em">Node</th>'
        '<th style="padding:6px 10px;text-align:left;font-size:11px;'
        'color:#6e7681;font-weight:600;text-transform:uppercase;letter-spacing:.06em">Type</th>'
        '<th style="padding:6px 6px;text-align:right;font-size:11px;'
        'color:#6e7681;font-weight:600;text-transform:uppercase;letter-spacing:.06em">Edges</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )
    if len(sorted_nodes) > 60:
        table += f'<p style="font-size:11px;color:#6e7681;margin-top:6px">Showing 60 of {len(sorted_nodes)}. Use search to narrow.</p>'

    st.markdown(table, unsafe_allow_html=True)


# ── Edge breakdown ─────────────────────────────────────────────────────────────

def _render_edge_breakdown(edges: list) -> None:
    counts: dict[str, int] = {}
    for e in edges:
        counts[e["type"]] = counts.get(e["type"], 0) + 1

    if not counts:
        st.caption("No edges.")
        return

    total = sum(counts.values())
    for etype, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        color, *_ = _EDGE_STYLES.get(etype, ("#8b949e",))
        pct = cnt / total
        bar = (
            f'<div style="margin-bottom:12px">'
            f'<div style="display:flex;justify-content:space-between;margin-bottom:4px">'
            f'<span style="font-size:12px;color:#cdd9e5;text-transform:capitalize;font-weight:500">'
            f'{etype}</span>'
            f'<span style="font-size:12px;color:{color};font-weight:700">{cnt}</span></div>'
            f'<div style="background:#21262d;border-radius:4px;height:6px;overflow:hidden">'
            f'<div style="background:{color};width:{pct*100:.1f}%;height:6px;'
            f'border-radius:4px;transition:width .4s"></div></div></div>'
        )
        st.markdown(bar, unsafe_allow_html=True)

    st.markdown(
        f'<p style="font-size:11px;color:#6e7681;margin-top:4px">Total: {total} edges</p>',
        unsafe_allow_html=True,
    )


# ── Main page ──────────────────────────────────────────────────────────────────

def render() -> None:
    # Page-level CSS tweaks
    st.markdown(
        """<style>
        /* Tighten the main content padding */
        section[data-testid="stMain"] > div { padding-top: 1rem; }
        /* Remove excess margin on radio */
        div[data-testid="stRadio"] { margin-bottom: 0; }
        /* Compact metric labels */
        [data-testid="stMetricLabel"] { font-size: 0.72rem; }
        </style>""",
        unsafe_allow_html=True,
    )

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("## 🕸️ Knowledge Graph")

    # ── Controls row ──────────────────────────────────────────────────────────
    repos_data, repos_err = api_get("/repos", timeout=10)
    if repos_err:
        st.error(f"Cannot load repositories: {repos_err}")
        return
    if not repos_data:
        st.info("No repositories indexed yet. Register and index a repo first.")
        return

    repo_options = [f"{r['owner']}/{r['name']}" for r in repos_data]

    c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
    with c1:
        selected_repo = st.selectbox(
            "Repository", repo_options, key="kg_repo_select", label_visibility="collapsed"
        )
    with c2:
        view = st.radio(
            "View",
            ["📁 Files", "🔷 Symbols", "🌐 All"],
            horizontal=True,
            key="kg_view",
            label_visibility="collapsed",
        )
    with c3:
        max_nodes = st.slider(
            "Max nodes", 50, 500, 200, step=50, key="kg_max_nodes", label_visibility="collapsed",
            help="Nodes cap (highest-degree nodes are kept first)"
        )
    with c4:
        build_clicked = st.button(
            "🔨 Build", key="kg_build_btn", use_container_width=True, type="primary"
        )

    if not selected_repo:
        return

    owner, name = selected_repo.split("/", 1)
    view_param = {"📁 Files": "files", "🔷 Symbols": "symbols", "🌐 All": "all"}[view]

    # ── Build on click ────────────────────────────────────────────────────────
    if build_clicked:
        with st.spinner(f"Building graph for **{selected_repo}**…"):
            build_data, build_err = api_post(
                f"/graph/{owner}/{name}/build", json={}, timeout=60
            )
        if build_err:
            st.error(f"Build failed: {build_err}")
        elif build_data:
            n = build_data.get("nodes", 0)
            e = build_data.get("edges", 0)
            ms = build_data.get("elapsed_ms", 0)
            st.success(f"Built — **{n} nodes**, **{e} edges** in {ms:.0f} ms", icon="✅")
            st.rerun()

    # ── Fetch graph data ──────────────────────────────────────────────────────
    graph_data, graph_err = api_get(
        f"/graph/{owner}/{name}?view={view_param}&max_nodes={max_nodes}",
        timeout=15,
    )

    if graph_err:
        st.error(f"API error: {graph_err}")
        return

    # ── Stats bar ─────────────────────────────────────────────────────────────
    if graph_data and graph_data.get("nodes"):
        nodes   = graph_data["nodes"]
        edges   = graph_data["edges"]
        built_at = graph_data.get("built_at")
        st.markdown(_stats_html(nodes, edges, built_at), unsafe_allow_html=True)
    else:
        st.markdown(
            '<div style="padding:12px 0;color:#6e7681;font-size:13px">'
            "No graph data yet — click <b style='color:#e6edf3'>🔨 Build Graph</b> to generate."
            "</div>",
            unsafe_allow_html=True,
        )
        return

    if not nodes:
        st.info("Graph is empty for this view + repo. Try **All** view or rebuild.")
        return

    # ── Graph canvas ──────────────────────────────────────────────────────────
    graph_height = 680
    graph_html = _build_graph_html(nodes, edges, view_param, graph_height)
    components.html(graph_html, height=graph_height + 10, scrolling=False)

    # ── Detail panels ─────────────────────────────────────────────────────────
    st.markdown(
        '<div style="height:8px"></div>',
        unsafe_allow_html=True,
    )
    left, right = st.columns([3, 2])

    with left:
        st.markdown(
            '<p style="font-size:12px;font-weight:700;color:#6e7681;'
            'text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">'
            f'Node Explorer — {len(nodes)} nodes</p>',
            unsafe_allow_html=True,
        )
        _render_node_explorer(nodes, edges)

    with right:
        st.markdown(
            '<p style="font-size:12px;font-weight:700;color:#6e7681;'
            'text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px">'
            "Edge Breakdown</p>",
            unsafe_allow_html=True,
        )
        _render_edge_breakdown(edges)

        # Keyboard tips
        st.markdown(
            '<div style="margin-top:16px;padding:10px 14px;border-radius:8px;'
            'background:#161b22;border:1px solid #21262d">'
            '<p style="font-size:10px;font-weight:700;color:#6e7681;'
            'text-transform:uppercase;letter-spacing:.07em;margin:0 0 6px">Tips</p>'
            '<p style="font-size:11px;color:#8b949e;margin:0;line-height:1.6">'
            "🖱 Drag nodes to rearrange<br>"
            "⊞ <b style='color:#cdd9e5'>Fit</b> button resets zoom<br>"
            "⏸ <b style='color:#cdd9e5'>Freeze</b> locks the layout<br>"
            "🔍 Hover nodes for details<br>"
            "⌨ Arrow keys pan the canvas"
            "</p></div>",
            unsafe_allow_html=True,
        )
