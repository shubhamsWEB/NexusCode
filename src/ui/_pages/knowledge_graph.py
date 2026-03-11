"""
Knowledge Graph — interactive per-repo visualization.

Layout
------
  Top bar : repo selector · view toggle · max-nodes slider · build button
  Stats   : colored chips — node count, per-edge-type counts, last built
  Canvas  : pyvis vis.js graph (720 px, full width) with injected overlays
  Below   : Node Explorer (searchable) | Edge Breakdown bar chart
"""

from __future__ import annotations

import json
import textwrap
from collections import defaultdict
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
    "c":          "#888888",
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

# edge_type → (normal_color, dash, line_width)
_EDGE_STYLES: dict[str, tuple[str, Any, float]] = {
    "imports":  ("#FF7B72", [6, 4], 1.5),   # red dashed
    "defines":  ("#4ECDC4", False,  1.2),   # teal solid
    "contains": ("#79C0FF", [2, 4], 1.2),   # blue dotted
    "calls":    ("#FFA657", False,  2.0),   # orange solid thick
    "semantic": ("#C792EA", True,   2.0),   # purple dashed curved
}

_NODE_SHAPES: dict[str, str] = {
    "file":     "box",
    "class":    "diamond",
    "function": "dot",
    "method":   "triangle",
}


# ── Connection map builder ─────────────────────────────────────────────────────

def _build_connection_map(
    nodes: list[dict],
    edges: list[dict],
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, list[str]]]]:
    """
    Build per-node outgoing / incoming adjacency grouped by edge type.

    Returns:
        outgoing  {node_id → {edge_type → [target_label …]}}
        incoming  {node_id → {edge_type → [source_label …]}}
    """
    id_to_label: dict[str, str] = {n["id"]: n["label"] for n in nodes}

    outgoing: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    incoming: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for e in edges:
        src   = e["source"]
        tgt   = e["target"]
        etype = e.get("type", "")
        outgoing[src][etype].append(id_to_label.get(tgt, tgt.split("/")[-1]))
        incoming[tgt][etype].append(id_to_label.get(src, src.split("/")[-1]))

    return dict(outgoing), dict(incoming)


# ── Tooltip helpers ────────────────────────────────────────────────────────────

def _conn_section(
    heading: str,
    items: list[str],
    color: str,
    max_items: int = 6,
) -> str:
    """Return an HTML connection-list block for a tooltip section."""
    if not items:
        return ""
    shown  = items[:max_items]
    extra  = len(items) - max_items
    rows   = "".join(
        f'<div style="padding:1px 0 1px 10px;color:#cdd9e5;font-size:10.5px;'
        f'font-family:ui-monospace,Consolas,monospace;overflow:hidden;'
        f'text-overflow:ellipsis;white-space:nowrap">• {item}</div>'
        for item in shown
    )
    if extra > 0:
        rows += (
            f'<div style="padding:1px 0 1px 10px;color:#6e7681;font-size:10px">'
            f'+ {extra} more…</div>'
        )
    return (
        f'<div style="margin-top:8px">'
        f'<div style="font-size:10px;font-weight:700;color:{color};'
        f'text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px">'
        f'{heading} ({len(items)})</div>'
        f'{rows}</div>'
    )


def _file_tooltip(
    node: dict,
    degree: int,
    outgoing: dict[str, list[str]],
    incoming: dict[str, list[str]],
) -> str:
    lang  = node.get("language", "unknown")
    color = _LANG_COLORS.get(lang, _LANG_DEFAULT)
    path  = node["id"]
    label = node["label"]

    # Connection sections
    imports_out  = _conn_section("imports",     outgoing.get("imports", []),  "#FF7B72")
    imports_in   = _conn_section("imported by", incoming.get("imports", []),  "#FF9E88")
    defines_out  = _conn_section("defines",     outgoing.get("defines", []),  "#4ECDC4")
    contains_out = _conn_section("contains",    outgoing.get("contains", []), "#79C0FF")
    conn_html    = imports_out + imports_in + defines_out + contains_out

    return textwrap.dedent(f"""
        <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;
             padding:12px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
             sans-serif;max-width:290px;box-shadow:0 8px 28px rgba(0,0,0,.75);
             max-height:340px;overflow-y:auto;
             scrollbar-width:thin;scrollbar-color:#30363d transparent">
          <div style="display:flex;align-items:center;gap:7px;margin-bottom:9px">
            <span style="width:9px;height:9px;border-radius:2px;background:{color};
                 flex-shrink:0"></span>
            <span style="font-size:12.5px;font-weight:600;color:#e6edf3;
                 overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{label}</span>
          </div>
          <table style="font-size:11px;border-collapse:collapse;width:100%">
            <tr>
              <td style="color:#7d8590;padding:2px 8px 2px 0;white-space:nowrap;
                   vertical-align:top">Path</td>
              <td style="color:#8b949e;font-family:ui-monospace,monospace;
                   font-size:10px;word-break:break-all">{path}</td>
            </tr>
            <tr>
              <td style="color:#7d8590;padding:2px 8px 2px 0">Language</td>
              <td style="color:{color};font-weight:600;text-transform:capitalize">{lang}</td>
            </tr>
            <tr>
              <td style="color:#7d8590;padding:2px 8px 2px 0">Connections</td>
              <td style="color:#f0883e;font-weight:700">{degree}</td>
            </tr>
          </table>
          {f'<div style="margin-top:9px;padding-top:8px;border-top:1px solid #21262d">{conn_html}</div>' if conn_html.strip() else ''}
        </div>
    """).strip()


