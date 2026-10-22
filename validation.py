"""Deterministic invoice validation service for MILAAN.

SCOPE — READ THIS
=================
GSTIN validation here checks *syntax and the GSTIN check digit only*. It
does NOT query the GSTN portal, so it does NOT confirm that a GSTIN
belongs to a real, active, GST-registered supplier. A checksum-valid
GSTIN is "format-valid", not "verified". Live registration checks are
out of scope.

MONEY POLICY
============
* All monetary amounts are integer paise (1 rupee = 100 paise).
* Binary floating point is never used. Values that are not integers are
  rejected with MONEY_NOT_INTEGER instead of being coerced.
* Rate-derived expected tax is
      expected_tax = round_half_up(taxable_value * gst_rate / 100)
  computed with integer arithmetic only.
* If the booked total_gst differs from expected_tax by exactly 1 paise
  (possible when a 9%+9% split is rounded component-wise), the invoice is
  `needs_review` with a ROUNDING_TOLERANCE warning. Larger differences
  are `invalid` (RATE_MISMATCH).
* invoice_total must equal taxable_value + total_gst exactly; because
  every value is integer paise there is no rounding allowance there.

STATUSES
========
* `valid`        — no errors and no review-triggering uncertainty.
* `invalid`      — at least one field error; cannot be used for ITC or
                   supplier follow-up.
* `needs_review` — no errors, but an uncertainty (missing supplier GSTIN
                   or +/-1 paise rounding ambiguity) needs a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_NEEDS_REVIEW = "needs_review"

# Statuses that the database schema accepts for invoices.validation_status.
# "mismatch" is reserved for the later GSTR-2B reconciliation task.
STATUSES = (STATUS_VALID, STATUS_INVALID, STATUS_NEEDS_REVIEW)

# Warnings that require a human decision before an invoice can be valid.
REVIEW_WARNING_CODES = frozenset({"GSTIN_MISSING", "ROUNDING_TOLERANCE"})

SUPPORTED_GST_RATES = (0, 5, 12, 18, 28)
MONEY_FIELDS = (
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "total_gst",
    "invoice_total",
)

GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
# 01-38 are the standard state/UT codes; 97 (Other Territory) and 99
# (centre jurisdiction / OIDAR) are also valid GSTIN prefixes.
VALID_STATE_CODES = frozenset(f"{code:02d}" for code in range(1, 39)) | {"97", "99"}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAX_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Issue:
    """A single structured validation finding."""

    code: str
    field: str
    message: str


@dataclass(frozen=True)
class GstinCheck:
    """Result of GSTIN syntax + check-digit validation (no live lookup)."""

    gstin: str | None
    is_format_valid: bool
    normalized: str | None
    errors: tuple[Issue, ...]


@dataclass(frozen=True)
class ValidationResult:
    """Structured result returned by validate_invoice()."""

    status: str
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_VALID

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "errors": [
                {"code": i.code, "field": i.field, "message": i.message}
                for i in self.errors
            ],
            "warnings": [
                {"code": i.code, "field": i.field, "message": i.message}
                for i in self.warnings
            ],
            "reasons": list(self.reasons),
        }


def _issue(code: str, field: str, message: str) -> Issue:
    return Issue(code=code, field=field, message=message)


def normalize_gstin(gstin: str | None) -> str | None:
    """Uppercase a GSTIN and strip internal whitespace; None/blank -> None."""
    if gstin is None:
        return None
    if not isinstance(gstin, str):
        raise TypeError("gstin must be a string or None")
    normalized = "".join(gstin.split()).upper()
    return normalized or None


def gstin_checksum(first_14: str) -> str:
    """Compute the GSTIN check character for the first 14 characters.

    Documented algorithm (GSTN check digit, base-36):
      * map each character through "0123456789A-Z" to 0..35;
      * multiply alternating characters by 1 and 2 (starting with 1);
      * add quotient + remainder of each product divided by 36;
      * check character = charset[(36 - total % 36) % 36].
    """
    if not isinstance(first_14, str) or len(first_14) != 14:
        raise ValueError("expected exactly 14 GSTIN characters")
    first_14 = first_14.upper()
    total = 0
    for index, char in enumerate(first_14):
        try:
            value = GSTIN_CHARSET.index(char)
        except ValueError as exc:
            raise ValueError(f"invalid GSTIN character: {char!r}") from exc
        product = value * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return GSTIN_CHARSET[(36 - total % 36) % 36]


def validate_gstin(gstin: str | None) -> GstinCheck:
    """Check GSTIN structure, state code and check digit.

    This is format validation only; it does not verify ownership or the
    supplier's live GST registration status.
    """
    normalized = normalize_gstin(gstin)
    if normalized is None:
        return GstinCheck(gstin=None, is_format_valid=False, normalized=None,
                          errors=())

    errors: list[Issue] = []
    if len(normalized) != 15 or not GSTIN_PATTERN.match(normalized):
        errors.append(_issue(
            "GSTIN_FORMAT", "supplier_gstin",
            "GSTIN must be 15 characters: 2-digit state code, 10-character "
            "PAN, entity number, 'Z' and a check character "
            "(e.g. 27AAACS1234A1Z2).",
        ))
        return GstinCheck(gstin, False, normalized, tuple(errors))

    if normalized[:2] not in VALID_STATE_CODES:
        errors.append(_issue(
            "GSTIN_STATE_CODE", "supplier_gstin",
            f"'{normalized[:2]}' is not a recognised GST state code.",
        ))

    expected = gstin_checksum(normalized[:14])
    if normalized[14] != expected:
        errors.append(_issue(
            "GSTIN_CHECKSUM", "supplier_gstin",
            f"GSTIN check digit failed: expected '{expected}', "
            f"got '{normalized[14]}'.",
        ))

    return GstinCheck(gstin, not errors, normalized, tuple(errors))


def _round_half_up(numerator: int, denominator: int) -> int:
    """Round a non-negative rational numerator/denominator half-up to int."""
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise TypeError("integer arithmetic only")
    if numerator < 0:
        raise ValueError("negative values are not supported")
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return (2 * numerator + denominator) // (2 * denominator)


def rupees_to_paise(amount: Decimal | str | int) -> int:
    """Convert an exact rupee amount to integer paise.

    Accepts Decimal, numeric string or int (whole rupees). Rejects
    binary floats and amounts with more than two decimal places so no
    implicit lossy conversion happens.
    """
    if isinstance(amount, bool) or isinstance(amount, float):
        raise TypeError("binary floating point is not allowed for money")
    if isinstance(amount, int):
        return amount * 100
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, str):
        try:
            value = Decimal(amount.strip())
        except InvalidOperation as exc:
            raise ValueError(f"invalid decimal amount: {amount!r}") from exc
    else:
        raise TypeError("amount must be Decimal, str or int")
    if not value.is_finite():
        raise ValueError("amount must be finite")
    paise = value * 100
    if paise != paise.to_integral_value():
        raise ValueError("amount has more than 2 decimal places")
    return int(paise)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def can_message_supplier(validation_status: str) -> bool:
    """Gate for supplier follow-up messaging.

    Only invoices that passed deterministic validation may be used for
    ITC claims or supplier messaging. Invalid, needs_review, pending and
    mismatch invoices are all blocked.
    """
    return validation_status == STATUS_VALID


def validate_invoice(
    invoice: Mapping[str, Any],
    *,
    duplicate_exists: bool = False,
) -> ValidationResult:
    """Validate one invoice without touching the database.

    `invoice` may be a dict/sqlite3.Row with the invoice columns plus
    `supplier_gstin`. Pass `duplicate_exists=True` when a matching
    (supplier, invoice number, tax period) record is already stored.
    """
    errors: list[Issue] = []
    warnings: list[Issue] = []
    reasons: list[str] = []

    invoice_number = _text(invoice.get("invoice_number"))
    if not invoice_number:
        errors.append(_issue(
            "REQUIRED_FIELD", "invoice_number", "Invoice number is required.",
        ))

    invoice_date = _text(invoice.get("invoice_date"))
    if not invoice_date:
        errors.append(_issue(
            "REQUIRED_FIELD", "invoice_date",
            "Invoice date is required (YYYY-MM-DD).",
        ))
    elif not _ISO_DATE_RE.match(invoice_date):
        errors.append(_issue(
            "INVALID_DATE", "invoice_date",
            "Invoice date must be an ISO date in YYYY-MM-DD form.",
        ))
    else:
        try:
            date.fromisoformat(invoice_date)
        except ValueError:
            errors.append(_issue(
                "INVALID_DATE", "invoice_date",
                f"'{invoice_date}' is not a real calendar date.",
            ))

    tax_period = _text(invoice.get("tax_period"))
    if not tax_period:
        errors.append(_issue(
            "REQUIRED_FIELD", "tax_period",
            "Tax period is required (YYYY-MM).",
        ))
    elif not _TAX_PERIOD_RE.match(tax_period):
        errors.append(_issue(
            "INVALID_TAX_PERIOD", "tax_period",
            "Tax period must be YYYY-MM with a month between 01 and 12.",
        ))

    gstin_check = validate_gstin(invoice.get("supplier_gstin"))
    if gstin_check.normalized is None:
        warnings.append(_issue(
            "GSTIN_MISSING", "supplier_gstin",
            "Supplier GSTIN is missing, so GSTIN validity cannot be "
            "checked; a human must review this invoice.",
        ))
    else:
        errors.extend(gstin_check.errors)
        if gstin_check.is_format_valid:
            reasons.append(
                "GSTIN structure and check digit are valid. This is NOT "
                "live GST registration verification: the supplier's "
                "active registration status is unconfirmed."
            )

    amounts: dict[str, int] = {}
    for field in MONEY_FIELDS:
        value = invoice.get(field)
        if value is None:
            errors.append(_issue(
                "REQUIRED_FIELD", field,
                f"{field} is required (integer paise).",
            ))
        elif isinstance(value, bool) or not isinstance(value, int):
            errors.append(_issue(
                "MONEY_NOT_INTEGER", field,
                f"{field} must be integer paise; binary floating-point "
                "values are not accepted.",
            ))
        elif value < 0:
            errors.append(_issue(
                "NEGATIVE_AMOUNT", field,
                f"{field} must not be negative.",
            ))
        else:
            amounts[field] = value

    rate = invoice.get("gst_rate")
    rate_ok = False
    if rate is None:
        errors.append(_issue(
            "REQUIRED_FIELD", "gst_rate", "gst_rate is required.",
        ))
    elif isinstance(rate, bool) or not isinstance(rate, int):
        errors.append(_issue(
            "UNSUPPORTED_RATE", "gst_rate",
            "gst_rate must be an integer percent.",
        ))
    elif rate not in SUPPORTED_GST_RATES:
        errors.append(_issue(
            "UNSUPPORTED_RATE", "gst_rate",
            f"GST rate {rate}% is not supported; expected one of "
            f"{SUPPORTED_GST_RATES}.",
        ))
    else:
        rate_ok = True

    if len(amounts) == len(MONEY_FIELDS):
        taxable_value = amounts["taxable_value"]
        cgst = amounts["cgst"]
        sgst = amounts["sgst"]
        igst = amounts["igst"]
        total_gst = amounts["total_gst"]
        invoice_total = amounts["invoice_total"]

        has_intra = cgst > 0 or sgst > 0
        has_inter = igst > 0
        if has_intra and has_inter:
            errors.append(_issue(
                "TAX_STRUCTURE_CONFLICT", "igst",
                "CGST/SGST and IGST cannot both be charged on the same "
                "taxable amount.",
            ))
        elif has_intra and cgst != sgst:
            errors.append(_issue(
                "CGST_SGST_MISMATCH", "cgst",
                f"CGST ({cgst} paise) and SGST ({sgst} paise) must be "
                "equal for an intra-state supply.",
            ))
        elif not has_intra and not has_inter:
            reasons.append(
                "No tax components charged (0% or exempt supply)."
            )

        if total_gst != cgst + sgst + igst:
            errors.append(_issue(
                "TAX_TOTAL_MISMATCH", "total_gst",
                f"total_gst ({total_gst} paise) must equal "
                f"cgst+sgst+igst ({cgst + sgst + igst} paise).",
            ))

        expected_invoice_total = taxable_value + total_gst
        if invoice_total != expected_invoice_total:
            errors.append(_issue(
                "INVOICE_TOTAL_MISMATCH", "invoice_total",
                f"invoice_total ({invoice_total} paise) must equal "
                f"taxable_value + total_gst ({expected_invoice_total} "
                "paise); integer paise leaves no rounding allowance here.",
            ))

        if rate_ok:
            expected_tax = _round_half_up(taxable_value * rate, 100)
            difference = total_gst - expected_tax
            if difference == 0:
                reasons.append(
                    f"Total GST matches the {rate}% rate on the taxable value."
                )
            elif abs(difference) <= 1:
                warnings.append(_issue(
                    "ROUNDING_TOLERANCE", "total_gst",
                    f"Total GST is off by {abs(difference)} paise from the "
                    f"exact {rate}% amount ({expected_tax} paise); "
                    "component-wise rounding needs human review.",
                ))
            else:
                errors.append(_issue(
                    "RATE_MISMATCH", "total_gst",
                    f"Total GST {total_gst} paise is inconsistent with a "
                    f"{rate}% rate on taxable value {taxable_value} paise "
                    f"(expected about {expected_tax} paise).",
                ))

    if duplicate_exists:
        errors.append(_issue(
            "DUPLICATE_INVOICE", "invoice_number",
            "An invoice with the same supplier, invoice number and tax "
            "period already exists.",
        ))

    if errors:
        status = STATUS_INVALID
        reasons.append(
            f"Invoice rejected: {len(errors)} error(s). It cannot be used "
            "for ITC claims or supplier follow-up."
        )
    elif any(w.code in REVIEW_WARNING_CODES for w in warnings):
        status = STATUS_NEEDS_REVIEW
        review_codes = ", ".join(
            sorted({w.code for w in warnings if w.code in REVIEW_WARNING_CODES})
        )
        reasons.append(
            f"Human review required ({review_codes}); the invoice is not "
            "yet valid."
        )
    else:
        status = STATUS_VALID
        reasons.append(
            "All required fields are present and the tax arithmetic is "
            "consistent."
        )

    return ValidationResult(
        status=status,
        errors=tuple(errors),
        warnings=tuple(warnings),
        reasons=tuple(reasons),
    )
