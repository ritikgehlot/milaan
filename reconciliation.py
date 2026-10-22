"""Deterministic invoice-to-GSTR-2B reconciliation (Task 5).

Pure module: no database access, no LLM, no network. It compares one
validated purchase invoice at a time against the accepted rows of one
GSTR-2B import batch and returns an auditable result for every invoice.

Matching policy (documented in README):

1. Only invoices whose stored ``validation_status`` is ``"valid"`` are
   eligible. Everything else is reported as ``NOT_ELIGIBLE`` with a
   specific reason code; invalid or unreviewed invoices are never
   silently treated as valid.
2. Only GSTR-2B rows with ``validation_status == "valid"`` are used as
   candidates. Rejected rows stay visible in the import audit trail but
   never become matches.
3. Match keys are the normalized supplier GSTIN, the normalized invoice
   number and the normalized tax period:
   - GSTIN: whitespace removed, uppercased.
   - Invoice number: leading/trailing whitespace stripped, internal
     whitespace runs collapsed to one space, then uppercased. No other
     characters are removed, because punctuation may be meaningful and
     removing it could merge distinct invoices.
   - Tax period: whitespace stripped; ``YYYY-MM`` and ``YYYY/MM`` are
     canonicalized to ``YYYY-MM``. A period that cannot be canonicalized
     makes the invoice ineligible for matching. Records from different
     tax periods are never compared; there is no cross-period fallback.
4. Exact-match-first: same key and single candidate. If the total GST is
   equal, the invoice is ``MATCHED``. If a positive tolerance is
   configured and the absolute difference is within it, the invoice is
   still ``MATCHED`` but the method is ``exact_tolerance`` and the signed
   difference is stored. Beyond the tolerance it is ``AMOUNT_MISMATCH``.
   The default tolerance is zero paise: the project compares GST amounts
   exactly and this module does not invent a tolerance.
5. If no exact-number candidate exists, a constrained candidate search
   considers rows with the same GSTIN and tax period whose total GST is
   within tolerance. A single such row is reported as ``NEEDS_REVIEW``
   (``candidate_amount``); it is never auto-accepted. Multiple rows are
   ``NEEDS_REVIEW`` with ``AMBIGUOUS_CANDIDATES``. No candidate at all is
   ``MISSING_FROM_2B``.
6. Multiple exact candidates (duplicate GSTR-2B rows after
   normalization) are ``NEEDS_REVIEW`` with
   ``DUPLICATE_GSTR2B_CANDIDATES``; no candidate is selected. Multiple
   eligible purchase invoices sharing a match key are ``NEEDS_REVIEW``
   with ``DUPLICATE_PURCHASE_INVOICE``.

Amount differences are signed ``invoice.total_gst - row.total_gst`` in
integer paise. A missing row proves only that this batch has no usable
counterpart: it does NOT prove supplier non-filing, ITC eligibility or
recoverability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STATUS_MATCHED = "MATCHED"
STATUS_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
STATUS_MISSING_FROM_2B = "MISSING_FROM_2B"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"

MATCH_STATUSES = (
    STATUS_MATCHED,
    STATUS_AMOUNT_MISMATCH,
    STATUS_MISSING_FROM_2B,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_ELIGIBLE,
)

METHOD_EXACT = "exact"
METHOD_EXACT_TOLERANCE = "exact_tolerance"
METHOD_CANDIDATE_AMOUNT = "candidate_amount"
METHOD_NONE = "none"
METHOD_INELIGIBLE = "ineligible"

MATCH_METHODS = (
    METHOD_EXACT,
    METHOD_EXACT_TOLERANCE,
    METHOD_CANDIDATE_AMOUNT,
    METHOD_NONE,
    METHOD_INELIGIBLE,
)

# Reason codes stored alongside each result (JSON array in SQLite).
REASON_INVOICE_INVALID = "INVOICE_INVALID"
REASON_INVOICE_NEEDS_REVIEW = "INVOICE_NEEDS_REVIEW"
REASON_INVOICE_PENDING = "INVOICE_PENDING"
REASON_INVOICE_NOT_VALID = "INVOICE_NOT_VALID"
REASON_MISSING_MATCH_KEY = "MISSING_MATCH_KEY"
REASON_DUPLICATE_PURCHASE_INVOICE = "DUPLICATE_PURCHASE_INVOICE"
REASON_DUPLICATE_GSTR2B_CANDIDATES = "DUPLICATE_GSTR2B_CANDIDATES"
REASON_AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
REASON_CANDIDATE_NOT_EXACT_INVOICE_NUMBER = "CANDIDATE_NOT_EXACT_INVOICE_NUMBER"
REASON_AMOUNT_WITHIN_TOLERANCE = "AMOUNT_WITHIN_TOLERANCE"
REASON_AMOUNT_DIFFERENCE_BEYOND_TOLERANCE = "AMOUNT_DIFFERENCE_BEYOND_TOLERANCE"
REASON_NO_2B_ROW_FOUND = "NO_2B_ROW_FOUND"

VALIDATION_VALID = "valid"

_TAX_PERIOD_RE = re.compile(r"^(\d{4})[-/](\d{2})$")


@dataclass(frozen=True)
class ReconciliationConfig:
    """Matching configuration. Tolerance is integer paise, default zero."""

    amount_tolerance_paise: int = 0

    def __post_init__(self) -> None:
        value = self.amount_tolerance_paise
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                "amount_tolerance_paise must be a non-negative integer "
                f"(paise), got {value!r}"
            )


@dataclass(frozen=True)
class InvoiceRecord:
    invoice_id: int
    supplier_gstin: str | None
    invoice_number: str
    tax_period: str | None
    total_gst: int
    validation_status: str


@dataclass(frozen=True)
class Gstr2bRecord:
    row_id: int
    supplier_gstin: str | None
    invoice_number: str
    tax_period: str | None
    total_gst: int | None
    validation_status: str


@dataclass(frozen=True)
class MatchResult:
    invoice_id: int
    match_status: str
    match_method: str
    reason_codes: tuple[str, ...]
    gstr2b_row_id: int | None = None
    amount_difference_paise: int | None = None
    invoice_number: str = ""
    supplier_gstin: str | None = None
    tax_period: str | None = None
    invoice_total_gst_paise: int = 0
    gstr2b_invoice_number: str | None = None
    gstr2b_total_gst_paise: int | None = None


def normalize_gstin(value: str | None) -> str | None:
    """Uppercase and remove whitespace; None/blank stays None."""
    if value is None:
        return None
    text = "".join(str(value).split()).upper()
    return text or None


def normalize_invoice_number(value: str | None) -> str | None:
    """Trim, collapse internal whitespace and uppercase.

    No characters are removed: punctuation is preserved so distinct
    invoice numbers cannot be merged by normalization.
    """
    if value is None:
        return None
    text = " ".join(str(value).split()).upper()
    return text or None


def normalize_tax_period(value: str | None) -> str | None:
    """Canonicalize ``YYYY-MM`` / ``YYYY/MM`` to ``YYYY-MM``.

    Returns None for blank, malformed or impossible month values.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    match = _TAX_PERIOD_RE.fullmatch(text)
    if match is None:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def _ineligible_reason(status: str) -> str:
    return {
        "invalid": REASON_INVOICE_INVALID,
        "needs_review": REASON_INVOICE_NEEDS_REVIEW,
        "pending": REASON_INVOICE_PENDING,
    }.get(status, REASON_INVOICE_NOT_VALID)