def _symbol_tooltip(
    node: dict,
    degree: int,
    outgoing: dict[str, list[str]],
    incoming: dict[str, list[str]],
) -> str:
    kind  = node.get("kind", "function")
    color = _KIND_COLORS.get(kind, _KIND_DEFAULT)
    icons = {"class": "◆", "function": "ƒ", "method": "⚡"}
    icon  = icons.get(kind, "•")
    fp    = node.get("file_path", "")

    # Connection sections
    calls_out   = _conn_section("calls",       outgoing.get("calls", []),    "#FFA657")
    called_by   = _conn_section("called by",   incoming.get("calls", []),    "#FFBF7F")
    contains_out = _conn_section("contains",   outgoing.get("contains", []), "#79C0FF")
    in_class    = _conn_section("inside",      incoming.get("contains", []), "#4ECDC4")
    in_file     = _conn_section("defined in",  incoming.get("defines", []),  "#4ECDC4")
    conn_html   = calls_out + called_by + contains_out + in_class + in_file

    file_row = (
        f'<tr><td style="color:#7d8590;padding:2px 8px 2px 0;white-space:nowrap">File</td>'
        f'<td style="color:#58a6ff;font-family:ui-monospace,monospace;font-size:10px;'
        f'word-break:break-all">{fp}</td></tr>'
        if fp else ""
    )

    return textwrap.dedent(f"""
        <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;
             padding:12px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
             sans-serif;max-width:290px;box-shadow:0 8px 28px rgba(0,0,0,.75);
             max-height:340px;overflow-y:auto;
             scrollbar-width:thin;scrollbar-color:#30363d transparent">
          <div style="display:flex;align-items:center;gap:7px;margin-bottom:9px">
            <span style="color:{color};font-size:15px;flex-shrink:0">{icon}</span>
            <span style="font-size:12.5px;font-weight:600;color:#e6edf3;
                 overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{node['label']}</span>
            <span style="margin-left:auto;font-size:10px;color:#6e7681;
                 text-transform:capitalize;flex-shrink:0">{kind}</span>
          </div>
          <table style="font-size:11px;border-collapse:collapse;width:100%">
            <tr>
              <td style="color:#7d8590;padding:2px 8px 2px 0;white-space:nowrap">Full name</td>
              <td style="color:#8b949e;font-family:ui-monospace,monospace;font-size:10px;
                   word-break:break-all">{node['id']}</td>
            </tr>
            {file_row}
            <tr>
              <td style="color:#7d8590;padding:2px 8px 2px 0">Connections</td>
              <td style="color:#f0883e;font-weight:700">{degree}</td>
            </tr>
          </table>
          {f'<div style="margin-top:9px;padding-top:8px;border-top:1px solid #21262d">{conn_html}</div>' if conn_html.strip() else ''}
        </div>
    """).strip()


# ── Semantic edge tooltip ──────────────────────────────────────────────────────

def _semantic_edge_tooltip(edge: dict) -> str:
    """Build an HTML tooltip for a semantic edge."""
    src = edge.get("source", "")
    tgt = edge.get("target", "")
    relationship = edge.get("relationship", "semantic")
    confidence = edge.get("confidence", 0.0)
    reasoning = edge.get("reasoning", "")

    src_short = src.split(".")[-1]
    tgt_short = tgt.split(".")[-1]

    reasoning_html = (
        f'<div style="margin-top:6px;font-size:10.5px;color:#cdd9e5;font-style:italic;'
        f'line-height:1.5;padding:5px 7px;border-left:2px solid #C792EA44;">{reasoning}</div>'
        if reasoning
        else ""
    )

    return (
        f'<div style="background:#161b22;border:1px solid #C792EA55;border-radius:10px;'
        f'padding:11px 14px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
        f'max-width:280px;box-shadow:0 6px 20px rgba(0,0,0,.7)">'
        f'<div style="font-size:12px;font-weight:600;color:#e6edf3;margin-bottom:7px">'
        f'<span style="color:#C792EA">{src_short}</span>'
        f' <span style="color:#6e7681;font-weight:400">—[</span>'
        f'<span style="color:#f0883e">{relationship}</span>'
        f'<span style="color:#6e7681;font-weight:400">]→</span> '
        f'<span style="color:#C792EA">{tgt_short}</span>'
        f'</div>'
        f'<div style="font-size:10.5px;color:#8b949e">Confidence: '
        f'<b style="color:#3fb950">{confidence:.0%}</b></div>'
        f'{reasoning_html}'
        f'</div>'
    )


# ── vis.js options ─────────────────────────────────────────────────────────────

