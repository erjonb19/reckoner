"""Whether Log Analytics has stopped accepting telemetry.

A daily cap makes absence ambiguous: no records could mean nothing ran, or it
could mean the workspace stopped listening. That ambiguity is exactly what a
monitoring system must not have, so the job asks rather than infers, and carries
the answer on the record whose truncation it would explain.

Read through ARM with the job's managed identity, which holds Reader on the
workspace. No key, no connection string.

**An unknown answer carries its reason.** The first version returned ``None`` for
every failure, so a missing library, a refused token and a workspace with no cap
configured all printed the same word. That is the same silent-misconfiguration
shape the job is meant to remove: the probe would report ``unknown`` forever and
nothing would say why, which would leave ``log_cap_hit`` permanently false for a
reason no one could see.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ARM = "https://management.azure.com"
API_VERSION = "2023-09-01"

#: Log Analytics reports this when a daily cap has stopped ingestion. A capped
#: day is the one case where absence of telemetry means something other than
#: "nothing happened", so it is worth naming rather than inferring from silence.
OVER_QUOTA = "OverQuota"

#: ...and this when a cap is configured and has not been reached.
RESPECT_QUOTA = "RespectQuota"

CONFIG_VARS = ("RECKONER_SUBSCRIPTION_ID", "RECKONER_RESOURCE_GROUP", "RECKONER_LOG_WORKSPACE")


@dataclass(frozen=True)
class CapProbe:
    """What the workspace said, and — when it said nothing — why."""

    status: str | None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.status is not None

    @property
    def cap_hit(self) -> bool:
        return self.status == OVER_QUOTA

    def __str__(self) -> str:
        return self.status or "unknown"


def ingestion_status(timeout: float = 10.0) -> CapProbe:
    """Ask ARM whether the workspace is still accepting data.

    Never raises. This is a diagnostic about the telemetry channel; failing the
    pipeline because the diagnostic is unavailable would let a monitoring
    question break the thing it monitors. The failure is reported instead.
    """
    missing = [name for name in CONFIG_VARS if not os.environ.get(name)]
    if missing:
        return CapProbe(None, f"not configured: {', '.join(missing)}")

    try:
        import httpx
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - the image installs both
        return CapProbe(None, f"dependency missing: {exc.name}")

    try:
        token = DefaultAzureCredential().get_token(f"{ARM}/.default").token
    except Exception as exc:
        return CapProbe(None, f"credential failed: {type(exc).__name__}")

    url = (
        f"{ARM}/subscriptions/{os.environ['RECKONER_SUBSCRIPTION_ID']}"
        f"/resourceGroups/{os.environ['RECKONER_RESOURCE_GROUP']}"
        f"/providers/Microsoft.OperationalInsights/workspaces"
        f"/{os.environ['RECKONER_LOG_WORKSPACE']}?api-version={API_VERSION}"
    )
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except Exception as exc:
        return CapProbe(None, f"request failed: {type(exc).__name__}")

    if response.status_code != 200:
        # 403 here is the interesting one: it means the identity lost Reader on
        # the workspace, which is a misconfiguration and not a transient error.
        return CapProbe(None, f"http {response.status_code}")

    try:
        capping = response.json().get("properties", {}).get("workspaceCapping") or {}
    except ValueError:
        return CapProbe(None, "response was not json")

    status = capping.get("dataIngestionStatus")
    if not status:
        return CapProbe(None, "no daily cap configured")
    return CapProbe(str(status))


__all__ = [
    "API_VERSION",
    "ARM",
    "CONFIG_VARS",
    "OVER_QUOTA",
    "RESPECT_QUOTA",
    "CapProbe",
    "ingestion_status",
]
