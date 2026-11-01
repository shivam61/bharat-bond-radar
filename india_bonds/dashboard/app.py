"""Research dashboard for the India bond screener.

Run:  streamlit run india_bonds/dashboard/app.py
DB:   INDIA_BONDS_DB env var, default data/bonds.db (relative to the working directory).

Research classification only. Not investment advice. No transactions are executed.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402

from india_bonds.dashboard import queries as q  # noqa: E402

DISCLAIMER = "Research classification only — not investment advice; no transactions are executed."

COLUMN_CONFIG = {
    "Our YTM (%)": st.column_config.NumberColumn(format="%.2f"),
    "Vendor YTM (unverified)": st.column_config.NumberColumn(format="%.2f"),
    "NSE reported traded yield (unverified)": st.column_config.NumberColumn(format="%.2f"),
    "Coupon (%)": st.column_config.NumberColumn(format="%.2f"),
    "Clean price": st.column_config.NumberColumn(format="%.2f"),
    "Dirty price": st.column_config.NumberColumn(format="%.2f"),
    "Discount (%)": st.column_config.NumberColumn(format="%.2f"),
    "Min investment": st.column_config.NumberColumn(format="%.2f"),
}


def _show_table(df: pd.DataFrame, empty_msg: str, **display_kwargs) -> None:
    if df is None or df.empty:
        st.info(empty_msg)
        return
    st.dataframe(q.display_frame(df, **display_kwargs), column_config=COLUMN_CONFIG, hide_index=True)


def _show_plain(df: pd.DataFrame, empty_msg: str) -> None:
    if df is None or df.empty:
        st.info(empty_msg)
        return
    st.dataframe(_plain(df), hide_index=True)


def _plain(df: pd.DataFrame) -> pd.DataFrame:
    """Detail tables mix types within a column (SQLite is loosely typed); show them as text."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
    return out


