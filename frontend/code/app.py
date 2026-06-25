import io
import importlib
import os
import sys
import tempfile
import altair as alt
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend', 'code'))

import pandas as pd
import streamlit as st

def _load_backend_symbols():
    loader_module = importlib.import_module("handlers.loader")
    pipeline_module = importlib.import_module("pipeline")
    return (
        loader_module.load_and_scope,
        pipeline_module.run_classic_pipeline,
        pipeline_module.run_url_pipeline,
    )

st.set_page_config(page_title="SKU Information Checking Tool", layout="wide")

_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'graphic', 'luxurynsight_logo.png')

_title_col, _logo_col = st.columns([10, 3], vertical_alignment="center")
with _logo_col:
    st.image(_LOGO)
with _title_col:
    st.title("SKU Information Checking Tool")
    st.caption("Input an Excel price list, run the pipeline, and review the updated results with the interactive charts below.")

# ── Stats table ───────────────────────────────────────────────────────────────
# Extend these rows to add more overview metrics in the Output panel.

def _build_stats_table(classified_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n = len(classified_df)
    rows.append(("Total", n))

    if "new_price" in classified_df.columns:
        rows.append(("With price", int(classified_df["new_price"].notna().sum())))
        rows.append(("Without price", int(classified_df["new_price"].isna().sum())))

    if "is_price_match" in classified_df.columns:
        rows.append(("Matching original price", int(classified_df["is_price_match"].fillna(False).astype(bool).sum())))

    if "remarks" in classified_df.columns:
        remarks = classified_df["remarks"].fillna("").astype(str).str.lower()
        rows.append(("Page not found (404)", int(remarks.eq("error404").sum())))
        rows.append(("Crawler blocked", int(remarks.eq("crawler_blocked").sum())))
        rows.append(("Skipped (threshold)", int(remarks.eq("skipped_error_threshold").sum())))

    return pd.DataFrame(rows, columns=["Metric", "Value"])


def _build_url_stats_table(classified_df: pd.DataFrame) -> pd.DataFrame:
    rows = [("Total", len(classified_df))]
    if "url_status" in classified_df.columns:
        status = classified_df["url_status"].fillna("").astype(str).str.strip().str.lower()
        rows.append(("Resolved", int(status.eq("resolved").sum())))
        rows.append(("Failed", int(status.eq("failed").sum())))
        rows.append(("Product not found", int(status.isin(["product page not found", "product not found"]).sum())))

    return pd.DataFrame(rows, columns=["Metric", "Value"])


def _render_metric_cards(metrics: list[tuple[str, int]]) -> None:
    if not metrics:
        return

    for start in range(0, len(metrics), 4):
        row = metrics[start:start + 4]
        cols = st.columns(len(row))
        for col, (label, value) in zip(cols, row):
            with col:
                st.metric(label, value)


def _section(title: str) -> None:
    """Render a compact section header and divider."""
    st.markdown(f"### {title}")
    st.divider()


# ── Brand/market list management ──────────────────────────────────────────────
# This callback resets the available scope whenever the uploaded file or sheet changes.
# Other widgets (counts, buttons, charts) do not mutate the brand/market lists.

def _reload_brands_markets() -> None:
    uploaded = st.session_state.get("run_upload")
    if uploaded is None:
        st.session_state.available_brands  = []
        st.session_state.available_markets = []
        st.session_state.selected_brands   = set()
        st.session_state.selected_markets  = set()
        return

    is_csv = uploaded.name.lower().endswith(".csv") if hasattr(uploaded, "name") else False

    try:
        uploaded.seek(0)
        if is_csv:
            # CSV bypasses sheet checks entirely
            df_peek = pd.read_csv(uploaded)
        else:
            # Standard sheet evaluation flow for Excel
            all_sheets = pd.ExcelFile(uploaded).sheet_names
            sheet = st.session_state.get("_sheet_key")
            if sheet not in all_sheets:
                sheet = all_sheets[0] if all_sheets else None
            if sheet is None:
                return
            df_peek = pd.read_excel(uploaded, sheet_name=sheet)

        # Standardize column naming formatting
        df_peek.columns = df_peek.columns.astype(str).str.strip().str.lower()
        
        # Populate session state tracking pools
        st.session_state.available_brands  = sorted(df_peek["brand"].dropna().unique().tolist())  if "brand"  in df_peek.columns else []
        st.session_state.available_markets = sorted(df_peek["market"].dropna().unique().tolist()) if "market" in df_peek.columns else []
        st.session_state.selected_brands   = set()
        st.session_state.selected_markets  = set()
        
    except Exception:
        # Graceful fallback state resets if parsing crashes
        st.session_state.available_brands  = []
        st.session_state.available_markets = []
        st.session_state.selected_brands   = set()
        st.session_state.selected_markets  = set()
    finally:
        try:
            uploaded.seek(0)
        except Exception:
            pass

def _count_scoped_items(uploaded, sheet_name: str | None, target_scope: dict) -> int | None:
    if uploaded is None:
        return None

    is_csv = uploaded.name.lower().endswith(".csv") if hasattr(uploaded, "name") else False

    if not is_csv and not sheet_name:
        return None

    try:
        uploaded.seek(0)
        if is_csv:
            df_scope = pd.read_csv(uploaded)
        else:
            df_scope = pd.read_excel(uploaded, sheet_name=sheet_name)
        
        df_scope.columns = df_scope.columns.astype(str).str.strip().str.lower()
        
    except Exception:
        return None
    finally:
        try:
            uploaded.seek(0)
        except Exception:
            pass

    if "new_price" in df_scope.columns:
        df_scope = df_scope[df_scope["new_price"].isna()]

    if not target_scope:
        return len(df_scope)

    total_count = 0
    for brand, markets in target_scope.items():
        if "brand" not in df_scope.columns:
            continue
            
        mask = df_scope["brand"] == brand
        
        if markets and "market" in df_scope.columns:
            valid_markets = {str(m).upper() for m in markets}
            mask &= df_scope["market"].astype(str).str.upper().isin(valid_markets)
            
        total_count += mask.sum()

    return total_count


# ── Pill selectors ────────────────────────────────────────────────────────────

def _render_pills(prefix: str, items: list, selected: set, max_per_row: int = 5) -> None:
    if not items:
        return

    n_cols = min(len(items), max_per_row)
    cols = st.columns(n_cols)
    for i, item in enumerate(items):
        with cols[i % n_cols]:
            is_sel = item in selected
            if st.button(
                item,
                key=f"pill_{prefix}_{item}",
                type="primary" if is_sel else "secondary",
            ):
                if is_sel:
                    selected.discard(item)
                else:
                    selected.add(item)
                st.rerun()


# ── Stdout tee ────────────────────────────────────────────────────────────────

class _Tee:
    """Write to both real stdout and a StringIO buffer."""
    def __init__(self, buf: io.StringIO):
        self._buf = buf
        self._real = sys.__stdout__

    def write(self, s):
        self._real.write(s)
        self._buf.write(s)

    def flush(self):
        self._real.flush()
        self._buf.flush()

def _show_charts_core(df: pd.DataFrame) -> None:
    if "status" not in df.columns and "new_price" in df.columns:
        df = df.copy()

        def _row_status(row):
            if pd.isna(row.get("new_price")):
                remark = str(row.get("remarks") or "").lower()
                if remark == "skipped_error_threshold":
                    return "Skipped (threshold)"
                if remark == "error404":
                    return "Page not found (404)"
                if remark == "crawler_blocked":
                    return "Crawler blocked"
                return "Price not retrieved"
            if bool(row.get("is_price_match", False)):
                return "Match"
            orig = str(row.get("currency") or "").strip().upper()
            new = str(row.get("new_currency") or "").strip().upper()
            if orig and new and orig != new:
                return "Currency mismatch"
            return "Price mismatch"

        df["status"] = df.apply(_row_status, axis=1)

    if st.session_state.result_mode == "url":
        status_col = "url_status"
    else:
        status_col = "status"

    if status_col not in df.columns:
        return

    _STATUS_COLORS = {
        # positive
        "Match": "#2ecc71",
        "Resolved": "#2ecc71",
        # neutral / intentional
        "Skipped (threshold)": "#f39c12",
        # negative
        "Price mismatch": "#e67e22",
        "Currency mismatch": "#e74c3c",
        "Price not retrieved": "#c0392b",
        "Product not found": "#c0392b",
        "Failed": "#c0392b",
        "Page not found (404)": "#922b21",
        "Crawler blocked": "#641e16",
    }

    status_df = df[status_col].value_counts().sort_values(ascending=False).reset_index()
    status_df.columns = [status_col, "count"]
    _all_statuses = status_df[status_col].tolist()
    _color_scale = alt.Scale(
        domain=_all_statuses,
        range=[_STATUS_COLORS.get(s, "#95a5a6") for s in _all_statuses],
    )

    donut = (
        alt.Chart(status_df)
        .mark_arc(innerRadius=55, outerRadius=105)
        .encode(
            theta=alt.Theta("count:Q"),
            color=alt.Color(
                f"{status_col}:N",
                scale=_color_scale,
                legend=alt.Legend(title="Status", orient="bottom"),
            ),
            tooltip=[f"{status_col}:N", "count:Q"],
        )
        .properties(title="Overall", width=260, height=260)
    )

    charts = []
    for col in ["brand", "market"]:
        if col in df.columns:
            group_df = df.groupby([col, status_col]).size().reset_index(name="count")
            group_df["pct"] = group_df.groupby(col)["count"].transform(lambda x: x / x.sum() * 100).round(1)
            col_order = group_df.groupby(col)["count"].sum().sort_values(ascending=False).index.tolist()

            chart = (
                alt.Chart(group_df)
                .mark_bar()
                .encode(
                    x=alt.X(f"{col}:N", sort=col_order, title=col.capitalize(), axis=alt.Axis(labelAngle=-30)),
                    y=alt.Y("count:Q", stack="zero", title="Rows"),
                    color=alt.Color(
                        f"{status_col}:N",
                        scale=_color_scale,
                        legend=alt.Legend(title="Status", orient="bottom"),
                    ),
                    tooltip=[
                        f"{col}:N", f"{status_col}:N", "count:Q",
                        alt.Tooltip("pct:Q", title=f"% of {col}", format=".1f"),
                    ],
                )
                .properties(title=f"By {col}", height=300)
            )
            charts.append(chart)

    st.divider()
    st.altair_chart(donut, use_container_width=True)

    if charts:
        st.caption("Breakdown by dimension")
        cols = st.columns(len(charts))
        for st_col, chart in zip(cols, charts):
            with st_col:
                st.altair_chart(chart, use_container_width=True)

    with st.expander("Preview data (first 100 rows)"):
        st.dataframe(df.head(100), use_container_width=False)


def _run_pipeline(uploaded, sheet_name: str | None, run_extract: bool, run_update: bool, max_errors_input) -> None:
    """Encapsulate pipeline run logic to keep the layout code clean.

    This function mirrors the previous inline logic but lives as a single callable
    so the Run UI remains easy to read and maintain.
    """
    if not uploaded:
        return

    load_and_scope, run_classic_pipeline, run_url_pipeline = _load_backend_symbols()
    brands = list(st.session_state.selected_brands)
    markets = list(st.session_state.selected_markets)
    target_scope = {b: (markets or None) for b in brands} if brands else {}
    max_err = int(max_errors_input) if max_errors_input is not None else None
    suffix = '.csv' if hasattr(uploaded, "name") and uploaded.name.lower().endswith('csv') else ".xlsx"
    # persist the uploaded file to a temp file for backend consumption
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(uploaded.read())
        tmp_in = f.name
    tmp_out = tmp_in.replace(".xlsx", "_out.csv")

    try:
        try:
            total_items = len(load_and_scope(tmp_in, sheet_name, target_scope))
        except Exception:
            total_items = 0

        _prog_state: dict = {}

        with st.status("Running pipeline…", expanded=True) as _pipeline_status:
            _progress_panel = st.container(border=True)

            with _progress_panel:
                _summary_placeholder = st.empty()
                _step_placeholder = st.empty()
                _bar_placeholder = st.empty()

                _step_placeholder.markdown("**Current phase:** waiting to start")
                _bar_placeholder.progress(0.0)

            def _on_progress(phase: str, brand: str, done: int, total: int, elapsed: float) -> None:
                _prog_state.update(phase=phase, brand=brand, done=done, total=total, elapsed=elapsed)
                raw_pct = done / total if total else 0.0
                pct = max(0.0, min(1.0, float(raw_pct)))
                eta_s = (elapsed / done * (total - done)) if done else 0
                eta_str = f"{eta_s/60:.2f}min" if eta_s < 3600 else f"{eta_s/3600:.1f}h"

                _summary_placeholder.markdown(f"**Current phase:** {phase}  \n**Current group:** {brand}")
                _step_placeholder.markdown(
                    f"**Progress:** {done}/{total} in current group &nbsp;|&nbsp; {elapsed/60:.2f}min elapsed · {elapsed/done:.2f}s/url · ETA {eta_str}"
                )
                _bar_placeholder.progress(pct)

            log_buf = io.StringIO()
            tee = _Tee(log_buf)
            old_stdout = sys.stdout
            sys.stdout = tee

            try:
                if run_extract:
                    run_classic_pipeline(
                        input_path=tmp_in,
                        sheet_name=sheet_name,
                        output_path=tmp_out,
                        target_scope=target_scope,
                        max_errors_threshold=max_err,
                        progress_callback=_on_progress,
                    )
                    st.session_state.result_mode = "price"
                else:
                    run_url_pipeline(
                        input_path=tmp_in,
                        sheet_name=sheet_name,
                        output_path=tmp_out,
                        target_scope=target_scope,
                        progress_callback=_on_progress,
                    )
                    st.session_state.result_mode = "url"

                df_done = pd.read_csv(tmp_out)
                st.session_state.result_csv = df_done.to_csv(index=False).encode("utf-8")
                st.session_state.run_log = log_buf.getvalue()
                _pipeline_status.update(label="Pipeline complete.", state="complete", expanded=False)
                st.rerun()

            except Exception as e:
                sys.stdout = old_stdout
                st.session_state.run_log = log_buf.getvalue()
                _pipeline_status.update(label=f"Pipeline error: {e}", state="error", expanded=True)
                st.error(f"Pipeline error: {e}")
            finally:
                sys.stdout = old_stdout
                for p in (tmp_in, tmp_out):
                    try:
                        os.unlink(p)
                    except (FileNotFoundError, PermissionError):
                        pass
    finally:
        try:
            uploaded.seek(0)
        except Exception:
            pass


def show_results(df: pd.DataFrame) -> None:
    """Show the summary cards, detailed counts, and a preview of the result file."""
    st.subheader("Overview")

    if st.session_state.result_mode == "url":
        summary_rows = _build_url_stats_table(df)
    else:
        summary_rows = _build_stats_table(df)

    _render_metric_cards(list(summary_rows.itertuples(index=False, name=None))[:4])

    with st.expander("Detailed counts", expanded=False):
        st.table(summary_rows)

    st.divider()
    st.subheader("Preview data")
    st.dataframe(df.head(100), use_container_width=True)


# ── Session state init ────────────────────────────────────────────────────────

if "result_csv" not in st.session_state:
    st.session_state.result_csv = None
if "result_mode" not in st.session_state:
    st.session_state.result_mode = None
if "run_log" not in st.session_state:
    st.session_state.run_log = ""
for _k in ("available_brands", "available_markets", "selected_brands", "selected_markets"):
    if _k not in st.session_state:
        st.session_state[_k] = [] if _k.startswith("available") else set()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_run, tab_view = st.tabs(["Run pipeline", "View results"])

# ── Tab 1: Run pipeline ───────────────────────────────────────────────────────

with tab_run:
    # Pre-load the last run so both tabs can render the same result set.
    df_res = None
    df_classified = None
    if st.session_state.result_csv is not None:
        df_res = pd.read_csv(pd.io.common.BytesIO(st.session_state.result_csv))
        df_classified = df_res

    col_inputs, col_stats = st.columns([1, 1])

    # ── Right column: stats table + download + log ────────────────────────────
    with col_stats:
        _section("Output")
        if df_classified is not None:
            show_results(df_classified)
            st.download_button(
                label="⬇ Download results CSV",
                data=st.session_state.result_csv,
                file_name=f"{'url' if st.session_state.result_mode == 'url' else 'price'}_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        else:
            st.info("Results will appear here after the pipeline runs.")

        if st.session_state.run_log:
            with st.expander("Run log", expanded=False):
                st.code(st.session_state.run_log, language=None)

    # ── Left column: inputs ───────────────────────────────────────────────────
    with col_inputs:
        _section("Input")

        # File uploader, sheet, max-errors and run buttons.
        narrow, _ = st.columns([1, 1])
        with narrow:
            uploaded = st.file_uploader(
                "Price list file", type=["xlsx", "xls", "csv"], key="run_upload",
                on_change=_reload_brands_markets,
                help="Upload the .xlsx or .csv file containing SKU data with at least 'brand' and 'url' columns.",
            )

            is_csv = uploaded.name.lower().endswith(".csv") if uploaded else False

            if uploaded is not None:
                # Check if the uploaded file is a CSV
                if is_csv:
                    # CSVs don't have sheets, so we disable the selectbox
                    st.selectbox(
                        "Sheet", ["N/A (CSV File)"], 
                        disabled=True, 
                        help="CSV files do not have multiple sheets."
                    )
                    sheet_name = None
                    uploaded.seek(0)
                else:
                    # It's an Excel file, read sheets as normal
                    _xl = pd.ExcelFile(uploaded)
                    sheet_name = st.selectbox(
                        "Sheet", _xl.sheet_names,
                        key="_sheet_key",
                        on_change=_reload_brands_markets,
                        help="Select the sheet that contains the price data.",
                    )
                    uploaded.seek(0)
            else:
                st.selectbox("Sheet", ["—"], disabled=True)
                sheet_name = None

            max_errors_input = st.number_input(
                "Max retries per brand",
                min_value=0,
                step=1,
                value=None,
                placeholder="No limit",
                help="Stop a brand after N total fetch failures (0 = stop at first error). Leave empty for no limit.",
            )
            
        # Brand pills — full right-column width so names are readable.
        if st.session_state.available_brands:
            st.caption("Brands — leave all unselected to process every brand")
            _render_pills("brand", st.session_state.available_brands, st.session_state.selected_brands)
        elif uploaded:
            st.caption("No 'brand' column found in file.")

        # Markets selector (searchable multiselect / drill-down)
        if st.session_state.available_markets:
            st.caption("Markets — leave all unselected to process every market (use search to drill down)")
            sel_markets = st.multiselect(
                "Select markets",
                options=st.session_state.available_markets,
                default=sorted(list(st.session_state.selected_markets)) if st.session_state.selected_markets else [],
                key="market_multiselect",
            )
            # synchronize the set used elsewhere in the app
            st.session_state.selected_markets = set(sel_markets)
        elif uploaded:
            st.caption("No 'market' column found in file.")

        if uploaded is not None and (sheet_name is not None or is_csv):
            brands  = list(st.session_state.selected_brands)
            markets = list(st.session_state.selected_markets)
            target_scope = {b: (markets or None) for b in brands} if brands else {}
            
            # Make sure your internal `_count_scoped_items` function is updated to handle sheet_name=None for CSVs
            scoped_total = _count_scoped_items(uploaded, sheet_name, target_scope)
            st.caption(f"Items to process: {scoped_total if scoped_total is not None else 'unavailable'}")

        # Run buttons also at half width.
        run_col_a, run_col_b = st.columns([1, 1])
        with run_col_a:
            run_extract = st.button(
                "Extract Price",
                disabled=uploaded is None,
                type="primary",
                use_container_width=True,
            )
        with run_col_b:
            run_update = st.button(
                "Update Url",
                disabled=uploaded is None,
                type="primary",
                use_container_width=True,
            )
    # ── Run logic ─────────────────────────────────────────────────────────────
    if (run_extract or run_update) and uploaded is not None:
        _run_pipeline(uploaded, sheet_name, run_extract, run_update, max_errors_input)
    if df_classified is not None:
        _show_charts_core(df_classified) 
# ── Tab 2: View saved results ─────────────────────────────────────────────────
with tab_view:
    _section("Load saved results")
    uploaded_csv = st.file_uploader(
        "Upload results CSV", type=["csv"], key="view_upload",
        help="Upload a CSV previously exported by the pipeline to visualize its results.",
    )
    if uploaded_csv is not None:
        df_view = pd.read_csv(uploaded_csv)
        show_results(df_view)
        _show_charts_core(df_view)
    else:
        st.info("Upload a previously saved results CSV to visualize it.")
