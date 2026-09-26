"""Run every page of the analyst app, without Streamlit and without a browser.

The same reason as ``test_streamlit_app``: Streamlit runs a page only when
someone opens it, so an error on the third page is invisible to a health check.
The fake navigation runs all four pages in turn, and records what they drew.
"""

from __future__ import annotations

import contextlib
import csv
import io
import runpy
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline import analyst_view as view

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "analyst_app.py"


class Recorder:
    def __init__(self, drawn: list[str]) -> None:
        self._drawn = drawn

    def __call__(self, *args: object, **kwargs: object) -> Recorder:
        self._drawn.extend(arg for arg in args if isinstance(arg, str))
        return self

    def __getattr__(self, name: str) -> Recorder:
        return self

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self) -> Iterator[object]:
        return iter(())


class QueryParams(dict[str, str]):
    def to_dict(self) -> dict[str, str]:
        return dict(self)

    def from_dict(self, values: dict[str, str]) -> None:
        self.clear()
        self.update(values)


class Switched(Exception):
    pass


class FakeStreamlit(ModuleType):
    def __init__(self, drawn: list[str], choices: dict[str, str], query: dict[str, str]) -> None:
        super().__init__("streamlit")
        self._drawn, self._choices = drawn, choices
        self.sidebar = self
        self.session_state: dict[str, object] = {}
        self.query_params = QueryParams(query)
        self.context = SimpleNamespace(theme=SimpleNamespace(type="light"))
        self.switched: list[dict[str, str]] = []
        self.tables: list[list[dict[str, object]]] = []
        self.styles: list[str] = []

    def __getattr__(self, name: str) -> object:
        drawn, choices = self._drawn, self._choices
        if name == "columns":
            return lambda spec, **_: [
                Recorder(drawn) for _ in range(spec if isinstance(spec, int) else len(spec))
            ]
        if name == "dataframe":

            def dataframe(data: list[object], **_: object) -> None:
                drawn.append(f"dataframe rows={len(data)}")
                self.tables.append(list(data))  # type: ignore[arg-type]

            return dataframe
        if name in ("cache_data", "cache_resource"):
            return lambda fn=None, **_: fn if fn else (lambda f: f)
        if name == "selectbox":

            def selectbox(label: str, options: list[str], key: str = "", **_: object) -> str:
                value = choices.get(label, self.session_state.get(key, options[0]))
                assert value in options, f"{label}: {value!r} is not a choice"
                return str(value)

            return selectbox
        if name == "markdown":

            def markdown(body: str = "", **kwargs: object) -> None:
                if kwargs.get("unsafe_allow_html"):
                    self.styles.append(body)
                drawn.append(body)

            return markdown
        if name == "text_input":
            return lambda label, value="", **_: choices.get(label, value)
        if name == "segmented_control":
            return lambda label, default=None, **_: choices.get(label, default)
        if name == "Page":
            return lambda fn, **kw: SimpleNamespace(fn=fn, **kw)
        if name == "navigation":

            def navigation(pages: list[SimpleNamespace], **_: object) -> SimpleNamespace:
                def run() -> None:
                    for page in pages:
                        drawn.append(f"page={page.title}")
                        with contextlib.suppress(Switched):
                            page.fn()

                return SimpleNamespace(run=run)

            return navigation
        if name == "switch_page":

            def switch_page(page: object, query_params: dict[str, str] | None = None) -> None:
                self.switched.append(query_params or {})
                raise Switched

            return switch_page
        if name == "stop":

            def stop(*_: object, **__: object) -> None:
                raise RuntimeError("st.stop() was called")

            return stop
        return Recorder(drawn)


def render(
    choices: dict[str, str] | None = None, query: dict[str, str] | None = None
) -> tuple[list[str], FakeStreamlit]:
    drawn: list[str] = []
    fake = FakeStreamlit(drawn, choices or {}, query or {})
    saved = sys.modules.get("streamlit")
    sys.modules["streamlit"] = fake
    try:
        runpy.run_path(str(APP), run_name="__not_main__")
    finally:
        if saved is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = saved
    return drawn, fake