_VIS_OPTIONS = """{
  "nodes": {
    "borderWidth": 1.5,
    "borderWidthSelected": 3,
    "shadow": {"enabled": true, "color": "rgba(0,0,0,0.6)", "size": 12, "x": 2, "y": 4},
    "font": {
      "size": 11,
      "face": "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace",
      "color": "#e6edf3",
      "strokeWidth": 3,
      "strokeColor": "#0d1117"
    },
    "scaling": {"min": 12, "max": 40, "label": {"enabled": true, "min": 9, "max": 13}}
  },
  "edges": {
    "selectionWidth": 4,
    "hoverWidth": 0,
    "shadow": {"enabled": false},
    "smooth": {"enabled": true, "type": "continuous", "roundness": 0.3},
    "font": {"size": 0, "color": "transparent"},
    "arrows": {"to": {"enabled": true, "scaleFactor": 0.45, "type": "arrow"}}
  },
  "physics": {
    "enabled": true,
    "solver": "forceAtlas2Based",
    "forceAtlas2Based": {
      "gravitationalConstant": -80,
      "centralGravity": 0.008,
      "springLength": 220,
      "springConstant": 0.08,
      "damping": 0.6,
      "avoidOverlap": 1.0
    },
    "stabilization": {
      "enabled": true,
      "iterations": 300,
      "updateInterval": 20,
      "onlyDynamicEdges": false,
      "fit": true
    },
    "maxVelocity": 80,
    "minVelocity": 1.0,
    "timestep": 0.5
  },
  "interaction": {
    "hover": true,
    "hoverConnectedEdges": false,
    "navigationButtons": false,
    "keyboard": {"enabled": true, "bindToWindow": false},
    "tooltipDelay": 9999999,
    "multiselect": false,
    "dragNodes": true,
    "dragView": true,
    "zoomView": true,
    "zoomSpeed": 0.8
  },
  "layout": {"randomSeed": 42, "improvedLayout": true}
}"""


# ── Main graph HTML builder ────────────────────────────────────────────────────

