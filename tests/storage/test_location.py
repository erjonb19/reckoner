"""Storage seam tests (ADR 0002).

The seam's value is entirely in what it does *not* do: there is no second
implementation to drift, so a dataset opened through it must be the same dataset
opened without it. That is the first test here and it runs on a real Parquet
file rather than a mock, because a seam that is transparent against a stub and
not against data is worth nothing.

The rest guard the two ways this could quietly go wrong: falling back to local
when the cloud was asked for, which would compute the right-looking number from
the wrong dataset; and reading a credential, which this module must never do.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest

import storage
from storage import ADLS, ADLS_ACCOUNT, ADLS_ROOT, LOCAL, STORAGE_MODE, StorageConfigError, resolve

REAL = Path(__file__).parent.parent / "fixtures" / "payer_parquet" / "Emblem_HIPHOSH00687.parquet"


class TestTheSeamIsTransparent:
    def test_a_dataset_through_the_seam_matches_one_without(self, tmp_path):
        """The property the whole design rests on: one code path, two filesystems."""
        pq.write_table(pq.read_table(REAL), tmp_path / "f.parquet")
        direct = ds.dataset(str(tmp_path / "f.parquet"), format="parquet")

        location = storage.local(tmp_path)
        through = ds.dataset(
            location.child("f.parquet").root,
            filesystem=location.filesystem,
            format="parquet",
        )

        assert through.count_rows() == direct.count_rows() == 20
        assert through.schema == direct.schema

    def test_the_local_default_needs_no_configuration(self):
        assert resolve(Path("data/lake"), env={}).is_local

    def test_an_explicit_local_mode_is_local(self):
        assert resolve(Path("x"), env={STORAGE_MODE: LOCAL}).is_local


class TestItRefusesRatherThanFallsBack:
    def test_adls_without_an_account_raises(self):
        """Falling back to local would compute a right-looking number from the wrong data."""
        with pytest.raises(StorageConfigError, match=ADLS_ACCOUNT):
            resolve(env={STORAGE_MODE: ADLS})

    def test_adls_without_a_root_raises(self):
        with pytest.raises(StorageConfigError, match=ADLS_ROOT):
            resolve(env={STORAGE_MODE: ADLS, ADLS_ACCOUNT: "acct"})

    def test_an_unknown_mode_raises_rather_than_defaulting(self):
        with pytest.raises(StorageConfigError, match="not a storage mode"):
            resolve(env={STORAGE_MODE: "onelake"})

    def test_a_misconfigured_run_never_silently_reads_the_laptop(self):
        for env in ({STORAGE_MODE: ADLS}, {STORAGE_MODE: ADLS, ADLS_ACCOUNT: "acct"}):
            with pytest.raises(StorageConfigError):
                resolve(Path("data/lake"), env=env)


class TestAdlsIsConstructedWithoutASecret:
    def test_only_the_account_name_is_needed(self):
        """AzureFileSystem falls back to DefaultAzureCredential, which lives outside."""
        location = resolve(
            env={STORAGE_MODE: ADLS, ADLS_ACCOUNT: "acct", ADLS_ROOT: "lake/curated"}
        )

        assert isinstance(location.filesystem, pafs.AzureFileSystem)
        assert not location.is_local
        assert location.root == "lake/curated"

    def test_the_local_root_is_ignored_in_the_cloud(self):
        """A laptop path has no meaning in a storage account."""
        location = resolve(
            Path("C:/Users/someone/data"),
            env={STORAGE_MODE: ADLS, ADLS_ACCOUNT: "acct", ADLS_ROOT: "lake"},
        )

        assert location.root == "lake"

    def test_this_module_reads_no_credential(self):
        """A guard, not a formality: nothing here may learn a key or a token.

        Authentication is DefaultAzureCredential's job, outside this process. If
        a future change starts reading a secret, this fails and says so.
        """
        source = Path(storage.__file__).read_text(encoding="utf-8").casefold()
        banned = ("account_key", "sas_token", "client_secret", "connection_string", "password")

        offending = [
            name for name in banned if f'"{name}"' in source or f"environ.get({name}" in source
        ]
        assert offending == [], f"credential material referenced: {offending}"


class TestPaths:
    def test_child_joins_with_the_only_delimiter_adls_accepts(self):
        location = resolve(env={STORAGE_MODE: ADLS, ADLS_ACCOUNT: "a", ADLS_ROOT: "lake"}).child(
            "curated", "hospital_rates"
        )

        assert location.root == "lake/curated/hospital_rates"
        assert "\\" not in location.root

    def test_child_tolerates_stray_slashes(self):
        base = resolve(env={STORAGE_MODE: ADLS, ADLS_ACCOUNT: "a", ADLS_ROOT: "lake/"})

        assert base.child("/curated/").root == "lake/curated"

    def test_child_with_nothing_is_the_same_place(self):
        base = storage.local("/tmp/x")

        assert base.child().root == base.root

    def test_describe_says_which_filesystem(self):
        assert storage.local("/tmp/x").describe().startswith(f"{LOCAL}:")
        assert (
            "Azure"
            in resolve(env={STORAGE_MODE: ADLS, ADLS_ACCOUNT: "a", ADLS_ROOT: "lake"}).describe()
        )


class TestTheEntryPointsAcceptALocation:
    """Both readers must give the same dataset with the seam and without it."""

    def test_open_payer_dataset_matches_itself_through_the_seam(self, tmp_path):
        from payer.curated import discover_payer_files, open_payer_dataset

        pq.write_table(pq.read_table(REAL), tmp_path / "Emblem_HIPHOSH00687.parquet")
        files = discover_payer_files(tmp_path)

        without = open_payer_dataset(files)
        through = open_payer_dataset(files, location=storage.local(tmp_path))

        assert through.count_rows() == without.count_rows() == 20
        assert through.schema == without.schema

    def test_open_curated_matches_itself_through_the_seam(self, tmp_path):
        from reconcile.curated import open_curated

        curated = tmp_path / "curated" / "hospital_rates"
        curated.mkdir(parents=True)
        pq.write_table(pq.read_table(REAL), curated / "part.parquet")

        without = open_curated(tmp_path)
        through = open_curated(tmp_path, location=storage.local(tmp_path))

        assert through.count_rows() == without.count_rows() == 20

    def test_a_missing_dataset_is_reported_with_where_it_looked(self, tmp_path):
        from reconcile.curated import open_curated

        with pytest.raises(FileNotFoundError, match="local:"):
            open_curated(tmp_path, location=storage.local(tmp_path / "nowhere"))
