"""Purchase-invoice import for normalized CSV/XLSX files.

This module is deliberately pure: it parses and validates an uploaded file
without touching the database. ``db.save_invoice_import`` persists accepted
rows and is responsible for existing-record duplicate checks.

The accepted schema mirrors the GSTR-2B importer: explicit column names,
integer-paise conversion through decimal-safe parsing, and row-level issues
that never abort the whole file. A file-level problem (unsupported format,
missing columns, no data rows) raises ValueError.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import gstr2b
import validation

REQUIRED_COLUMNS = (
    "supplier_name",
    "supplier_gstin",
    "invoice_number",
    "invoice_date",
    "tax_period",
    "taxable_value",
    "gst_rate",
    "cgst",
    "sgst",
    "igst",
    "total_gst",
    "invoice_total",
)
OPTIONAL_COLUMNS = ("contact_phone",)
MONEY_FIELDS = (
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "total_gst",
    "invoice_total",
)

COLUMN_ALIASES = {
    "supplier_name": "supplier_name",
    "supplier": "supplier_name",
    "vendor": "supplier_name",
    "vendor_name": "supplier_name",
    "supplier_gstin": "supplier_gstin",
    "gstin": "supplier_gstin",
    "gstin_of_supplier": "supplier_gstin",
    "vendor_gstin": "supplier_gstin",
    "invoice_number": "invoice_number",
    "invoice_no": "invoice_number",
    "invoice_num": "invoice_number",
    "document_number": "invoice_number",
    "invoice_date": "invoice_date",
    "invoice_dt": "invoice_date",
    "document_date": "invoice_date",
    "tax_period": "tax_period",
    "return_period": "tax_period",
    "taxable_value": "taxable_value",
    "taxable_amount": "taxable_value",
    "taxable_val": "taxable_value",
    "gst_rate": "gst_rate",
    "rate": "gst_rate",
    "gst_percent": "gst_rate",
    "cgst": "cgst",
    "cgst_amount": "cgst",
    "sgst": "sgst",
    "sgst_amount": "sgst",
    "igst": "igst",
    "igst_amount": "igst",
    "total_gst": "total_gst",
    "total_tax": "total_gst",
    "tax_amount": "total_gst",
    "invoice_total": "invoice_total",
    "invoice_value": "invoice_total",
    "total_invoice_value": "invoice_total",
    "contact_phone": "contact_phone",
    "phone": "contact_phone",
    "supplier_phone": "contact_phone",
}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAX_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_RATE_RE = re.compile(r"^\d{1,2}%?$")


@dataclass(frozen=True)
class ParsedInvoiceRow:
    row_number: int
    supplier_name: str
    supplier_gstin: str
    contact_phone: str
    invoice_number: str
    invoice_date: str
    tax_period: str
    taxable_value: int | None
    gst_rate: int | None
    cgst: int | None
    sgst: int | None
    igst: int | None
    total_gst: int | None
    invoice_total: int | None
    validation_status: str
    errors: tuple[validation.Issue, ...]
    warnings: tuple[validation.Issue, ...]
    raw_data: str

    @property
    def accepted(self) -> bool:
        return self.validation_status == validation.STATUS_VALID

    @property
    def issue_text(self) -> str:
        return "; ".join(
            f"[{issue.code}] {issue.field}: {issue.message}"
            for issue in (*self.errors, *self.warnings)
        )

    def record(self) -> dict[str, Any]:
        """Database/validation-shaped record for this row."""
        return {
            "supplier_name": self.supplier_name,
            "supplier_gstin": self.supplier_gstin,
            "contact_phone": self.contact_phone,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "tax_period": self.tax_period,
            "taxable_value": self.taxable_value,
            "gst_rate": self.gst_rate,
            "cgst": self.cgst,
            "sgst": self.sgst,
            "igst": self.igst,
            "total_gst": self.total_gst,
            "invoice_total": self.invoice_total,
        }


@dataclass(frozen=True)
class ParsedInvoiceImport:
    file_name: str
    source_format: str
    file_hash: str
    file_size: int
    headers: tuple[str, ...]
    detected_columns: tuple[str, ...]
    rows: tuple[ParsedInvoiceRow, ...]

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def accepted_rows(self) -> int:
        return sum(1 for row in self.rows if row.accepted)

    @property
    def rejected_rows(self) -> int:
        return self.total_rows - self.accepted_rows


def _issue(code: str, field: str, message: str) -> validation.Issue:
    return validation.Issue(code=code, field=field, message=message)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_blank_row(row: list[Any]) -> bool:
    return all(_cell_text(cell) == "" for cell in row)


def _raw_data_json(headers: list[str], row: list[Any]) -> str:
    mapping: dict[str, Any] = {}
    for index, cell in enumerate(row):
        header = headers[index] if index < len(headers) else ""
        key = str(header).strip() or f"column_{index + 1}"
        if key in mapping:
            key = f"{key}_{index + 1}"
        mapping[key] = cell
    return json.dumps(mapping, ensure_ascii=False, default=str)


def _build_column_index(headers: list[str]) -> tuple[dict[str, int], list[str]]:
    index: dict[str, int] = {}
    detected: list[str] = []
    for position, header in enumerate(headers):
        original_name = gstr2b.normalize_header(header)
        if not original_name:
            continue
        name = COLUMN_ALIASES.get(original_name, original_name)
        detected.append(name)
        if name in index:
            raise ValueError(
                f"duplicate column for {name!r} in header row "
                f"(including {original_name!r})"
            )
        index[name] = position
    missing = [name for name in REQUIRED_COLUMNS if name not in index]
    if missing:
        raise ValueError(
            "missing required column(s): " + ", ".join(missing)
        )
    return index, sorted(set(detected))


def _parse_row(
    row_number: int,
    row: list[Any],
    headers: list[str],
    column_index: dict[str, int],
    seen_keys: set[tuple[str, str, str]],
) -> ParsedInvoiceRow:
    parse_errors: list[validation.Issue] = []

    def cell(field: str) -> Any:
        position = column_index.get(field)
        if position is None or position >= len(row):
            return None
        return row[position]

    supplier_name = _cell_text(cell("supplier_name"))
    if not supplier_name:
        parse_errors.append(_issue(
            "REQUIRED_FIELD", "supplier_name",
            "supplier_name is required.",
        ))

    gstin_text = _cell_text(cell("supplier_gstin"))
    gstin_check = validation.validate_gstin(gstin_text)
    supplier_gstin = gstin_check.normalized or gstin_text.upper()
    if not gstin_text:
        parse_errors.append(_issue(
            "REQUIRED_FIELD", "supplier_gstin",
            "supplier_gstin is required to create or reuse a supplier.",
        ))
    else:
        parse_errors.extend(gstin_check.errors)

    invoice_number = _cell_text(cell("invoice_number"))
    if not invoice_number:
        parse_errors.append(_issue(
            "REQUIRED_FIELD", "invoice_number", "invoice_number is required.",
        ))

    invoice_date = _cell_text(cell("invoice_date"))
    if not invoice_date:
        parse_errors.append(_issue(
            "REQUIRED_FIELD", "invoice_date",
            "invoice_date is required (YYYY-MM-DD).",
        ))
    elif not _ISO_DATE_RE.match(invoice_date):
        parse_errors.append(_issue(
            "INVALID_DATE", "invoice_date",
            "invoice_date must be an ISO date in YYYY-MM-DD form.",
        ))
    else:
        try:
            date.fromisoformat(invoice_date)
        except ValueError:
            parse_errors.append(_issue(
                "INVALID_DATE", "invoice_date",
                f"'{invoice_date}' is not a real calendar date.",
            ))

    tax_period = _cell_text(cell("tax_period"))
    if not tax_period:
        parse_errors.append(_issue(
            "REQUIRED_FIELD", "tax_period",
            "tax_period is required (YYYY-MM).",
        ))
    elif not _TAX_PERIOD_RE.match(tax_period):
        parse_errors.append(_issue(
            "INVALID_TAX_PERIOD", "tax_period",
            "tax_period must be YYYY-MM with a month between 01 and 12.",
        ))

    money: dict[str, int | None] = {field: None for field in MONEY_FIELDS}
    for field in MONEY_FIELDS:
        raw_value = cell(field)
        if raw_value is None or _cell_text(raw_value) == "":
            parse_errors.append(_issue(
                "REQUIRED_FIELD", field, f"{field} is required.",
            ))
            continue
        try:
            money[field] = gstr2b.parse_money_to_paise(raw_value)
        except gstr2b.MoneyParseError as exc:
            parse_errors.append(_issue(exc.code, field, str(exc)))

    gst_rate: int | None = None
    rate_text = _cell_text(cell("gst_rate"))
    if not rate_text:
        parse_errors.append(_issue(
            "REQUIRED_FIELD", "gst_rate", "gst_rate is required.",
        ))
    elif not _RATE_RE.match(rate_text):
        parse_errors.append(_issue(
            "UNSUPPORTED_RATE", "gst_rate",
            f"gst_rate {rate_text!r} must be a whole percent such as 18.",
        ))
    else:
        gst_rate = int(rate_text.rstrip("%"))
        if gst_rate not in validation.SUPPORTED_GST_RATES:
            parse_errors.append(_issue(
                "UNSUPPORTED_RATE", "gst_rate",
                f"GST rate {gst_rate}% is not supported; expected one of "
                f"{validation.SUPPORTED_GST_RATES}.",
            ))

    contact_phone = _cell_text(cell("contact_phone"))

    if supplier_gstin and invoice_number and tax_period:
        key = (supplier_gstin, invoice_number, tax_period)
        if key in seen_keys:
            parse_errors.append(_issue(
                "DUPLICATE_ROW", "invoice_number",
                "Another row in this file has the same supplier GSTIN, "
                "invoice number and tax period.",
            ))
        else:
            seen_keys.add(key)

    errors = list(parse_errors)
    warnings: list[validation.Issue] = []
    if not parse_errors:
        result = validation.validate_invoice({
            "supplier_gstin": supplier_gstin,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "tax_period": tax_period,
            "taxable_value": money["taxable_value"],
            "gst_rate": gst_rate,
            "cgst": money["cgst"],
            "sgst": money["sgst"],
            "igst": money["igst"],
            "total_gst": money["total_gst"],
            "invoice_total": money["invoice_total"],
        })
        errors = list(result.errors)
        warnings = list(result.warnings)
        status = result.status
    else:
        status = validation.STATUS_INVALID

    return ParsedInvoiceRow(
        row_number=row_number,
        supplier_name=supplier_name,
        supplier_gstin=supplier_gstin,
        contact_phone=contact_phone,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        tax_period=tax_period,
        taxable_value=money["taxable_value"],
        gst_rate=gst_rate,
        cgst=money["cgst"],
        sgst=money["sgst"],
        igst=money["igst"],
        total_gst=money["total_gst"],
        invoice_total=money["invoice_total"],
        validation_status=status,
        errors=tuple(errors),
        warnings=tuple(warnings),
        raw_data=_raw_data_json(headers, row),
    )


def parse_invoice_file(content: bytes, file_name: str) -> ParsedInvoiceImport:
    """Parse a normalized purchase-invoice CSV/XLSX upload.

    Raises ValueError only for file-level problems. Row-level problems are
    returned on the parsed rows so the UI can preview them before import.
    """
    headers, data_rows, source_format = gstr2b.read_tabular_file(
        content, file_name
    )
    column_index, detected = _build_column_index(headers)
    non_blank_rows = [row for row in data_rows if not _is_blank_row(row)]
    if not non_blank_rows:
        raise ValueError("file contains a header but no data rows")

    seen_keys: set[tuple[str, str, str]] = set()
    rows = tuple(
        _parse_row(
            row_number=position,
            row=row,
            headers=headers,
            column_index=column_index,
            seen_keys=seen_keys,
        )
        for position, row in enumerate(non_blank_rows, start=1)
    )
    return ParsedInvoiceImport(
        file_name=file_name,
        source_format=source_format,
        file_hash=gstr2b.sha256_hex(content),
        file_size=len(content),
        headers=tuple(str(header) for header in headers),
        detected_columns=tuple(detected),
        rows=rows,
    )


def template_csv() -> str:
    """A header-complete template whose sample row passes validation."""
    header = ",".join(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)
    sample = (
        "(Synthetic) Template Supplier,"
        "27AAACS1234A1Z2,"
        "TEMPLATE-001,"
        "2026-03-05,"
        "2026-03,"
        "1000.00,"
        "18,"
        "90.00,"
        "90.00,"
        "0.00,"
        "180.00,"
        "1180.00,"
        "9900000000"
    )
    return header + "\n" + sample + "\n"
