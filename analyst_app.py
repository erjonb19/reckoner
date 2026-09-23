"""Reckoner, for an analyst: where does a hospital's rate disagree with the insurer's?

The preview of the redesign in ``docs/design/streamlit-redesign.md``. Like the
live page it is layout only: what it counts, ranks and says lives in
``pipeline.analyst_view``, which imports no UI and is covered by tests.

It reads ``summary/`` from this repository. Code lookup's rows are too many to
commit, so they come from the monthly GitHub Release that ``run.json`` names,
verified against the checksum recorded there. That is the page's one network
call, and when it fails the page says so rather than showing nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import altair as alt
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pipeline import analyst_view as view

SUMMARY = Path(__file__).parent / "summary"

# Validated with the dataviz palette checker against each theme's surface.
HOSPITAL = {"light": "#B4532A", "dark": "#C96A3E"}
INSURER = {"light": "#1A5E96", "dark": "#4F93D0"}
UNEXPLAINED = {"light": "#A33B3B", "dark": "#D06A6A"}
QUIET = {"light": "#B9B5A9", "dark": "#4A505C"}
INK = {"light": "#1D2433", "dark": "#E7E4DC"}
RULE = {"light": "#D9D6CC", "dark": "#343A45"}

st.set_page_config(page_title="Reckoner", page_icon="🧾", layout="wide")


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # an older runtime, or no browser session
        return "light"


@st.cache_data
def load() -> view.Dataset:
    return view.load(SUMMARY)


@st.cache_resource(show_spinner="Downloading this month's rate file…")
def release(tag: str) -> tuple[Any, Any, str]:
    """The rates and codes tables, once per release tag, or why they are missing."""
    del tag  # the cache key: a new month's release is a new download
    metadata = load().metadata
    rates = view.fetch_release_file(metadata, "rates.parquet")
    codes = view.fetch_release_file(metadata, "codes.parquet")
    if not rates.available:
        return None, None, rates.reason
    return (
        view.read_rates(rates.path),  # type: ignore[arg-type]
        view.read_rates(codes.path) if codes.available else None,  # type: ignore[arg-type]
        "",
    )


def how_to_read(text: str) -> None:
    with st.expander("How to read this"):
        st.markdown(text)


def download(rows: list[dict[str, Any]], name: str, key: str) -> None:
    st.download_button(
        "Download CSV",
        view.to_csv(rows),
        file_name=f"reckoner-{name}.csv",
        mime="text/csv",
        key=key,
        disabled=not rows,
    )


MONEY = st.column_config.NumberColumn(format="dollar")
SHARE = st.column_config.NumberColumn(format="percent")
COUNT = st.column_config.NumberColumn(format="localized")


# --- shared frame -----------------------------------------------------------------

data = load()


def sidebar_filters() -> view.Filters:
    """The four filters, read from and written back to the URL."""
    labels = {
        "system": "Health system",
        "facility": "Facility",
        "carrier": "Insurer",
        "code_type": "Code type",
    }
    everything = st.query_params.to_dict()
    chosen = view.Filters.from_query(everything)
    # A URL that differs from the one this session last wrote came from outside
    # -- a shared link, or a row picked on another page -- and wins over the
    # widgets. Otherwise the widgets win, so a changed filter is not undone.
    external = chosen.to_query() != st.session_state.get("_filters_query")
    picked: dict[str, str] = {}
    for name, label in labels.items():
        choices = view.options(data, view.Filters(**picked))[name]
        key = f"filter_{name}"
        if external:
            st.session_state[key] = getattr(chosen, name)
        if st.session_state.get(key, view.ALL) not in choices:
            st.session_state[key] = view.ALL
        picked[name] = st.sidebar.selectbox(label, choices, key=key)
    filters = view.Filters(**picked)
    st.session_state["_filters_query"] = filters.to_query()
    extra = {k: v for k, v in everything.items() if k not in labels}
    st.query_params.from_dict({**filters.to_query(), **extra})
    return filters


def header() -> None:
    st.sidebar.caption(view.freshness(data.metadata))
    for caveat in data.metadata.get("caveats") or []:
        st.info(caveat)
    if data.missing:
        st.warning(f"Missing from the dataset: {', '.join(data.missing)}")


# --- pages -----------------------------------------------------------------------


def rankings_page() -> None:
    filters = sidebar_filters()
    st.title("Where the two disclosures disagree")
    st.caption(
        "Each hospital rate is compared with every rate the same insurer publishes for "
        "the same code, facility and billing class."
    )
    header()

    numbers = view.headline(data, filters)
    a, b, c = st.columns(3)
    a.metric("Hospital rates", f"{numbers['hospital_rates']:,}")
    b.metric("Compared with the insurer", f"{numbers['compared']:,}")
    c.metric("Unexplained and material", f"{numbers['unexplained']:,}")

    by = (
        st.segmented_control(
            "Rank by",
            options=["unexplained", "gap"],
            format_func=lambda k: view.RANKINGS[k][1],
            default="unexplained",
            key="rank_by",
        )
        or "unexplained"
    )
    if by == "gap":
        st.caption(
            "Summed gap adds up dollar differences per rate. Neither file publishes how "
            "often a service is billed, so a large sum is many rates or a few large "
            "ones, never spend."
        )

    rows = view.rankings(data, filters, by)
    if not rows:
        st.info("No facility and insurer pair matches these filters.")
    else:
        column = view.RANKINGS[by][0]
        top = [r for r in rows[:15] if r.get(column)]
        if top:
            st.altair_chart(ranking_chart(top, column, view.RANKINGS[by][1]))
        event = st.dataframe(
            rows,
            column_order=[
                "rank",
                "facility",
                "carrier",
                "hospital_rates",
                "compared",
                "unexplained_material",
                "median_signed_gap",
                "summed_abs_gap_usd",
                "inside_range_share",
                "like_class_share",
            ],
            column_config={
                "rank": st.column_config.NumberColumn("#", width="small"),
                "facility": "Facility",
                "carrier": "Insurer",
                "hospital_rates": st.column_config.NumberColumn(
                    "Hospital rates", format="localized"
                ),
                "compared": st.column_config.NumberColumn("Compared", format="localized"),
                "unexplained_material": st.column_config.NumberColumn(
                    "Unexplained", format="localized"
                ),
                "median_signed_gap": st.column_config.NumberColumn(
                    "Median gap",
                    format="percent",
                    help="Insurer median over hospital rate, less one",
                ),
                "summed_abs_gap_usd": st.column_config.NumberColumn("Summed gap", format="dollar"),
                "inside_range_share": st.column_config.NumberColumn(
                    "Inside insurer range", format="percent"
                ),
                "like_class_share": st.column_config.NumberColumn(
                    "Comparable share", format="percent"
                ),
            },
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="rankings_table",
        )
        download(rows, "rankings", "dl_rankings")
        chosen = getattr(getattr(event, "selection", None), "rows", None)
        if chosen:
            row = rows[chosen[0]]
            st.switch_page(
                PAIR,
                query_params={
                    **filters.to_query(),
                    "facility": str(row["facility"]),
                    "carrier": str(row["carrier"]),
                },
            )
        st.caption("Select a row to open that facility and insurer.")

    how_to_read(
        """
