# MILAAN

An agent that recovers the input tax credit a small trader is silently losing.
The trader talks to it on **WhatsApp, in Hindi or English, by text, photo or voice note**.

## Round 2: the WhatsApp agent (`agent/`)

```
WhatsApp (Twilio or Meta) ──► agent/server.py ──► agent/core.py  (one traced turn per message)
                                            │
   PERCEPTION (LLM)            DECISION (deterministic)            CONSENT (human)
   bill photo → fields         validation.py: GSTIN checksum,      every supplier message
   + double read               tax arithmetic                      needs the trader's 'haan'
   voice note → text           reconciliation + diagnose.py:       — the LLM has no send tool
   Hindi/Hinglish → tools      MATCHED / FORMAT_VARIANT / QRMP /
   supplier reply → intent     NON_FILING / AMOUNT_MISMATCH /      policy re-checked at send
                               GSTIN_CONFLICT / UNKNOWN_GSTIN       time; idempotent sends
                               §17(5) screen, escalation ladder
                                            │
                       SQLite case state + full trace ──► Streamlit "WhatsApp agent" page
```

* **Setup and demo:** [SETUP_AGENT.md](SETUP_AGENT.md) (Twilio in section 3, Meta WhatsApp Cloud API in section 3b)
* **Finale checklist and handoff:** [FINAL_CHECKLIST.md](FINAL_CHECKLIST.md)
* **Offline rehearsal:** `python -m agent.simulate --script demo --offline` (no network, no API keys used)
* **Tests:** `python -m unittest discover -p "test_*.py"` (335 tests, all passing on 2026-10-22)
* **Evaluation:** [EVALUATION.md](EVALUATION.md), section "Agent evaluation"

**What is real:**
* WhatsApp in both directions, through the Twilio sandbox or Meta's WhatsApp Cloud API test number (`WHATSAPP_PROVIDER`)
* Vision-LLM bill extraction
* Speech-to-text for voice notes
* LLM tool-calling planner
* GSTR-2B portal-JSON and CSV/XLSX parsing
* Deterministic diagnosis
* Consent-gated supplier messages to consenting test numbers
* Retries and failure handling
* Persistent multi-cycle state
* A trace of every step

**What is simulated or limited (disclosed):**
* The supplier is a consenting teammate's phone, not a real supplier.
* The demo month and its GSTR-2B files are synthetic.
* The portal-JSON parser follows GSTN's published layout but has not been tested on a live export.
* `#advance N` fast-forwards the agent clock for the demo.
* The fixture reader stands in for the vision model only in offline rehearsal and tests.
* The Meta WhatsApp channel is covered by offline tests (`test_meta.py`); run a live rehearsal before relying on it.
* MILAAN never files returns or touches the GST portal.

---

## Core pipeline (Round 1 build)

ITC gap detection for small Indian traders. MILAAN ingests purchase
invoice data, validates fields, reconciles against GSTR-2B, explains
mismatches, and prepares supplier follow-ups. (Image extraction lives in
the agent layer above.)

**Status:** Task 9 complete — Python + Streamlit app shell with SQLite
persistence, supplier/invoice data models, an idempotent synthetic
demo-data seed, deterministic invoice validation, GSTR-2B CSV/XLSX
staging import with a full row-level audit trail, deterministic
invoice-to-GSTR-2B reconciliation with persisted run history, supplier
follow-up **draft preparation with approval records**, and audited mock-only
delivery simulation. The core
invoice-to-follow-up workflow is covered by deterministic end-to-end
tests, including the Streamlit approval path. Task 9 adds realistic
synthetic compatibility fixtures, a measured benchmark with a baseline,
and an isolated judge demo. Live delivery is disabled in the app UI and
cannot be enabled through environment configuration; mock mode is for
workflow testing only. No real WhatsApp message is sent by the test suite. There is
no AI diagnosis, no OCR pipeline, no GST filing and no recovery claim
anywhere in the pipeline.

## GSTR-2B import (Task 4) — staging only

`gstr2b.py` parses an **explicitly normalized schema** (arbitrary GST
portal export layouts are NOT supported). Importing never modifies
purchase invoices; reconciliation is a separate, read-only step.

Supported files: `.csv` (UTF-8, optional BOM, comma-separated) and
`.xlsx` (the workbook's **active worksheet**, which may not be the first
sheet). Header names are matched case-insensitively and
spaces/hyphens are treated as underscores. All nine columns are
required; extra columns are ignored for validation but preserved in the
audit trail. Fully blank rows are skipped.

