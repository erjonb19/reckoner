"""Parse every real cms-hpt.txt captured from a live crawl.

CLAUDE.md requires parser tests built from real files rather than from the CMS
spec. Fixtures are captured with:

    python -m discovery.cli --hospitals config/hospitals.yml --save-fixtures tests/fixtures/cms_hpt

Files that are genuine cms-hpt.txt documents sit in tests/fixtures/cms_hpt/ and
must yield a usable record. Files that are real-world deviations sit in
deviations/ and must only parse without raising -- they are the quarantine path,
not the happy path. Both suites skip while their directory is empty so a fresh
clone stays green.
"""

from pathlib import Path

import pytest

from discovery.cms_hpt import parse_cms_hpt

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cms_hpt"
REAL_FILES = sorted(FIXTURES.glob("*.txt"))
DEVIATIONS = sorted((FIXTURES / "deviations").glob("*.txt"))


@pytest.mark.skipif(not REAL_FILES, reason="no real cms-hpt.txt fixtures captured yet")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_file_yields_a_usable_record(path):
    doc = parse_cms_hpt(path.read_text(encoding="utf-8"))

    assert doc.records, f"{path.name}: no records parsed"
    assert doc.mrf_urls, f"{path.name}: no mrf-url extracted"
    assert all(url.startswith("http") for url in doc.mrf_urls), f"{path.name}: relative mrf-url"


@pytest.mark.skipif(not REAL_FILES, reason="no real cms-hpt.txt fixtures captured yet")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.stem)
def test_real_file_parses_without_unexplained_noise(path):
    """A conforming file should not trip the tolerant paths in the parser."""
    doc = parse_cms_hpt(path.read_text(encoding="utf-8"))

    assert len(doc.warnings) <= len(doc.records), (
        f"{path.name}: {len(doc.warnings)} warnings for {len(doc.records)} records: "
        f"{doc.warnings[:3]}"
    )


@pytest.mark.skipif(not DEVIATIONS, reason="no deviation fixtures captured yet")
@pytest.mark.parametrize("path", DEVIATIONS, ids=lambda p: p.stem)
def test_deviation_quarantines_rather_than_raising(path):
    """Quarantine, never hard-fail: a non-conforming file warns and yields nothing."""
    doc = parse_cms_hpt(path.read_text(encoding="utf-8"))

    assert doc.warnings, f"{path.name}: expected warnings for a non-conforming file"
    assert not doc.mrf_urls, f"{path.name}: unexpectedly produced an mrf-url"
