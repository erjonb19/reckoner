"""Caching the facility-eligibility answer.

The scan costs a two-column projection over 156M rows — seconds, which is fine
once and wasteful every invocation. The cache makes it once per lake state.

The test that matters most is the stale one. A cache that outlives its lake
would apply the facility assumption to a system that has since started
publishing professional rates, which is the single case the assumption is
plainly wrong for. Serving a stale answer here is worse than being slow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from reconcile.eligibility import (
    CACHE_VERSION,
    EligibilityCache,
    compute,
    facility_only_hospitals,
    lake_vintage,
)


def lake(root: Path, rows: list[tuple[str, str, str]], name: str = "part") -> ds.Dataset:
    root.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "hospital": pa.array([r[0] for r in rows], pa.string()),
                "billing_class": pa.array([r[1] for r in rows], pa.string()),
                "code_type": pa.array([r[2] for r in rows], pa.string()),
            }
        ),
        root / f"{name}.parquet",
    )
    return ds.dataset(root, partitioning="hive")


class TestTheAnswer:
    def test_a_system_with_no_professional_row_is_eligible(self, tmp_path):
        d = lake(tmp_path / "l", [("A", "facility", "CPT"), ("A", "", "CPT")])

        assert facility_only_hospitals(d) == frozenset({"A"})

    def test_the_evidence_is_recorded_not_just_the_verdict(self, tmp_path):
        """ "Why is this system excluded" has to be answerable from the cache."""
        d = lake(tmp_path / "l", [("A", "professional", "CPT")] * 7 + [("B", "facility", "CPT")])

        answer = compute(d)

        by_system = {s.system: s for s in answer.systems}
        assert by_system["A"].professional_rows == 7
        assert by_system["A"].eligible is False
        assert by_system["B"].professional_rows == 0
        assert by_system["B"].eligible is True


class TestTheCache:
    def test_the_first_call_writes_it(self, tmp_path):
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])
        cache = tmp_path / "_meta" / "facility_eligibility.json"

        facility_only_hospitals(d, cache_path=cache)

        assert cache.exists()
        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert stored["vintage"] == lake_vintage(d)
        assert stored["systems"][0] == {
            "system": "A",
            "professional_rows": 0,
            "eligible": True,
        }
        assert stored["computed_at"]

    def test_a_matching_vintage_is_served_from_cache(self, tmp_path):
        """Proved by tampering: the cached answer is returned, not recomputed."""
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])
        cache = tmp_path / "_meta" / "c.json"
        facility_only_hospitals(d, cache_path=cache)

        tampered = EligibilityCache.from_json(cache.read_text(encoding="utf-8"))
        tampered.systems[0] = type(tampered.systems[0])("A", 999, False)
        cache.write_text(tampered.to_json(), encoding="utf-8")

        assert facility_only_hospitals(d, cache_path=cache) == frozenset()

    def test_a_stale_vintage_triggers_a_recompute(self, tmp_path):
        """The case that matters: the lake moved, so the cached answer is void."""
        root = tmp_path / "l"
        d = lake(root, [("A", "facility", "CPT")])
        cache = tmp_path / "_meta" / "c.json"
        assert facility_only_hospitals(d, cache_path=cache) == frozenset({"A"})
        first = json.loads(cache.read_text(encoding="utf-8"))["vintage"]

        # A starts publishing professional rates, in a new file.
        moved = lake(root, [("A", "professional", "CPT")], name="part2")

        assert facility_only_hospitals(moved, cache_path=cache) == frozenset()
        rewritten = json.loads(cache.read_text(encoding="utf-8"))
        assert rewritten["vintage"] != first, "the cache must be re-keyed, not just refreshed"
        assert {s["system"]: s["professional_rows"] for s in rewritten["systems"]}["A"] == 1

    def test_recompute_forces_a_rescan(self, tmp_path):
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])
        cache = tmp_path / "_meta" / "c.json"
        facility_only_hospitals(d, cache_path=cache)
        cache.write_text(
            EligibilityCache(vintage=lake_vintage(d), systems=[]).to_json(), encoding="utf-8"
        )

        assert facility_only_hospitals(d, cache_path=cache) == frozenset()
        assert facility_only_hospitals(d, cache_path=cache, recompute=True) == frozenset({"A"})

    def test_an_older_cache_version_is_ignored(self, tmp_path):
        """Rules change; an answer computed under different ones is not an answer."""
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])
        cache = tmp_path / "_meta" / "c.json"
        stale = EligibilityCache(vintage=lake_vintage(d), cache_version=CACHE_VERSION - 1)
        cache.parent.mkdir(parents=True)
        cache.write_text(stale.to_json(), encoding="utf-8")

        assert facility_only_hospitals(d, cache_path=cache) == frozenset({"A"})

    def test_a_corrupt_cache_is_ignored_rather_than_fatal(self, tmp_path):
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])
        cache = tmp_path / "_meta" / "c.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("{not json", encoding="utf-8")

        assert facility_only_hospitals(d, cache_path=cache) == frozenset({"A"})

    def test_no_cache_path_still_answers(self, tmp_path):
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])

        assert facility_only_hospitals(d) == frozenset({"A"})


class TestItDoesNotMaterialiseTheLake:
    def test_compute_never_calls_to_table(self, tmp_path):
        """The whole lake as one table is several GB, and stage 2 has 4.

        Not a style preference: this ran first in the mart stage and killed the
        container 50 seconds in with no traceback. A guard rather than a comment,
        because the batched and unbatched versions return the same answer and
        nothing else would notice the difference until it ran in production.
        """
        d = lake(tmp_path / "l", [("A", "facility", "CPT")] * 100)

        class NoToTable:
            """Everything a dataset offers except the one call that would OOM."""

            def __init__(self, inner: ds.Dataset) -> None:
                self._inner = inner
                self.files = inner.files

            def scanner(self, **kwargs: object) -> object:
                return self._inner.scanner(**kwargs)

            def to_table(self, **kwargs: object) -> object:
                raise AssertionError(
                    "compute() must stream; to_table holds the whole lake in memory"
                )

        assert compute(NoToTable(d)).eligible == frozenset({"A"})

    def test_batching_gives_the_same_answer_as_one_pass(self, tmp_path):
        import reconcile.eligibility as module

        rows = [("A", "facility", "CPT")] * 50 + [("B", "professional", "CPT")] * 10
        d = lake(tmp_path / "l", rows)

        whole = compute(d)
        original = module.SCAN_BATCH
        try:
            module.SCAN_BATCH = 7
            tiny = compute(d)
        finally:
            module.SCAN_BATCH = original

        assert tiny.eligible == whole.eligible == frozenset({"A"})
        assert {s.system: s.professional_rows for s in tiny.systems} == {
            s.system: s.professional_rows for s in whole.systems
        }


class TestTheFingerprint:
    def test_it_reads_no_rows(self, tmp_path):
        """It guards a 156M-row scan, so it must be cheaper than one."""
        d = lake(tmp_path / "l", [("A", "facility", "CPT")] * 5000)

        assert len(lake_vintage(d)) == 16

    def test_adding_a_file_moves_it(self, tmp_path):
        root = tmp_path / "l"
        before = lake_vintage(lake(root, [("A", "facility", "CPT")]))

        assert lake_vintage(lake(root, [("B", "facility", "CPT")], name="p2")) != before

    def test_the_same_lake_gives_the_same_answer(self, tmp_path):
        d = lake(tmp_path / "l", [("A", "facility", "CPT")])

        assert lake_vintage(d) == lake_vintage(ds.dataset(tmp_path / "l", partitioning="hive"))
