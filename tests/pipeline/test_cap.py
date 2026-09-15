"""Asking Log Analytics whether it is still listening.

The probe must never raise: it is a diagnostic about the telemetry channel, and
failing the pipeline because the diagnostic is unavailable would let a
monitoring question break the thing it monitors.

It must also never say "unknown" without saying why. The first version returned
None for every failure, so a missing library and a refused token were the same
word -- which would leave the cap signal permanently false for an invisible
reason, the exact failure this stage exists to remove.
"""

from __future__ import annotations

from typing import Any, Never

import azure.identity
import httpx
import pytest

from pipeline import cap
from pipeline.cap import OVER_QUOTA, RESPECT_QUOTA, CapProbe, ingestion_status


class TestTheProbeValue:
    def test_over_quota_is_a_cap_hit(self):
        probe = CapProbe(OVER_QUOTA)

        assert probe.cap_hit is True
        assert probe.known is True
        assert str(probe) == OVER_QUOTA

    def test_respecting_the_quota_is_not(self):
        assert CapProbe(RESPECT_QUOTA).cap_hit is False

    def test_unknown_is_not_a_cap_hit(self):
        """Absence of an answer is not evidence of a cap, and must not read as one."""
        probe = CapProbe(None, "http 403")

        assert probe.cap_hit is False
        assert probe.known is False
        assert str(probe) == "unknown"


class TestMissingConfiguration:
    def test_it_names_the_variables_that_are_absent(self, monkeypatch):
        for name in cap.CONFIG_VARS:
            monkeypatch.delenv(name, raising=False)

        probe = ingestion_status()

        assert probe.status is None
        assert probe.detail.startswith("not configured: ")
        for name in cap.CONFIG_VARS:
            assert name in probe.detail

    def test_one_missing_variable_is_enough_to_stop(self, monkeypatch):
        monkeypatch.setenv("RECKONER_SUBSCRIPTION_ID", "sub")
        monkeypatch.setenv("RECKONER_RESOURCE_GROUP", "rg")
        monkeypatch.delenv("RECKONER_LOG_WORKSPACE", raising=False)

        probe = ingestion_status()

        assert probe.detail == "not configured: RECKONER_LOG_WORKSPACE"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("RECKONER_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("RECKONER_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("RECKONER_LOG_WORKSPACE", "ws")


class StubToken:
    token = "stub"


def stub_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class Credential:
        def get_token(self, *scopes: str, **kwargs: object) -> StubToken:
            return StubToken()

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: Credential())


def stub_get(monkeypatch: pytest.MonkeyPatch, *, status_code: int, payload: dict[str, Any]) -> None:
    class Response:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return payload

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())


class TestItNeverRaises:
    def test_a_credential_failure_is_reported_not_raised(self, configured, monkeypatch):
        def boom(*args: object, **kwargs: object) -> Never:
            raise RuntimeError("no managed identity endpoint")

        monkeypatch.setattr(azure.identity, "DefaultAzureCredential", boom)

        probe = ingestion_status()

        assert probe.status is None
        assert probe.detail == "credential failed: RuntimeError"

    def test_a_refused_read_reports_the_status_code(self, configured, monkeypatch):
        """403 means the identity lost Reader -- a misconfiguration, not a blip."""
        stub_token(monkeypatch)
        stub_get(monkeypatch, status_code=403, payload={})

        assert ingestion_status().detail == "http 403"

    def test_a_workspace_without_a_cap_says_so(self, configured, monkeypatch):
        stub_token(monkeypatch)
        stub_get(monkeypatch, status_code=200, payload={"properties": {}})

        assert ingestion_status().detail == "no daily cap configured"

    def test_a_capped_workspace_is_read(self, configured, monkeypatch):
        stub_token(monkeypatch)
        stub_get(
            monkeypatch,
            status_code=200,
            payload={"properties": {"workspaceCapping": {"dataIngestionStatus": OVER_QUOTA}}},
        )

        probe = ingestion_status()

        assert probe.status == OVER_QUOTA
        assert probe.cap_hit is True
        assert probe.detail == ""

    def test_a_network_error_is_reported_not_raised(self, configured, monkeypatch):
        stub_token(monkeypatch)

        def boom(*args: object, **kwargs: object) -> Never:
            raise httpx.ConnectTimeout("no route")

        monkeypatch.setattr(httpx, "get", boom)

        assert ingestion_status().detail == "request failed: ConnectTimeout"