def _render_detail(conn, isin: str, run_id: str) -> None:
    d = q.isin_detail(conn, isin, run_id)
    master = d["master"]
    if master is None:
        st.warning("No instrument master row for this ISIN.")
    else:
        st.markdown("**Instrument master**")
        st.table(pd.DataFrame(
            [(k, "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
             for k, v in master.items()],
            columns=["field", "value"],
        ))
    st.markdown("**Rating rows (instrument rating, per agency)**")
    _show_plain(d["ratings"], "No rating rows recorded for this ISIN.")
    st.markdown("**Price history**")
    _show_plain(d["prices"], "No price snapshots recorded for this ISIN.")
    st.markdown("**Availability rows**")
    _show_plain(d["availability"], "No availability rows recorded for this ISIN.")
    st.markdown("**Rejection reasons (this run)**")
    if d["rejections"].empty:
        st.info("Not rejected in this run.")
    else:
        for _, r in d["rejections"].iterrows():
            st.write(f"- `{r['rule']}`: {r['reason']}")
    st.markdown("**Source URLs and timestamps**")
    _show_plain(d["sources"], "No source URLs recorded for this ISIN.")


def main() -> None:
    st.set_page_config(page_title="India bonds screener (research)", layout="wide")
    st.title("India corporate bond screener")
    st.caption(DISCLAIMER)

    db_path = q.resolve_db_path()
    st.sidebar.markdown(f"**DB path:** `{db_path}`")

    conn = q.open_readonly(db_path)
    if conn is None:
        st.error(
            f"Database not found or unreadable at `{db_path}`. "
            f"Run the screener first, or set {q.DB_ENV_VAR} to the correct path."
        )
        return
    run = q.latest_run(conn)
    if run is None:
        st.info(
            "The database has no screen results yet (screen_result is missing or empty). "
            "Run the screener to populate it."
        )
        return

    run_id = run["run_id"]
    st.sidebar.markdown(f"**Latest run_id:** `{run_id}`")
    st.sidebar.markdown(f"**Settlement:** {run['settlement'] or 'not recorded in DB (custom run_id)'}")
    st.sidebar.markdown(f"**Run created (UTC):** {run['created_at'] or 'unknown'}")

    primary = q.shortlist(conn, run_id, "primary")
    speculative = q.shortlist(conn, run_id, "speculative")
    combined = pd.concat([primary, speculative], ignore_index=True)

    # ---- sidebar filters (apply to the bucket tabs, not to Rejected)
    st.sidebar.header("Filters")
    rating_options = sorted(
        {r for r in combined["worst_rating"].dropna().unique()},
        key=lambda r: q.RATING_ORDER.get(r, 999),
    )
    f_ratings = st.sidebar.multiselect("Lowest instrument rating", rating_options)
    bounds = q.ytm_bounds(combined)
    f_ytm = None
    if bounds is not None:
        lo, hi = bounds
        if hi <= lo:
            hi = lo + 0.01
        f_ytm = st.sidebar.slider("Our YTM (%)", min_value=float(lo), max_value=float(hi),
                                  value=(float(lo), float(hi)), step=0.01)
    use_mat = st.sidebar.checkbox("Maturity before date")
    f_mat = None
    if use_mat:
        f_mat = st.sidebar.date_input("Maturity before", value=date.today() + timedelta(days=365 * 5))
    f_freq = st.sidebar.multiselect("Payout frequency", list(q.PAYOUT_FREQUENCIES))
    f_issuer = st.sidebar.text_input("Issuer / name search")
    f_conf = st.sidebar.selectbox("Minimum confidence", list(q.CONFIDENCE_LEVELS), index=0)
    f_disc = st.sidebar.number_input("Minimum discount (%)", min_value=0.0, value=0.0, step=0.5)

    filters = dict(
        ratings=f_ratings or None,
        ytm_range=f_ytm,
        maturity_before=f_mat if use_mat else None,
        frequencies=f_freq or None,
        issuer_query=f_issuer or None,
        min_confidence=f_conf,
        discount_min=f_disc if f_disc > 0 else None,
    )

    def filtered(df: pd.DataFrame) -> pd.DataFrame:
        return q.add_rank(q.apply_filters(df, **filters))

    tab_primary, tab_spec, tab_rej, tab_income, tab_disc = st.tabs([
        "Primary shortlist",
        "Speculative (A+ / A)",
        "Rejected",
        "Monthly / quarterly income",
        "Discounted",
    ])

    with tab_primary:
        shown = filtered(primary)
        st.write(f"Bucket `primary` (lowest instrument rating AAA to AA-): {len(shown)} rows after filters.")
        _show_table(shown, "No primary bonds match the current filters.")
        if not shown.empty:
            st.caption(f"Rows marked LOW CONFIDENCE: {(shown['confidence'] == 'Low').sum()}")

    with tab_spec:
        st.warning(
            "Speculative bucket: lowest instrument rating A+ or A. Kept strictly separate from "
            "the primary shortlist."
        )
        shown_s = filtered(speculative)
        st.write(f"{len(shown_s)} rows after filters.")
        _show_table(shown_s, "No speculative bonds match the current filters.")

    with tab_rej:
        st.write("Rejected in this run: reasons grouped per ISIN, and the count of rejected ISINs per rule.")
        counts = q.rejection_counts(conn, run_id)
        if counts.empty:
            st.info("No rejection rows for this run.")
        else:
            fig = px.bar(counts, x="count", y="rule", orientation="h",
                         labels={"count": "Rejected ISINs", "rule": "Rule"})
            fig.update_layout(yaxis={"categoryorder": "total ascending"},
                              height=max(250, 40 * len(counts)))
            st.plotly_chart(fig)
        _show_table(q.rejected(conn, run_id), "No rejected bonds in this run.")

    with tab_income:
        st.write("Monthly and quarterly payout bonds from both buckets. The Bucket column shows which "
                 "bucket each bond is in: Primary (AAA to AA-) or Speculative (A+ / A).")
        inc = filtered(q.income(conn, run_id))
        _show_table(inc, "No monthly or quarterly payout bonds match the current filters.", bucket=True)

    with tab_disc:
        st.write("Primary and speculative bonds priced more than 2% below face. Flags: "
                 "Below 98 (discount > 2%), Below 95 (> 5%), Below 90 (> 10%).")
        disc = filtered(q.discounted(conn, run_id))
        _show_table(disc, "No discounted bonds match the current filters.", bucket=True, discount_flags=True)

    st.divider()
    st.subheader("Details for one ISIN")
    isins = q.all_isins(conn, run_id)
    if not isins:
        st.info("No ISINs in this run.")
        return
    pick = st.selectbox("ISIN", isins)
    with st.expander(f"Details: {pick}", expanded=True):
        _render_detail(conn, pick, run_id)
    conn.close()


main()