| Column | Format |
| --- | --- |
| `supplier_gstin` | 15-char GSTIN (syntax + check digit only) |
| `invoice_number` | text, required |
| `invoice_date` | `YYYY-MM-DD` (XLSX date cells accepted) |
| `tax_period` | `YYYY-MM` |
| `taxable_value` | rupee amount, ≤2 decimals |
| `cgst` | rupee amount, ≤2 decimals |
| `sgst` | rupee amount, ≤2 decimals |
| `igst` | rupee amount, ≤2 decimals |
| `total_gst` | rupee amount, ≤2 decimals; must equal cgst+sgst+igst |

Monetary cells are parsed with `Decimal` and stored as integer paise.
Booleans, commas, NaN/Infinity, negatives, malformed numbers and >2
decimal places are rejected with row-level errors — they are never
coerced to zero. Row checks also cover required fields, date/period
formats, GSTIN structure/checksum, intra-state vs inter-state
consistency, arithmetic, and duplicate rows within the same file.

Persistence (`db.save_gstr2b_import`) stores a batch row plus **every
source row**, including rejected rows with their JSON error list and raw
source values. Batch and rows commit in one transaction, so a failure
leaves nothing behind; partial acceptance (accepted vs rejected counts)
is reported explicitly. Tables: `gstr2b_imports`, `gstr2b_rows`
(FK with `ON DELETE CASCADE`, unique `(import_id, row_number)`,
integer-only money CHECKs, indexes on hash/import/status/natural key).

Repeat uploads: the SHA-256 of the file is stored per batch. If an
identical file was imported before, the UI shows the previous batch(es)
and requires an explicit "Import this identical file again as a new
batch" confirmation. Imports are never silently deduplicated, and a
separate statement with identical bytes becomes a deliberate new batch.

XLSX dependency: `openpyxl==3.1.5` (plus pinned `et_xmlfile==2.0.0`) is
the only added dependency; the Python stdlib has no XLSX reader.

## Reconciliation (Task 5)

`reconciliation.py` is a pure, deterministic matcher (no DB access, no
LLM). One run compares every stored purchase invoice against the
accepted rows of **one selected GSTR-2B batch** and records one result
per invoice.

**Eligibility.** Only invoices with `validation_status = 'valid'` are
matched. `pending`, `invalid` and `needs_review` invoices are reported
as `NOT_ELIGIBLE` with a specific reason code; they are never silently
treated as valid. Only GSTR-2B rows with `validation_status = 'valid'`
are candidates; rejected rows remain in the import audit trail but are
never matched.

**Normalization.** The natural key is supplier GSTIN + invoice number +
tax period:

- GSTIN: whitespace removed, uppercased.
- Invoice number: trimmed, internal whitespace runs collapsed to one
  space, uppercased. **No other characters are removed** — punctuation
  is preserved so `INV-1` and `INV1` stay distinct.
- Tax period: trimmed; `YYYY-MM` and `YYYY/MM` canonicalize to
  `YYYY-MM`. Malformed/impossible periods (e.g. `2026-13`) cannot be
  matched. Records from different tax periods are never compared; there
  is no cross-period fallback.

**Matching policy (exact-match-first).**

1. Exactly one row with the same normalized key: equal total GST →
   `MATCHED` (`exact`). With a positive configured tolerance, a
   non-zero absolute difference within tolerance is still `MATCHED`
   (`exact_tolerance`) and the signed difference is stored in the
   result. Beyond tolerance → `AMOUNT_MISMATCH`.
2. Multiple rows with the same normalized key → `NEEDS_REVIEW`
   (`DUPLICATE_GSTR2B_CANDIDATES`); no row is selected.
3. No same-number row: candidate search is limited to rows with the
   same GSTIN **and** same tax period whose total GST is within
   tolerance. A single such row → `NEEDS_REVIEW`
   (`candidate_amount`, reason `CANDIDATE_NOT_EXACT_INVOICE_NUMBER`);
   it is never auto-accepted. Several → `NEEDS_REVIEW`
   (`AMBIGUOUS_CANDIDATES`). None → `MISSING_FROM_2B`.
4. Multiple eligible purchase invoices sharing a normalized key →
   `NEEDS_REVIEW` (`DUPLICATE_PURCHASE_INVOICE`) for each; none is
   chosen arbitrarily.

**Amounts.** All comparisons use integer paise. The difference is
signed `invoice.total_gst - gstr2b_row.total_gst`. The tolerance is an
integer number of paise, configurable in the UI/persistence call and
defaulting to **zero** (exact equality), consistent with the project's
zero-tolerance GST arithmetic. An accepted non-zero difference is never
hidden: it is stored and displayed with `AMOUNT_WITHIN_TOLERANCE`.

