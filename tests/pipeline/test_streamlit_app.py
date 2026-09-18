"""Run the whole page script, without Streamlit and without a browser.

`streamlit run` starting and answering its health endpoint proves the server
came up, not that the script executes -- Streamlit runs the script per session,
so a typo at the bottom of the file is invisible until someone opens the page.
This executes every line against the real committed dataset with a stubbed UI,
which is how a stray expression got caught before it shipped.

The stub records what was drawn, so the test can also assert the page says the
things it must: the vintages, the caveats, and that two filters do not apply to
refusals.
"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "streamlit_app.py"


class Recorder:
    """Accepts anything Streamlit's API accepts, and remembers the text."""

    def __init__(self, drawn: list[str]) -> None:
        self._drawn = drawn

    def __call__(self, *args: object, **kwargs: object) -> Recorder:
        for arg in args:
            if isinstance(arg, str):
                self._drawn.append(arg)
        return self

    def __getattr__(self, name: str) -> Recorder:
        return self

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self) -> Iterator[object]:
        return iter(())


class FakeStreamlit(ModuleType):
    def __init__(self, drawn: list[str], selections: dict[str, str]) -> None:
        super().__init__("streamlit")
        self._drawn = drawn
        self._selections = selections
        self.sidebar = Recorder(drawn)

    def __getattr__(self, name: str) -> object:
        drawn, selections = self._drawn, self._selections

        if name == "columns":
            return lambda spec, **_: [
                Recorder(drawn) for _ in range(spec if isinstance(spec, int) else len(spec))
            ]
        if name == "tabs":
            return lambda labels, **_: [Recorder(drawn) for _ in labels]
        if name == "cache_data":
            return lambda fn: fn
        if name == "selectbox":
            # Return the caller's choice so a filtered render is exercised too.
            return lambda label, choices, **_: selections.get(label, choices[0])
        if name == "stop":

            def stop(*_: object, **__: object) -> None:
                raise RuntimeError("st.stop() was called")

            return stop
        return Recorder(drawn)


def render(selections: dict[str, str] | None = None) -> list[str]:
    """Execute the page and return everything it drew as text."""
    drawn: list[str] = []
    fake = FakeStreamlit(drawn, selections or {})
    saved = sys.modules.get("streamlit")
    sys.modules["streamlit"] = fake
    try:
        runpy.run_path(str(APP), run_name="__not_main__")
    finally:
        if saved is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = saved
    return drawn


@pytest.mark.skipif(not (ROOT / "summary" / "coverage.csv").exists(), reason="no dataset")
class TestThePageRuns:
    def test_it_executes_end_to_end_against_the_committed_dataset(self):
        """Catches what a health check cannot: an error anywhere in the script."""
        drawn = render()

        assert drawn, "the page drew nothing"
        assert any("Reckoner" in text for text in drawn)

    def test_it_renders_with_every_filter_narrowed(self):
        """The filtered path is a different path and has its own ways to fail."""
        drawn = render({"Health system": "Mount Sinai", "Carrier": "Aetna", "Code type": "CPT"})

        assert drawn

    def test_it_tells_the_reader_to_read_the_share(self):
        drawn = render()

        assert any("comparable share, not the pair count" in text for text in drawn)

    def test_it_states_the_refusals_grain(self):
        """Two of the three controls do nothing on that view; it must say so."""
        from pipeline.summary_view import REFUSALS_GRAIN_NOTE

        drawn = render({"Carrier": "Aetna"})

        assert any(REFUSALS_GRAIN_NOTE[:40] in text for text in drawn)

    def test_it_shows_the_no_phi_caveat(self):
        drawn = render()

        assert any("No PHI" in text for text in drawn)
