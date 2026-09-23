"""Reckoner: where two federal price transparency disclosures disagree.

Streamlit Community Cloud looks for this file at the repository root. It is
deliberately thin: every decision it makes lives in
``pipeline.summary_view``, which imports no UI and is covered by tests. What is
left here is layout.

The page reads CSV from ``summary/`` in this repository and makes **no network
calls**. There is no storage account to reach and nothing to authenticate with.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pipeline import summary_view as view

SUMMARY = Path(__file__).parent / "summary"

st.set_page_config(page_title="Reckoner", page_icon="🧾", layout="wide")


@st.cache_data
def load() -> view.Dataset:
    return view.load(SUMMARY)


data = load()

st.title("Reckoner")
st.caption(
    "Two federal rules require hospitals and insurers to publish the same "
    "negotiated rates. They disagree. This is where, and by how much."
)

if data.is_empty:
    st.error(
        "No summary dataset found in `summary/`. Generate it with "
        "`python -m reckoner_job --stage report`."
    )
    st.stop()

# Vintages above the fold. The committed dataset is a snapshot of something in
# ADLS, and a stale snapshot looks exactly like a fresh one.
stale = view.staleness(data.metadata)
columns = st.columns(len(stale))
for column, (label, value) in zip(columns, stale.items(), strict=True):
    column.metric(label, value[:10] if len(value) > 10 else value)

for caveat in view.caveats(data.metadata):
    st.info(caveat)

if data.missing:
    st.warning(f"Missing from the dataset: {', '.join(data.missing)}")

choices = view.options(data)
side = st.sidebar
side.header("Filters")
system = side.selectbox("Health system", choices["system"])
carrier = side.selectbox("Carrier", choices["carrier"])
code_type = side.selectbox("Code type", choices["code_type"])
side.caption(
    "Filters apply to every view. Where a view cannot answer one, or a filter "
    "drops rows it cannot attribute, the view says so."
)

selected = {"system": system, "carrier": carrier, "code_type": code_type}

coverage, outcomes, magnitude, exemplars, refusals = st.tabs(
    ["Coverage", "Outcomes", "Magnitude", "Widest gaps", "Refusals"]
)

with coverage:
    st.subheader("What reconciles at all")
    st.markdown(
        "**Read the comparable share, not the pair count.** It is the fraction of "
        "candidate pairs that survive the comparability rules. Everything else is "
        "refused for a stated reason, counted under *Refusals*."
    )
    rows = view.funnel(data, system=system)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if rows:
        st.bar_chart(
            {r["system"]: r["comparable_share"] for r in rows},
            y_label="comparable share",
        )

with outcomes:
    st.subheader("Why pairs differ")
    rows = view.apply_filters(data.table("outcomes"), **selected)
    chart = view.outcomes_chart(rows)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if chart:
        st.bar_chart({r["explanation"]: r["pairs"] for r in chart}, y_label="pairs")
    st.caption(
        "`systematic_offset` means one constant ratio covered many services: a "
        "single fact about two base rates, not one finding per code. "
        "`unexplained` is the residual that survived every rule."
    )

with magnitude:
    st.subheader("How large the surviving disagreements are")
    rows = view.apply_filters(data.table("magnitude"), **selected)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if rows:
        st.bar_chart(
            {f"{r['carrier']} / {r['code_type']}": r["median_relative_difference"] for r in rows},
            y_label="median relative difference",
        )

with exemplars:
    st.subheader("Widest gaps")
    st.markdown(
        "Capped per system and carrier. A rate ten times another for the same "
        "service and payer is flagged **implausible**: more likely a unit or "
        "methodology mismatch than a negotiated difference."
    )
    rows = view.widest(view.apply_filters(data.table("exemplars"), **selected))
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if rows:
        st.bar_chart(
            {f"{r['code']} ({r['carrier']})": r["relative_difference"] for r in rows[:15]},
            y_label="relative difference",
        )

with refusals:
    st.subheader("Why candidates never became pairs")
    rows = view.apply_filters(data.table("refusals"), **selected)
    inapplicable = view.inapplicable_filters(data.table("refusals"), **selected)
    if inapplicable:
        # Only said when it is true. The refusal grain was widened to carrier
        # and code type, so this branch is now the exception rather than the
        # rule -- and a note claiming a limitation that no longer exists is its
        # own kind of wrong.
        st.warning(
            f"{view.REFUSALS_GRAIN_NOTE} Ignored here: {', '.join(inapplicable)}.",
            icon="⚠",
        )
    else:
        st.caption(
            "A blank carrier or code type means the refusal was recorded without "
            "one: either before carrier grain existed, or before the candidate "
            "could be attributed. It is left blank rather than guessed at."
        )
    dropped = view.unattributed_excluded(data.table("refusals"), **selected)
    if dropped:
        st.warning(
            "This filter excludes refusals recorded without a carrier or code type: "
            + ", ".join(f"{system} ({count:,} candidates)" for system, count in dropped.items())
            + ". Their gold predates carrier grain, so the totals below are missing them.",
            icon="⚠",
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    if rows:
        totals: dict[str, int] = {}
        for row in rows:
            totals[str(row["reason"])] = totals.get(str(row["reason"]), 0) + int(row["candidates"])
        st.bar_chart(totals, y_label="candidates refused")

st.divider()
st.caption(
    "All data derives from public hospital (45 CFR 180) and payer (Transparency "
    "in Coverage) filings. No PHI. A personal project: deployed, scheduled and "
    "tested, but serving no users and supporting no one's decisions."
)