**Persistence.** `reconciliation_runs` stores run id, selected batch,
tolerance, per-status counts, `created_at` and an `is_latest` flag (a
partial unique index guarantees at most one latest run). A new run
clears the previous latest flag inside the same transaction, so history
is never overwritten. `reconciliation_results` stores run id, invoice
id, matched GSTR-2B row id (nullable), normalized natural-key snapshots,
invoice/2B GST in paise, signed difference, status, method, JSON reason
codes and a timestamp. Foreign keys, indexes, status/method CHECK
constraints and `UNIQUE(run_id, invoice_id)` are enforced. Run + all
results commit atomically; failures roll back completely. Matching only
reads invoices and GSTR-2B rows.

**Statuses:** `MATCHED`, `AMOUNT_MISMATCH`, `MISSING_FROM_2B`,
`NEEDS_REVIEW`, `NOT_ELIGIBLE`. **Methods:** `exact`,
`exact_tolerance`, `candidate_amount`, `none`, `ineligible`.

**UI.** The reconciliation section requires a selected import batch and
offers a paise tolerance input (default 0). It shows run history with
the latest run marked, per-status counts with exact rupee totals, and an
invoice-level evidence table. If no invoices, no batch rows, no valid
2B rows, or no eligible invoices exist, the run is refused with a
warning instead of reporting success; a run with zero matches is
reported explicitly as zero matches. `MISSING_FROM_2B` means only that
this batch has no usable counterpart — it does **not** prove supplier
non-filing, ITC eligibility, recoverability, or that anything was
"recovered". No AI is used to diagnose mismatches.

## Supplier follow-ups (Task 6) — drafts only

`followups.py` is a pure, deterministic planner. Generating, editing or
approving a draft sends nothing. A draft is prepared for human review,
and `APPROVED` means it is eligible for the separately gated Task 7
delivery section; approval itself never contacts a provider. Draft
statuses are exactly:

| Status | Meaning |
| --- | --- |
| `DRAFT` | Generated and clean; may be edited, approved or skipped |
| `NEEDS_REVIEW` | Generated but approval is blocked (missing phone, missing/duplicate supplier identity); may be edited or skipped |
| `APPROVED` | Explicitly approved for delivery review; no message sent yet; no longer editable |
| `SKIPPED` | Excluded from follow-up; no message sent |

**Source of truth.** Only the **latest reconciliation run for the
explicitly selected GSTR-2B batch** is used. Older runs of the same
batch are preserved as history but are not used for new drafts, and
approving a draft from a superseded run is blocked (`STALE_RECONCILIATION_RUN`).

**Eligibility (all required).**

- the purchase invoice still has `validation_status='valid'`;
- its result is `MISSING_FROM_2B` with no attached GSTR-2B row, no
  amount difference, match method `none`, and exactly the
  `NO_2B_ROW_FOUND` reason code;
- invoice number, date and tax period are present.

`MATCHED`, `AMOUNT_MISMATCH`, `NEEDS_REVIEW`, `NOT_ELIGIBLE`, invalid/
unreviewed invoices, and ambiguous or duplicate evidence are excluded.
The UI lists every excluded invoice and the reason; nothing is silently
dropped. A missing 2B row still does not establish supplier fault, ITC
eligibility or recoverability.

**Draft content.** Eligible invoices are grouped by `supplier_id` into
one neutral English (`language='en'`) draft per supplier, built only from
stored supplier/invoice data: invoice numbers, dates, tax periods,
taxable values, GST amounts, total GST, and the batch/run reference. The
text asks the supplier to verify reporting status and explicitly states
that it is a data-verification request, does not imply default, does not
confirm ITC eligibility or recoverability, and is not tax advice. No
contact details are invented; when `contact_phone` is absent the draft
is still prepared for review but the delivery channel is
`unavailable` and approval is blocked.

**Persistence.** Tables `followup_drafts`, `followup_draft_invoices` and
`followup_draft_events`:

- `followup_drafts` snapshots supplier name/GSTIN/phone, draft text,
  language, status, review reasons, inclusion reason, totals, run id and
  batch id, with `UNIQUE(reconciliation_run_id, supplier_id)`.
- `followup_draft_invoices` stores the associated invoice id,
  reconciliation result id, invoice-number/date/period snapshots, exact
  taxable and GST paise, and the per-invoice inclusion reason. Unique per
  `(draft_id, invoice_id)`.
- `followup_draft_events` records `CREATED`, `EDITED` and
  `STATUS_CHANGED` events with from/to status and timestamp.

Foreign keys, CHECK constraints (statuses, non-negative integer money,
channel flag) and indexes are enforced. Generation inserts a draft, its
associations and its creation event in one transaction. Re-running
generation for the same run is idempotent: existing drafts are left
untouched (including edits/status) and no duplicate draft is created.

