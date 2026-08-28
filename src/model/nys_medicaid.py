"""NY Medicaid inpatient payment model (APR-DRG).

Implements the calculation NYS DOH publishes in its "Medicaid FFS and HMO Claims
Payment Calculation" workbook, for both Traditional Fee-for-Service and Medicaid
Managed Care. Three payment paths:

* **Inlier** -- the ordinary case: case-mix adjusted discharge rate, plus DME
  (FFS only) and capital, plus any alternate-level-of-care days.
* **Transfer** -- paid per diem off the inlier amount, capped so a transfer can
  never pay more than the full DRG would have.
* **High-cost outlier** -- paid *in addition* to the inlier when converted costs
  clear an ISAF-adjusted threshold. Does not apply to transfers.

Every result carries its intermediate lines, keyed to the DOH worksheet line
numbers, because a payment model that only returns a total cannot be checked
against the published methodology or explained to anyone who disagrees with it.

Reference: https://www.health.ny.gov/facilities/hospital/reimbursement/apr-drg/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: Indigent Care and Health Care Initiatives surcharge, 4/1/09 forward.
MEDICAID_SURCHARGE = 0.0704

#: DOH Transfer worksheet line 8: 100% where the DRG's statewide average
#: arithmetic inlier length of stay is 1 day, 120% where it exceeds 1.
TRANSFER_FACTOR_SHORT_STAY = 1.0
TRANSFER_FACTOR_STANDARD = 1.2


class Basis(StrEnum):
    FFS = "ffs"
    MMC = "mmc"


class PaymentType(StrEnum):
    INLIER = "inlier"
    TRANSFER = "transfer"
    HIGH_COST_OUTLIER = "high_cost_outlier"


@dataclass(frozen=True)
class RateSchedule:
    """One hospital's published rate row, from PUB_MA_FFS/HMO_Acute."""

    opcert: str
    hospital: str
    discharge_rate: float
    isaf: float
    high_cost_charge_converter: float
    capital_per_discharge: float = 0.0
    capital_per_diem: float = 0.0
    alc_rate: float = 0.0
    dme_rate: float = 0.0
    ime_pct: float = 0.0
    statewide_price: float = 0.0
    #: MMC columns 8-13: non-comparable / directed payment add-ons.
    addons_per_discharge: float = 0.0
    #: MMC columns 12-13: safety net / financially distressed / NYC H+H.
    transfer_addons: float = 0.0
    effective_date: str = ""
    basis: str = Basis.MMC


@dataclass(frozen=True)
class DrgWeight:
    """One APR-DRG at one severity, from the DOH SIW table."""

    apr_drg: str
    severity: int
    siw: float
    alos: float
    cost_outlier_threshold: float
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.apr_drg}-{self.severity}"


@dataclass(frozen=True)
class Claim:
    """The claim-side inputs the model needs."""

    apr_drg: str
    severity: int
    total_days: int = 1
    alc_days: int = 0
    is_transfer: bool = False
    gross_charges: float = 0.0
    non_covered_charges: float = 0.0
    #: Whether the provider signed the surcharge election. When they have, the
    #: surcharge is remitted separately and the hospital is paid the base amount.
    provider_signed_surcharge_election: bool = True

    @property
    def days_excluding_alc(self) -> int:
        return max(self.total_days - self.alc_days, 0)


@dataclass
class Payment:
    """A computed payment, with the worksheet lines that produced it."""

    payment_type: str
    basis: str
    total: float
    lines: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def surcharge(self) -> float:
        return self.lines.get("B_surcharge_amount", 0.0)

    @property
    def paid_to_hospital(self) -> float:
        """DOH lines C and D: the surcharge rides along only when unsigned."""
        return self.total + self.surcharge if self.lines.get("unsigned") else self.total


def inlier_payment(claim: Claim, rate: RateSchedule, weight: DrgWeight, basis: Basis) -> Payment:
    """DOH 'Inlier' worksheet."""
    line1 = rate.discharge_rate
    line2 = weight.siw
    line3 = line1 * line2
    # DME is an FFS-only add-on; the MMC discharge rate already excludes IME.
    line4 = rate.dme_rate if basis is Basis.FFS else 0.0
    line5 = rate.capital_per_discharge + (rate.addons_per_discharge if basis is Basis.MMC else 0.0)
    line6 = line3 + line4 + line5
    line7c = rate.alc_rate * claim.alc_days
    line8 = line6 + line7c

    return _with_surcharge(
        Payment(
            payment_type=PaymentType.INLIER,
            basis=str(basis),
            total=line8,
            lines={
                "1_discharge_rate": line1,
                "2_siw": line2,
                "3_case_mix_adjusted": line3,
                "4_dme": line4,
                "5_capital": line5,
                "6_inlier_drg": line6,
                "7c_alc": line7c,
                "8_total_with_alc": line8,
            },
        ),
        claim,
    )


