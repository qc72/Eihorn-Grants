from __future__ import annotations

import csv
import html
import io
import json
import math
import re
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import networkx as nx
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pyvis.network import Network


REQUIRED_COLUMNS = [
    "Grant_ID",
    "Academic Year",
    "Program",
    "Total Funding",
    "Community Partners",
    "Accepted",
    "PICollege",
    "TeamSize",
    "Domestic/International",
    "Communities",
]

# Add confirmed spelling variants here. The CSV itself is not changed.
ALIASES: dict[str, str] = {
    # "Ithaca Area Waste Water Treatment Facility":
    #     "Ithaca Area Wastewater Treatment Facility",
}


def parse_money(value: Any) -> float:
    """Convert values such as '$10,000 ' to 10000.0."""
    cleaned = re.sub(r"[^0-9.\-]", "", str(value or "").strip())
    if cleaned in {"", "-", ".", "-."}:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_partners(value: Any) -> list[str]:
    """
    Parse Community Partners as a miniature CSV row.

    This preserves organization names containing quoted commas, for example:
    "Wegmans Food Markets, Inc",Other Organization
    """
    text = "" if pd.isna(value) else str(value).strip()
    if not text:
        return []

    parsed = next(csv.reader([text], skipinitialspace=True))
    partners: list[str] = []
    seen: set[str] = set()

    for partner in parsed:
        partner = re.sub(r"\s+", " ", partner.strip())
        partner = ALIASES.get(partner, partner)
        if partner and partner not in seen:
            partners.append(partner)
            seen.add(partner)

    return partners


def _is_url(source: str) -> bool:
    return urlparse(source).scheme in {"http", "https"}