**Approval semantics.** Approval requires (a) an explicit confirmation
checkbox in the UI and `confirmed=True` at the persistence layer,
(b) `DRAFT` status, (c) an available delivery channel, (d) no review
reasons, (e) the batch's latest run, and (f) re-verified evidence — every
associated invoice still `valid`, supplier id/GSTIN unchanged, a current
phone number still on file, and every result still an unambiguous
missing row. Failure leaves the status unchanged and no
message is sent. Edits and status changes are persisted with audit
events. Skipped or approved drafts are not editable.

## Approved-draft delivery history (Task 7)

`delivery.py` contains the provider integration and its fail-closed
configuration checks. The current app UI does not construct the live
provider or make outbound WhatsApp requests. `db.py` persists delivery
state and the UI can create mock-only records. Importing the app, opening
the delivery section, approving a draft, or rerunning Streamlit never
sends anything.

**Provider configuration and fail-closed behavior.** The service module
validates these variables, but the app UI keeps live delivery disabled.
Variables are
`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_API_VERSION`, `WHATSAPP_ENABLED`, and optional
`WHATSAPP_MAX_ATTEMPTS` (default 3, maximum 5). `WHATSAPP_ENABLED`
defaults to false. Configuration cannot enable live delivery through the
app. `.env` files are **not loaded automatically**; export the
variables in the shell that starts Streamlit (or configure them through
your deployment environment). Never commit or log the token.

The configured API version is required and validated against
`v<major>.<minor>`; MILAAN does not guess or hardcode a currently
supported Meta version. The endpoint is
`https://graph.facebook.com/{api_version}/{phone_number_id}/messages`.
The message is a WhatsApp text message built from the frozen approved
draft. No contact details are invented, and a phone number on file does
not establish consent or ownership.

**Eligibility and frozen approval snapshot.** Delivery requires
`APPROVED` status and revalidates the current database at send/retry
time: the batch's latest reconciliation run, valid invoices, unambiguous
`MISSING_FROM_2B` evidence, unchanged supplier id/GSTIN, and a phone on
file matching the approved recipient. Approval snapshots the exact text,
recipient and timestamp into `followup_drafts`; later edits cannot
silently change what is sent. The migration is additive and preserves
data. A draft approved before Task 7 that has no snapshot is blocked
with `APPROVAL_SNAPSHOT_MISSING`.

**Idempotency and concurrency.** The idempotency key is a SHA-256 of the
canonical approved snapshot (draft id, approval timestamp, exact text,
normalized recipient, provider) and is `UNIQUE` in SQLite. Creation
returns the existing record for duplicate clicks, and an atomic
`QUEUED`/`RETRY_PENDING` → `SENDING` claim ensures only one attempt.
Only one accepted or unresolved delivery may block a draft from getting
a second unrelated record.

**Statuses and outcomes.**

| Status | Meaning |
| --- | --- |
| `QUEUED` | Persisted, not yet claimed |
| `SENDING` | Atomically claimed; an attempt is in progress |
| `SENT` | Provider returned a valid message ID; Meta accepted the request |
| `RETRY_PENDING` | Safe transient failure recorded with a finite attempt budget |
| `FAILED` | Terminal failure or exhausted retries |
| `UNKNOWN` | Ambiguous outcome; requires operator resolution |
| `DELIVERED` / `READ` | Reserved for authenticated webhook evidence; never produced by this build |

A 2xx response is only recorded `SENT` when it contains a non-empty
provider message ID; a 2xx without one is `UNKNOWN`. Timeouts and
ambiguous transport failures are `UNKNOWN` and are **never** retried
automatically. HTTP 429 and definitive 5xx responses may become
`RETRY_PENDING`, honouring `Retry-After` or capped exponential backoff
(30s doubling, 900s cap). Retries are manual, only when due and only
while attempts remain. Authentication, invalid-recipient and other
definitive 4xx failures are `FAILED` without automatic retry.

An operator may resolve `UNKNOWN` only with provider evidence:
`SENT_CONFIRMED` requires a provider message ID and records `SENT`;
`NOT_SENT` records `RETRY_PENDING` if attempts remain, otherwise
`FAILED`. Resolution never records `DELIVERED`. A webhook receiver is
**deferred** because authenticated, replay-safe webhook verification is
not implemented; `SENT` therefore does not mean the message was
delivered or read.