def _build_graph_html(
    nodes: list[dict],
    edges: list[dict],
    view_param: str,
    height: int,
) -> str:
    """Build a fully styled pyvis HTML page with all injected overlays."""
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

    # Degree map for node sizing
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1

    # Connection maps for tooltips
    outgoing_map, incoming_map = _build_connection_map(nodes, edges)

    tooltip_data: dict[str, str] = {}

    for node in nodes:
        nid   = node["id"]
        ntype = node["type"]
        kind  = node.get("kind", ntype)
        deg   = degree.get(nid, 1)
        # Progressive size: sqrt of degree so hubs don't become enormous
        size  = max(14, min(42, 14 + int((deg ** 0.6) * 5)))

        node_out = outgoing_map.get(nid, {})
        node_in  = incoming_map.get(nid, {})

        if ntype == "file":
            color   = _LANG_COLORS.get(node.get("language", ""), _LANG_DEFAULT)
            shape   = "box"
            tooltip = _file_tooltip(node, deg, node_out, node_in)
        else:
            color   = _KIND_COLORS.get(kind, _KIND_DEFAULT)
            shape   = _NODE_SHAPES.get(kind, "dot")
            tooltip = _symbol_tooltip(node, deg, node_out, node_in)

        tooltip_data[nid] = tooltip

        net.add_node(
            nid,
            label=node["label"],
            color={
                "background":  color,
                "border":      _darken(color, 0.55),
                "highlight":   {"background": _lighten(color, 1.4), "border": color},
                "hover":       {"background": _lighten(color, 1.25), "border": color},
            },
            size=size,
            shape=shape,
            font={"color": "#e6edf3", "strokeColor": "#0d1117", "strokeWidth": 3},
        )

    # Track edge restore data keyed by insertion order (pyvis uses sequential ints)
    # Also collect edge tooltips for semantic edges
    edge_restore: dict[int, dict] = {}
    edge_tooltip_data: dict[str, str] = {}  # "src->tgt" → tooltip HTML
    for idx, edge in enumerate(edges):
        etype = edge.get("type", "defines")
        color, dashes, width = _EDGE_STYLES.get(etype, ("#8B949E", False, 1.5))
        hover_color = _lighten(color, 1.5)
        edge_restore[idx] = {"color": color, "width": width, "dashes": dashes}

        # Semantic edges get curved smooth lines
        smooth = (
            {"type": "curvedCW", "roundness": 0.2}
            if etype == "semantic"
            else {"type": "continuous", "roundness": 0.3}
        )

        net.add_edge(
            edge["source"],
            edge["target"],
            color={
                "color":     color,
                "highlight": _lighten(color, 1.4),
                "hover":     hover_color,
                "opacity":   0.72,
                "inherit":   False,
            },
            width=width,
            dashes=dashes,
            smooth=smooth,
        )

        if etype == "semantic":
            tip_key = f"{edge['source']}->{edge['target']}"
            edge_tooltip_data[tip_key] = _semantic_edge_tooltip(edge)

    raw_html = net.generate_html(local=True)

    # ── Page CSS ───────────────────────────────────────────────────────────────
    page_css = """<style>
* { box-sizing: border-box; }
body { margin: 0; padding: 0; background: #0d1117; overflow: hidden; }
#mynetwork {
  border-radius: 0;
  border: none;
  background:
    radial-gradient(ellipse at 20% 20%, rgba(55,80,120,0.08) 0%, transparent 60%),
    radial-gradient(ellipse at 80% 80%, rgba(30,60,90,0.06) 0%, transparent 60%),
    #0d1117 !important;
}
.vis-network:focus { outline: none; }
.vis-tooltip { display: none !important; }
/* Thin themed scrollbar inside the tooltip card */
#kg-tip ::-webkit-scrollbar { width: 4px; }
#kg-tip ::-webkit-scrollbar-track { background: transparent; }
#kg-tip ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 2px; }
#kg-tip ::-webkit-scrollbar-thumb:hover { background: #484f58; }
</style>"""

    # ── Legend ─────────────────────────────────────────────────────────────────
    if view_param == "files":
        legend_items = "".join(
            f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
            f'<span style="width:8px;height:8px;border-radius:2px;background:{c};'
            f'flex-shrink:0"></span>'
            f'<span style="font-size:10.5px;color:#cdd9e5;text-transform:capitalize">{lang}</span></div>'
            for lang, c in _LANG_COLORS.items()
            if any(n.get("language") == lang for n in nodes)
        )
    else:
        legend_items = "".join(
            f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
            f'<span style="width:8px;height:8px;border-radius:50%;background:{c};'
            f'flex-shrink:0"></span>'
            f'<span style="font-size:10.5px;color:#cdd9e5;text-transform:capitalize">{k}</span></div>'
            for k, c in _KIND_COLORS.items()
        )

    _dash_attr = "stroke-dasharray='4,3'"
    edge_legend = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">'
        f'<svg width="20" height="5" style="flex-shrink:0">'
        f'<line x1="0" y1="2.5" x2="20" y2="2.5" stroke="{c}" stroke-width="{w}" '
        f'{_dash_attr if d else ""}/></svg>'
        f'<span style="font-size:10.5px;color:#8b949e;text-transform:capitalize">{et}</span></div>'
        for et, (c, d, w) in _EDGE_STYLES.items()
        if any(e.get("type") == et for e in edges)
    )

    _semantic_badge = (
        '<div style="margin-top:6px;padding:3px 6px;border-radius:5px;'
        'background:#C792EA18;border:1px solid #C792EA44;'
        'font-size:9px;color:#C792EA;font-weight:600;text-align:center">'
        '● Semantic view</div>'
        if view_param == "semantic"
        else ""
    )

    legend_html = f"""
<div id="kg-legend" style="
  position:absolute;top:12px;left:12px;z-index:300;
  background:rgba(13,17,23,0.88);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border:1px solid rgba(48,54,61,0.85);border-radius:10px;
  padding:12px 14px;min-width:118px;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="font-size:9.5px;font-weight:700;color:#6e7681;
       text-transform:uppercase;letter-spacing:.09em;margin-bottom:8px">
    {'Files' if view_param == 'files' else 'Symbols'}
  </div>
  {legend_items}
  <div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(48,54,61,.6)">
    <div style="font-size:9.5px;font-weight:700;color:#6e7681;
         text-transform:uppercase;letter-spacing:.09em;margin-bottom:6px">Edges</div>
    {edge_legend}
  </div>
  {_semantic_badge}
</div>"""

    # ── Controls ───────────────────────────────────────────────────────────────
    controls_html = """
<div id="kg-controls" style="
  position:absolute;bottom:12px;right:12px;z-index:300;display:flex;gap:6px">
  <button onclick="togglePhysics()" id="phys-btn" style="
    background:rgba(13,17,23,0.85);backdrop-filter:blur(8px);
    border:1px solid rgba(48,54,61,0.75);border-radius:7px;
    color:#8b949e;font-size:11px;padding:5px 11px;cursor:pointer;
    font-family:-apple-system,sans-serif;transition:all .15s;line-height:1.4">
    ▶ Unfreeze
  </button>
  <button onclick="fitGraph()" style="
    background:rgba(13,17,23,0.85);backdrop-filter:blur(8px);
    border:1px solid rgba(48,54,61,0.75);border-radius:7px;
    color:#8b949e;font-size:11px;padding:5px 11px;cursor:pointer;
    font-family:-apple-system,sans-serif;line-height:1.4">
    ⊞ Fit
  </button>
</div>
<script>
var physicsOn = false;
function togglePhysics() {
  physicsOn = !physicsOn;
  network.setOptions({physics: {enabled: physicsOn}});
  document.getElementById('phys-btn').textContent = physicsOn ? '⏸ Freeze' : '▶ Unfreeze';
  document.getElementById('phys-btn').style.color = physicsOn ? '#3fb950' : '#8b949e';
}
function fitGraph() {
  network.fit({animation: {duration: 600, easingFunction: 'easeInOutCubic'}});
}
</script>"""

    # ── Loading indicator ──────────────────────────────────────────────────────
    loading_html = """
<div id="kg-loading" style="
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:400;
  background:rgba(13,17,23,0.9);backdrop-filter:blur(10px);
  border:1px solid rgba(48,54,61,0.8);border-radius:10px;
  padding:10px 20px;color:#8b949e;font-size:11.5px;
  font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  display:flex;align-items:center;gap:9px;pointer-events:none;
  transition:opacity .35s">
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3fb950"
       stroke-width="2.5" style="animation:kg-spin 0.9s linear infinite;flex-shrink:0">
    <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83
             M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
  </svg>
  <span id="kg-loading-txt">Laying out graph…</span>
</div>
<style>@keyframes kg-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}</style>
<script>
document.addEventListener('DOMContentLoaded', function() {
  var MAX_WAIT = 8000;
  var t = Date.now();
  var check = setInterval(function() {
    if (typeof network === 'undefined') {
      if (Date.now() - t > MAX_WAIT) {
        clearInterval(check);
        var el = document.getElementById('kg-loading');
        if (el) el.style.display = 'none';
      }
      return;
    }
    clearInterval(check);

    // ── Auto-disable physics after stabilization ──────────────────────────
    network.on('stabilizationProgress', function(p) {
      var pct = Math.round(100 * p.iterations / p.total);
      var el = document.getElementById('kg-loading-txt');
      if (el) el.textContent = 'Laying out… ' + pct + '%';
    });
    network.on('stabilizationIterationsDone', function() {
      network.setOptions({physics: {enabled: false}});
      physicsOn = false;
      var el = document.getElementById('kg-loading');
      if (el) { el.style.opacity = '0'; setTimeout(function(){ el.style.display='none'; }, 350); }
    });
    network.on('stabilized', function() {
      network.setOptions({physics: {enabled: false}});
      physicsOn = false;
      var el = document.getElementById('kg-loading');
      if (el) { el.style.opacity = '0'; setTimeout(function(){ el.style.display='none'; }, 350); }
    });

    // Fallback hide after MAX_WAIT
    setTimeout(function() {
      var el = document.getElementById('kg-loading');
      if (el) el.style.display = 'none';
    }, MAX_WAIT);
  }, 80);
});
</script>"""

    # ── Edge hover highlight JS ────────────────────────────────────────────────
    # On hoverNode: connected edges → bright white, others → near-invisible
    # On blurNode: restore all edges from captured snapshot
    edge_hover_js = """
<script>
(function() {
  var _snap = null;  // snapshot before highlight
  var _hovering = false;

  function _waitNetwork(cb) {
    var att = 0;
    var t = setInterval(function() {
      if (typeof network !== 'undefined') { clearInterval(t); cb(); }
      if (++att > 120) clearInterval(t);
    }, 80);
  }

  function _snapshot() {
    if (_snap) return;
    _snap = {};
    network.body.data.edges.get().forEach(function(e) {
      _snap[e.id] = {
        color: e.color ? JSON.parse(JSON.stringify(e.color)) : e.color,
        width: e.width
      };
    });
  }

  function _restore() {
    if (!_snap) return;
    var upd = [];
    Object.keys(_snap).forEach(function(id) {
      var o = _snap[id];
      upd.push({id: id, color: o.color, width: o.width});
    });
    network.body.data.edges.update(upd);
    _snap = null;
  }

  _waitNetwork(function() {
    network.on('hoverNode', function(p) {
      if (_hovering) _restore();  // clean up previous hover first
      _hovering = true;
      _snapshot();
      var nid = p.node;
      var conn = new Set(network.getConnectedEdges(nid).map(String));
      var upd = [];
      network.body.data.edges.get().forEach(function(e) {
        if (conn.has(String(e.id))) {
          // Bright accent on connected edges
          upd.push({id: e.id,
            color: {color:'#f0f6fc', highlight:'#f0f6fc', hover:'#f0f6fc', opacity:1, inherit:false},
            width: 3.0
          });
        } else {
          // Dim unrelated edges significantly
          upd.push({id: e.id,
            color: {color:'#1c2128', opacity:0.07, inherit:false},
            width: 0.3
          });
        }
      });
      network.body.data.edges.update(upd);
    });

    network.on('blurNode', function() {
      _hovering = false;
      _restore();
    });

    // Also restore on canvas click (not on a node)
    network.on('click', function(p) {
      if (!p.nodes || p.nodes.length === 0) {
        _hovering = false;
        _restore();
      }
    });
  });
})();
</script>"""

    # ── Custom tooltip JS ──────────────────────────────────────────────────────
    def _clean_tooltip_json(data: dict) -> str:
        s = json.dumps(data)
        return (
            s.replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&")
            .replace("\\'", "'")
        )

    _tooltip_json = _clean_tooltip_json(tooltip_data)
    _edge_tip_json = _clean_tooltip_json(edge_tooltip_data)

    tooltip_js = f"""
<div id="kg-tip" style="
  position:fixed;z-index:9999;pointer-events:auto;display:none;
  max-width:295px;transition:opacity .12s"></div>
<script>
(function(){{
  var TIP      = {_tooltip_json};
  var EDGE_TIP = {_edge_tip_json};
  var el   = document.getElementById('kg-tip');
  var _rdy = false;
  var _hideTimer = null;   // debounce handle for delayed hide

  /* ---- hide helpers -------------------------------------------------- */
  function _scheduleHide() {{
    _clearHide();
    _hideTimer = setTimeout(function() {{
      el.style.opacity = '0';
      // wait for fade then actually remove from layout
      setTimeout(function() {{
        if (el.style.opacity === '0') el.style.display = 'none';
      }}, 130);
    }}, 350);          // 350 ms grace period — long enough to move mouse to tooltip
  }}

  function _clearHide() {{
    if (_hideTimer) {{ clearTimeout(_hideTimer); _hideTimer = null; }}
  }}

  function _forceHide() {{
    _clearHide();
    el.style.opacity = '0';
    el.style.display = 'none';
  }}

  function _showAt(html, mx, my) {{
    el.innerHTML = html;
    el.style.display = 'block';
    el.style.opacity = '0';
    requestAnimationFrame(function() {{ el.style.opacity = '1'; }});
    var c  = document.getElementById('mynetwork').getBoundingClientRect();
    var tx = c.left + mx + 22;
    var ty = c.top  + my - 16;
    if (tx + 300 > window.innerWidth)  tx = c.left + mx - 312;
    if (ty + 260 > window.innerHeight) ty = window.innerHeight - 268;
    ty = Math.max(8, ty);
    el.style.left = tx + 'px';
    el.style.top  = ty + 'px';
  }}

  /* ---- tooltip element events ---------------------------------------- */
  // Mouse entered the tooltip itself → cancel the pending hide
  el.addEventListener('mouseenter', function() {{ _clearHide(); }});
  // Mouse left the tooltip → hide immediately
  el.addEventListener('mouseleave', function() {{ _forceHide(); }});

  /* ---- vis.js network events ----------------------------------------- */
  function _init() {{
    if (_rdy || typeof network === 'undefined') return;
    _rdy = true;

    network.on('hoverNode', function(p) {{
      _clearHide();   // cancel any pending hide from a previous blurNode
      var html = TIP[String(p.node)];
      if (!html) return;
      _showAt(html, p.pointer.DOM.x, p.pointer.DOM.y);
    }});

    // Semantic edge tooltips on hoverEdge
    network.on('hoverEdge', function(p) {{
      var edgeId = p.edge;
      var edgeData = network.body.data.edges.get(edgeId);
      if (!edgeData) return;
      var key = edgeData.from + '->' + edgeData.to;
      var html = EDGE_TIP[key];
      if (!html) return;
      _clearHide();
      _showAt(html, p.pointer.DOM.x, p.pointer.DOM.y);
    }});

    // blurNode fires the moment cursor leaves the node hitbox — use the
    // grace-period timer so the user can move the cursor onto the tooltip.
    network.on('blurNode',  function() {{ _scheduleHide(); }});
    network.on('blurEdge',  function() {{ _scheduleHide(); }});

    // Hard-hide on explicit user actions
    network.on('click',     function() {{ _forceHide(); }});
    network.on('dragStart', function() {{ _forceHide(); }});
    network.on('zoom',      function() {{ _forceHide(); }});
  }}

  var att = 0;
  var chk = setInterval(function() {{
    att++;
    _init();
    if (_rdy || att > 80) clearInterval(chk);
  }}, 100);
}})();
</script>"""

    # ── Assemble final HTML ────────────────────────────────────────────────────
    raw_html = raw_html.replace("</head>", page_css + "</head>")
    raw_html = raw_html.replace("<body>", f"<body>{legend_html}{loading_html}")
    raw_html = raw_html.replace("</body>", tooltip_js + edge_hover_js + controls_html + "</body>")

    return raw_html


