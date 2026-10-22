# Synthetic GSTR-2B fixtures

These files are **synthetic inputs for automated tests**, not GST portal
exports. They use fabricated GSTINs, invoice numbers and amounts. No real
taxpayer data, credentials or personal information is included.

The fixtures exercise the normalized schema documented in `gstr2b.py`:

`supplier_gstin, invoice_number, invoice_date, tax_period, taxable_value,
cgst, sgst, igst, total_gst`

| File | Purpose |
| --- | --- |
| `valid_normalized.csv` | Two valid rows; one intra-state and one inter-state. |
| `valid_normalized.xlsx` | XLSX equivalent of the valid CSV. |
| `active_sheet.xlsx` | Sheets are ordered so the active sheet is not the first; one valid row. |
| `bom_crlf.csv` | UTF-8 BOM and Windows CRLF line endings. |
| `extra_columns.csv` | Extra columns retained in raw row evidence. |
| `invalid_rows.csv` | Seven rows covering bad date, GSTIN, period, money, negative amount and tax-total mismatch; one row is valid. |
| `duplicate_rows.csv` | Duplicate natural key within one file; first accepted, second rejected. |
| `decimal_values.csv` | Rupee decimals converted exactly to integer paise. |
| `header_only.csv` | Header with no data rows; must fail safely. |
| `empty.csv` | Empty file; must fail safely. |
| `malformed.xlsx` | Bytes masquerading as XLSX; must fail safely. |
| `malformed_utf8.csv` | Invalid UTF-8; must fail safely. |

Run the compatibility checks with:

```bash
.venv/bin/python -m unittest test_realworld -v
```

The XLSX parser reads the workbook's **active worksheet**, not necessarily
the first worksheet. `active_sheet.xlsx` locks that behavior in.

These fixtures do not establish compatibility with actual GST portal export
layouts. Portal compatibility is unverified without an authorized genuine
sample.