**Persistence.** `message_deliveries` stores the draft FK, idempotency
key, frozen recipient/body, provider (`whatsapp_cloud_api` or `mock`),
status, attempt/max counts, provider message ID, last HTTP status,
sanitized error code/message (maximum 300 characters), next-attempt
time, lifecycle timestamps, and manual-resolution fields. It never
stores the access token, authorization headers, raw provider responses,
or arbitrary request bodies. Foreign keys cascade with the draft;
indexes cover draft/status/provider/next-attempt/provider-message ID.
`followup_drafts` additionally stores the three approval snapshot
columns.

**UI.** The delivery section states that live delivery is disabled and
offers a clearly labelled **simulated (mock)** action. It shows the frozen
message and recipient, requires confirmation, and labels mock results as
simulated. Delivery history shows provider IDs, attempts, statuses,
next-attempt time and sanitized errors. Mock retry requires confirmation;
historical real records cannot be retried from the UI. UNKNOWN resolution requires an operator,
evidence note and (for acceptance) a provider message ID. There is no
automatic send, no scheduler and no webhook receiver.

Tests use fake transports/providers and assert no network access. They
cover configuration fail-closed behavior, frozen snapshots, stale
evidence, duplicate/concurrent claims, missing provider IDs, 429/5xx
retry bounds, timeouts as `UNKNOWN`, manual resolution, 4xx terminal
failures, secret redaction, FK integrity and the absence of real sends.

## Real-world validation, benchmark and demo (Task 9)

Task 9 does not change the matching rules. It adds reproducibility and a
more honest evidence trail:

- `fixtures/` contains synthetic CSV/XLSX variations: UTF-8 BOM + CRLF,
  extra columns, invalid rows, duplicates, decimal values, an active
  worksheet that is not the first sheet, empty/header-only files, and
  malformed files. See `fixtures/README.md`.
- `test_realworld.py` covers those fixtures plus a Streamlit freshness
  check proving import/reconciliation update visible totals.
- `benchmark.py` runs 15 invoices against 14 normalized GSTR-2B rows
  through the real validation, import, reconciliation and follow-up
  services on a temporary database. It reports measured precision,
  recall, exact-match, amount-mismatch, invalid-exclusion, ambiguous-case,
  follow-up, duplicate-import and repeated-run results, plus a simple
  GSTIN + invoice-number baseline.
- `EVALUATION.md` records the measured results and limitations.
- `demo/setup_demo.py` resets an isolated synthetic database and writes
  `demo/DEMO_GSTR2B.csv`; `DEMO.md` is the judge walkthrough.

Run the benchmark:

```bash
.venv/bin/python benchmark.py
```

Current benchmark result is 3/3 true positives, 0 false positives and
0 false negatives on 9 clear cases (precision 1.00, recall 1.00); the
5 ambiguous cases all went to review and the 1 invalid invoice was
excluded. These are synthetic results, not a production accuracy claim.

Run the isolated demo:

```bash
.venv/bin/python demo/setup_demo.py
DATABASE_PATH="$PWD/demo/milaan_demo.db" .venv/bin/streamlit run app.py
```

`demo/milaan_demo.db` is separate from `milaan.db`. The demo creates two
clean drafts; approve one and leave the other in `DRAFT`. Delivery remains
disabled/simulated and no message is sent.

**Task 9 limitations.** The fixtures are normalized synthetic inputs, not
GST portal exports; actual portal compatibility is unverified. GSTIN
checks are syntax/checksum only. There is no OCR/extraction pipeline, so
extraction accuracy is not measured (reported as `null`). The benchmark is
small and designed alongside the rules, not a blind real-world holdout.
The Streamlit tests use AppTest, not a real browser.

## Validation (Task 3)

`validation.py` is a pure, deterministic module (no DB access) that
returns structured results: `status` (`valid` / `invalid` /
`needs_review`), field-level `errors`, `warnings`, and explanatory
`reasons`. `db.revalidate_invoice()` / `db.revalidate_all_invoices()`
persist the outcome; the Streamlit "Validate all invoices" button calls
them and shows failures, never swallowing them.

Checks performed:

1. **GSTIN** — structure (state code, PAN block, entity number, `Z`,
   check character), valid state code, and the documented base-36 GSTN
   check digit. **This is format/checksum validation only; it does NOT
   verify live GST registration or supplier ownership.** Fabricated
   seed GSTINs such as `27FAKE1000F1Z5` are deliberately NOT converted
   and are flagged invalid.
2. **Required fields** — invoice number, ISO invoice date, `YYYY-MM`
   tax period.
3. **Money** — integer paise only; floats, booleans and negative values
   are rejected (`MONEY_NOT_INTEGER`, `NEGATIVE_AMOUNT`). No binary
   floating-point arithmetic is used anywhere.