def load_grants(source: str | Path) -> pd.DataFrame:
    """Read the existing CSV without changing its schema or values."""
    source_text = str(source)

    if _is_url(source_text):
        # Google Sheets and other public CSV hosts occasionally return a
        # transient 429/5xx response. Requests does not retry by default, so
        # use bounded exponential backoff for safe GET requests.
        retry_policy = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry_policy,
            pool_connections=4,
            pool_maxsize=4,
        )

        with requests.Session() as session:
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            response = session.get(
                source_text,
                timeout=(10, 30),
                headers={
                    "User-Agent": "grant-network-streamlit-app/1.1",
                    "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )

        response.raise_for_status()

        body_start = response.text.lstrip()[:500].lower()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "<html" in body_start:
            raise ValueError(
                "The remote URL returned an HTML webpage instead of CSV data. "
                "Use the Google Sheets Publish-to-web CSV URL."
            )

        csv_input: str | Path | io.StringIO = io.StringIO(response.text)
    else:
        path = Path(source_text)
        if not path.exists():
            raise FileNotFoundError(
                f"CSV file not found: {path.resolve()}"
            )
        csv_input = path

    df = pd.read_csv(
        csv_input,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    df.columns = df.columns.str.strip()

    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required CSV columns: {missing}")

    # Keep the same visible columns and order. Extra helper columns begin with _.
    df = df.copy().reset_index(drop=True)
    df["_event_id"] = [f"row_{index:05d}" for index in range(len(df))]
    df["_csv_row"] = df.index + 2  # Header is row 1 in spreadsheet software.
    df["_funding"] = df["Total Funding"].map(parse_money)
    df["_partners"] = df["Community Partners"].map(parse_partners)
    df["_partner_count"] = df["_partners"].map(len)

    for column in REQUIRED_COLUMNS:
        df[column] = df[column].astype(str).str.strip()

    return df


def filter_grants(
    df: pd.DataFrame,
    program: str | None = None,
    academic_year: str | None = None,
) -> pd.DataFrame:
    filtered = df
    if program:
        filtered = filtered[filtered["Program"] == program]
    if academic_year:
        filtered = filtered[filtered["Academic Year"] == academic_year]
    return filtered.copy()


def _grant_record(row: pd.Series) -> dict[str, Any]:
    grant_id = row["Grant_ID"] or f"CSV row {int(row['_csv_row'])}"
    return {
        "event_id": row["_event_id"],
        "grant_id": grant_id,
        "csv_row": int(row["_csv_row"]),
        "academic_year": row["Academic Year"],
        "program": row["Program"],
        "funding_display": row["Total Funding"],
        "funding_numeric": float(row["_funding"]),
        "partners": list(row["_partners"]),
        "accepted": row["Accepted"],
        "pi_college": row["PICollege"],
        "team_size": row["TeamSize"],
        "domestic_international": row["Domestic/International"],
        "communities": row["Communities"],
    }


def build_collaboration_graph(
    df: pd.DataFrame,
) -> tuple[nx.Graph, dict[str, dict[str, Any]]]:
    """
    Build the partner projection used in the notebook.

    A node represents a community partner. Two nodes share an edge when they
    appear in the same grant row. Funding is the full award amount associated
    with the grant, matching the original notebook's interpretation.
    """
    graph = nx.Graph(graph_type="receiver_collaboration_projection")
    grant_lookup: dict[str, dict[str, Any]] = {}

    partner_df = df[df["_partner_count"] > 0]

    for _, row in partner_df.iterrows():
        grant = _grant_record(row)
        event_id = grant["event_id"]
        grant_lookup[event_id] = grant

        partners = grant["partners"]
        amount = grant["funding_numeric"]
        partner_count = len(partners)
        program = grant["program"]

        for partner in partners:
            if partner not in graph:
                graph.add_node(
                    partner,
                    grant_count=0,
                    associated_funding=0.0,
                    equal_share_funding=0.0,
                    singleton_grant_count=0,
                    multi_receiver_grant_count=0,
                    grant_event_ids=[],
                    by_program={},
                )

            attrs = graph.nodes[partner]
            attrs["grant_count"] += 1
            attrs["associated_funding"] += amount
            attrs["equal_share_funding"] += amount / partner_count
            attrs["grant_event_ids"].append(event_id)

            if partner_count == 1:
                attrs["singleton_grant_count"] += 1
            else:
                attrs["multi_receiver_grant_count"] += 1

            program_stats = attrs["by_program"].setdefault(
                program,
                {
                    "grant_count": 0,
                    "associated_funding": 0.0,
                    "equal_share_funding": 0.0,
                    "grant_event_ids": [],
                },
            )
            program_stats["grant_count"] += 1
            program_stats["associated_funding"] += amount
            program_stats["equal_share_funding"] += amount / partner_count
            program_stats["grant_event_ids"].append(event_id)

        if partner_count >= 2:
            number_of_pairs = math.comb(partner_count, 2)

            for partner_1, partner_2 in combinations(sorted(partners), 2):
                if not graph.has_edge(partner_1, partner_2):
                    graph.add_edge(
                        partner_1,
                        partner_2,
                        shared_grant_count=0,
                        shared_associated_funding=0.0,
                        pair_fractional_funding=0.0,
                        grant_event_ids=[],
                    )

                attrs = graph[partner_1][partner_2]
                attrs["shared_grant_count"] += 1
                attrs["shared_associated_funding"] += amount
                attrs["pair_fractional_funding"] += amount / number_of_pairs
                attrs["grant_event_ids"].append(event_id)

    return graph, grant_lookup


def make_receiver_view(
    graph: nx.Graph,
    minimum_shared_grants: int = 1,
    largest_component_only: bool = True,
) -> nx.Graph:
    """Apply the edge threshold and optional largest-component filter."""
    view = nx.Graph(**graph.graph)

    # Preserve singleton partners and partners whose edges fall below threshold.
    view.add_nodes_from(graph.nodes(data=True))

    for partner_1, partner_2, attrs in graph.edges(data=True):
        if attrs.get("shared_grant_count", 0) >= minimum_shared_grants:
            view.add_edge(partner_1, partner_2, **attrs)

    if largest_component_only and view.number_of_edges() > 0:
        largest_nodes = max(nx.connected_components(view), key=len)
        view = view.subgraph(largest_nodes).copy()

    return view


def _safe_json(value: Any) -> str:
    # Avoid an accidental closing script tag if a CSV cell contains </script>.
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _dominant_program(
    node_attrs: dict[str, Any],
) -> str:
    by_program = node_attrs.get("by_program", {})
    if not by_program:
        return ""
    return max(
        by_program,
        key=lambda program: (
            by_program[program].get("grant_count", 0),
            by_program[program].get("associated_funding", 0),
            program,
        ),
    )


def build_interactive_html(
    graph: nx.Graph,
    grant_lookup: dict[str, dict[str, Any]],
    focus_partner: str | None = None,
    height: int = 820,
) -> str:
    """Create a PyVis graph with a client-side click details panel."""
    network = Network(
        height=f"{height}px",
        width="100%",
        directed=False,
        notebook=False,
        cdn_resources="in_line",
        bgcolor="#ffffff",
        font_color="#1f2937",
    )

    node_meta: dict[str, dict[str, Any]] = {}
    edge_meta: dict[str, dict[str, Any]] = {}

    for partner, attrs in graph.nodes(data=True):
        grant_count = int(attrs.get("grant_count", 0))
        funding = float(attrs.get("associated_funding", 0))
        collaborator_count = int(graph.degree(partner))
        dominant_program = _dominant_program(attrs)

        tooltip = (
            f"<b>{html.escape(partner)}</b><br>"
            f"Associated grants: {grant_count:,}<br>"
            f"Associated funding: ${funding:,.0f}<br>"
            f"Visible collaborators: {collaborator_count:,}<br>"
            "Click for individual grant details"
        )

        network.add_node(
            partner,
            label=partner,
            title=tooltip,
            value=max(grant_count, 1),
            shape="dot",
            borderWidth=1,
            color={
                "background": "#7db7df",
                "border": "#2f6f9f",
                "highlight": {
                    "background": "#f7c873",
                    "border": "#9a6700",
                },
                "hover": {
                    "background": "#a9d4ef",
                    "border": "#2f6f9f",
                },
            },
        )

        node_meta[partner] = {
            "partner": partner,
            "grant_count": grant_count,
            "associated_funding": funding,
            "equal_share_funding": float(
                attrs.get("equal_share_funding", 0)
            ),
            "singleton_grant_count": int(
                attrs.get("singleton_grant_count", 0)
            ),
            "multi_receiver_grant_count": int(
                attrs.get("multi_receiver_grant_count", 0)
            ),
            "collaborator_count": collaborator_count,
            "dominant_program": dominant_program,
            "grant_event_ids": list(attrs.get("grant_event_ids", [])),
        }

    for index, (partner_1, partner_2, attrs) in enumerate(
        graph.edges(data=True)
    ):
        edge_id = f"edge_{index:06d}"
        shared_count = int(attrs.get("shared_grant_count", 0))
        shared_funding = float(attrs.get("shared_associated_funding", 0))

        tooltip = (
            f"<b>{html.escape(partner_1)}</b> + "
            f"<b>{html.escape(partner_2)}</b><br>"
            f"Shared grants: {shared_count:,}<br>"
            f"Shared associated funding: ${shared_funding:,.0f}<br>"
            "Click for shared grant details"
        )

        network.add_edge(
            partner_1,
            partner_2,
            id=edge_id,
            value=max(shared_count, 1),
            title=tooltip,
            color={
                "color": "rgba(80, 92, 110, 0.34)",
                "highlight": "#d38b19",
                "hover": "#6b7280",
            },
        )

        edge_meta[edge_id] = {
            "partner_1": partner_1,
            "partner_2": partner_2,
            "shared_grant_count": shared_count,
            "shared_associated_funding": shared_funding,
            "pair_fractional_funding": float(
                attrs.get("pair_fractional_funding", 0)
            ),
            "grant_event_ids": list(attrs.get("grant_event_ids", [])),
        }

    network.set_options(
        """
        {
          "interaction": {
            "hover": true,
            "navigationButtons": true,
            "keyboard": true,
            "multiselect": false,
            "tooltipDelay": 120
          },
          "nodes": {
            "font": {
              "size": 13,
              "face": "Arial",
              "strokeWidth": 3,
              "strokeColor": "#ffffff"
            },
            "scaling": {
              "min": 9,
              "max": 42,
              "label": {
                "enabled": true,
                "min": 11,
                "max": 19,
                "drawThreshold": 6
              }
            }
          },
          "edges": {
            "smooth": {
              "enabled": true,
              "type": "dynamic"
            },
            "scaling": {
              "min": 1,
              "max": 10
            },
            "selectionWidth": 2.5,
            "hoverWidth": 1.5
          },
          "physics": {
            "enabled": true,
            "barnesHut": {
              "gravitationalConstant": -7200,
              "centralGravity": 0.22,
              "springLength": 145,
              "springConstant": 0.035,
              "damping": 0.28,
              "avoidOverlap": 0.2
            },
            "stabilization": {
              "enabled": true,
              "iterations": 450,
              "updateInterval": 40
            },
            "minVelocity": 0.75
          }
        }
        """
    )

    base_html = network.generate_html(notebook=False)

    panel_html = """
    <div id="network-layout">
      <div id="mynetwork" class="card-body"></div>
      <aside id="details-panel" aria-live="polite">
        <div class="details-empty">
          <div class="details-icon">↖</div>
          <h2>Grant details</h2>
          <p>Click a partner node to see every associated grant.</p>
          <p>Click an edge to see the grants shared by that pair.</p>
        </div>
      </aside>
    </div>
    """

    base_html = base_html.replace(
        '<div id="mynetwork" class="card-body"></div>',
        panel_html,
        1,
    )

    custom_css = f"""
    <style>
      html, body {{
        margin: 0;
        padding: 0;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system,
          BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #ffffff;
      }}
      .card {{ border: 0 !important; }}
      #network-layout {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) 390px;
        gap: 12px;
        width: 100%;
        align-items: stretch;
      }}
      #mynetwork {{
        width: 100% !important;
        height: {height}px !important;
        float: none !important;
        border: 1px solid #d8dee8 !important;
        border-radius: 12px;
        background: #ffffff;
        overflow: hidden;
      }}
      #details-panel {{
        height: {height}px;
        box-sizing: border-box;
        overflow-y: auto;
        border: 1px solid #d8dee8;
        border-radius: 12px;
        background: #f8fafc;
        padding: 18px;
        color: #172033;
      }}
      #details-panel h2 {{
        margin: 0 0 8px;
        font-size: 21px;
        line-height: 1.25;
        overflow-wrap: anywhere;
      }}
      #details-panel h3 {{
        margin: 22px 0 10px;
        font-size: 15px;
        text-transform: uppercase;
        letter-spacing: .04em;
        color: #536176;
      }}
      .details-empty {{
        display: flex;
        flex-direction: column;
        justify-content: center;
        min-height: 65%;
        text-align: center;
        color: #64748b;
      }}
      .details-empty h2 {{ color: #334155; }}
      .details-icon {{ font-size: 34px; }}
      .detail-subtitle {{
        color: #596579;
        font-size: 13px;
        margin-bottom: 13px;
      }}
      .stats-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        margin: 12px 0 4px;
      }}
      .stat {{
        border: 1px solid #dbe3ee;
        background: #ffffff;
        border-radius: 9px;
        padding: 9px;
      }}
      .stat-value {{
        display: block;
        font-size: 17px;
        font-weight: 700;
        color: #172033;
      }}
      .stat-label {{
        display: block;
        margin-top: 2px;
        color: #64748b;
        font-size: 11px;
        line-height: 1.25;
      }}
      .grant-card {{
        border: 1px solid #dbe3ee;
        border-left: 4px solid #4f8fbd;
        background: #ffffff;
        border-radius: 9px;
        padding: 11px 12px;
        margin: 0 0 10px;
      }}
      .grant-heading {{
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 8px;
      }}
      .grant-id {{
        font-weight: 750;
        font-size: 14px;
        color: #172033;
        overflow-wrap: anywhere;
      }}
      .grant-year {{
        white-space: nowrap;
        color: #506177;
        font-size: 12px;
      }}
      .grant-program {{
        margin: 5px 0 8px;
        font-size: 12px;
        color: #334155;
      }}
      .grant-funding {{
        display: inline-block;
        font-weight: 700;
        color: #14532d;
        background: #dcfce7;
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 12px;
        margin-bottom: 7px;
      }}
      .grant-field {{
        margin-top: 5px;
        font-size: 12px;
        line-height: 1.4;
        color: #475569;
        overflow-wrap: anywhere;
      }}
      .grant-field strong {{ color: #27364b; }}
      .panel-button {{
        float: right;
        border: 1px solid #cbd5e1;
        border-radius: 7px;
        background: #ffffff;
        padding: 4px 8px;
        cursor: pointer;
        color: #475569;
        font-size: 12px;
      }}
      .panel-button:hover {{ background: #eef2f7; }}
      @media (max-width: 930px) {{
        #network-layout {{ grid-template-columns: 1fr; }}
        #mynetwork {{ height: 650px !important; }}
        #details-panel {{ height: 470px; }}
      }}
    </style>
    """
    base_html = base_html.replace("</head>", custom_css + "</head>", 1)

    grant_json = _safe_json(grant_lookup)
    node_json = _safe_json(node_meta)
    edge_json = _safe_json(edge_meta)
    focus_json = _safe_json(focus_partner or "")

    click_script = f"""
    <script>
      const grantData = {grant_json};
      const nodeDetails = {node_json};
      const edgeDetails = {edge_json};
      const initialFocusPartner = {focus_json};
      const detailPanel = document.getElementById("details-panel");

      function escapeHtml(value) {{
        return String(value ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#039;");
      }}

      function money(value) {{
        const number = Number(value || 0);
        return new Intl.NumberFormat("en-US", {{
          style: "currency",
          currency: "USD",
          maximumFractionDigits: 0
        }}).format(number);
      }}

      function nonempty(value) {{
        return String(value ?? "").trim().length > 0;
      }}

      function clearPanel() {{
        detailPanel.innerHTML = `
          <div class="details-empty">
            <div class="details-icon">↖</div>
            <h2>Grant details</h2>
            <p>Click a partner node to see every associated grant.</p>
            <p>Click an edge to see the grants shared by that pair.</p>
          </div>`;
      }}

      function field(label, value) {{
        if (!nonempty(value)) return "";
        return `<div class="grant-field"><strong>${{escapeHtml(label)}}:</strong> ${{escapeHtml(value)}}</div>`;
      }}

      function grantCard(eventId, selectedPartner = "") {{
        const grant = grantData[eventId];
        if (!grant) return "";

        const otherPartners = (grant.partners || []).filter(
          partner => partner !== selectedPartner
        );

        const partnerLabel = selectedPartner ? "Other partners" : "Partners";
        const partnerText = otherPartners.length
          ? otherPartners.join("; ")
          : selectedPartner
            ? "No other partner listed"
            : "No partner listed";

        const extraFields = [
          field("PI college", grant.pi_college),
          field("Team size", grant.team_size),
          field("Domestic / international", grant.domestic_international),
          field("Communities", grant.communities),
          field("Accepted", grant.accepted),
          field("CSV row", grant.csv_row)
        ].join("");

        return `
          <article class="grant-card">
            <div class="grant-heading">
              <div class="grant-id">${{escapeHtml(grant.grant_id)}}</div>
              <div class="grant-year">${{escapeHtml(grant.academic_year)}}</div>
            </div>
            <div class="grant-program">${{escapeHtml(grant.program)}}</div>
            <div class="grant-funding">${{money(grant.funding_numeric)}}</div>
            ${{field(partnerLabel, partnerText)}}
            ${{extraFields}}
          </article>`;
      }}

      function sortEvents(eventIds) {{
        return [...eventIds].sort((a, b) => {{
          const ga = grantData[a] || {{}};
          const gb = grantData[b] || {{}};
          const yearCompare = String(gb.academic_year || "")
            .localeCompare(String(ga.academic_year || ""));
          if (yearCompare !== 0) return yearCompare;
          return String(ga.grant_id || "").localeCompare(
            String(gb.grant_id || "")
          );
        }});
      }}

      function stat(value, label) {{
        return `
          <div class="stat">
            <span class="stat-value">${{value}}</span>
            <span class="stat-label">${{escapeHtml(label)}}</span>
          </div>`;
      }}

      function renderNode(nodeId) {{
        const node = nodeDetails[nodeId];
        if (!node) return clearPanel();

        const grantCards = sortEvents(node.grant_event_ids || [])
          .map(eventId => grantCard(eventId, node.partner))
          .join("");

        detailPanel.innerHTML = `
          <button class="panel-button" onclick="clearPanel()">Clear</button>
          <h2>${{escapeHtml(node.partner)}}</h2>
          <div class="detail-subtitle">Partner node · click another node to replace this panel</div>
          <div class="stats-grid">
            ${{stat(node.grant_count.toLocaleString(), "Associated grants")}}
            ${{stat(money(node.associated_funding), "Associated funding (full awards)")}}
            ${{stat(node.collaborator_count.toLocaleString(), "Visible collaborators")}}
            ${{stat(money(node.equal_share_funding), "Equal-share funding")}}
            ${{stat(node.singleton_grant_count.toLocaleString(), "Single-partner grants")}}
            ${{stat(node.multi_receiver_grant_count.toLocaleString(), "Multi-partner grants")}}
          </div>
          ${{node.dominant_program
              ? `<div class="grant-field"><strong>Most frequent program:</strong> ${{escapeHtml(node.dominant_program)}}</div>`
              : ""}}
          <h3>Individual grants</h3>
          ${{grantCards || "<p>No grant records available.</p>"}}`;
      }}

      function renderEdge(edgeId) {{
        const edge = edgeDetails[edgeId];
        if (!edge) return clearPanel();

        const grantCards = sortEvents(edge.grant_event_ids || [])
          .map(eventId => grantCard(eventId))
          .join("");

        detailPanel.innerHTML = `
          <button class="panel-button" onclick="clearPanel()">Clear</button>
          <h2>${{escapeHtml(edge.partner_1)}} ↔ ${{escapeHtml(edge.partner_2)}}</h2>
          <div class="detail-subtitle">Collaboration edge</div>
          <div class="stats-grid">
            ${{stat(edge.shared_grant_count.toLocaleString(), "Shared grants")}}
            ${{stat(money(edge.shared_associated_funding), "Shared associated funding")}}
            ${{stat(money(edge.pair_fractional_funding), "Pair-fractional funding")}}
          </div>
          <h3>Shared grants</h3>
          ${{grantCards || "<p>No shared grant records available.</p>"}}`;
      }}

      function attachGrantPanel() {{
        if (typeof network === "undefined" || !network) {{
          window.setTimeout(attachGrantPanel, 80);
          return;
        }}

        network.on("click", function(params) {{
          if (params.nodes && params.nodes.length > 0) {{
            renderNode(params.nodes[0]);
          }} else if (params.edges && params.edges.length > 0) {{
            renderEdge(params.edges[0]);
          }} else {{
            clearPanel();
          }}
        }});

        if (initialFocusPartner && nodeDetails[initialFocusPartner]) {{
          window.setTimeout(function() {{
            network.selectNodes([initialFocusPartner]);
            network.focus(initialFocusPartner, {{
              scale: 1.25,
              animation: {{ duration: 700, easingFunction: "easeInOutQuad" }}
            }});
            renderNode(initialFocusPartner);
          }}, 700);
        }}
      }}

      attachGrantPanel();
    </script>
    """

    return base_html.replace("</body>", click_script + "</body>", 1)