def transfer_payment(claim: Claim, rate: RateSchedule, weight: DrgWeight, basis: Basis) -> Payment:
    """DOH 'Transfer' worksheet, capped at the inlier amount."""
    if weight.alos <= 0:
        raise ValueError(f"{weight.key}: transfer payment needs a positive ALOS")

    line5 = rate.discharge_rate * weight.siw
    line6 = weight.alos
    line7 = line5 / line6
    line8 = TRANSFER_FACTOR_SHORT_STAY if weight.alos <= 1 else TRANSFER_FACTOR_STANDARD
    line9 = line7 * line8
    line10 = rate.capital_per_diem
    line11 = line9 + line10
    line12 = line11 * claim.days_excluding_alc
    line13 = rate.dme_rate if basis is Basis.FFS else 0.0
    line14 = rate.transfer_addons if basis is Basis.MMC else 0.0
    line15 = line12 + line13 + line14

    inlier = inlier_payment(claim, rate, weight, basis)
    line16a = inlier.lines["6_inlier_drg"]
    # A transfer must never pay more than the full DRG would have.
    line17 = min(line15, line16a)
    line18 = inlier.lines["7c_alc"]
    line19 = line17 + line18

    payment = Payment(
        payment_type=PaymentType.TRANSFER,
        basis=str(basis),
        total=line19,
        lines={
            "1c_days_excluding_alc": float(claim.days_excluding_alc),
            "5_case_mix_adjusted": line5,
            "6_alos": line6,
            "7_avg_inlier_cost_per_day": line7,
            "8_transfer_adjustment_factor": line8,
            "9_transfer_cost_per_day": line9,
            "10_capital_per_diem": line10,
            "11_total_per_diem": line11,
            "12_transfer_before_addons": line12,
            "15_transfer_before_cap": line15,
            "16a_inlier_drg": line16a,
            "17_capped": line17,
            "18_alc": line18,
            "19_total_with_alc": line19,
        },
        notes=["capped at inlier"] if line15 > line16a else [],
    )
    return _with_surcharge(payment, claim)


def high_cost_outlier_payment(
    claim: Claim, rate: RateSchedule, weight: DrgWeight, basis: Basis
) -> Payment | None:
    """DOH 'High Cost' worksheet. Returns None when the case does not qualify.

    Outlier payment is *in addition* to the inlier, and does not apply to
    transfers.
    """
    if claim.is_transfer:
        return None

    line3 = claim.gross_charges - claim.non_covered_charges
    line4 = rate.high_cost_charge_converter
    line5 = line3 * line4
    line6a = weight.cost_outlier_threshold
    line6b = rate.isaf
    line6c = line6a * line6b
    if line5 <= line6c:
        return None

    line8 = line5 - line6c
    inlier = inlier_payment(claim, rate, weight, basis)
    line9 = inlier.lines["8_total_with_alc"]
    line10 = line8 + line9

    return _with_surcharge(
        Payment(
            payment_type=PaymentType.HIGH_COST_OUTLIER,
            basis=str(basis),
            total=line10,
            lines={
                "3_net_charges": line3,
                "4_charge_converter": line4,
                "5_converted_costs": line5,
                "6a_threshold": line6a,
                "6b_isaf": line6b,
                "6c_adjusted_threshold": line6c,
                "8_outlier_before_inlier": line8,
                "9_inlier_with_alc": line9,
                "10_total": line10,
            },
        ),
        claim,
    )


def calculate(claim: Claim, rate: RateSchedule, weight: DrgWeight, basis: Basis) -> Payment:
    """The payment a claim actually receives, choosing the right path.

    Transfers pay per diem (capped). Otherwise the inlier applies, topped up by
    a high-cost outlier where costs clear the adjusted threshold.
    """
    if claim.is_transfer:
        return transfer_payment(claim, rate, weight, basis)
    outlier = high_cost_outlier_payment(claim, rate, weight, basis)
    return outlier if outlier is not None else inlier_payment(claim, rate, weight, basis)


def _with_surcharge(payment: Payment, claim: Claim) -> Payment:
    amount = payment.total * MEDICAID_SURCHARGE
    payment.lines["A_surcharge_rate"] = MEDICAID_SURCHARGE
    payment.lines["B_surcharge_amount"] = amount
    payment.lines["unsigned"] = 0.0 if claim.provider_signed_surcharge_election else 1.0
    return payment
