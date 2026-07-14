from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from grant_network import (
    REQUIRED_COLUMNS,
    build_collaboration_graph,
    build_interactive_html,
    filter_grants,
    load_grants,
    make_receiver_view,
)


st.set_page_config(
    page_title="Grant Collaboration Network",
    page_icon="🕸️",
    layout="wide",
)

DEFAULT_CSV = "Grants-Network.csv"
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))


def csv_source() -> str:
    """
    Source priority:
      1. Streamlit secret CSV_SOURCE
      2. Environment variable CSV_SOURCE
      3. Grants-Network.csv in the app folder
    """
    try:
        secret_source = str(st.secrets.get("CSV_SOURCE", "")).strip()
    except Exception:
        secret_source = ""

    return (
        secret_source
        or os.getenv("CSV_SOURCE", "").strip()
        or DEFAULT_CSV
    )


def source_description(source: str) -> str:
    if source.startswith(("http://", "https://")):
        parts = urlsplit(source)
        # Do not expose access tokens or other query-string secrets in the UI.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return str(Path(source).resolve())


st.title("Grant Collaboration Network")
st.caption(
    "The app reads the existing CSV schema and does not rewrite the file. "
    "Click a node for its individual grants, or click an edge for shared grants."
)

source = csv_source()

try:
    initial_df = load_grants(source)
except Exception as exc:
    st.error(f"Could not load the grant CSV: {exc}")
    st.code(
        'CSV_SOURCE = "https://raw.githubusercontent.com/.../Grants-Network.csv"',
        language="toml",
    )
    st.stop()

program_options = sorted(
    value for value in initial_df["Program"].unique() if value
)
year_options = sorted(
    (value for value in initial_df["Academic Year"].unique() if value),
    reverse=True,
)
partner_options = sorted(
    {
        partner
        for partners in initial_df["_partners"]
        for partner in partners
    },
    key=str.casefold,
)

with st.sidebar:
    st.header("Network filters")

    selected_program = st.selectbox(
        "Program",
        ["All programs", *program_options],
    )
    selected_year = st.selectbox(
        "Academic year",
        ["All years", *year_options],
    )
    minimum_shared_grants = st.number_input(
        "Minimum shared grants per edge",
        min_value=1,
        max_value=100,
        value=1,
        step=1,
        help="Increasing this keeps only stronger collaboration ties.",
    )
    largest_component_only = st.checkbox(
        "Show largest connected component only",
        value=True,
        help=(
            "Turn this off to retain singleton partners and disconnected "
            "groups."
        ),
    )
    focus_partner = st.selectbox(
        "Find and focus a partner",
        ["", *partner_options],
        format_func=lambda value: "No partner selected" if not value else value,
    )

    if st.button("Reload CSV now", use_container_width=True):
        st.rerun()

    st.divider()
    st.caption(f"Auto-refresh: every {REFRESH_SECONDS} seconds")
    st.caption(f"CSV source: {source_description(source)}")

program_filter = None if selected_program == "All programs" else selected_program
year_filter = None if selected_year == "All years" else selected_year


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def live_network() -> None:
    try:
        df = load_grants(source)
    except Exception as exc:
        st.error(f"Automatic CSV reload failed: {exc}")
        return

    filtered_df = filter_grants(
        df,
        program=program_filter,
        academic_year=year_filter,
    )
    graph, grant_lookup = build_collaboration_graph(filtered_df)
    displayed_graph = make_receiver_view(
        graph,
        minimum_shared_grants=int(minimum_shared_grants),
        largest_component_only=largest_component_only,
    )

    grants_with_partners = int((filtered_df["_partner_count"] > 0).sum())
    associated_funding = float(
        filtered_df.loc[
            filtered_df["_partner_count"] > 0,
            "_funding",
        ].sum()
    )

    metric_1, metric_2, metric_3, metric_4 = st.columns(4)
    metric_1.metric("Grant rows", f"{len(filtered_df):,}")
    metric_2.metric("Grants with partners", f"{grants_with_partners:,}")
    metric_3.metric("Visible partners", f"{displayed_graph.number_of_nodes():,}")
    metric_4.metric(
        "Visible collaboration ties",
        f"{displayed_graph.number_of_edges():,}",
    )

    st.caption(
        f"Associated funding among filtered grants with partners: "
        f"${associated_funding:,.0f}. "
        f"Last CSV read: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
    )

    if displayed_graph.number_of_nodes() == 0:
        st.warning("No partner nodes meet the selected filters.")
        return

    active_focus = (
        focus_partner if focus_partner in displayed_graph.nodes else None
    )

    network_html = build_interactive_html(
        displayed_graph,
        grant_lookup,
        focus_partner=active_focus,
        height=820,
    )

    # PyVis is a JavaScript visualization, so it is rendered in an iframe.
    components.html(
        network_html,
        height=860,
        scrolling=False,
    )

    with st.expander("View filtered grant rows"):
        visible_columns = [
            column for column in REQUIRED_COLUMNS if column in filtered_df.columns
        ]
        st.dataframe(
            filtered_df[visible_columns],
            use_container_width=True,
            hide_index=True,
        )

        csv_bytes = filtered_df[visible_columns].to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download filtered CSV",
            data=csv_bytes,
            file_name="filtered_grants.csv",
            mime="text/csv",
        )


live_network()