def _result(
    invoice: InvoiceRecord,
    status: str,
    method: str,
    reasons: tuple[str, ...],
    *,
    row: Gstr2bRecord | None = None,
    difference: int | None = None,
) -> MatchResult:
    return MatchResult(
        invoice_id=invoice.invoice_id,
        match_status=status,
        match_method=method,
        reason_codes=reasons,
        gstr2b_row_id=row.row_id if row is not None else None,
        amount_difference_paise=difference,
        invoice_number=invoice.invoice_number,
        supplier_gstin=normalize_gstin(invoice.supplier_gstin),
        tax_period=normalize_tax_period(invoice.tax_period),
        invoice_total_gst_paise=invoice.total_gst,
        gstr2b_invoice_number=row.invoice_number if row is not None else None,
        gstr2b_total_gst_paise=row.total_gst if row is not None else None,
    )


def reconcile_invoices(
    invoices: list[InvoiceRecord],
    gstr2b_rows: list[Gstr2bRecord],
    *,
    config: ReconciliationConfig | None = None,
) -> list[MatchResult]:
    """Reconcile invoices against one batch's rows, deterministically.

    Results are returned in invoice_id order, one per supplied invoice.
    Source records are only read, never modified.
    """
    config = config or ReconciliationConfig()
    tolerance = config.amount_tolerance_paise

    results: dict[int, MatchResult] = {}
    eligible: list[tuple[InvoiceRecord, str, str, str]] = []

    for invoice in sorted(invoices, key=lambda item: item.invoice_id):
        if invoice.validation_status != VALIDATION_VALID:
            results[invoice.invoice_id] = _result(
                invoice,
                STATUS_NOT_ELIGIBLE,
                METHOD_INELIGIBLE,
                (_ineligible_reason(invoice.validation_status),),
            )
            continue
        gstin = normalize_gstin(invoice.supplier_gstin)
        number = normalize_invoice_number(invoice.invoice_number)
        period = normalize_tax_period(invoice.tax_period)
        if gstin is None or number is None or period is None:
            results[invoice.invoice_id] = _result(
                invoice,
                STATUS_NEEDS_REVIEW,
                METHOD_NONE,
                (REASON_MISSING_MATCH_KEY,),
            )
            continue
        eligible.append((invoice, gstin, number, period))

    # Duplicate purchase invoices sharing a normalized natural key are
    # never resolved arbitrarily.
    keyed_invoices: dict[tuple[str, str, str], list[InvoiceRecord]] = {}
    for invoice, gstin, number, period in eligible:
        keyed_invoices.setdefault((gstin, number, period), []).append(invoice)
    duplicate_invoice_ids: set[int] = set()
    for group in keyed_invoices.values():
        if len(group) > 1:
            for invoice in group:
                duplicate_invoice_ids.add(invoice.invoice_id)
                results[invoice.invoice_id] = _result(
                    invoice,
                    STATUS_NEEDS_REVIEW,
                    METHOD_NONE,
                    (REASON_DUPLICATE_PURCHASE_INVOICE,),
                )

    usable_rows: list[tuple[Gstr2bRecord, str, str, str]] = []
    for row in gstr2b_rows:
        if row.validation_status != VALIDATION_VALID:
            continue
        gstin = normalize_gstin(row.supplier_gstin)
        number = normalize_invoice_number(row.invoice_number)
        period = normalize_tax_period(row.tax_period)
        if gstin is None or number is None or period is None:
            continue
        if row.total_gst is None:
            continue
        usable_rows.append((row, gstin, number, period))

    exact_index: dict[tuple[str, str, str], list[Gstr2bRecord]] = {}
    for row, gstin, number, period in usable_rows:
        exact_index.setdefault((gstin, number, period), []).append(row)

    for invoice, gstin, number, period in eligible:
        if invoice.invoice_id in duplicate_invoice_ids:
            continue
        key = (gstin, number, period)
        exact_rows = exact_index.get(key, [])

        if len(exact_rows) > 1:
            results[invoice.invoice_id] = _result(
                invoice,
                STATUS_NEEDS_REVIEW,
                METHOD_NONE,
                (REASON_DUPLICATE_GSTR2B_CANDIDATES,),
            )
            continue

        if len(exact_rows) == 1:
            row = exact_rows[0]
            difference = invoice.total_gst - row.total_gst
            if difference == 0:
                results[invoice.invoice_id] = _result(
                    invoice,
                    STATUS_MATCHED,
                    METHOD_EXACT,
                    (),
                    row=row,
                    difference=0,
                )
            elif abs(difference) <= tolerance:
                results[invoice.invoice_id] = _result(
                    invoice,
                    STATUS_MATCHED,
                    METHOD_EXACT_TOLERANCE,
                    (REASON_AMOUNT_WITHIN_TOLERANCE,),
                    row=row,
                    difference=difference,
                )
            else:
                results[invoice.invoice_id] = _result(
                    invoice,
                    STATUS_AMOUNT_MISMATCH,
                    METHOD_EXACT,
                    (REASON_AMOUNT_DIFFERENCE_BEYOND_TOLERANCE,),
                    row=row,
                    difference=difference,
                )
            continue

        candidates = [
            row
            for row, row_gstin, _row_number, row_period in usable_rows
            if row_gstin == gstin
            and row_period == period
            and abs(invoice.total_gst - row.total_gst) <= tolerance
        ]
        if len(candidates) == 1:
            row = candidates[0]
            results[invoice.invoice_id] = _result(
                invoice,
                STATUS_NEEDS_REVIEW,
                METHOD_CANDIDATE_AMOUNT,
                (REASON_CANDIDATE_NOT_EXACT_INVOICE_NUMBER,),
                row=row,
                difference=invoice.total_gst - row.total_gst,
            )
        elif len(candidates) > 1:
            results[invoice.invoice_id] = _result(
                invoice,
                STATUS_NEEDS_REVIEW,
                METHOD_NONE,
                (REASON_AMBIGUOUS_CANDIDATES,),
            )
        else:
            results[invoice.invoice_id] = _result(
                invoice,
                STATUS_MISSING_FROM_2B,
                METHOD_NONE,
                (REASON_NO_2B_ROW_FOUND,),
            )

    return [results[invoice.invoice_id] for invoice in
            sorted(invoices, key=lambda item: item.invoice_id)]