4. **Rate** — supported rates {0, 5, 12, 18, 28} and consistency with
   the taxable value.
5. **Tax structure** — CGST/SGST and IGST cannot both be charged;
   intra-state CGST must equal SGST.
6. **Arithmetic** — `total_gst = cgst+sgst+igst`, and
   `invoice_total = taxable_value + total_gst` exactly.
7. **Duplicates** — same (supplier, invoice number, tax period) as an
   existing row, matching the DB UNIQUE rule.

Rounding policy: expected tax is `round_half_up(taxable * rate / 100)`
in integer paise. A 1-paise difference (possible with component-wise
rounding) yields `needs_review` with a `ROUNDING_TOLERANCE` warning;
larger differences are invalid. `invoice_total` has zero tolerance
because every value is an integer number of paise.

Messaging gate: `validation.can_message_supplier(status)` returns True
only for `valid`. Invalid, needs-review, pending and mismatch invoices
are blocked from supplier follow-up by design; delivery additionally
requires approval, current evidence revalidation and explicit operator
confirmation.

## Data model (Task 2)

- `suppliers`: id, supplier_name, gstin (unique), contact_phone, created_at
- `invoices`: FK to suppliers (`ON DELETE CASCADE`), invoice_number,
  invoice_date, tax_period, taxable_value, gst_rate, cgst, sgst, igst,
  total_gst, invoice_total, source, validation_status, case_status,
  created_at. Unique per (supplier_id, invoice_number, tax_period).
  CHECK constraints: non-negative amounts, `total_gst = cgst+sgst+igst`,
  gst_rate in {0,5,12,18,28}, whitelisted statuses, and all monetary
  columns must be integers.
- **Money is integer paise** — `db.format_paise()` renders it as rupees
  using integer arithmetic (no floats). gst_rate is an integer percent.
- Databases created with the Task 1 legacy `suppliers(name)` schema are
  migrated in place on `init_db()` (data preserved, atomic transaction).
  Task 2 databases are likewise rebuilt in place to allow the
  `needs_review` validation status; rows are preserved.

Demo suppliers use checksum-valid synthetic GSTINs (`DEMO_SUPPLIERS`:
`27AAACS1234A1Z2`, `29AAACB5678B1ZP`, `24AAACD9012C1ZB`), so the
deterministic validator confirms them as structurally valid. They are
still fabricated and do not correspond to real registrations.
`seed_demo_data()` seeds six synthetic invoices: five validate as valid
and one (`INV-DEMO-201`) is intentionally invalid because its date is
`2026-04-31`. Seeding is idempotent (WHERE NOT EXISTS on natural keys,
one all-or-nothing transaction).

## Stack

- Python 3.14 (Homebrew), venv at `.venv/`
- Streamlit 1.66.0
- openpyxl 3.1.5 (XLSX reading; pinned with et_xmlfile 2.0.0)
- requests 2.34.2 (WhatsApp Cloud API transport only; no other HTTP use)
- SQLite via Python stdlib (`sqlite3`), path from `DATABASE_PATH` env var
  (defaults to `milaan.db` in the project root; relative values resolve
  from the project root regardless of the launch directory)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Production operation

Set `APP_ENV=production` for a deployed instance. This requires OIDC sign-in
by default and prevents the application from serving the workspace before a
user has authenticated. Copy `.streamlit/secrets.example.toml` to
`.streamlit/secrets.toml`, replace its placeholders using an approved OIDC
provider registration, and keep the resulting file out of version control.
Set `MILAAN_ALLOWED_EMAIL_DOMAINS` when access should be limited to one or
more company email domains. Do not enable `MILAAN_AUTH_REQUIRED=false` in a
production deployment.

The container configuration starts as an unprivileged user, stores SQLite
data at `/data/milaan.db`, enables CORS/XSRF protections, hides internal
error details, and includes a SQLite integrity/foreign-key health check:

```bash
docker build -t milaan .
docker run --rm -p 8501:8501 \
  -v "$PWD/runtime-data:/data" \
  -v "$PWD/.streamlit/secrets.toml:/app/.streamlit/secrets.toml:ro" \
  milaan
```

Create a consistent backup without stopping the application. The destination
must already exist; the script refuses to overwrite a backup and verifies its
SQLite integrity before reporting success:

```bash
DATABASE_PATH=/srv/milaan/milaan.db .venv/bin/python scripts/backup_database.py /srv/milaan-backups
```

MILAAN accepts the normalized import schema and common accounting-export
column labels such as `GSTIN of supplier`, `Invoice No`, `Invoice Dt`,
`Return Period`, `Taxable Amount`, component tax amounts, and `Total Tax`.
It still does not claim compatibility with every GST portal export; validate
an actual export in a non-production database before relying on it.

