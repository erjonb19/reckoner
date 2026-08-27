"""Parser tests for cms-hpt.txt.

The inline samples below cover the shapes the parser is written to survive. They
are placeholders: per CLAUDE.md, parser tests must be built from real files, and
`test_real_fixtures.py` asserts against whatever the first crawl captures into
tests/fixtures/cms_hpt/.
"""

from pathlib import Path

from discovery.cms_hpt import parse_cms_hpt

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cms_hpt"

CANONICAL = """\
location-name: Example Hospital
source-page-url: https://example.org/patients/pricing
mrf-url: https://example.org/files/123456789_example-hospital_standardcharges.json
contact-name: Jane Roe
contact-email: pricing@example.org
"""


def test_parses_canonical_layout():
    doc = parse_cms_hpt(CANONICAL)

    assert doc.source_format == "key-value"
    assert doc.warnings == ()
    assert len(doc.records) == 1

    record = doc.records[0]
    assert record.location_name == "Example Hospital"
    assert record.source_page_url == "https://example.org/patients/pricing"
    assert record.mrf_url.endswith("_example-hospital_standardcharges.json")
    assert record.contact_email == "pricing@example.org"
    assert record.is_usable


def test_key_variants_normalise():
    """Underscores, casing, spacing and quoted values all collapse to one form."""
    doc = parse_cms_hpt(
        'Location_Name: "Example Hospital"\n'
        "MRF URL: https://example.org/mrf.json\n"
        "Hospital Contact Email: pricing@example.org\n"
    )

    assert doc.records[0].location_name == "Example Hospital"
    assert doc.records[0].mrf_url == "https://example.org/mrf.json"
    assert doc.records[0].contact_email == "pricing@example.org"


def test_colon_in_value_is_preserved():
    doc = parse_cms_hpt("mrf-url: https://example.org:8443/a/b.json\n")
    assert doc.records[0].mrf_url == "https://example.org:8443/a/b.json"


def test_blank_line_separates_locations():
    doc = parse_cms_hpt(
        "location-name: North Campus\n"
        "mrf-url: https://example.org/north.json\n"
        "\n"
        "location-name: South Campus\n"
        "mrf-url: https://example.org/south.json\n"
    )

    assert len(doc.records) == 2
    assert doc.mrf_urls == ("https://example.org/north.json", "https://example.org/south.json")


def test_repeated_key_without_blank_line_starts_new_record():
    doc = parse_cms_hpt(
        "location-name: North Campus\n"
        "mrf-url: https://example.org/north.json\n"
        "location-name: South Campus\n"
        "mrf-url: https://example.org/south.json\n"
    )

    assert len(doc.records) == 2
    assert doc.records[1].location_name == "South Campus"


def test_json_object_form():
    doc = parse_cms_hpt(
        '{"location-name": "Example Hospital", "mrf-url": "https://example.org/mrf.json"}'
    )

    assert doc.source_format == "json"
    assert doc.records[0].mrf_url == "https://example.org/mrf.json"


def test_json_array_and_wrapper_forms():
    array = parse_cms_hpt('[{"mrf-url": "https://example.org/a.json"}]')
    wrapper = parse_cms_hpt('{"locations": [{"mrf-url": "https://example.org/a.json"}]}')

    assert array.mrf_urls == ("https://example.org/a.json",)
    assert wrapper.mrf_urls == ("https://example.org/a.json",)


def test_unknown_key_warns_but_keeps_record():
    doc = parse_cms_hpt("mrf-url: https://example.org/mrf.json\nlicense-number: 1234\n")

    assert doc.records[0].mrf_url == "https://example.org/mrf.json"
    assert doc.records[0].extra["licensenumber"] == "1234"
    assert any("license-number" in w for w in doc.warnings)


def test_malformed_input_warns_rather_than_raising():
    """Quarantine, never hard-fail: garbage in produces warnings, not exceptions."""
    empty = parse_cms_hpt("   \n")
    prose = parse_cms_hpt("Please see our pricing page for details.\n")
    broken_json = parse_cms_hpt('{"mrf-url": ')

    assert empty.records == () and empty.warnings
    assert prose.records == () and prose.warnings
    assert broken_json.records == () and broken_json.warnings


def test_bom_and_crlf_are_tolerated():
    doc = parse_cms_hpt("﻿location-name: Example\r\nmrf-url: https://example.org/mrf.json\r\n")

    assert doc.records[0].location_name == "Example"
    assert doc.records[0].mrf_url == "https://example.org/mrf.json"
