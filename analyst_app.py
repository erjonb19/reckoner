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

import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import altair as alt
import pyarrow as pa
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pipeline import analyst_view as view
from pipeline import compare_view as compare
from pipeline import thirtytwobj_view as bj

# The committed dataset. RECKONER_SUMMARY points the page at another copy, which
# is how the tests run every page against facility-grain rows before the
# committed summary has them.
SUMMARY = Path(os.environ.get("RECKONER_SUMMARY") or Path(__file__).parent / "summary")

# Validated with the dataviz palette checker against each theme's surface.
HOSPITAL = {"light": "#B4532A", "dark": "#C96A3E"}
INSURER = {"light": "#1A5E96", "dark": "#4F93D0"}
UNEXPLAINED = {"light": "#A33B3B", "dark": "#D06A6A"}
QUIET = {"light": "#B9B5A9", "dark": "#4A505C"}
INK = {"light": "#1D2433", "dark": "#E7E4DC"}
RULE = {"light": "#D9D6CC", "dark": "#343A45"}

st.set_page_config(page_title="Reckoner", page_icon="🧾", layout="wide")

# The ledger faces, loaded by the page itself. config.toml names them, and
# Streamlit 1.64 applies the names, but it never requested the Google Fonts
# stylesheets its "Name:url" form points at: measured in a browser, the page
# loaded Source Sans only, and every heading rendered in the body face. One
# import loads all three; headings are set to the serif explicitly, since the
# configured heading font did not reach them either.
FONTS = (
    "https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600"
    "&family=Source+Serif+4:wght@500;600&family=JetBrains+Mono:wght@400;500&display=swap"
)
st.markdown(
    f"""<style>
@import url('{FONTS}');
h1, h2, h3, h4 {{ font-family: "Source Serif 4", Georgia, serif !important; }}
</style>""",
    unsafe_allow_html=True,
)


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
        shown = view.for_display(
            rows,
            {
                "median_signed_gap": "signed_pct",
                "inside_range_share": "pct",
                "like_class_share": "pct",
            },
        )
        event = st.dataframe(
            shown,
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
                "median_signed_gap": st.column_config.TextColumn(
                    "Median gap",
                    help="Insurer median over hospital rate, less one. Blank: nothing compared.",
                    alignment="right",
                ),
                "summed_abs_gap_usd": st.column_config.NumberColumn("Summed gap", format="dollar"),
                "inside_range_share": st.column_config.TextColumn(
                    "Inside insurer range", alignment="right"
                ),
                "like_class_share": st.column_config.TextColumn(
                    "Comparable share", alignment="right"
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
        view.for_display(
            rows,
            {
                "payer_min": "money",
                "payer_median": "money",
                "payer_max": "money",
                "gap": "signed_pct",
            },
        ),
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
            "payer_min": st.column_config.TextColumn("Insurer lowest", alignment="right"),
            "payer_median": st.column_config.TextColumn("Insurer median", alignment="right"),
            "payer_max": st.column_config.TextColumn("Insurer highest", alignment="right"),
            "gap": st.column_config.TextColumn(
                "Gap", help="Blank: not compared; Outcome says why.", alignment="right"
            ),
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


# --- compare ---------------------------------------------------------------------

POOLED = (
    "**Early look.** Each rate is the middle of every rate the hospital lists for this "
    "insurer and service. That can mix commercial, Medicare Advantage and Medicaid plans, "
    "and different settings, so read the gaps as direction, not exact contract prices."
)
VIEWS = ["One service", "Several services", "One hospital, every insurer"]
OWN = "Pick my own"
OTHER = "Another code…"


def compare_choices(rates: pa.Table) -> tuple[list[compare.Hospital], str, str]:
    """Hospitals, insurer and view, read from and written back to the URL."""
    everyone = compare.hospitals(rates)
    by_label = {h.label: h for h in everyone}
    params = st.query_params

    preset_options = [*compare.PRESETS, OWN]
    # First visit: a shared link's hospitals, else the first preset. Seeded through
    # session state rather than ``default=``, which Streamlit refuses to combine
    # with a key the page also writes.
    if "cmp_hospitals" not in st.session_state:
        shared = [label for label in params.get("h", "").split("~") if label in by_label]
        start = shared or compare.PRESETS[preset_options[0]]
        st.session_state["cmp_hospitals"] = start
        st.session_state["cmp_preset"] = next(
            (k for k, v in compare.PRESETS.items() if v == start), OWN
        )
        st.session_state["_cmp_preset_applied"] = st.session_state["cmp_preset"]
    preset = st.segmented_control("Hospital set", preset_options, key="cmp_preset") or OWN
    if preset != st.session_state.get("_cmp_preset_applied"):
        st.session_state["_cmp_preset_applied"] = preset
        if preset in compare.PRESETS:
            st.session_state["cmp_hospitals"] = compare.PRESETS[preset]
    picked = st.multiselect(
        "Hospitals (2 to 6)",
        list(by_label),
        max_selections=6,
        key="cmp_hospitals",
        help="Each entry is one published price list. Some cover several campuses; "
        "hover a name in the table to see which.",
    )
    chosen = [by_label[label] for label in picked if label in by_label]

    insurers = compare.carriers(rates)
    if st.session_state.get("cmp_carrier") not in insurers:
        wanted = params.get("insurer", "Aetna")
        st.session_state["cmp_carrier"] = wanted if wanted in insurers else insurers[0]
    left, right = st.columns([1, 2])
    with left:
        carrier = st.selectbox("Insurer", insurers, key="cmp_carrier")
    with right:
        shown = st.segmented_control("View", VIEWS, default=VIEWS[0], key="cmp_view") or VIEWS[0]
    params["h"] = "~".join(h.label for h in chosen)
    params["insurer"] = carrier
    return chosen, carrier, shown


def compare_page() -> None:
    st.title("Compare hospitals")
    st.caption("Same service, same insurer: what each hospital's own price file says it is paid.")
    tag = str((data.metadata.get("release") or {}).get("tag") or "")
    rates, codes, reason = release(tag)
    if rates is None:
        st.error(f"Rate-level data unavailable: {reason}.")
        return

    st.sidebar.caption(view.freshness(data.metadata))
    chosen, carrier, shown = compare_choices(rates)
    st.info(POOLED)
    if len(chosen) < 2:
        st.info("Pick at least two hospitals to compare.")
    elif shown == VIEWS[1]:
        several_services(rates, codes, chosen, carrier)
    elif shown == VIEWS[2]:
        every_insurer(rates, codes, chosen)
    else:
        one_service(rates, codes, chosen, carrier)

    how_to_read(DEFINITIONS)


DEFINITIONS = f"""
**Definitions**

- **Rate:** what the hospital's own price file says this insurer pays it for the
  service. Where it lists several (different plans), this is the middle one.
- **Rates behind it:** how many rates that middle value comes from.
- **vs. lowest:** how much more than the cheapest hospital you picked.
- **vs. group:** distance from the middle of the hospitals you picked.
- **Price index:** across several services, how far a hospital typically sits from
  the group. 1.20 means about 20% above; 0.80 about 20% below.
- **Check before quoting:** the rate is more than {compare.FAR:.0f} times, or under
  a third of, the group's middle. With pooled rates that is usually a mix of plan
  types or settings, so look before using the number.

**Limits**

- Hospital price files only. Insurers' own files are used elsewhere in this app.
- Only the five insurers that also publish their own files are listed here.
- Some entries cover several campuses because the hospital publishes one price
  list for all of them. Montefiore's Bronx campuses are one example.
- Files are dated: hospitals update theirs at least once a year.
"""


def pick_service(codes: pa.Table | None, key: str, default: str = "70450") -> str:
    options = [*compare.SERVICES, OTHER]
    wanted = st.query_params.get("code", default)
    if key not in st.session_state:
        st.session_state[key] = wanted if wanted in compare.SERVICES else OTHER
    choice = st.selectbox(
        "Service",
        options,
        format_func=lambda c: c if c == OTHER else f"{compare.SERVICES[c]} ({c})",
        key=key,
    )
    if choice != OTHER:
        st.query_params["code"] = choice
        return choice
    query = st.text_input(
        "Code or description",
        value=wanted if wanted not in compare.SERVICES else "",
        placeholder="e.g. 70450, or 'ct head'",
        key=f"{key}_text",
    )
    code = view.normalise_code(query)
    matches = view.suggest(codes, query)
    if matches and not any(c == code for c, _, _ in matches):
        picked = st.selectbox(
            "Matching codes", [f"{c} · {d}" for c, _, d in matches], key=f"{key}_match"
        )
        code = picked.split(" · ")[0]
    if code:
        st.query_params["code"] = code
    return code


def one_service(
    rates: pa.Table, codes: pa.Table | None, chosen: list[compare.Hospital], carrier: str
) -> None:
    code = pick_service(codes, "cmp_code")
    if not code:
        st.info("Pick a service, or type a CPT, HCPCS or MS-DRG code.")
        return
    rows = compare.head_to_head(rates, chosen, carrier, code)
    name = compare.service_name(code, view.describe(codes, code))
    st.subheader(f"{name} · {carrier}")
    published = [r for r in rows if r["published"]]
    if not published:
        st.info("None of these hospitals publishes a rate for this service and insurer.")
        return

    a, b, c = st.columns(3)
    low, high = published[0], published[-1]
    a.metric("Lowest", compare.money(low["rate"]))
    a.caption(low["hospital"])
    b.metric("Highest", compare.money(high["rate"]))
    b.caption(high["hospital"])
    c.metric("Highest vs. lowest", f"{high['rate'] / low['rate']:.1f}x" if low["rate"] else "")
    c.caption(f"{len(published)} of {len(rows)} hospitals publish it")
    st.altair_chart(bar_chart(published, "hospital", "Rate"))
    covers = {h.label: ", ".join(h.covers) for h in chosen}
    st.dataframe(
        [
            {
                "hospital": r["hospital"],
                "rate": compare.money(r["rate"]) or "not published",
                "vs_lowest": compare.percent(r["vs_lowest"]),
                "vs_middle": compare.percent(r["vs_middle"]),
                "rates_behind_it": r["rates_behind_it"] or "",
                "check": r["check"],
                "covers": covers.get(r["hospital"], ""),
            }
            for r in rows
        ],
        column_config={
            "hospital": "Hospital",
            "rate": st.column_config.TextColumn("Rate", alignment="right"),
            "vs_lowest": st.column_config.TextColumn("vs. lowest", alignment="right"),
            "vs_middle": st.column_config.TextColumn("vs. group", alignment="right"),
            "rates_behind_it": "Rates behind it",
            "check": "Check",
            "covers": st.column_config.TextColumn("Price list covers", width="large"),
        },
        hide_index=True,
    )
    download(rows, f"compare-{code}-{carrier}", "dl_cmp_one")


def several_services(
    rates: pa.Table, codes: pa.Table | None, chosen: list[compare.Hospital], carrier: str
) -> None:
    picked = st.multiselect(
        "Services",
        list(compare.SERVICES),
        default=compare.DEFAULT_BASKET,
        format_func=lambda c: f"{compare.SERVICES[c]} ({c})",
        key="cmp_basket",
    )
    cells, summary = compare.scorecard(rates, chosen, carrier, picked)
    if not cells:
        st.info("Fewer than two of these hospitals publish any of these services.")
        return
    st.subheader(f"{summary[0]['compared']} services compared · {carrier}")
    columns = st.columns(len(summary))
    for column, row in zip(columns, summary, strict=True):
        column.metric(
            row["hospital"],
            f"{row['index']:.2f}" if row["index"] is not None else "n/a",
            help="Price index: 1.00 is the group's middle, 1.20 about 20% above it.",
        )
        column.caption(f"Lowest on {row['lowest_on']} of {row['compared']}")
    order = [r["hospital"] for r in summary]
    st.altair_chart(heatmap(cells, order))
    st.caption(
        "Blue is below the group's middle, orange above. "
        "\\* far from the group: check before quoting (hover for detail)."
    )

    table: dict[str, dict[str, Any]] = {}
    for cell in cells:
        row = table.setdefault(cell["code"], {"service": compare.SERVICES.get(cell["code"])})
        row[cell["hospital"]] = compare.money(cell["rate"]) or "—"
    st.dataframe(
        list(table.values()),
        column_order=["service", *order],
        column_config={"service": "Service"},
        hide_index=True,
    )
    download(cells, f"compare-services-{carrier}", "dl_cmp_many")


def every_insurer(rates: pa.Table, codes: pa.Table | None, chosen: list[compare.Hospital]) -> None:
    left, right = st.columns(2)
    with left:
        label = st.selectbox("Hospital", [h.label for h in chosen], key="cmp_one_hospital")
    with right:
        code = pick_service(codes, "cmp_code_insurers")
    hospital = next(h for h in chosen if h.label == label)
    if not code:
        return
    rows = compare.by_insurer(rates, hospital, compare.carriers(rates), code)
    st.subheader(f"{compare.service_name(code, view.describe(codes, code))} at {label}, by insurer")
    published = [r for r in rows if r["rate"] is not None]
    if not published:
        st.info("This hospital publishes no rate for this service from these insurers.")
        return
    st.altair_chart(bar_chart(published, "carrier", "Rate"))
    st.dataframe(
        [
            {
                "carrier": r["carrier"],
                "rate": compare.money(r["rate"]) or "not published",
                "rates_behind_it": r["rates_behind_it"] or "",
            }
            for r in rows
        ],
        column_config={
            "carrier": "Insurer",
            "rate": st.column_config.TextColumn("Rate", alignment="right"),
            "rates_behind_it": "Rates behind it",
        },
        hide_index=True,
    )


def bar_chart(rows: list[dict[str, Any]], by: str, title: str) -> alt.Chart:
    m = mode()
    lowest = min(r["rate"] for r in rows)
    shown = [{**r, "label": compare.money(r["rate"]), "low": r["rate"] == lowest} for r in rows]
    base = alt.Chart(alt.Data(values=shown)).encode(
        y=alt.Y(f"{by}:N", sort=None, title=None, axis=alt.Axis(labelLimit=320)),
        x=alt.X("rate:Q", title=title, axis=alt.Axis(format="$,.0f")),
    )
    bars = base.mark_bar(height=18).encode(
        color=alt.condition(alt.datum.low, alt.value(INSURER[m]), alt.value(QUIET[m])),
        tooltip=[alt.Tooltip(f"{by}:N"), alt.Tooltip("label:N", title=title)],
    )
    text = base.mark_text(align="left", dx=4, color=INK[m]).encode(text="label:N")
    return (bars + text).properties(height=alt.Step(30)).configure_view(stroke=None)


def heatmap(cells: list[dict[str, Any]], order: list[str]) -> alt.Chart:
    """Services down, hospitals across, colored by distance from each row's middle.

    Cell text is set on the cell's own color, not the page's, so it is the same in
    both themes: dark on the pale middle of the scale, white on the strong ends.
    """
    wrapped = {label: "|".join(textwrap.wrap(label, 20)) for label in order}
    shown = [
        {
            **c,
            "column": wrapped[c["hospital"]],
            "service": compare.SERVICES.get(c["code"], c["code"]),
            "label": (compare.money(c["rate"]) or "—") + (" *" if c["check"] else ""),
            "ratio_clipped": min(max(c["ratio"], 0.33), 3.0) if c["ratio"] else None,
            # Dark cells (far from 1.0 either way) take light text.
            "strong": bool(c["ratio"]) and not 0.55 <= c["ratio"] <= 1.8,
        }
        for c in cells
    ]
    base = alt.Chart(alt.Data(values=shown)).encode(
        x=alt.X(
            "column:N",
            sort=[wrapped[label] for label in order],
            title=None,
            axis=alt.Axis(
                labelAngle=0,
                labelExpr="split(datum.label, '|')",
                labelBaseline="top",
                labelLimit=200,
                labelOverlap=False,
                labelPadding=8,
            ),
        ),
        y=alt.Y("service:N", sort=None, title=None, axis=alt.Axis(labelLimit=300)),
    )
    rects = base.mark_rect().encode(
        color=alt.Color(
            "ratio_clipped:Q",
            title="vs. group",
            scale=alt.Scale(type="log", domain=[0.33, 3.0], domainMid=1, scheme="blueorange"),
            legend=alt.Legend(format=".1f", values=[0.33, 0.5, 1, 2, 3]),
        ),
        tooltip=[
            alt.Tooltip("service:N"),
            alt.Tooltip("hospital:N"),
            alt.Tooltip("label:N", title="Rate"),
            alt.Tooltip("ratio:Q", title="vs. group", format=".2f"),
            alt.Tooltip("check:N"),
        ],
    )
    text = base.mark_text(fontSize=11).encode(
        text="label:N",
        color=alt.condition(alt.datum.strong, alt.value("#FFFFFF"), alt.value(INK["light"])),
    )
    return (
        (rects + text)
        .properties(height=alt.Step(30))
        .configure_view(stroke=None)
        .configure_axis(labelFontSize=11, domain=False, ticks=False)
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


# --- 32BJ ------------------------------------------------------------------------
# Self-contained: its own data, its own sidebar, nothing shared with the pages above,
# so it can move to another entry point as one block. Logic in
# ``pipeline.thirtytwobj_view``; the data is committed in ``summary/32bj``, built by
# ``scripts/32bj``, and the page says so when it is absent.

THIRTYTWOBJ = Path(os.environ.get("RECKONER_32BJ") or Path(__file__).parent / "summary/32bj")


@st.cache_resource(show_spinner="Opening the 32BJ rates…")
def load_32bj() -> bj.Data | None:
    return bj.load(THIRTYTWOBJ)


def thirtytwobj_filters(d: bj.Data) -> bj.Filters:
    st.sidebar.markdown("**32BJ filters**")
    opts = bj.options(d)
    systems = st.sidebar.multiselect(
        "System",
        opts["systems"],
        default=[s for s in bj.DEFAULT_SYSTEMS if s in opts["systems"]],
        format_func=lambda s: bj.SYSTEMS.get(s, s),
        key="bj_systems",
    )
    campuses = st.sidebar.multiselect(
        "Campus",
        bj.options(d, systems)["campuses"],
        key="bj_campuses",
        help="Empty means every campus.",
    )
    if bj.OUTPATIENT_UNDER_REVIEW:
        setting = "inpatient"
        st.sidebar.caption(f"Inpatient only. {bj.OUTPATIENT_NOTE}")
    else:
        setting = st.sidebar.radio(
            "Inpatient or outpatient",
            ["All", "inpatient", "outpatient"],
            horizontal=True,
            key="bj_setting",
        )
    code_types = st.sidebar.multiselect("Code type", opts["code_types"], key="bj_code_types")
    network = st.sidebar.radio(
        "Network",
        [bj.BOTH, bj.TIER, bj.STANDARD],
        key="bj_network",
        help="32BJ tier: Anthem's 32BJ hospital network. Standard: Anthem's HMO POS network.",
    )
    units = st.sidebar.checkbox(
        "Include units and clinics",
        key="bj_units",
        help="Psychiatric, rehabilitation and dialysis units, labs and clinics, besides the "
        "acute campuses.",
    )
    return bj.Filters(
        systems=tuple(systems),
        campuses=tuple(campuses),
        setting=setting,
        code_types=tuple(code_types),
        network=network,
        include_units=units,
    )


def bj_downloads(table: bj.Table, name: str, key: str) -> None:
    a, b = st.columns(2)
    a.download_button(
        "Download Excel",
        bj.to_xlsx({name[:31]: table.rows, "Detail": table.long}),
        file_name=f"32bj-{name}.xlsx",
        key=f"{key}_x",
        disabled=not table.rows,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    b.download_button(
        "Download CSV",
        bj.to_csv(table.rows),
        file_name=f"32bj-{name}.csv",
        mime="text/csv",
        key=f"{key}_c",
        disabled=not table.rows,
    )


def bj_show(table: bj.Table) -> None:
    if not table.rows:
        st.info("No rates for these codes under these filters.")
        return
    st.dataframe(
        table.rows,
        column_config={
            "volume": st.column_config.NumberColumn("Volume", format="localized"),
            "description": st.column_config.TextColumn("Description", width="medium"),
        },
        hide_index=True,
    )


def thirtytwobj_page() -> None:
    st.title("32BJ hospital rates")
    st.caption("32BJ Health Fund's Anthem tier beside standard Anthem rates, per hospital campus.")
    try:
        d = load_32bj()
    except ImportError:
        st.warning(
            "The 32BJ page needs DuckDB, which this install does not have "
            "(`pip install -e .[thirtytwobj]`)."
        )
        return
    if d is None:
        st.warning(
            f"The 32BJ data is not present here (looked in `{THIRTYTWOBJ}`). It is committed in "
            "`summary/32bj` and rebuilt by `scripts/32bj`."
        )
        return
    filters = thirtytwobj_filters(d)

    st.markdown("#### What this shows")
    for point in bj.summary_points(d):
        st.markdown(f"- {point}")
    if d.crosscheck:
        st.caption(d.crosscheck.get("sentence", ""))

    with st.expander("32BJ ÷ standard, per campus"):
        disc = bj.discount_summary(d, filters)
        st.dataframe(disc, hide_index=True)

    st.markdown("#### Choose codes")
    pasted = st.text_area(
        "Paste codes",
        height=120,
        key="bj_paste",
        placeholder="One per line, or a column copied from Excel. Two columns (revenue code, "
        "CPT) are read as pairs.",
    )
    labels = {f"{t} {c} · {desc}"[:120]: (t, c) for (t, c), desc in sorted(d.codes.items())}
    picked = st.multiselect(
        "Or pick codes", list(labels), key="bj_pick", placeholder="Search by code or description"
    )
    paste = bj.parse_paste(pasted or "")
    res = bj.resolve(paste, d.codes.keys(), [labels[p] for p in picked])

    if paste.entries or picked:
        a, b, c, e, f = st.columns(5)
        a.metric("Pasted", paste.pasted)
        b.metric("Found", len(paste.entries) - len(res.not_found))
        c.metric("Not found", len(res.not_found))
        e.metric("Duplicates", paste.duplicates)
        f.metric("Ambiguous", len(res.ambiguous))
        if paste.split_note:
            st.caption(f"Read as APR-DRG with severity: {', '.join(paste.split_note[:10])}")
        if res.ambiguous:
            st.warning(
                "Some codes match more than one kind of code. Every match is shown, flagged "
                "here, not guessed."
            )
            st.dataframe(res.ambiguous, hide_index=True)
        if res.not_found:
            with st.expander(f"Not found ({len(res.not_found)})"):
                st.write(", ".join(res.not_found[:500]))
                st.download_button(
                    "Download not-found codes",
                    "\n".join(res.not_found).encode(),
                    file_name="32bj-not-found.csv",
                    mime="text/csv",
                    key="bj_nf",
                )
        extra = {k: {"pasted RC": rc} for k, rc in res.rc_for.items()}
        table = bj.wide(d, res.found, filters, extra)
        if res.rc_for:
            for r in table.long:
                r["RC link"] = bj.rc_label(r, res.rc_for.get((r["code_type"], r["code"])))
        st.markdown(f"#### Your codes ({len(res.found)})")
        if bj.OUTPATIENT_UNDER_REVIEW:
            st.info(
                f"{bj.OUTPATIENT_NOTE} Outpatient codes you pasted or picked show no rates "
                "until then."
            )
        bj_show(table)
        bj_downloads(table, "your-codes", "bj_dl_yours")
        if res.rc_for:
            st.caption(
                "Revenue-code pairs: each cell, and the RC link column in the Excel detail "
                f"sheet, says whether the CPT rate is a {bj.RC_LISTED!r}, "
                f"{bj.RC_NOT_LISTED!r} or {bj.RC_NOT_SPECIFIC!r}."
            )
        all_rows = table.long
    else:
        all_rows = []
        for name in bj.visible_tables():
            title = bj.TOP_TABLES[name][0]
            if filters.setting == "outpatient" and name != "outpatient":
                continue
            if filters.setting == "inpatient" and name == "outpatient":
                continue
            table = bj.top_table(d, name, filters)
            st.markdown(f"#### Top 25 · {title}")
            st.caption(bj.top_caption(d, name))
            bj_show(table)
            bj_downloads(table, name, f"bj_dl_{name}")
            all_rows += table.long
        if bj.OUTPATIENT_UNDER_REVIEW:
            st.markdown("#### Top 25 · Outpatient · CPT/HCPCS")
            st.info(bj.OUTPATIENT_NOTE)

    st.download_button(
        "Download findings workbook (summary, definitions, data)",
        bj.findings_workbook(d, all_rows),
        file_name="32bj-findings.xlsx",
        key="bj_findings",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("#### Other kinds of rate")
    st.caption(
        "Kept apart from the dollar tables: they are not comparable with a case rate or a fee."
    )
    for kind, label in (
        ("per diem", "Per diem (per day)"),
        ("percentage", "Percent of charges"),
        ("rc", "Revenue-code rates"),
    ):
        with st.expander(label):
            rows = bj.other_methods(d, filters, kind)
            st.dataframe(rows[:2000], hide_index=True)
            st.download_button(
                "Download CSV",
                bj.to_csv(rows),
                file_name=f"32bj-{kind.replace(' ', '-')}.csv",
                mime="text/csv",
                key=f"bj_other_{kind}",
                disabled=not rows,
            )

    st.markdown("#### Definitions")
    for term, meaning in bj.DEFINITIONS:
        st.markdown(f"**{term}.** {meaning}")
    st.markdown("#### Where the data comes from")
    for term, meaning in bj.SOURCES:
        st.markdown(f"**{term}.** {meaning}")
    st.markdown("#### Limits")
    for limit in bj.LIMITS:
        st.markdown(f"- {limit}")


COMPARE = st.Page(compare_page, title="Compare hospitals", icon=":material/balance:", default=True)
RANKINGS = st.Page(
    rankings_page, title="Rankings", icon=":material/leaderboard:", url_path="rankings"
)
PAIR = st.Page(pair_page, title="Pair detail", icon=":material/compare_arrows:", url_path="pair")
LOOKUP = st.Page(lookup_page, title="Code lookup", icon=":material/search:", url_path="code")
COVERAGE = st.Page(
    coverage_page, title="Coverage and data quality", icon=":material/rule:", url_path="coverage"
)

THIRTYTWOBJ_PAGE = st.Page(
    thirtytwobj_page, title="32BJ", icon=":material/apartment:", url_path="32bj"
)

if not data.tables:
    st.error("No summary dataset found in `summary/`.")
    st.stop()

st.navigation([COMPARE, RANKINGS, PAIR, LOOKUP, COVERAGE, THIRTYTWOBJ_PAGE]).run()