**One row is one facility and one insurer.** *Compared* counts the hospital's rates
that had an insurer counterpart of the same billing class; *Unexplained* counts
those more than 5% from the insurer's median that no rule accounts for.

**Median gap** is signed: +20% means the insurer's median is 20% above the
hospital's published rate. **Inside insurer range** is the share of compared rates
that fall between the lowest and highest the insurer publishes for that code.

**Comparable share** is what fraction of the hospital's rates could be compared at
all. Read it before the counts: a pair with few comparable rates has little to say.

Hospital files are updated yearly and insurer files monthly, so a gap may be
timing. Pair detail shows how far apart each pair's files are.
"""
    )


def ranking_chart(rows: list[dict[str, Any]], column: str, title: str) -> alt.Chart:
    m = mode()
    values = [
        {
            "pair": f"{r['facility']} · {r['carrier']}",
            "value": abs(float(r[column])),
        }
        for r in rows
    ]
    money = column == "summed_abs_gap_usd"
    return (
        alt.Chart(alt.Data(values=values))
        .mark_bar(
            color=UNEXPLAINED[m] if column == "unexplained_material" else INK[m],
            cornerRadiusEnd=2,
            height=14,
        )
        .encode(
            x=alt.X(
                "value:Q",
                title=title,
                axis=alt.Axis(format="$,.0f" if money else ",d", grid=True, gridColor=RULE[m]),
            ),
            y=alt.Y("pair:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
            tooltip=[
                alt.Tooltip("pair:N", title="Pair"),
                alt.Tooltip("value:Q", title=title, format="$,.0f" if money else ","),
            ],
        )
        .properties(height=alt.Step(22))
        .configure_view(stroke=None)
    )


def range_chart(rows: list[dict[str, Any]], label: str, hospital: str, payer: str) -> alt.Chart:
    """The insurer's range as a rule, its median as a tick, the hospital as a dot."""
    m = mode()
    values = [
        {
            "row": str(r[label]),
            "low": r.get("payer_min"),
            "median": r.get(payer),
            "high": r.get("payer_max"),
            "hospital": r.get(hospital),
        }
        for r in rows
        if r.get(hospital) is not None and r.get(payer) is not None
    ]
    base = alt.Chart(alt.Data(values=values)).encode(
        y=alt.Y("row:N", sort=None, title=None, axis=alt.Axis(labelLimit=260))
    )
    tip = [
        alt.Tooltip("row:N", title="Rate"),
        alt.Tooltip("hospital:Q", title="Hospital", format="$,.2f"),
        alt.Tooltip("median:Q", title="Insurer median", format="$,.2f"),
        alt.Tooltip("low:Q", title="Insurer lowest", format="$,.2f"),
        alt.Tooltip("high:Q", title="Insurer highest", format="$,.2f"),
    ]
    span = base.mark_rule(color=INSURER[m], strokeWidth=2).encode(
        x=alt.X(
            "low:Q",
            title="Rate (USD, log scale)",
            scale=alt.Scale(type="log"),
            axis=alt.Axis(format="$,.0f", gridColor=RULE[m]),
        ),
        x2="high:Q",
        tooltip=tip,
    )
    tick = base.mark_tick(color=INSURER[m], thickness=2, size=14).encode(x="median:Q", tooltip=tip)
    dot = base.mark_point(
        color=HOSPITAL[m], filled=True, size=90, stroke=RULE[m], strokeWidth=0
    ).encode(x="hospital:Q", tooltip=tip)
    return (span + tick + dot).properties(height=alt.Step(24)).configure_view(stroke=None)