Environment variables are documented in `.env.example`. The app does
**not** auto-load `.env`; export variables in your shell or deployment
environment before starting Streamlit. `.env` is gitignored; never commit
real secrets. Live WhatsApp delivery remains disabled in the app regardless
of environment settings; only clearly labelled mock records are available.

## Run

```bash
.venv/bin/streamlit run app.py
```

Then open http://localhost:8501. The app uses `st.navigation` with a left
sidebar; each page has its own URL:

- **Dashboard** (`/`) - workspace counts, GSTR-2B evidence, and an
  attention table of invoices that need action.
- **Purchase invoices** (`/purchase-invoices`) - CSV/XLSX import, template
  download, "Validate all invoices", and validation results by tab.
- **GSTR-2B import** (`/gstr2b-import`) - stage a GSTR-2B file, batch
  history, and row-level issues.
- **Reconciliation** (`/reconciliation`) - select a batch, set a paise
  tolerance (0 = exact), run reconciliation, and inspect run history.
- **Mismatches** (`/mismatches`) - invoice-level evidence for
  non-matched results in the selected run.
- **Follow-ups** (`/follow-ups`) - eligibility groups, draft generation,
  edit/approve/skip, and later-statement verification.
- **Suppliers** (`/suppliers`), **Audit log** (`/audit-log`) and
  **Settings** (`/settings`) - directory, event trail, and workspace
  status including the isolated demo workspace and mock-only delivery
  history.

Nothing is transmitted by the app.

To import a GSTR-2B file: prepare a CSV/XLSX using the nine-column
schema above, upload it, and click "Import GSTR-2B file". If the exact
file was uploaded before, confirm the intentional re-import checkbox;
otherwise the button stays disabled and the previous batches are shown.
Import results, batch history and row-level issues appear below the
uploader.

Validation failures are listed with error codes; invoices are grouped
into All / Valid / Needs review / Invalid / Pending tabs. A valid result
means the deterministic field checks passed; follow-up eligibility also
requires a qualifying result in the selected reconciliation run.

To reconcile, validate the invoices first, select a GSTR-2B batch,
optionally set a paise tolerance (0 = exact), and click "Reconcile
selected batch". The UI then shows the persisted run history (latest
marked), count/rupee totals for matched, amount-mismatched, missing and
review-required invoices, and the invoice-level evidence table. A run is
refused when there are no invoices, no rows in the selected batch, no
valid GSTR-2B rows, or no eligible invoices.

To prepare follow-ups, select the same batch in the follow-up section.
The UI previews eligible supplier groups and draft text, lists excluded
invoices with reasons, and generates drafts idempotently. Each saved
draft can be edited, approved (behind an explicit confirmation checkbox)
or skipped; every change is persisted with an audit event. Approval is
disabled when the channel/review/evidence checks fail. The UI states on
every draft that no message has been sent yet.

To exercise delivery UI state, select an approved draft and use the
simulated action. It never uses the network. Live delivery is disabled in
this build regardless of `WHATSAPP_*` settings.
The attempt is persisted before/after the provider call. If the result is
`RETRY_PENDING`, use the manual retry control only when it is due; if it
is `UNKNOWN`, use the manual resolution form only with authoritative
provider evidence. There is no automatic retry and no delivery/read
webhook, so `SENT` means only that Meta accepted the request.

## Later-statement verification

The later-statement panel can compare an approved follow-up with a separate
imported GSTR-2B batch. It uses the existing strict CSV/XLSX importer and
integer-paise values, then records one audit check per linked invoice
and later batch. A unique supplier GSTIN + normalized invoice-number match
is reported as present only when the GST amount also agrees; differing
amounts, duplicates and missing rows have separate outcomes. This is
evidence scoped to the selected batch, not proof of supplier non-filing,
ITC eligibility or recovery. Verification does not change invoice
validation, reconciliation results, follow-up approval, or delivery state.

## Tests

```bash
.venv/bin/python -m unittest test_db test_validation test_invoice_import test_gstr2b test_reconciliation test_followups test_e2e test_delivery test_later_statement test_security test_realworld test_demo_workflow test_benchmark -v
```

The same 272 tests (100 of them parameterized subtests) also run under
pytest, which is not a runtime dependency and can be installed only for
development:

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

