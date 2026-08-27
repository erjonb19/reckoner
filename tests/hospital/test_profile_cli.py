import json

from hospital.profile import MrfProfile
from hospital.profile_cli import _serialise


def _profile() -> MrfProfile:
    profile = MrfProfile(url="https://example.org/mrf.json", layout="json", rate_lines=0)
    profile.record("Aetna", "All Commercial Plans", "fee schedule", dollar="412.55")
    profile.record("Humana", "Medicare Managed Care Plan", "other", algorithm="per contract")
    return profile


def test_serialised_row_is_json_encodable():
    """dataclasses.asdict turns a Counter into tuple keys, which JSON rejects."""
    row = _serialise("Example Hospital", "example.org", _profile())

    encoded = json.dumps(row)
    restored = json.loads(encoded)

    assert restored["pairs"]["Aetna || All Commercial Plans"] == 1
    assert restored["product_class"]["medicare_advantage"] == 1
    assert restored["value_kind"] == {"dollar": 1, "algorithm": 1}


def test_serialised_counter_keys_are_strings():
    row = _serialise("Example Hospital", "example.org", _profile())

    for key in ("pairs", "methodology", "value_kind", "product_class"):
        assert all(isinstance(k, str) for k in row[key]), key


def test_identity_fields_are_carried_through():
    row = _serialise("Example Hospital", "example.org", _profile())

    assert row["hospital"] == "Example Hospital"
    assert row["domain"] == "example.org"
    assert row["url"] == "https://example.org/mrf.json"
    assert row["rate_lines"] == 2