def legend() -> None:
    m = mode()
    st.markdown(
        f"<span style='color:{HOSPITAL[m]}'>●</span> hospital's published rate &nbsp;&nbsp; "
        f"<span style='color:{INSURER[m]}'>━┃━</span> insurer's lowest, median and highest",
        unsafe_allow_html=True,
    )


def pair_page() -> None:
    filters = sidebar_filters()
    st.title("Pair detail")
    header()
    pairs = view.rankings(data, view.Filters(system=filters.system))
    if filters.facility == view.ALL or filters.carrier == view.ALL:
        if not pairs:
            st.info("No facility and insurer pairs are in this dataset yet.")
            return
        names = [f"{r['facility']} · {r['carrier']}" for r in pairs]
        pick = st.selectbox("Facility and insurer", names, key="pair_pick")
        facility, carrier = (
            pairs[names.index(pick)]["facility"],
            pairs[names.index(pick)]["carrier"],
        )
    else:
        facility, carrier = filters.facility, filters.carrier

    detail = view.pair_detail(data, str(facility), str(carrier))
    if detail is None:
        st.info(f"No comparison exists for {facility} and {carrier}.")
        return
    s = detail.summary
    st.subheader(f"{facility} · {carrier}")
    a, b, c, d = st.columns(4)
    a.metric("Hospital rates", f"{int(s['hospital_rates'] or 0):,}")
    b.metric("Compared", f"{int(s['compared'] or 0):,}")
    c.metric("Unexplained", f"{int(s['unexplained_material'] or 0):,}")
    d.metric("Inside insurer range", f"{float(s['inside_range_share'] or 0):.0%}")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### What explains the compared rates")
        if detail.explanations:
            st.altair_chart(explanation_chart(detail.explanations))
        st.dataframe(
            detail.explanations,
            column_order=["label", "rates", "share"],
            column_config={"label": "Explanation", "rates": COUNT, "share": SHARE},
            hide_index=True,
        )
        download(detail.explanations, "explanations", "dl_explanations")
    with right:
        st.markdown("#### Why the rest were not compared")
        st.dataframe(
            detail.refusals,
            column_order=["label", "rates"],
            column_config={"label": "Reason", "rates": COUNT},
            hide_index=True,
        )
        download(detail.refusals, "refusals", "dl_pair_refusals")

    st.markdown("#### The largest unexplained dollar gaps")
    if detail.residual:
        legend()
        shown = [
            {**r, "label": f"{r['code_type']} {r['code']} · {r.get('setting') or ''}"}
            for r in detail.residual[:20]
        ]
        st.altair_chart(range_chart(shown, "label", "hospital_rate", "payer_rate"))
    st.dataframe(
        detail.residual,
        column_order=[
            "code_type",
            "code",
            "setting",
            "hospital_plan",
            "hospital_rate",
            "payer_min",
            "payer_rate",
            "payer_max",
            "payer_count",
            "difference",
            "hospital_vintage",
            "payer_vintage",
            "notes",
        ],
        column_config={
            "code_type": "Code type",
            "code": "Code",
            "setting": "Setting",
            "hospital_plan": "Hospital plan",
            "hospital_rate": st.column_config.NumberColumn("Hospital", format="dollar"),
            "payer_min": st.column_config.NumberColumn("Insurer lowest", format="dollar"),
            "payer_rate": st.column_config.NumberColumn("Insurer median", format="dollar"),
            "payer_max": st.column_config.NumberColumn("Insurer highest", format="dollar"),
            "payer_count": st.column_config.NumberColumn("Insurer rates", format="localized"),
            "difference": st.column_config.NumberColumn("Gap", format="dollar"),
            "hospital_vintage": "Hospital file",
            "payer_vintage": "Insurer file",
            "notes": "Notes",
        },
        hide_index=True,
    )
    download(detail.residual, f"residual-{facility}-{carrier}", "dl_residual")
    how_to_read(
        """
**Every hospital rate for this pair lands in exactly one place:** compared (left)
or not compared, with the reason (right).

Of the compared rates, *Inside the insurer's range* means the hospital's figure is
one the insurer itself publishes for some network — agreement, not a finding.
*Unexplained* rates are the finding: more than 5% from the insurer's median with
no rule to account for it.

**The gap table** holds this pair's largest unexplained dollar differences, up to
100. The dot is the hospital's rate; the line runs from the insurer's lowest to
highest, with a tick at its median. Check the two file dates before treating a gap
as a price difference: hospital files are updated yearly, insurer files monthly.
"""
    )


