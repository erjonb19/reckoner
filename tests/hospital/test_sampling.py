from hospital.profile import MrfProfile
from hospital.sampling import find_header_offset, parse_window, sample_csv, window_starts

HEADER = (
    "description,code|1,payer_name,plan_name,"
    "standard_charge|negotiated_dollar,standard_charge|negotiated_algorithm,"
    "standard_charge|negotiated_percentage,standard_charge|methodology"
)


def ordered_csv(dollar_rows: int, algorithm_rows: int) -> bytes:
    """A file whose dollar rows all precede its algorithm rows.

    This is the shape that makes a head-capped read lie: read the front and the
    file looks 100% dollar-denominated.
    """
    lines = ["hospital_name,last_updated_on", "Example Hospital,2026-04-01", HEADER]
    lines += [
        f"Svc {i},70450,Aetna,Commercial,{100 + i}.00,,,fee schedule" for i in range(dollar_rows)
    ]
    lines += [f"Svc {i},70450,Aetna,Commercial,,per contract,,other" for i in range(algorithm_rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def fetcher(blob: bytes):
    def fetch(start: int, end: int) -> bytes:
        return blob[start : end + 1]

    return fetch


class TestWindowStarts:
    def test_small_file_reads_from_one_window(self):
        assert window_starts(1000, body_start=100, windows=8, window_bytes=1024) == [100]

    def test_windows_are_evenly_spread_across_the_body(self):
        starts = window_starts(10_000, body_start=0, windows=4, window_bytes=100)

        assert len(starts) == 4
        assert starts == [0, 2500, 5000, 7500]

    def test_windows_start_after_the_header(self):
        starts = window_starts(10_000, body_start=400, windows=4, window_bytes=100)

        assert starts[0] == 400
        assert all(s >= 400 for s in starts)

    def test_degenerate_inputs(self):
        assert window_starts(0) == []
        assert window_starts(100, body_start=100) == []


class TestParseWindow:
    def test_leading_and_trailing_fragments_are_dropped(self):
        blob = b"tial line\nwhole one\nwhole two\npartial"

        assert parse_window(blob, at_body_start=False) == ["whole one", "whole two"]

    def test_first_window_keeps_its_opening_line(self):
        blob = b"first line\nsecond line\npartial"

        assert parse_window(blob, at_body_start=True) == ["first line", "second line"]


class TestFindHeaderOffset:
    def test_offset_points_at_the_first_data_row(self):
        blob = ordered_csv(2, 0)
        header, offset = find_header_offset(blob)

        assert header is not None
        assert header[2] == "payer_name"
        assert blob[offset:].startswith(b"Svc 0,")

    def test_non_ascii_before_the_header_does_not_shift_the_offset(self):
        """Byte offsets, not character offsets -- an accented name breaks the latter."""
        blob = ordered_csv(2, 0).replace(b"Example Hospital", "Hôpital Exampleé".encode())
        header, offset = find_header_offset(blob)

        assert header is not None
        assert blob[offset:].startswith(b"Svc 0,")


class TestSampleCsv:
    def test_spread_sampling_sees_both_halves_of_an_ordered_file(self):
        blob = ordered_csv(4000, 4000)
        profile = MrfProfile(url="x")

        sample_csv(fetcher(blob), len(blob), profile, windows=16, window_bytes=4096)

        assert profile.value_kind["dollar"] > 0
        assert profile.value_kind["algorithm"] > 0
        share = profile.value_kind["dollar"] / profile.rate_lines
        assert 0.35 < share < 0.65, f"expected ~50% dollar, got {share:.1%}"

    def test_a_head_only_read_of_the_same_file_is_badly_biased(self):
        """Demonstrates the bias the windowed sampler exists to remove."""
        blob = ordered_csv(4000, 4000)
        profile = MrfProfile(url="x")

        sample_csv(fetcher(blob), len(blob), profile, windows=1, window_bytes=4096)

        assert profile.value_kind["dollar"] == profile.rate_lines
        assert profile.value_kind["algorithm"] == 0

    def test_small_file_is_read_exactly(self):
        blob = ordered_csv(10, 10)
        profile = MrfProfile(url="x")

        sample_csv(fetcher(blob), len(blob), profile, windows=16, window_bytes=1 << 20)

        assert profile.rate_lines == 20
        assert profile.value_kind["dollar"] == 10
        assert not profile.truncated

    def test_missing_header_is_reported(self):
        profile = MrfProfile(url="x")

        sample_csv(fetcher(b"a,b,c\n1,2,3\n"), 12, profile)

        assert profile.error is not None
