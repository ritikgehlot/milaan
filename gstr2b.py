"""Deterministic GSTR-2B file ingestion (CSV / XLSX) — staging only.

This module parses and normalizes an explicitly documented column
schema; it does NOT support arbitrary GST-portal export layouts, and it
does NOT perform reconciliation matching against purchase invoices.

SUPPORTED INPUT SCHEMA (normalized test columns)
================================================
The header row is matched case-insensitively. Spaces and hyphens in
header names are treated as underscores; all nine canonical columns are
required. Common export aliases are accepted for each canonical field:

    supplier_gstin, invoice_number, invoice_date, tax_period,
    taxable_value, cgst, sgst, igst, total_gst

Examples include ``GSTIN of supplier``, ``Invoice No``, ``Invoice Dt``,
``Return Period``, ``Taxable Amount``, ``CGST Amount`` and ``Total Tax``.

* invoice_date: ISO date `YYYY-MM-DD` (XLSX date cells are accepted).
* tax_period: `YYYY-MM`.
* Monetary columns: rupee amounts with at most two decimal places,
  parsed via `Decimal` and stored as integer paise. Commas, booleans,
  NaN/Infinity, negative values and more than two decimals are rejected
  (never coerced to zero).
* Extra columns are ignored for validation but retained verbatim in the
  row audit trail (`raw_data`).

GSTIN validation is syntax + check digit only (via validation.py). It
does NOT verify live GST registration. Fully blank rows are skipped.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import validation

REQUIRED_COLUMNS = (
    "supplier_gstin",
    "invoice_number",
    "invoice_date",
    "tax_period",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "total_gst",
)
MONEY_FIELDS = ("taxable_value", "cgst", "sgst", "igst", "total_gst")

# Common labels in accounting exports are accepted, but the parser still
# requires every canonical field and records each original row verbatim.
COLUMN_ALIASES = {
    "supplier_gstin": "supplier_gstin",
    "gstin": "supplier_gstin",
    "gstin_of_supplier": "supplier_gstin",
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
    "cgst": "cgst",
    "cgst_amount": "cgst",
    "sgst": "sgst",
    "sgst_amount": "sgst",
    "igst": "igst",
    "igst_amount": "igst",
    "total_gst": "total_gst",
    "total_tax": "total_gst",
    "tax_amount": "total_gst",
}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAX_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MONEY_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")


class MoneyParseError(ValueError):
    """Raised for a monetary cell that cannot be safely converted."""

    def __init__(self, message: str, code: str = "INVALID_MONEY") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    supplier_gstin: str | None
    invoice_number: str | None
    invoice_date: str | None
    tax_period: str | None
    taxable_value: int | None
    cgst: int | None
    sgst: int | None
    igst: int | None
    total_gst: int | None
    validation_status: str
    errors: tuple[validation.Issue, ...]
    warnings: tuple[validation.Issue, ...]
    raw_data: str


@dataclass(frozen=True)
class ParsedGstr2b:
    file_name: str
    source_format: str
    file_hash: str
    file_size: int
    rows: tuple[ParsedRow, ...]

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def accepted_rows(self) -> int:
        return sum(1 for row in self.rows
                   if row.validation_status == validation.STATUS_VALID)

    @property
    def rejected_rows(self) -> int:
        return self.total_rows - self.accepted_rows


def sha256_hex(content: bytes) -> str:
    """Stable content hash used for repeat-upload detection."""
    return hashlib.sha256(content).hexdigest()


def parse_money_to_paise(value: Any) -> int:
    """Convert a rupee amount to integer paise without binary float math.

    Raises MoneyParseError for booleans, non-finite values, malformed
    strings, negatives, and amounts with more than two decimal places.
    """
    if isinstance(value, bool):
        raise MoneyParseError("boolean is not a valid monetary value")
    if isinstance(value, int):
        if value < 0:
            raise MoneyParseError("negative monetary value",
                                  code="NEGATIVE_AMOUNT")
        return value * 100
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MoneyParseError("non-finite monetary value")
        return _decimal_to_paise(Decimal(str(value)))
    if isinstance(value, Decimal):
        return _decimal_to_paise(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyParseError("blank monetary value",
                                  code="REQUIRED_FIELD")
        if not _MONEY_RE.match(text):
            raise MoneyParseError(
                f"malformed monetary value {value!r}; expected a plain "
                "number such as 12500.50 (commas are not accepted)"
            )
        try:
            decimal_value = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyParseError(f"malformed monetary value {value!r}") from exc
        return _decimal_to_paise(decimal_value)
    raise MoneyParseError(
        f"unsupported monetary value type: {type(value).__name__}"
    )


def _decimal_to_paise(value: Decimal) -> int:
    if not value.is_finite():
        raise MoneyParseError("non-finite monetary value")
    if value < 0:
        raise MoneyParseError("negative monetary value", code="NEGATIVE_AMOUNT")
    paise = value * 100
    if paise != paise.to_integral_value():
        raise MoneyParseError(
            "monetary value has more than 2 decimal places"
        )
    return int(paise)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value).strip()


def _normalize_header(value: Any) -> str:
    text = _cell_text(value).lower()
    return re.sub(r"[\s\-]+", "_", text)


def _is_blank_row(row: Iterable[Any]) -> bool:
    return all(_cell_text(cell) == "" for cell in row)


def _raw_data_json(original_headers: list[str], row: Iterable[Any]) -> str:
    mapping: dict[str, Any] = {}
    for index, cell in enumerate(row):
        header = original_headers[index] if index < len(original_headers) else ""
        key = header.strip() or f"column_{index + 1}"
        if key in mapping:
            key = f"{key}_{index + 1}"
        if isinstance(cell, (datetime, date)):
            mapping[key] = cell.isoformat()
        elif isinstance(cell, Decimal):
            mapping[key] = str(cell)
        else:
            mapping[key] = cell
    return json.dumps(mapping, ensure_ascii=False, default=str)


def _read_csv(content: bytes) -> tuple[list[str], list[list[Any]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV file is not valid UTF-8 text") from exc
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader]
    while rows and _is_blank_row(rows[-1]):
        rows.pop()
    if not rows:
        raise ValueError("file is empty")
    return rows[0], rows[1:]


def _read_xlsx(content: bytes) -> tuple[list[str], list[list[Any]]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ValueError(
            "XLSX support is unavailable (openpyxl is not installed)"
        ) from exc
    try:
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
    except Exception as exc:
        raise ValueError("file is not a readable XLSX workbook") from exc
    try:
        rows = [list(row) for row in workbook.active.iter_rows(values_only=True)]
    finally:
        workbook.close()
    while rows and _is_blank_row(rows[-1]):
        rows.pop()
    if not rows:
        raise ValueError("file is empty")
    return [_cell_text(cell) for cell in rows[0]], rows[1:]


def read_tabular_file(
    content: bytes, file_name: str
) -> tuple[list[str], list[list[Any]], str]:
    """Read a CSV/XLSX upload and return headers, data rows and format.

    Shared by the GSTR-2B and purchase-invoice importers so both apply the
    same safe decoding, blank-row handling and extension checks.
    """
    extension = Path(file_name).suffix.lower()
    if extension == ".csv":
        headers, data_rows = _read_csv(content)
        return headers, data_rows, "csv"
    if extension == ".xlsx":
        headers, data_rows = _read_xlsx(content)
        return headers, data_rows, "xlsx"
    raise ValueError("unsupported file type; expected .csv or .xlsx")


def normalize_header(value: Any) -> str:
    """Normalize a column label for alias matching."""
    return _normalize_header(value)


def _build_column_index(headers: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, header in enumerate(headers):
        original_name = _normalize_header(header)
        if not original_name:
            continue
        name = COLUMN_ALIASES.get(original_name, original_name)
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
    return index


def _issue(code: str, field: str, message: str) -> validation.Issue:
    return validation.Issue(code=code, field=field, message=message)


def _parse_row(
    row_number: int,
    row: list[Any],
    headers: list[str],
    column_index: dict[str, int],
    seen_keys: set[tuple[str, str, str]],
) -> ParsedRow:
    errors: list[validation.Issue] = []
    warnings: list[validation.Issue] = []

    def cell(field: str) -> Any:
        position = column_index.get(field)
        if position is None or position >= len(row):
            return None
        return row[position]

    supplier_gstin_text = _cell_text(cell("supplier_gstin"))
    supplier_gstin: str | None = None
    if not supplier_gstin_text:
        errors.append(_issue(
            "REQUIRED_FIELD", "supplier_gstin",
            "supplier_gstin is required.",
        ))
    else:
        gstin_check = validation.validate_gstin(supplier_gstin_text)
        supplier_gstin = gstin_check.normalized or supplier_gstin_text
        errors.extend(gstin_check.errors)

    invoice_number = _cell_text(cell("invoice_number")) or None
    if invoice_number is None:
        errors.append(_issue(
            "REQUIRED_FIELD", "invoice_number", "invoice_number is required.",
        ))

    invoice_date = _cell_text(cell("invoice_date")) or None
    if invoice_date is None:
        errors.append(_issue(
            "REQUIRED_FIELD", "invoice_date",
            "invoice_date is required (YYYY-MM-DD).",
        ))
    elif not _ISO_DATE_RE.match(invoice_date):
        errors.append(_issue(
            "INVALID_DATE", "invoice_date",
            "invoice_date must be an ISO date in YYYY-MM-DD form.",
        ))
    else:
        try:
            date.fromisoformat(invoice_date)
        except ValueError:
            errors.append(_issue(
                "INVALID_DATE", "invoice_date",
                f"'{invoice_date}' is not a real calendar date.",
            ))

    tax_period = _cell_text(cell("tax_period")) or None
    if tax_period is None:
        errors.append(_issue(
            "REQUIRED_FIELD", "tax_period",
            "tax_period is required (YYYY-MM).",
        ))
    elif not _TAX_PERIOD_RE.match(tax_period):
        errors.append(_issue(
            "INVALID_TAX_PERIOD", "tax_period",
            "tax_period must be YYYY-MM with a month between 01 and 12.",
        ))

    money: dict[str, int | None] = {field: None for field in MONEY_FIELDS}
    for field in MONEY_FIELDS:
        raw_value = cell(field)
        if raw_value is None or _cell_text(raw_value) == "":
            errors.append(_issue(
                "REQUIRED_FIELD", field, f"{field} is required.",
            ))
            continue
        try:
            money[field] = parse_money_to_paise(raw_value)
        except MoneyParseError as exc:
            errors.append(_issue(exc.code, field, str(exc)))

    if all(money[field] is not None for field in MONEY_FIELDS):
        cgst = money["cgst"]
        sgst = money["sgst"]
        igst = money["igst"]
        total_gst = money["total_gst"]

        if total_gst != cgst + sgst + igst:
            errors.append(_issue(
                "TAX_TOTAL_MISMATCH", "total_gst",
                f"total_gst ({total_gst} paise) must equal "
                f"cgst+sgst+igst ({cgst + sgst + igst} paise).",
            ))
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
                f"CGST ({cgst} paise) and SGST ({sgst} paise) must be equal.",
            ))

    if supplier_gstin and invoice_number and tax_period:
        key = (supplier_gstin, invoice_number, tax_period)
        if key in seen_keys:
            errors.append(_issue(
                "DUPLICATE_ROW", "invoice_number",
                "Another row in this file has the same supplier GSTIN, "
                "invoice number and tax period.",
            ))
        else:
            seen_keys.add(key)

    status = (
        validation.STATUS_INVALID if errors else validation.STATUS_VALID
    )
    return ParsedRow(
        row_number=row_number,
        supplier_gstin=supplier_gstin,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        tax_period=tax_period,
        taxable_value=money["taxable_value"],
        cgst=money["cgst"],
        sgst=money["sgst"],
        igst=money["igst"],
        total_gst=money["total_gst"],
        validation_status=status,
        errors=tuple(errors),
        warnings=tuple(warnings),
        raw_data=_raw_data_json(headers, row),
    )


def parse_gstr2b(content: bytes, file_name: str) -> ParsedGstr2b:
    """Parse a normalized GSTR-2B CSV/XLSX file.

    Raises ValueError for fatal file-level problems (unreadable file,
    missing/duplicate required headers, no data rows). Row-level problems
    never abort the batch; they are returned as rejected ParsedRows.
    """
    headers, data_rows, source_format = read_tabular_file(content, file_name)

    column_index = _build_column_index(headers)
    non_blank_rows = [
        (row) for row in data_rows if not _is_blank_row(row)
    ]
    if not non_blank_rows:
        raise ValueError("file contains a header but no data rows")

    seen_keys: set[tuple[str, str, str]] = set()
    parsed_rows = tuple(
        _parse_row(
            row_number=position,
            row=row,
            headers=headers,
            column_index=column_index,
            seen_keys=seen_keys,
        )
        for position, row in enumerate(non_blank_rows, start=1)
    )

    return ParsedGstr2b(
        file_name=file_name,
        source_format=source_format,
        file_hash=sha256_hex(content),
        file_size=len(content),
        rows=parsed_rows,
    )