def a_release(tmp_path: Path) -> Callable[..., view.ReleaseFile]:
    rates = pa.table(
        {
            "system": ["White Plains"] * 2,
            "facility": ["White Plains Hospital"] * 2,
            "carrier": ["Aetna", "Cigna"],
            "code": ["70450", "70450"],
            "code_type": ["CPT", "CPT"],
            "hospital_rate": [1000.0, 800.0],
            "compared": [1, 0],
            "payer_min": [900.0, None],
            "payer_median": [1200.0, None],
            "payer_max": [1500.0, None],
            "explanation_before_offsets": ["unexplained", ""],
            "refusal": ["", "tic_exempt_product"],
        }
    )
    codes = pa.table(
        {"code_type": ["CPT"], "code": ["70450"], "description": ["CT HEAD W/O CONTRAST"]}
    )
    for name, table in (("rates", rates), ("codes", codes)):
        pq.write_table(table, tmp_path / f"{name}.parquet")

    def fetch(metadata: object, name: str, *_: object, **__: object) -> view.ReleaseFile:
        return view.ReleaseFile(tmp_path / name)

    return fetch


FACILITY, SYSTEM = "White Plains Hospital", "White Plains"


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture(autouse=True)
def summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The committed summary, plus facility-grain rows it may not have yet.

    The page tests once ran only where summary/pairs.csv existed, which on main
    is nowhere until the October run: every page test skipped, in CI too.
    """
    target = tmp_path / "summary"
    shutil.copytree(ROOT / "summary", target, ignore=shutil.ignore_patterns("*.parquet"))
    write_csv(
        target / "pairs.csv",
        [
            {
                "system": SYSTEM,
                "facility": FACILITY,
                "carrier": carrier,
                "hospital_rates": rates,
                "compared": compared,
                "material": 3,
                "unexplained_material": unexplained,
                "median_signed_gap": gap,
                "summed_abs_gap_usd": 900.0 if compared else 0.0,
                "inside_range_share": share,
                "like_class_share": 0.5,
                "hospital_slug": "wp",
            }
            for carrier, rates, compared, unexplained, gap, share in (
                ("Cigna", 100, 40, 12, 0.08, 0.4),
                ("Aetna", 80, 30, 2, -0.01, 0.6),
                ("Fidelis Care", 20, 0, 0, None, None),
            )
        ],
    )
    write_csv(
        target / "residual.csv",
        [
            {
                "system": SYSTEM,
                "facility": FACILITY,
                "carrier": "Cigna",
                "code": "70450",
                "code_type": "CPT",
                "setting": "outpatient",
                "billing_class": "facility",
                "hospital_plan": "LocalPlus",
                "payer_plan": "LocalPlus",
                "hospital_rate": 1000.0,
                "payer_rate": 1400.0,
                "difference": 400.0,
                "ratio": 1.4,
                "relative_difference": 0.4,
                "is_implausible": False,
                "hospital_vintage": "2026-04-01",
                "payer_vintage": "2026-08-01",
                "notes": "",
                "payer_min": 1200.0,
                "payer_max": 1500.0,
                "payer_count": 3,
                "inside_payer_range": False,
                "hospital_slug": "wp",
            }
        ],
    )
    # Outcomes gain a facility column with the facility-grain build; add it,
    # empty for every committed row, as a system-grain partition reads.
    with (target / "outcomes.csv").open(newline="", encoding="utf-8") as handle:
        outcomes = [{"facility": "", **row} for row in csv.DictReader(handle)]
    outcomes.append(
        {
            **{k: "" for k in outcomes[0]},
            "system": SYSTEM,
            "facility": FACILITY,
            "carrier": "Cigna",
            "code_type": "CPT",
            "explanation": "unexplained",
            "pairs": 12,
            "material_pairs": 12,
            "hospital_slug": "wp",
        }
    )
    write_csv(target / "outcomes.csv", outcomes)
    monkeypatch.setenv("RECKONER_SUMMARY", str(target))
    return target


class TestEveryPageRuns:
    def test_all_four_pages_execute_against_the_committed_dataset(self, tmp_path, monkeypatch):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))

        drawn, _ = render({"Code or description": "70450"})

        pages = [t for t in drawn if t.startswith("page=")]
        assert pages == [
            "page=Rankings",
            "page=Pair detail",
            "page=Code lookup",
            "page=Coverage and data quality",
        ]
        assert sum("How to read this" in t for t in drawn) == 4

    def test_the_default_ranking_is_the_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))

        drawn, _ = render()

        assert not any("never spend" in t for t in drawn), "the volume note is for the toggle"

    def test_the_gap_toggle_says_the_files_carry_no_volumes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))

        drawn, _ = render({"Rank by": "gap"})

        assert any("Neither file publishes how often" in t for t in drawn)

    def test_a_failed_release_says_so_and_the_other_pages_still_run(self, monkeypatch):
        monkeypatch.setattr(
            view,
            "fetch_release_file",
            lambda *a, **k: view.ReleaseFile(None, "could not download rates.parquet: URLError"),
        )

        drawn, _ = render({"Code or description": "70450"})

        assert any(t.startswith("Rate-level data unavailable") for t in drawn)
        assert drawn[-1] != "page=Code lookup", "coverage still rendered after it"
        assert "page=Coverage and data quality" in drawn

    def test_a_shared_link_narrows_the_page(self, tmp_path, monkeypatch, summary):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))
        data = view.load(summary)
        pair = data.table("pairs")[0]

        drawn, fake = render(
            query={"facility": str(pair["facility"]), "carrier": str(pair["carrier"])}
        )

        assert fake.query_params["facility"] == pair["facility"]
        assert any(str(pair["facility"]) in t and str(pair["carrier"]) in t for t in drawn)

    def test_a_filter_the_data_does_not_hold_falls_back_to_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))

        _, fake = render(query={"system": "Nowhere General"})

        assert "system" not in fake.query_params


def test_the_theme_ships_both_modes():
    import tomllib

    theme = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))[
        "theme"
    ]

    assert theme["light"]["backgroundColor"] == "#F7F6F2"
    assert theme["dark"]["backgroundColor"] == "#15181E"


def test_the_app_needs_nothing_the_live_page_does_not_install():
    """Community Cloud installs requirements.txt only. Streamlit brings pyarrow and altair."""
    text = io.StringIO((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    wanted = {line.split(">")[0].split("=")[0].strip() for line in text if line[:1].isalpha()}

    assert wanted == {"streamlit"}


class TestTheRoughEdges:
    def test_no_cell_on_screen_reads_none(self, tmp_path, monkeypatch):
        """Streamlit printed an empty numeric cell as the word "None"."""
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))

        _, fake = render({"Code or description": "70450"})

        nullable = ("gap", "payer_median", "median_signed_gap", "inside_range_share")
        for table in fake.tables:
            for row in table:
                assert None not in [row[k] for k in nullable if k in row], row

    def test_an_uncompared_row_is_blank_and_a_compared_one_formatted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))

        _, fake = render({"Code or description": "70450"})

        lookup = next(t for t in fake.tables if t and "why" in t[0])
        by_carrier = {r["carrier"]: r for r in lookup}
        assert by_carrier["Cigna"]["gap"] == "" and by_carrier["Cigna"]["payer_median"] == ""
        assert by_carrier["Aetna"]["gap"] == "+20.0%"
        assert by_carrier["Aetna"]["payer_median"] == "$1,200.00"


def test_the_page_loads_the_serif_for_headings():
    """config.toml's "Name:url" fonts were named but never loaded (measured)."""
    drawn: list[str] = []
    fake = FakeStreamlit(drawn, {}, {})
    saved = sys.modules.get("streamlit")
    sys.modules["streamlit"] = fake
    try:
        with contextlib.suppress(RuntimeError):
            runpy.run_path(str(APP), run_name="__not_main__")
    finally:
        if saved is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = saved

    css = "\n".join(fake.styles)
    assert "family=Source+Serif+4" in css and "family=Inter+Tight" in css
    assert 'h1, h2, h3, h4 { font-family: "Source Serif 4"' in css
