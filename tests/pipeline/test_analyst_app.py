"""Run every page of the analyst app, without Streamlit and without a browser.

The same reason as ``test_streamlit_app``: Streamlit runs a page only when
someone opens it, so an error on the third page is invisible to a health check.
The fake navigation runs all four pages in turn, and records what they drew.
"""

from __future__ import annotations

import contextlib
import io
import runpy
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

    def __getattr__(self, name: str) -> object:
        drawn, choices = self._drawn, self._choices
        if name == "columns":
            return lambda spec, **_: [
                Recorder(drawn) for _ in range(spec if isinstance(spec, int) else len(spec))
            ]
        if name == "dataframe":

            def dataframe(data: list[object], **_: object) -> None:
                drawn.append(f"dataframe rows={len(data)}")

            return dataframe
        if name in ("cache_data", "cache_resource"):
            return lambda fn=None, **_: fn if fn else (lambda f: f)
        if name == "selectbox":

            def selectbox(label: str, options: list[str], key: str = "", **_: object) -> str:
                value = choices.get(label, self.session_state.get(key, options[0]))
                assert value in options, f"{label}: {value!r} is not a choice"
                return str(value)

            return selectbox
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


needs_data = pytest.mark.skipif(
    not (ROOT / "summary" / "pairs.csv").exists(), reason="no facility-grain dataset"
)


@needs_data
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

    def test_a_shared_link_narrows_the_page(self, tmp_path, monkeypatch):
        monkeypatch.setattr(view, "fetch_release_file", a_release(tmp_path))
        data = view.load(ROOT / "summary")
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
