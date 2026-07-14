from __future__ import annotations

import os
import traceback
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
CACHE_SECONDS = 300


def csv_source() -> str:
    """Return the configured CSV source, or the bundled CSV by default."""
    try:
        secret_source = str(st.secrets.get("CSV_SOURCE", "")).strip()
    except Exception:
        secret_source = ""

    return secret_source or os.getenv("CSV_SOURCE", "").strip() or DEFAULT_CSV


def source_description(source: str) -> str:
    """Display the source without exposing query-string values."""
    if source.startswith(("http://", "https://")):
        parts = urlsplit(source)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return str(Path(source).resolve())


@st.cache_data(ttl=CACHE_SECONDS, max_entries=2, show_spinner=False)
def cached_load_grants(source: str) -> pd.DataFrame:
    """Cache the CSV briefly so ordinary widget clicks do not redownload it."""
    return load_grants(source)


def load_current_grants(source: str) -> tuple[pd.DataFrame, str | None]:
    """
    Load the shared CSV with a simple fallback.

    Fallback order:
      1. Shared CSV or cached copy
      2. Last successful copy in this browser session
      3. Bundled Grants-Network.csv
    """
    try:
        df = cached_load_grants(source)
        st.session_state["last_good_grants"] = df.copy()
        return df, None
    except Exception as exc:
        last_good = st.session_state.get("last_good_grants")
        if isinstance(last_good, pd.DataFrame) and not last_good.empty:
            return last_good.copy(), str(exc)

        local_path = Path(DEFAULT_CSV)
        if source != DEFAULT_CSV and local_path.exists():
            local_df = load_grants(local_path)
            st.session_state["last_good_grants"] = local_df.copy()
            return local_df, str(exc)

        raise


st.title("Grant Collaboration Network")
st.caption(
    "Click a partner node to view its individual grants, or click an edge "
    "to view grants shared by two partners."
)

source = csv_source()

try:
    grants_df, source_warning = load_current_grants(source)
except Exception as exc:
    st.error(f"Could not load the grant CSV: {exc}")
    st.markdown(
        "Use the Google Sheets **Publish to web → CSV** URL in Streamlit "
        "Secrets. Do not use a Sheets `/edit` URL or a temporary "
        "`googleusercontent.com` URL."
    )
    with st.expander("Technical details"):
        st.code(traceback.format_exc(), language="text")
    st.stop()

if source_warning:
    st.warning(
        "The shared CSV was temporarily unavailable. The app is showing "
        f"the most recent available copy. Source error: {source_warning}"
    )

program_options = sorted(
    value for value in grants_df["Program"].unique() if value
)
year_options = sorted(
    (value for value in grants_df["Academic Year"].unique() if value),
    reverse=True,
)
partner_options = sorted(
    {
        partner
        for partners in grants_df["_partners"]
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

    if st.button("Reload CSV now", width="stretch", type="primary"):
        cached_load_grants.clear()
        st.session_state.pop("last_good_grants", None)
        st.rerun()

    st.divider()
    st.caption("Automatic refresh is disabled for stability.")
    st.caption("Use ‘Reload CSV now’ after the Sheet is edited.")
    st.caption(f"CSV source: {source_description(source)}")

program_filter = None if selected_program == "All programs" else selected_program
year_filter = None if selected_year == "All years" else selected_year

try:
    filtered_df = filter_grants(
        grants_df,
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
        f"Page rendered: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
    )

    if displayed_graph.number_of_nodes() == 0:
        st.warning("No partner nodes meet the selected filters.")
    else:
        active_focus = (
            focus_partner if focus_partner in displayed_graph.nodes else None
        )

        network_html = build_interactive_html(
            displayed_graph,
            grant_lookup,
            focus_partner=active_focus,
            height=820,
        )

        components.html(
            network_html,
            height=860,
            scrolling=False,
        )

    with st.expander("Filtered grant data"):
        visible_columns = [
            column for column in REQUIRED_COLUMNS if column in filtered_df.columns
        ]
        export_df = filtered_df[visible_columns]

        # Avoid serializing the full DataFrame through Apache Arrow on every
        # widget rerun. A compact text preview is sufficient here, and the
        # complete data remains available as a CSV download.
        preview_rows = min(20, len(export_df))
        st.caption(
            f"Previewing the first {preview_rows:,} of {len(export_df):,} rows. "
            "Download the CSV for the complete table."
        )
        st.code(
            export_df.head(preview_rows).to_csv(index=False),
            language="text",
        )

        csv_bytes = export_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download filtered CSV",
            data=csv_bytes,
            file_name="filtered_grants.csv",
            mime="text/csv",
            on_click="ignore",
            width="stretch",
        )
except Exception as exc:
    st.error(f"The network could not be rendered: {exc}")
    with st.expander("Technical details"):
        st.code(traceback.format_exc(), language="text")
