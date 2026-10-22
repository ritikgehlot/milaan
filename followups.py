"""Deterministic supplier follow-up preparation (Task 6).

Pure module: no database access, no network, no LLM and **no sending**.
It turns eligible reconciliation evidence into neutral draft messages and
review metadata. Persistence and approval live in ``db.py``; approval only
marks a draft as eligible for delivery review. This module never transmits
anything; outbound delivery is isolated in ``delivery.py``.

Eligibility is deliberately narrow:

- the invoice must still have ``validation_status == "valid"``;
- its result in the selected batch's latest reconciliation run must be
  exactly ``MISSING_FROM_2B`` with no attached GSTR-2B row, no amount
  difference, method ``none`` and only the ``NO_2B_ROW_FOUND`` reason.

Everything else (MATCHED, AMOUNT_MISMATCH, NEEDS_REVIEW, NOT_ELIGIBLE,
invalid invoices, ambiguous/duplicate evidence) is excluded and reported
with a reason. A missing 2B row does not establish supplier fault, ITC
eligibility or recoverability, and drafts say so explicitly.

Drafts are grouped by supplier_id. Missing phone numbers never cause
invented contact details: the draft is still prepared for review but the
delivery channel is marked unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_DRAFT = "DRAFT"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_SKIPPED = "SKIPPED"

DRAFT_STATUSES = (
    STATUS_DRAFT,
    STATUS_NEEDS_REVIEW,
    STATUS_APPROVED,
    STATUS_SKIPPED,
)

LANGUAGE_EN = "en"

REASON_NO_2B_ROW_FOUND = "NO_2B_ROW_FOUND"
REASON_EVIDENCE_NOT_UNAMBIGUOUS = "EVIDENCE_NOT_UNAMBIGUOUS"
REASON_INVOICE_NOT_VALID = "INVOICE_NOT_VALID"
REASON_UNEXPECTED_ROW_ATTACHED = "UNEXPECTED_ROW_ATTACHED"
REASON_UNEXPECTED_AMOUNT_DIFFERENCE = "UNEXPECTED_AMOUNT_DIFFERENCE"
REASON_UNEXPECTED_MATCH_METHOD = "UNEXPECTED_MATCH_METHOD"
REASON_INVOICE_DETAILS_MISSING = "INVOICE_DETAILS_MISSING"
REASON_CONTACT_PHONE_MISSING = "CONTACT_PHONE_MISSING"
REASON_SUPPLIER_NAME_MISSING = "SUPPLIER_NAME_MISSING"
REASON_SUPPLIER_GSTIN_MISSING = "SUPPLIER_GSTIN_MISSING"
REASON_DUPLICATE_SUPPLIER_GSTIN = "DUPLICATE_SUPPLIER_GSTIN"

VALIDATION_VALID = "valid"
MATCH_MISSING = "MISSING_FROM_2B"
METHOD_NONE = "none"


@dataclass(frozen=True)
class CandidateInvoice:
    invoice_id: int
    invoice_number: str
    invoice_date: str
    tax_period: str
    taxable_value_paise: int
    total_gst_paise: int
    supplier_id: int
    supplier_name: str
    supplier_gstin: str | None
    contact_phone: str | None
    validation_status: str
    result_id: int
    match_status: str
    match_method: str
    gstr2b_row_id: int | None
    amount_difference_paise: int | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class DraftInvoice:
    invoice_id: int
    invoice_number: str
    invoice_date: str
    tax_period: str
    taxable_value_paise: int
    total_gst_paise: int
    reconciliation_result_id: int
    reason: str


@dataclass(frozen=True)
class ExcludedInvoice:
    invoice_id: int
    invoice_number: str
    supplier_id: int
    supplier_name: str
    validation_status: str
    match_status: str
    reason: str


@dataclass(frozen=True)
class DraftPlan:
    supplier_id: int
    supplier_name: str
    supplier_gstin: str | None
    contact_phone: str | None
    channel_available: bool
    status: str
    review_reasons: tuple[str, ...]
    invoices: tuple[DraftInvoice, ...]
    total_taxable_value_paise: int
    total_gst_paise: int
    inclusion_reason: str
    draft_text: str
    language: str = LANGUAGE_EN


def normalize_gstin(value: str | None) -> str | None:
    if value is None:
        return None
    text = "".join(str(value).split()).upper()
    return text or None


def format_rupees(paise: int) -> str:
    """Integer-only rupee rendering, e.g. 1_250_050 -> '12500.50'."""
    if isinstance(paise, bool) or not isinstance(paise, int):
        raise ValueError(f"expected integer paise, got {paise!r}")
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"{sign}{whole}.{fraction:02d}"


def eligibility_problem(candidate: CandidateInvoice) -> str | None:
    """Return the exclusion reason, or None if the invoice is eligible."""
    if candidate.validation_status != VALIDATION_VALID:
        return f"{REASON_INVOICE_NOT_VALID}:{candidate.validation_status}"
    if candidate.match_status != MATCH_MISSING:
        return f"MATCH_STATUS_{candidate.match_status}"
    if candidate.gstr2b_row_id is not None:
        return REASON_UNEXPECTED_ROW_ATTACHED
    if candidate.amount_difference_paise is not None:
        return REASON_UNEXPECTED_AMOUNT_DIFFERENCE
    if candidate.match_method != METHOD_NONE:
        return REASON_UNEXPECTED_MATCH_METHOD
    if candidate.reason_codes != (REASON_NO_2B_ROW_FOUND,):
        return REASON_EVIDENCE_NOT_UNAMBIGUOUS
    if (
        not (candidate.invoice_number or "").strip()
        or not (candidate.invoice_date or "").strip()
        or not (candidate.tax_period or "").strip()
    ):
        return REASON_INVOICE_DETAILS_MISSING
    return None


def _invoice_line(index: int, invoice: DraftInvoice) -> str:
    return (
        f"{index}. Invoice {invoice.invoice_number} dated "
        f"{invoice.invoice_date} (tax period {invoice.tax_period}): "
        f"taxable value Rs {format_rupees(invoice.taxable_value_paise)}, "
        f"GST Rs {format_rupees(invoice.total_gst_paise)}"
    )


def build_draft_text(
    *,
    supplier_name: str,
    supplier_gstin: str | None,
    invoices: tuple[DraftInvoice, ...],
    run_id: int,
    import_id: int,
) -> str:
    """Neutral verification request built only from stored data."""
    lines = [
        f"Dear {supplier_name},",
        "",
        "We are reviewing purchase records for input tax credit "
        "reconciliation. Our records show the following invoice(s) from "
        "you:",
        "",
    ]
    for index, invoice in enumerate(invoices, start=1):
        lines.append(_invoice_line(index, invoice))
    total_gst = sum(invoice.total_gst_paise for invoice in invoices)
    lines.extend([
        "",
        f"Total GST across these invoices: Rs {format_rupees(total_gst)}",
        "",
        "We could not find a matching entry for the invoice(s) above in "
        "the GSTR-2B data we imported "
        f"(batch #{import_id}, reconciliation run #{run_id}). Please "
        "verify whether these invoices were reported in your GST returns "
        "for the relevant tax period(s), and let us know if any invoice "
        "number, date or amount needs correction.",
        "",
        "This is a data-verification request only. It does not imply any "
        "default, and it does not confirm that any input tax credit is "
        "eligible or recoverable. It is not tax advice.",
        "",
        "Regards,",
        "Accounts team",
    ])
    return "\n".join(lines)


def plan_followup_drafts(
    candidates: list[CandidateInvoice],
    *,
    run_id: int,
    import_id: int,
) -> tuple[list[DraftPlan], list[ExcludedInvoice]]:
    """Group eligible candidates by supplier and exclude everything else.

    Deterministic: plans are ordered by supplier_id, invoices by
    invoice_date/number/id.
    """
    excluded: list[ExcludedInvoice] = []
    eligible: list[CandidateInvoice] = []

    for candidate in candidates:
        problem = eligibility_problem(candidate)
        if problem is not None:
            excluded.append(
                ExcludedInvoice(
                    invoice_id=candidate.invoice_id,
                    invoice_number=candidate.invoice_number,
                    supplier_id=candidate.supplier_id,
                    supplier_name=candidate.supplier_name,
                    validation_status=candidate.validation_status,
                    match_status=candidate.match_status,
                    reason=problem,
                )
            )
        else:
            eligible.append(candidate)

    grouped: dict[int, list[CandidateInvoice]] = {}
    for candidate in eligible:
        grouped.setdefault(candidate.supplier_id, []).append(candidate)

    gstin_counts: dict[str, int] = {}
    for supplier_id, group in grouped.items():
        gstin = normalize_gstin(group[0].supplier_gstin)
        if gstin is not None:
            gstin_counts[gstin] = gstin_counts.get(gstin, 0) + 1

    plans: list[DraftPlan] = []
    for supplier_id in sorted(grouped):
        group = sorted(
            grouped[supplier_id],
            key=lambda item: (
                item.invoice_date,
                item.invoice_number,
                item.invoice_id,
            ),
        )
        first = group[0]
        supplier_name = (first.supplier_name or "").strip()
        gstin = normalize_gstin(first.supplier_gstin)
        phone = (first.contact_phone or "").strip()
        channel_available = bool(phone)

        review_reasons: list[str] = []
        if not supplier_name:
            review_reasons.append(REASON_SUPPLIER_NAME_MISSING)
        if gstin is None:
            review_reasons.append(REASON_SUPPLIER_GSTIN_MISSING)
        elif gstin_counts.get(gstin, 0) > 1:
            review_reasons.append(REASON_DUPLICATE_SUPPLIER_GSTIN)
        if not channel_available:
            review_reasons.append(REASON_CONTACT_PHONE_MISSING)

        invoices = tuple(
            DraftInvoice(
                invoice_id=item.invoice_id,
                invoice_number=item.invoice_number,
                invoice_date=item.invoice_date,
                tax_period=item.tax_period,
                taxable_value_paise=item.taxable_value_paise,
                total_gst_paise=item.total_gst_paise,
                reconciliation_result_id=item.result_id,
                reason=f"{MATCH_MISSING}:{REASON_NO_2B_ROW_FOUND}",
            )
            for item in group
        )
        total_taxable = sum(item.taxable_value_paise for item in invoices)
        total_gst = sum(item.total_gst_paise for item in invoices)
        inclusion_reason = (
            f"{MATCH_MISSING} with {REASON_NO_2B_ROW_FOUND} in "
            f"reconciliation run #{run_id} for GSTR-2B batch #{import_id}"
        )
        display_name = supplier_name or "Supplier"
        plans.append(
            DraftPlan(
                supplier_id=supplier_id,
                supplier_name=display_name,
                supplier_gstin=gstin,
                contact_phone=phone or None,
                channel_available=channel_available,
                status=(
                    STATUS_DRAFT if not review_reasons else STATUS_NEEDS_REVIEW
                ),
                review_reasons=tuple(review_reasons),
                invoices=invoices,
                total_taxable_value_paise=total_taxable,
                total_gst_paise=total_gst,
                inclusion_reason=inclusion_reason,
                draft_text=build_draft_text(
                    supplier_name=display_name,
                    supplier_gstin=gstin,
                    invoices=invoices,
                    run_id=run_id,
                    import_id=import_id,
                ),
            )
        )
    return plans, excluded