# ── Color math helpers ─────────────────────────────────────────────────────────

def _darken(hex_color: str, factor: float = 0.7) -> str:
    r, g, b = _parse_hex(hex_color)
    return _to_hex(int(r * factor), int(g * factor), int(b * factor))


def _lighten(hex_color: str, factor: float = 1.3) -> str:
    r, g, b = _parse_hex(hex_color)
    return _to_hex(
        min(255, int(r * factor)),
        min(255, int(g * factor)),
        min(255, int(b * factor)),
    )


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

    def chip(color: str, label: str, value: int | str) -> str:
        r, g, b = _parse_hex(color)
        return (
            f'<span style="display:inline-flex;align-items:center;gap:5px;'
            f'padding:4px 10px;border-radius:20px;font-size:11.5px;font-weight:500;'
            f'background:rgba({r},{g},{b},0.12);'
            f'border:1px solid rgba({r},{g},{b},0.3);color:#cdd9e5;margin:2px 3px">'
            f'<span style="width:6px;height:6px;border-radius:50%;'
            f'background:{color};display:inline-block"></span>'
            f'{label} <b style="color:{color}">{value}</b></span>'
        )

    chips = ""
    for kind, cnt in sorted(node_counts.items()):
        c = _LANG_DEFAULT if kind == "file" else _KIND_COLORS.get(kind, _KIND_DEFAULT)
        chips += chip(c, kind, cnt)

    chips += '<span style="color:#21262d;margin:0 3px;font-size:16px;align-self:center">│</span>'

    for etype, cnt in sorted(edge_counts.items()):
        c, *_ = _EDGE_STYLES.get(etype, ("#8b949e", False, 1.5))
        chips += chip(c, etype, cnt)

    ago = time_ago(built_at) if built_at else "never"
    chips += (
        f'<span style="display:inline-flex;align-items:center;gap:4px;'
        f'padding:4px 10px;border-radius:20px;font-size:11.5px;'
        f'color:#6e7681;border:1px solid #21262d;margin:2px 3px 2px 7px">'
        f'🕐 built {ago}</span>'
    )

    return (
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
        f'padding:6px 2px 4px;gap:0">{chips}</div>'
    )


