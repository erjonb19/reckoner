"""SPARCS discharge volumes and costs, by facility and APR-DRG severity.

This is what turns a rate difference into a dollar figure. A 40% variance on a
service performed twice a year is noise; 3% on the highest-volume DRG is the
negotiation. Until volume is attached, the pipeline can say prices differ but
not what that is worth.

The same dataset carries `mean_cost`, which is what makes margin possible: a
rate is only good or bad relative to what delivering the service costs.

One caveat that must travel with every figure derived from here: SPARCS runs
years behind the rate schedules. The volumes are a case-mix *shape*, reliable
for weighting, and not a claim about this year's activity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from model.scenarios import CaseMix, CaseMixEntry
from reconcile.provenance import Provenance

SPARCS_DATASET = "7dtz-qxmr"
SPARCS_URL = f"https://health.data.ny.gov/resource/{SPARCS_DATASET}.json"
USER_AGENT = "reckoner/0.1 (price transparency research)"

#: Socrata caps a page at 50k rows.
PAGE_SIZE = 50_000


@dataclass(frozen=True)
class FacilityVolume:
    """Discharges and cost for one facility, APR-DRG and severity."""

    pfi: str
    facility: str
    year: int
    apr_drg: str
    severity: int
    discharges: int
    mean_cost: float = 0.0
    mean_charge: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.apr_drg}-{self.severity}"

    @property
    def total_cost(self) -> float:
        return self.mean_cost * self.discharges


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def parse_rows(rows: Iterable[dict[str, Any]]) -> list[FacilityVolume]:
    """Turn Socrata JSON into typed volumes, skipping unusable rows."""
    volumes = []
    for row in rows:
        drg = str(row.get("apr_drg_code") or "").strip()
        severity = str(row.get("apr_severity_of_illness_code") or "").strip()
        if not drg.isdigit() or severity not in {"1", "2", "3", "4"}:
            continue
        discharges = int(_number(row.get("discharges")))
        if discharges <= 0:
            continue
        volumes.append(
            FacilityVolume(
                pfi=str(row.get("pfi") or "").strip(),
                facility=str(row.get("facility_name") or "").strip(),
                year=int(_number(row.get("year"))),
                apr_drg=drg.zfill(3),
                severity=int(severity),
                discharges=discharges,
                mean_cost=_number(row.get("mean_cost")),
                mean_charge=_number(row.get("mean_charge")),
            )
        )
    return volumes


def fetch(
    year: int,
    client: httpx.Client | None = None,
    facility_names: list[str] | None = None,
    max_pages: int = 20,
) -> list[FacilityVolume]:
    """Page the SPARCS API for one year, optionally filtered to facilities."""
    owned = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=180.0)
    volumes: list[FacilityVolume] = []
    try:
        for page in range(max_pages):
            params: dict[str, Any] = {
                "$limit": PAGE_SIZE,
                "$offset": page * PAGE_SIZE,
                "$where": f"year='{year}'",
                "$order": "pfi, apr_drg_code, apr_severity_of_illness_code",
            }
            if facility_names:
                quoted = ", ".join(f"'{name}'" for name in facility_names)
                params["$where"] += f" AND facility_name IN ({quoted})"
            response = client.get(SPARCS_URL, params=params)
            response.raise_for_status()
            rows = response.json()
            if not rows:
                break
            volumes.extend(parse_rows(rows))
            if len(rows) < PAGE_SIZE:
                break
    finally:
        if owned:
            client.close()
    return volumes


@dataclass
class VolumeTable:
    """Discharge volumes and costs, queryable by facility."""

    volumes: list[FacilityVolume] = field(default_factory=list)
    year: int = 0

    def __post_init__(self) -> None:
        self._by_pfi: dict[str, list[FacilityVolume]] = {}
        for volume in self.volumes:
            self._by_pfi.setdefault(volume.pfi, []).append(volume)
        if not self.year and self.volumes:
            self.year = max(v.year for v in self.volumes)

    @property
    def facilities(self) -> dict[str, str]:
        return {v.pfi: v.facility for v in self.volumes}

    def for_facility(self, pfi: str) -> list[FacilityVolume]:
        return self._by_pfi.get(str(pfi).strip(), [])

    def total_discharges(self, pfi: str) -> int:
        return sum(v.discharges for v in self.for_facility(pfi))

    def case_mix(
        self,
        pfi: str,
        drg_codes: set[str] | None = None,
        average_days: float = 1.0,
    ) -> tuple[CaseMix, Provenance]:
        """Build a case mix from real discharges, with its provenance.

        ``drg_codes`` narrows to a service line. The provenance records what that
        narrowing excluded, so a service-line figure never looks like a total.
        """
        rows = self.for_facility(pfi)
        provenance = Provenance(hospitals=1 if rows else 0)
        provenance.add_source(f"SPARCS {SPARCS_DATASET}", vintage=str(self.year), rows=len(rows))
        provenance.extra_caveats.append(
            f"discharge volumes are SPARCS {self.year}; a case-mix shape, not current activity"
        )

        entries = []
        kept = 0
        for volume in rows:
            if drg_codes is not None and volume.apr_drg not in drg_codes:
                provenance.exclude("out of scope for this service line", volume.discharges)
                continue
            entries.append(
                CaseMixEntry(
                    apr_drg=volume.apr_drg,
                    severity=volume.severity,
                    cases=volume.discharges,
                    average_days=average_days,
                )
            )
            kept += volume.discharges

        provenance.rows = kept
        total = self.total_discharges(pfi)
        provenance.coverage = kept / total if total else None
        name = self.facilities.get(str(pfi).strip(), pfi)
        return CaseMix(f"{name} SPARCS {self.year}", entries), provenance

    def cost_index(self, pfi: str) -> dict[str, float]:
        """Mean cost per discharge, keyed by APR-DRG and severity."""
        return {v.key: v.mean_cost for v in self.for_facility(pfi) if v.mean_cost > 0}

    def charge_index(self, pfi: str) -> dict[str, float]:
        """Mean *charges* per discharge, keyed by APR-DRG and severity.

        Needed to test the high-cost outlier. Without charges the payment model
        never fires an outlier, which understates what Medicaid actually pays on
        exactly the expensive cases outliers exist to cover.
        """
        return {v.key: v.mean_charge for v in self.for_facility(pfi) if v.mean_charge > 0}

    def save(self, path: Path) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for volume in self.volumes:
                handle.write(json.dumps(volume.__dict__, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, path: Path) -> VolumeTable:
        import json

        with path.open(encoding="utf-8") as handle:
            volumes = [FacilityVolume(**json.loads(line)) for line in handle if line.strip()]
        return cls(volumes)


#: Discharges by facility and payer type -- the denominator that stops an
#: all-payer volume being priced at a single payer's rates.
PAYER_MIX_DATASET = "ivw2-k53g"


@dataclass(frozen=True)
class PayerMix:
    """Share of a facility's discharges by payer type.

    Without this, pricing a facility's whole discharge volume at Medicaid rates
    silently answers a question nobody asked: Maimonides is 52% Medicaid, not
    100%, so an unweighted figure overstates Medicaid exposure by about 2x.
    """

    facility: str
    year: int
    discharges_by_payer: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.discharges_by_payer.values())

    def share(self, payer: str) -> float:
        if not self.total:
            return 0.0
        wanted = payer.strip().casefold()
        for name, count in self.discharges_by_payer.items():
            if name.strip().casefold() == wanted:
                return count / self.total
        return 0.0


def fetch_payer_mix(
    year: int, facility_like: str, client: httpx.Client | None = None
) -> PayerMix | None:
    """Discharges by payer type for one facility, from the SPARCS facility file."""
    owned = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120.0)
    try:
        response = client.get(
            f"https://health.data.ny.gov/resource/{PAYER_MIX_DATASET}.json",
            params={
                "$select": "hospital_name, type_of_insurance, sum(number_of_discharges) as d",
                "$where": (f"discharge_year='{year}' AND hospital_name like '%{facility_like}%'"),
                "$group": "hospital_name, type_of_insurance",
            },
        )
        response.raise_for_status()
        rows = response.json()
    except httpx.HTTPError:
        return None
    finally:
        if owned:
            client.close()

    if not rows:
        return None
    by_payer: dict[str, int] = {}
    for row in rows:
        payer = str(row.get("type_of_insurance") or "").strip()
        by_payer[payer] = by_payer.get(payer, 0) + int(_number(row.get("d")))
    return PayerMix(str(rows[0].get("hospital_name") or facility_like), year, by_payer)