def explanation_chart(rows: list[dict[str, Any]]) -> alt.Chart:
    m = mode()
    values = [
        {
            "label": r["label"],
            "rates": r["rates"],
            "share": r["share"],
            "unexplained": r["explanation"] == "unexplained",
        }
        for r in rows
    ]
    return (
        alt.Chart(alt.Data(values=values))
        .mark_bar(cornerRadiusEnd=2, height=14)
        .encode(
            x=alt.X(
                "rates:Q", title="Hospital rates", axis=alt.Axis(format=",d", gridColor=RULE[m])
            ),
            y=alt.Y("label:N", sort="-x", title=None, axis=alt.Axis(labelLimit=240)),
            color=alt.condition(
                "datum.unexplained", alt.value(UNEXPLAINED[m]), alt.value(QUIET[m])
            ),
            tooltip=[
                alt.Tooltip("label:N", title="Explanation"),
                alt.Tooltip("rates:Q", title="Rates", format=","),
                alt.Tooltip("share:Q", title="Share", format=".1%"),
            ],
        )
        .properties(height=alt.Step(24))
        .configure_view(stroke=None)
    )


def lookup_page() -> None:
    filters = sidebar_filters()
    st.title("Code lookup")
    st.caption("One code, every facility and insurer that publishes it.")
    header()

    tag = str((data.metadata.get("release") or {}).get("tag") or "")
    rates, codes, reason = release(tag)
    if rates is None:
        st.error(
            f"Rate-level data unavailable: {reason}. The rankings and pair pages still "
            "work, because they read the committed summary."
        )
        return

    query = st.text_input(
        "Code or description",
        value=str(st.query_params.get("code", "")),
        placeholder="e.g. 70450, or 'ct head'",
        key="code_query",
    )
    code = view.normalise_code(query)
    matches = view.suggest(codes, query)
    exact = any(c == code for c, _, _ in matches) or bool(view.describe(codes, code))
    if matches and not exact:
        picked = st.selectbox(
            "Matching codes",
            [f"{c} · {t} · {d}" for c, t, d in matches],
            key="code_pick",
        )
        code = picked.split(" · ")[0]
    if not code:
        st.info("Type a CPT, HCPCS or MS-DRG code, or part of a description.")
        return
    if st.query_params.get("code") != code:
        st.query_params["code"] = code

    rows = view.code_lookup(rates, code, filters)
    st.subheader(f"{code} · {view.describe(codes, code) or 'no description published'}")
    if not rows:
        st.info("No facility publishes this code under these filters.")
        return
    summary = view.lookup_summary(rows)
    a, b, c = st.columns(3)
    a.metric("Facilities", summary["facilities"])
    b.metric("Insurers", summary["carriers"])
    c.metric("Compared", f"{summary['compared']:,} of {len(rows):,}")

    compared = [r for r in rows if r.get("compared")]
    if compared:
        legend()
        shown = [{**r, "label": f"{r['facility']} · {r['carrier']}"} for r in compared[:30]]
        st.altair_chart(range_chart(shown, "label", "hospital_rate", "payer_median"))
    st.dataframe(
        rows,
        column_order=[
            "facility",
            "carrier",
            "code_type",
            "hospital_rate",
            "payer_min",
            "payer_median",
            "payer_max",
            "gap",
            "why",
        ],
        column_config={
            "facility": "Facility",
            "carrier": "Insurer",
            "code_type": "Code type",
            "hospital_rate": st.column_config.NumberColumn("Hospital", format="dollar"),
            "payer_min": st.column_config.NumberColumn("Insurer lowest", format="dollar"),
            "payer_median": st.column_config.NumberColumn("Insurer median", format="dollar"),
            "payer_max": st.column_config.NumberColumn("Insurer highest", format="dollar"),
            "gap": st.column_config.NumberColumn("Gap", format="percent"),
            "why": "Outcome",
        },
        hide_index=True,
    )
    download(rows, f"code-{code}", "dl_code")
    how_to_read(
        """
**One row is one facility, one insurer and this code.** Where a facility publishes
several rates for the code, *Hospital* is their median.

A row with no insurer figures was not compared; *Outcome* says why. The most common
reasons are that the rate is Medicare Advantage or Medicaid, which only hospitals
must publish, or that the insurer publishes no rate for this code at this facility.

*Gap* is the insurer's median over the hospital's rate, less one: +20% means the
insurer's median is 20% higher. Codes are as the files publish them; descriptions
are the hospital's own wording, the most common one across the files.
"""
    )