# ── Node explorer ──────────────────────────────────────────────────────────────

def _render_node_explorer(nodes: list, edges: list) -> None:
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1

    search = st.text_input(
        "🔍 Search nodes",
        placeholder="filter by name or path…",
        key="kg_node_search",
        label_visibility="collapsed",
    )

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
            f'background:{color}22;color:{color};border:1px solid {color}44">{tag}</span>'
        )
        deg = degree.get(n["id"], 0)
        rows.append(
            f'<tr style="border-bottom:1px solid #21262d">'
            f'<td style="padding:6px 8px 6px 4px;color:#cdd9e5;font-family:ui-monospace,monospace;'
            f'font-size:11px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
            f'{n["id"]}</td>'
            f'<td style="padding:6px 8px;white-space:nowrap">{badge}</td>'
            f'<td style="padding:6px 4px;color:#f0883e;font-weight:700;font-size:12px;'
            f'text-align:right">{deg}</td>'
            f'</tr>'
        )

    table = (
        '<table style="width:100%;border-collapse:collapse;font-family:-apple-system,sans-serif">'
        '<thead><tr style="border-bottom:2px solid #30363d">'
        '<th style="padding:5px 8px 5px 4px;text-align:left;font-size:10.5px;'
        'color:#6e7681;font-weight:700;text-transform:uppercase;letter-spacing:.07em">Node</th>'
        '<th style="padding:5px 8px;text-align:left;font-size:10.5px;'
        'color:#6e7681;font-weight:700;text-transform:uppercase;letter-spacing:.07em">Type</th>'
        '<th style="padding:5px 4px;text-align:right;font-size:10.5px;'
        'color:#6e7681;font-weight:700;text-transform:uppercase;letter-spacing:.07em">Edges</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )
    if len(sorted_nodes) > 60:
        table += (
            f'<p style="font-size:10.5px;color:#6e7681;margin-top:5px">'
            f'Showing 60 of {len(sorted_nodes)}. Use search to narrow.</p>'
        )

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
            f'<div style="margin-bottom:10px">'
            f'<div style="display:flex;justify-content:space-between;margin-bottom:3px">'
            f'<span style="font-size:11.5px;color:#cdd9e5;text-transform:capitalize;'
            f'font-weight:500">{etype}</span>'
            f'<span style="font-size:11.5px;color:{color};font-weight:700">{cnt}</span></div>'
            f'<div style="background:#21262d;border-radius:4px;height:5px;overflow:hidden">'
            f'<div style="background:{color};width:{pct*100:.1f}%;height:5px;'
            f'border-radius:4px;transition:width .5s ease"></div></div></div>'
        )
        st.markdown(bar, unsafe_allow_html=True)

    st.markdown(
        f'<p style="font-size:10.5px;color:#6e7681;margin-top:2px">Total: {total} edges</p>',
        unsafe_allow_html=True,
    )


