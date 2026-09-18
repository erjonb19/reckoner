"""Screenshot every view of the local page, and record the filters in use.

Run against a locally served app rather than the deployed one. The deployed URL
sleeps on idle and wakes on the next visit, so a screenshot run against it
captures a loading spinner as often as a page -- and a screenshot of a spinner
is worse than no screenshot, because it looks like the app.

    streamlit run streamlit_app.py --server.headless true --server.port 8599 &
    python scripts/shoot_streamlit.py --port 8599 --out docs/img

Streamlit renders over a websocket after the HTML arrives, so every wait here is
for *content* -- a known string, a settled table -- never for a fixed number of
seconds. A sleep long enough to be reliable on this machine would be too short
on a slower one and would fail by capturing a half-drawn page, which is the kind
of failure that ships.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

try:
    from playwright.sync_api import Page, sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - the shooter is a dev tool
    print("playwright is not installed: pip install playwright && playwright install chromium")
    raise SystemExit(1) from None

#: Tab label -> file stem. The order the page shows them in.
VIEWS = (
    ("Coverage", "coverage"),
    ("Outcomes", "outcomes"),
    ("Magnitude", "magnitude"),
    ("Widest gaps", "widest-gaps"),
    ("Refusals", "refusals"),
)

VIEWPORT = {"width": 1500, "height": 1000}

#: Generous, because a cold Streamlit start compiles and connects before it
#: draws anything. Exceeded means broken, not slow.
READY_TIMEOUT_MS = 60_000


def settle(page: Page) -> None:
    """Wait for Streamlit to finish drawing, not for the clock."""
    page.wait_for_load_state("networkidle")
    # The status widget is present while a script run is in flight. Absent or
    # hidden means the run finished; waiting on it beats waiting on time.
    with contextlib.suppress(PlaywrightTimeout):
        page.wait_for_selector("[data-testid='stStatusWidget']", state="hidden", timeout=15_000)


def shoot(page: Page, out: Path, stem: str) -> Path:
    target = out / f"{stem}.png"
    page.screenshot(path=str(target), full_page=True)
    print(f"  {target.relative_to(out.parent.parent)}")
    return target


def select(page: Page, label: str, value: str) -> None:
    """Choose a sidebar filter by its visible label."""
    box = page.get_by_label(label, exact=True)
    box.click()
    page.get_by_role("option", name=value, exact=True).click()
    settle(page)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8599)
    parser.add_argument("--out", type=Path, default=Path("docs/img"))
    parser.add_argument("--frames", type=Path, default=None, help="where to drop GIF frames")
    args = parser.parse_args()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    frames: Path = args.frames or (out / "_frames")
    frames.mkdir(parents=True, exist_ok=True)
    url = f"http://localhost:{args.port}"

    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        page.goto(url, wait_until="domcontentloaded")
        try:
            # Wait on the page's own words. If the title never arrives, the app
            # did not render and every screenshot after this would be a blank.
            page.get_by_text("Reckoner", exact=False).first.wait_for(timeout=READY_TIMEOUT_MS)
        except PlaywrightTimeout:
            print(f"the app never rendered at {url}; is it running?")
            browser.close()
            return 1
        settle(page)

        print("views:")
        for label, stem in VIEWS:
            page.get_by_role("tab", name=label, exact=True).click()
            settle(page)
            shoot(page, out, stem)

        # The GIF is about the filters, so the frames walk one narrowing and
        # back out again -- a still cannot show that a control does anything.
        print("filter frames:")
        page.get_by_role("tab", name="Outcomes", exact=True).click()
        settle(page)
        steps: list[tuple[str, str]] = [
            ("Health system", "Mount Sinai"),
            ("Carrier", "Aetna"),
            ("Code type", "CPT"),
            ("Code type", "All"),
            ("Carrier", "All"),
            ("Health system", "All"),
        ]
        index = 0
        page.screenshot(path=str(frames / f"{index:02d}.png"), full_page=False)
        for label, value in steps:
            try:
                select(page, label, value)
            except (PlaywrightTimeout, AssertionError) as exc:
                print(f"  could not set {label}={value}: {type(exc).__name__}")
                continue
            index += 1
            page.screenshot(path=str(frames / f"{index:02d}.png"), full_page=False)
            print(f"  {index:02d} {label} = {value}")
        browser.close()

    print(f"\n{len(VIEWS)} views in {out}, {index + 1} frames in {frames}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