def coverage_page() -> None:
    filters = sidebar_filters()
    st.title("Coverage and data quality")
    header()

    coverage = view.apply(data.table("coverage"), view.Filters(system=filters.system))
    st.markdown("#### How much of each system could be compared")
    st.dataframe(
        coverage,
        column_order=[
            "system",
            "candidates",
            "like_class_candidates",
            "pairs_formed",
            "comparable_share",
            "like_class_share",
            "unexplained_and_material",
            "facilities",
            "carriers",
        ],
        column_config={
            "system": "Health system",
            "candidates": st.column_config.NumberColumn("Hospital rates", format="localized"),
            "like_class_candidates": st.column_config.NumberColumn(
                "Same billing class", format="localized"
            ),
            "pairs_formed": st.column_config.NumberColumn("Compared", format="localized"),
            "comparable_share": st.column_config.ProgressColumn(
                "Share of all rates", format="percent", min_value=0.0, max_value=1.0
            ),
            "like_class_share": st.column_config.ProgressColumn(
                "Share of like-class rates", format="percent", min_value=0.0, max_value=1.0
            ),
            "unexplained_and_material": st.column_config.NumberColumn(
                "Unexplained", format="localized"
            ),
            "facilities": "Facilities",
            "carriers": "Insurers",
        },
        hide_index=True,
    )
    download(coverage, "coverage", "dl_coverage")

    st.markdown("#### Why rates were not compared")
    refusals = view.refusal_summary(data, filters)
    st.dataframe(
        refusals,
        column_order=["label", "rates", "share", "sentence"],
        column_config={
            "label": "Reason",
            "rates": COUNT,
            "share": st.column_config.ProgressColumn(
                "Share", format="percent", min_value=0.0, max_value=1.0
            ),
            "sentence": st.column_config.TextColumn("What it means", width="large"),
        },
        hide_index=True,
    )
    download(refusals, "refusals", "dl_refusals")

    st.markdown("#### How far apart the files are")
    vintages = view.apply(data.table("vintage_alignment"), filters)
    st.dataframe(
        vintages,
        column_order=[
            "system",
            "carrier",
            "pairs",
            "median_gap_days",
            "p90_gap_days",
            "max_gap_days",
            "beyond_limit",
        ],
        column_config={
            "system": "Health system",
            "carrier": "Insurer",
            "pairs": st.column_config.NumberColumn("Sampled comparisons", format="localized"),
            "median_gap_days": st.column_config.NumberColumn("Median days apart"),
            "p90_gap_days": st.column_config.NumberColumn("90th percentile"),
            "max_gap_days": st.column_config.NumberColumn("Most"),
            "beyond_limit": st.column_config.NumberColumn("Over 400 days"),
        },
        hide_index=True,
    )
    download(vintages, "vintages", "dl_vintages")
    how_to_read(
        """
**Read the shares, not the counts.** A hospital rate can only be compared with an
insurer rate of the same billing class, and many hospitals do not say which class a
rate is. *Share of like-class rates* is the fairer measure: of the rates that had a
same-class insurer counterpart to find, how many were compared.

**Some rates can never be compared.** Medicare Advantage and Medicaid managed-care
rates appear only in hospital files: the insurer rule exempts them.

**File dates matter.** Hospitals update their files at least yearly, insurers
monthly. Comparisons more than 400 days apart are refused rather than reported.
"""
    )


RANKINGS = st.Page(rankings_page, title="Rankings", icon=":material/leaderboard:", default=True)
PAIR = st.Page(pair_page, title="Pair detail", icon=":material/compare_arrows:", url_path="pair")
LOOKUP = st.Page(lookup_page, title="Code lookup", icon=":material/search:", url_path="code")
COVERAGE = st.Page(
    coverage_page, title="Coverage and data quality", icon=":material/rule:", url_path="coverage"
)

if not data.tables:
    st.error("No summary dataset found in `summary/`.")
    st.stop()

st.navigation([RANKINGS, PAIR, LOOKUP, COVERAGE]).run()