# ── Main page ──────────────────────────────────────────────────────────────────

def render() -> None:
    st.markdown(
        """<style>
        section[data-testid="stMain"] > div { padding-top: 0.75rem; }
        div[data-testid="stRadio"] { margin-bottom: 0; }
        [data-testid="stMetricLabel"] { font-size: 0.72rem; }
        /* Remove white border/bg from the components iframe */
        iframe { border-radius: 12px !important; }
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
            "Repository", repo_options, key="kg_repo_select",
            label_visibility="collapsed",
        )
    with c2:
        view = st.radio(
            "View",
            ["📁 Files", "🔷 Symbols", "🌐 All", "🔮 Semantic"],
            horizontal=True,
            key="kg_view",
            label_visibility="collapsed",
        )
    with c3:
        max_nodes = st.slider(
            "Max nodes", 50, 500, 200, step=50,
            key="kg_max_nodes",
            label_visibility="collapsed",
            help="Nodes cap — highest-degree nodes are kept first",
        )
    with c4:
        build_clicked = st.button(
            "🔨 Build", key="kg_build_btn",
            use_container_width=True, type="primary",
        )

    if not selected_repo:
        return

    owner, name = selected_repo.split("/", 1)
    view_param = {
        "📁 Files": "files",
        "🔷 Symbols": "symbols",
        "🌐 All": "all",
        "🔮 Semantic": "semantic",
    }[view]

    # ── Semantic enrichment status banner ────────────────────────────────────
    if view_param in ("semantic", "all"):
        sem_status, _ = api_get(f"/graph/{owner}/{name}/semantic?limit=0", timeout=5)
        sem_total = (sem_status or {}).get("total", 0)
        if sem_total:
            sem_col, btn_col = st.columns([5, 1])
            with sem_col:
                st.info(
                    f"Semantic graph: **{sem_total}** architectural relationships indexed.",
                    icon="ℹ️",
                )
            with btn_col:
                enrich_clicked = st.button(
                    "🔄 Re-enrich", key="kg_enrich_btn", use_container_width=True
                )
            if enrich_clicked:
                with st.spinner("Running semantic enrichment…"):
                    enrich_data, enrich_err = api_post(
                        f"/graph/{owner}/{name}/enrich", json={}, timeout=120
                    )
                if enrich_err:
                    st.error(f"Enrichment failed: {enrich_err}")
                elif enrich_data:
                    st.success(
                        f"Enriched — **{enrich_data.get('edges_inserted', 0)} edges** "
                        f"from {enrich_data.get('symbols_processed', 0)} symbols "
                        f"in {enrich_data.get('elapsed_ms', 0):.0f} ms",
                        icon="✅",
                    )
                    st.rerun()
        else:
            enrich_col, _ = st.columns([5, 1])
            with enrich_col:
                st.warning(
                    "No semantic edges yet. Click **🔄 Enrich** to extract architectural relationships.",
                    icon="⚠️",
                )
            with _:
                if st.button("🔄 Enrich", key="kg_enrich_btn_empty", use_container_width=True):
                    with st.spinner("Running semantic enrichment…"):
                        enrich_data, enrich_err = api_post(
                            f"/graph/{owner}/{name}/enrich", json={}, timeout=120
                        )
                    if enrich_err:
                        st.error(f"Enrichment failed: {enrich_err}")
                    elif enrich_data:
                        st.success(
                            f"Enriched — **{enrich_data.get('edges_inserted', 0)} edges** inserted.",
                            icon="✅",
                        )
                        st.rerun()

    # ── Build on click ────────────────────────────────────────────────────────
    if build_clicked:
        with st.spinner(f"Building graph for **{selected_repo}**…"):
            build_data, build_err = api_post(
                f"/graph/{owner}/{name}/build", json={}, timeout=3600
            )
        if build_err:
            st.error(f"Build failed: {build_err}")
        elif build_data:
            n  = build_data.get("nodes", 0)
            e  = build_data.get("edges", 0)
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
        nodes    = graph_data["nodes"]
        edges    = graph_data["edges"]
        built_at = graph_data.get("built_at")
        st.markdown(_stats_html(nodes, edges, built_at), unsafe_allow_html=True)
    else:
        st.markdown(
            '<div style="padding:10px 0;color:#6e7681;font-size:13px">'
            "No graph data yet — click <b style='color:#e6edf3'>🔨 Build</b> to generate."
            "</div>",
            unsafe_allow_html=True,
        )
        return

    if not nodes:
        st.info("Graph is empty for this view + repo. Try **All** view or rebuild.")
        return

    # ── Graph canvas ──────────────────────────────────────────────────────────
    graph_height = 700
    graph_html   = _build_graph_html(nodes, edges, view_param, graph_height)
    components.html(graph_html, height=graph_height + 2, scrolling=False)

    # ── Detail panels ─────────────────────────────────────────────────────────
    st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
    left, right = st.columns([3, 2])

    with left:
        st.markdown(
            '<p style="font-size:11.5px;font-weight:700;color:#6e7681;'
            'text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">'
            f'Node Explorer — {len(nodes)} nodes</p>',
            unsafe_allow_html=True,
        )
        _render_node_explorer(nodes, edges)

    with right:
        st.markdown(
            '<p style="font-size:11.5px;font-weight:700;color:#6e7681;'
            'text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px">'
            "Edge Breakdown</p>",
            unsafe_allow_html=True,
        )
        _render_edge_breakdown(edges)

        # Tips card
        st.markdown(
            '<div style="margin-top:14px;padding:10px 13px;border-radius:8px;'
            'background:#161b22;border:1px solid #21262d">'
            '<p style="font-size:10px;font-weight:700;color:#6e7681;'
            'text-transform:uppercase;letter-spacing:.07em;margin:0 0 6px">Tips</p>'
            '<p style="font-size:11px;color:#8b949e;margin:0;line-height:1.7">'
            "🖱 Hover node → see connections + tooltip<br>"
            "🖱 Drag nodes to rearrange freely<br>"
            "⊞ <b style='color:#cdd9e5'>Fit</b> — reset zoom<br>"
            "▶ <b style='color:#cdd9e5'>Unfreeze</b> — re-run layout<br>"
            "⌨ Arrow keys pan the canvas"
            "</p></div>",
            unsafe_allow_html=True,
        )
