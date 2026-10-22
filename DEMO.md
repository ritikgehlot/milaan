# MILAAN judge demo

A repeatable, fully synthetic walkthrough of the core workflow:

1. Seed an isolated demo database.
2. Import a normalized GSTR-2B CSV with one rejected row.
3. Reconcile purchase invoices.
4. Generate two clean supplier drafts.
5. Approve one draft for delivery review and leave the other in `DRAFT`.
6. Verify that no WhatsApp message was sent.

The demo uses only `demo/milaan_demo.db` and never touches `milaan.db`.

## Setup

Stop any running Streamlit instance first. Run from the project root:

```bash
.venv/bin/python demo/setup_demo.py
```

This resets only the demo database and writes `demo/DEMO_GSTR2B.csv`.
Expected setup result: 4 synthetic suppliers, 5 invoices, 4 valid and
1 invalid; no import, reconciliation, draft or delivery.

Start the isolated app:

```bash
DATABASE_PATH="$PWD/demo/milaan_demo.db" \
  .venv/bin/streamlit run app.py
```

Open the URL Streamlit prints (normally `http://localhost:8501`). Open the
**Settings** page and confirm the database path ends in
`demo/milaan_demo.db`.

**Safety:** do not configure WhatsApp credentials for this demo. Do not
click a send action. The delivery section remains disabled or simulated.

## Demo script

### 1. Show seeded data

- Invoices: **5**.
- Validation: **4 valid, 0 needs review, 1 invalid**.
- `DEMO-INVALID-001` has an invalid date (`2026-02-30`), so it cannot be
  matched or used for follow-up.
- No GSTR-2B batches, reconciliation runs or follow-up drafts yet.

Talking point: validation is deterministic field/checksum/arithmetic
checking, not a claim of live GST registration verification.

### 2. Import GSTR-2B

- Upload `demo/DEMO_GSTR2B.csv`.
- Click **Import GSTR-2B file**.
- Expected: **Batch #1 imported: 2 of 3 row(s) accepted, 1 rejected**.
- Expand **Row-level issues** and show the rejected bad row. Its negative
  amount and bad GSTIN are preserved in the audit trail; the file is not
  silently accepted.

Talking point: the supported input is MILAAN's normalized schema, not an
arbitrary real portal export.

### 3. Reconcile

- Select batch #1.
- Leave tolerance at **0 paise**.
- Click **Reconcile selected batch**.
- Expected run:
  - **1 matched**
  - **1 amount mismatch**
  - **2 missing from 2B**
  - **0 needs review**
  - **1 not eligible**

Show the evidence table:

| Invoice | Expected result |
| --- | --- |
| `DEMO-EXACT-001` | `MATCHED` |
| `DEMO-AMOUNT-001` | `AMOUNT_MISMATCH`, difference `-50.00` |
| `DEMO-MISSING-001` | `MISSING_FROM_2B` |
| `DEMO-MISSING-002` | `MISSING_FROM_2B` |
| `DEMO-INVALID-001` | `NOT_ELIGIBLE` |

Talking point: `MISSING_FROM_2B` means only that this batch has no usable
counterpart. It does not prove supplier non-filing, ITC eligibility or
recoverability.

### 4. Generate follow-ups

- In **Supplier follow-ups**, select batch #1.
- Show the preview: two clean supplier groups, one invoice each, both with
  a phone on file.
- Click **Generate follow-up drafts**.
- Expected: **2 drafts created**. Both start as `DRAFT`; nothing is sent.

Open the **Ineligible invoices and why** expander. Show that the matched,
amount-mismatched and invalid invoices were excluded with explicit reason
codes.

### 5. Approve one draft, leave one clean draft

- Select the Meridian Components draft.
- Show the neutral wording and supporting invoice evidence.
- Check **Confirm approval (no send occurs)**.
- Click **Approve for delivery review**.
- Expected success message: **APPROVED for delivery review. No message has
  been sent.**
- Leave the second draft in `DRAFT` to demonstrate the pre-approval state.

Show the draft status history: `CREATED -> DRAFT -> APPROVED` with a note
that no message was sent.

Do not proceed to delivery. Leave WhatsApp disabled/simulated.

## Reset between judges

Stop the app, then rerun:

```bash
.venv/bin/python demo/setup_demo.py
```

Start the app again with the same `DATABASE_PATH` command. The demo returns
to a clean state with no batches, runs, drafts or deliveries.

## What the demo proves

- End-to-end deterministic import, reconciliation, draft creation and
  approval.
- Exact, amount-mismatch, missing, invalid and review paths are explicit.
- Invalid rows and invoices are excluded, not coerced.
- Drafts require human approval and no external send occurs.
- The demo is isolated from the user database and uses synthetic data only.

## What it does not prove

- Compatibility with genuine GST portal export layouts.
- Live GSTIN validity, ITC eligibility or supplier fault.
- OCR or arbitrary invoice extraction (no such pipeline exists).
- Real WhatsApp delivery.
- Production-scale or real-world accuracy; see `EVALUATION.md`.