272 tests: schema/constraints, seeding idempotency, legacy migrations
(Task 1 suppliers, Task 2 validation-status CHECK, Task 7 additive
approval snapshots), the full validation rule set (GSTIN format/checksum,
money exactness, arithmetic, tax structure, rounding boundaries,
duplicates, uncertain cases), GSTR-2B CSV/XLSX parsing,
malformed/invalid money handling, audit-trail persistence, hash-based
repeat-upload detection, transactional rollback, reconciliation
normalization (case/whitespace, tax periods), exact and tolerance-boundary
matching, GSTIN/period mismatch, ineligible invoices, duplicate/ambiguous
candidates, missing rows, run-history preservation, rollback,
foreign-key/status integrity, source-record immutability, follow-up
eligibility gates, supplier grouping, neutral wording, missing-contact
handling, latest-run/stale handling, idempotent repeated generation,
edit/approve/skip persistence, rollback, FK constraints, and the absence
of any outbound send function in `db.py`/`followups.py`. Delivery tests
cover fail-closed configuration, frozen snapshots, stale evidence,
idempotency/concurrent claims, missing provider IDs, 429/5xx retry bounds,
timeouts as `UNKNOWN`, manual resolution, terminal 4xx failures, secret
redaction, FK cascade integrity, and mock/simulated behavior with fake
transports. End-to-end tests cover validation, import, reconciliation,
draft generation, approval, exact and amount-mismatch exclusions, invalid
invoice exclusion, duplicate-file imports, idempotent regeneration and
stale-evidence approval blocking. Task 9 tests cover CSV/XLSX variants,
active-worksheet behavior, malformed inputs, UI freshness, benchmark
reproducibility, ground-truth statuses, baseline measurements and the
absence of OCR/real-send claims. Streamlit AppTest exercises seed,
validation, import, reconciliation, draft-generation, approval and
mock-delivery flows via `streamlit.testing.v1` (AppTest, not a real
browser). No test performs a real WhatsApp request or uses real taxpayer
data.

Later-statement tests cover approved-draft gating, unique/duplicate/missing
matches, amount differences, idempotency and source-record immutability.

## Project layout

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit entry point and page renderers; `main()` wires the `st.navigation` sidebar |
| `pages/` | Thin page wrappers (dashboard, purchase invoices, GSTR-2B, reconciliation, mismatches, follow-ups, suppliers, audit log, settings) |
| `db.py` | SQLite schema, migrations, seed, validation/import/reconciliation/follow-up/verification/delivery persistence |
| `delivery.py` | WhatsApp config, HTTP provider, deterministic mock, idempotent delivery/retry/UNKNOWN orchestration |
| `validation.py` | Pure deterministic invoice/GSTIN validation service |
| `invoice_import.py` | CSV/XLSX purchase-invoice parser, normalized columns and downloadable template |
| `gstr2b.py` | Pure GSTR-2B CSV/XLSX parser (normalized schema) |
| `reconciliation.py` | Pure deterministic invoice-to-GSTR-2B matcher |
| `followups.py` | Pure draft planning, eligibility and neutral message wording |
| `demo_workflow.py` | In-app synthetic demo workspace (seed, validate, import, reconcile; no drafts or sends) |
| `test_db.py` | DB + Streamlit AppTest suite |
| `test_validation.py` | Validation rules, DB integration, migration tests |
| `test_invoice_import.py` | Invoice CSV/XLSX parsing, template and persistence tests |
| `test_gstr2b.py` | GSTR-2B parsing, persistence, rollback tests |
| `test_reconciliation.py` | Matching, normalization, persistence, run-history tests |
| `test_followups.py` | Eligibility, drafts, approval/skip, history, no-send tests |
| `test_e2e.py` | Core invoice-to-follow-up workflow and Streamlit approval tests |
| `test_demo_workflow.py` | Demo workspace seeding, counts and no-send tests |
| `test_delivery.py` | Config, provider, retry, idempotency, snapshot, UI and no-send tests |
| `test_later_statement.py` | Later-batch evidence checks for approved follow-up drafts |
| `test_security.py` | Production access policy and operational audit tests |
| `security.py` | Environment-driven OIDC access policy |
| `scripts/backup_database.py` | Consistent SQLite backup with integrity verification |
| `INTEGRATION_REVIEW.md` | Source inventory, integration choices and attribution limitation |
| `benchmark.py` | Deterministic synthetic benchmark with measured metrics and baseline |
| `test_benchmark.py` | Benchmark reproducibility and metric regression tests |
| `test_realworld.py` | Fixture compatibility and Streamlit freshness tests |
| `fixtures/` | Synthetic CSV/XLSX edge-case fixtures and their README |
| `demo/setup_demo.py` | Reset/seed isolated demo database and write demo GSTR-2B CSV |
| `EVALUATION.md` | Measured evaluation results, baseline and limitations |
| `DEMO.md` | Repeatable judge demo script |
| `.env.example` | Documented environment variables |
#   m i l a a n  
 