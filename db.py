"""SQLite persistence layer for MILAAN.

Uses the stdlib sqlite3 module. The database file path is read from the
DATABASE_PATH environment variable (see .env.example), defaulting to
"milaan.db" in the project root.

Task 2: supplier + purchase-invoice data models, migration from the
Task 1 legacy suppliers schema, and an idempotent synthetic seed.
Task 3: validation-status integration (needs_review), migration of the
Task 2 CHECK constraint, and helpers that run deterministic validation
against stored invoices.
Task 4: GSTR-2B staging tables plus transactional batch/row persistence.
Task 5: immutable reconciliation runs/results with audit evidence; source
invoices and imported GSTR-2B rows are never modified by matching.
Task 6: supplier follow-up drafts, invoice associations and status-change
audit events.
Task 7: approval snapshots and fail-closed WhatsApp delivery state,
idempotency, retry accounting and manual UNKNOWN resolution. Provider
networking lives only in delivery.py.

All monetary amounts are stored as integer paise (1 rupee = 100 paise),
per the schema CHECK constraints that forbid floats. gst_rate is an
integer percent (0, 5, 12, 18 or 28).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

import followups
import reconciliation
import validation

PROJECT_ROOT = Path(__file__).resolve().parent

# Accepted values for invoices.validation_status. "mismatch" is reserved
# for the later GSTR-2B reconciliation task.
VALIDATION_STATUSES = ("pending", "valid", "invalid", "needs_review", "mismatch")

# Task 7 delivery statuses. DELIVERED/READ have no product transition:
# they would require an authenticated provider webhook, which is deferred.
MESSAGE_DELIVERY_STATUSES = (
    "QUEUED",
    "SENDING",
    "SENT",
    "DELIVERED",
    "READ",
    "RETRY_PENDING",
    "FAILED",
    "UNKNOWN",
)

# Statuses a normal send attempt may record. DELIVERED/READ are deliberately
# absent: only authenticated provider evidence could produce them.
SEND_RESULT_STATUSES = ("SENT", "RETRY_PENDING", "FAILED", "UNKNOWN")

SUPPLIERS_DDL = """
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_name TEXT NOT NULL,
    gstin TEXT,
    contact_phone TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

INVOICES_DDL = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    tax_period TEXT NOT NULL,
    taxable_value INTEGER NOT NULL,
    gst_rate INTEGER NOT NULL,
    cgst INTEGER NOT NULL DEFAULT 0,
    sgst INTEGER NOT NULL DEFAULT 0,
    igst INTEGER NOT NULL DEFAULT 0,
    total_gst INTEGER NOT NULL DEFAULT 0,
    invoice_total INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    validation_status TEXT NOT NULL DEFAULT 'pending',
    case_status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,
    UNIQUE (supplier_id, invoice_number, tax_period),
    CHECK (taxable_value >= 0 AND cgst >= 0 AND sgst >= 0 AND igst >= 0
           AND total_gst >= 0 AND invoice_total >= 0),
    CHECK (cgst + sgst + igst = total_gst),
    CHECK (gst_rate IN (0, 5, 12, 18, 28)),
    CHECK (validation_status IN ('pending', 'valid', 'invalid',
                                 'needs_review', 'mismatch')),
    CHECK (case_status IN ('open', 'reconciled', 'follow_up', 'resolved')),
    CHECK (typeof(taxable_value) = 'integer' AND typeof(gst_rate) = 'integer'
           AND typeof(cgst) = 'integer' AND typeof(sgst) = 'integer'
           AND typeof(igst) = 'integer' AND typeof(total_gst) = 'integer'
           AND typeof(invoice_total) = 'integer')
);
"""

SYSTEM_META_DDL = """
CREATE TABLE IF NOT EXISTS system_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SYSTEM_AUDIT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS system_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (length(actor) <= 320),
    CHECK (length(event_type) <= 80),
    CHECK (length(subject_type) <= 80),
    CHECK (length(subject_id) <= 160),
    CHECK (length(details) <= 4000)
);
CREATE INDEX IF NOT EXISTS idx_system_audit_events_created_at
    ON system_audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_system_audit_events_actor
    ON system_audit_events(actor);
"""

INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_gstin ON suppliers(gstin);
CREATE INDEX IF NOT EXISTS idx_invoices_supplier_id ON invoices(supplier_id);
CREATE INDEX IF NOT EXISTS idx_invoices_tax_period ON invoices(tax_period);
CREATE INDEX IF NOT EXISTS idx_invoices_validation_status ON invoices(validation_status);
CREATE INDEX IF NOT EXISTS idx_invoices_case_status ON invoices(case_status);
"""

GSTR2B_IMPORTS_DDL = """
CREATE TABLE IF NOT EXISTS gstr2b_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    source_format TEXT NOT NULL CHECK (source_format IN ('csv', 'xlsx')),
    total_rows INTEGER NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    accepted_rows INTEGER NOT NULL DEFAULT 0 CHECK (accepted_rows >= 0),
    rejected_rows INTEGER NOT NULL DEFAULT 0 CHECK (rejected_rows >= 0),
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('completed', 'failed')),
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (accepted_rows + rejected_rows = total_rows)
);
"""

GSTR2B_ROWS_DDL = """
CREATE TABLE IF NOT EXISTS gstr2b_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL,
    row_number INTEGER NOT NULL CHECK (row_number >= 1),
    supplier_gstin TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    tax_period TEXT,
    taxable_value INTEGER,
    cgst INTEGER,
    sgst INTEGER,
    igst INTEGER,
    total_gst INTEGER,
    validation_status TEXT NOT NULL
        CHECK (validation_status IN ('valid', 'invalid', 'needs_review')),
    errors TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]',
    raw_data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (import_id) REFERENCES gstr2b_imports(id) ON DELETE CASCADE,
    UNIQUE (import_id, row_number),
    CHECK (taxable_value IS NULL OR (taxable_value >= 0
           AND typeof(taxable_value) = 'integer')),
    CHECK (cgst IS NULL OR (cgst >= 0 AND typeof(cgst) = 'integer')),
    CHECK (sgst IS NULL OR (sgst >= 0 AND typeof(sgst) = 'integer')),
    CHECK (igst IS NULL OR (igst >= 0 AND typeof(igst) = 'integer')),
    CHECK (total_gst IS NULL OR (total_gst >= 0
           AND typeof(total_gst) = 'integer'))
);
"""

GSTR2B_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_gstr2b_imports_hash
    ON gstr2b_imports(file_hash);
CREATE INDEX IF NOT EXISTS idx_gstr2b_imports_imported_at
    ON gstr2b_imports(imported_at);
CREATE INDEX IF NOT EXISTS idx_gstr2b_rows_import_id
    ON gstr2b_rows(import_id);
CREATE INDEX IF NOT EXISTS idx_gstr2b_rows_validation_status
    ON gstr2b_rows(validation_status);
CREATE INDEX IF NOT EXISTS idx_gstr2b_rows_natural_key
    ON gstr2b_rows(supplier_gstin, invoice_number, tax_period);
"""

RECONCILIATION_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gstr2b_import_id INTEGER NOT NULL,
    amount_tolerance_paise INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(amount_tolerance_paise) = 'integer'
               AND amount_tolerance_paise >= 0),
    invoice_count INTEGER NOT NULL DEFAULT 0 CHECK (invoice_count >= 0),
    matched_count INTEGER NOT NULL DEFAULT 0 CHECK (matched_count >= 0),
    amount_mismatch_count INTEGER NOT NULL DEFAULT 0
        CHECK (amount_mismatch_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    needs_review_count INTEGER NOT NULL DEFAULT 0
        CHECK (needs_review_count >= 0),
    not_eligible_count INTEGER NOT NULL DEFAULT 0
        CHECK (not_eligible_count >= 0),
    is_latest INTEGER NOT NULL DEFAULT 1 CHECK (is_latest IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (gstr2b_import_id) REFERENCES gstr2b_imports(id)
        ON DELETE CASCADE,
    CHECK (matched_count + amount_mismatch_count + missing_count
           + needs_review_count + not_eligible_count = invoice_count)
);
"""

RECONCILIATION_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS reconciliation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    invoice_id INTEGER NOT NULL,
    gstr2b_row_id INTEGER,
    match_status TEXT NOT NULL
        CHECK (match_status IN ('MATCHED', 'AMOUNT_MISMATCH',
                                'MISSING_FROM_2B', 'NEEDS_REVIEW',
                                'NOT_ELIGIBLE')),
    match_method TEXT NOT NULL
        CHECK (match_method IN ('exact', 'exact_tolerance',
                                'candidate_amount', 'none', 'ineligible')),
    amount_difference_paise INTEGER,
    reason_codes TEXT NOT NULL DEFAULT '[]',
    invoice_number TEXT NOT NULL,
    supplier_gstin TEXT,
    tax_period TEXT,
    invoice_total_gst_paise INTEGER NOT NULL
        CHECK (typeof(invoice_total_gst_paise) = 'integer'
               AND invoice_total_gst_paise >= 0),
    gstr2b_invoice_number TEXT,
    gstr2b_total_gst_paise INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id)
        ON DELETE CASCADE,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
    FOREIGN KEY (gstr2b_row_id) REFERENCES gstr2b_rows(id)
        ON DELETE SET NULL,
    UNIQUE (run_id, invoice_id),
    CHECK (amount_difference_paise IS NULL
           OR typeof(amount_difference_paise) = 'integer'),
    CHECK (gstr2b_total_gst_paise IS NULL
           OR (typeof(gstr2b_total_gst_paise) = 'integer'
               AND gstr2b_total_gst_paise >= 0)),
    CHECK (match_status NOT IN ('MATCHED', 'AMOUNT_MISMATCH')
           OR gstr2b_row_id IS NOT NULL)
);
"""

RECONCILIATION_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciliation_runs_latest
    ON reconciliation_runs(is_latest) WHERE is_latest = 1;
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_import_id
    ON reconciliation_runs(gstr2b_import_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_created_at
    ON reconciliation_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_reconciliation_results_run_id
    ON reconciliation_results(run_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_results_invoice_id
    ON reconciliation_results(invoice_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_results_status
    ON reconciliation_results(match_status);
CREATE INDEX IF NOT EXISTS idx_reconciliation_results_row_id
    ON reconciliation_results(gstr2b_row_id);
"""

FOLLOWUP_DRAFTS_DDL = """
CREATE TABLE IF NOT EXISTS followup_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    reconciliation_run_id INTEGER NOT NULL,
    gstr2b_import_id INTEGER NOT NULL,
    supplier_name TEXT NOT NULL,
    supplier_gstin TEXT,
    contact_phone TEXT,
    channel_available INTEGER NOT NULL DEFAULT 0
        CHECK (channel_available IN (0, 1)),
    draft_text TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    status TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'NEEDS_REVIEW', 'APPROVED', 'SKIPPED')),
    review_reasons TEXT NOT NULL DEFAULT '[]',
    inclusion_reason TEXT NOT NULL,
    approved_text TEXT,
    approved_recipient_phone TEXT,
    approved_at TEXT,
    total_taxable_value_paise INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(total_taxable_value_paise) = 'integer'
               AND total_taxable_value_paise >= 0),
    total_gst_paise INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(total_gst_paise) = 'integer'
               AND total_gst_paise >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,
    FOREIGN KEY (reconciliation_run_id)
        REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (gstr2b_import_id) REFERENCES gstr2b_imports(id)
        ON DELETE CASCADE,
    UNIQUE (reconciliation_run_id, supplier_id)
);
"""

FOLLOWUP_DRAFT_INVOICES_DDL = """
CREATE TABLE IF NOT EXISTS followup_draft_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    invoice_id INTEGER NOT NULL,
    reconciliation_result_id INTEGER NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    tax_period TEXT NOT NULL,
    taxable_value_paise INTEGER NOT NULL
        CHECK (typeof(taxable_value_paise) = 'integer'
               AND taxable_value_paise >= 0),
    total_gst_paise INTEGER NOT NULL
        CHECK (typeof(total_gst_paise) = 'integer'
               AND total_gst_paise >= 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (draft_id) REFERENCES followup_drafts(id) ON DELETE CASCADE,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
    FOREIGN KEY (reconciliation_result_id)
        REFERENCES reconciliation_results(id) ON DELETE CASCADE,
    UNIQUE (draft_id, invoice_id)
);
"""

FOLLOWUP_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS followup_draft_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('CREATED', 'EDITED', 'STATUS_CHANGED')),
    from_status TEXT
        CHECK (from_status IS NULL
               OR from_status IN ('DRAFT', 'NEEDS_REVIEW',
                                  'APPROVED', 'SKIPPED')),
    to_status TEXT NOT NULL
        CHECK (to_status IN ('DRAFT', 'NEEDS_REVIEW', 'APPROVED',
                             'SKIPPED')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (draft_id) REFERENCES followup_drafts(id) ON DELETE CASCADE
);
"""

FOLLOWUP_VERIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS followup_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    followup_draft_id INTEGER NOT NULL,
    invoice_id INTEGER NOT NULL,
    original_result_id INTEGER NOT NULL,
    original_import_id INTEGER NOT NULL,
    later_import_id INTEGER NOT NULL,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('PRESENT', 'AMOUNT_MISMATCH', 'NOT_FOUND',
                           'NEEDS_REVIEW')),
    later_row_id INTEGER,
    amount_difference_paise INTEGER,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (followup_draft_id)
        REFERENCES followup_drafts(id) ON DELETE CASCADE,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
    FOREIGN KEY (original_result_id)
        REFERENCES reconciliation_results(id) ON DELETE CASCADE,
    FOREIGN KEY (original_import_id) REFERENCES gstr2b_imports(id),
    FOREIGN KEY (later_import_id) REFERENCES gstr2b_imports(id),
    FOREIGN KEY (later_row_id) REFERENCES gstr2b_rows(id) ON DELETE SET NULL,
    CHECK (amount_difference_paise IS NULL
           OR typeof(amount_difference_paise) = 'integer'),
    UNIQUE (followup_draft_id, invoice_id, later_import_id)
);
"""

FOLLOWUP_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_followup_drafts_supplier_id
    ON followup_drafts(supplier_id);
CREATE INDEX IF NOT EXISTS idx_followup_drafts_run_id
    ON followup_drafts(reconciliation_run_id);
CREATE INDEX IF NOT EXISTS idx_followup_drafts_import_id
    ON followup_drafts(gstr2b_import_id);
CREATE INDEX IF NOT EXISTS idx_followup_drafts_status
    ON followup_drafts(status);
CREATE INDEX IF NOT EXISTS idx_followup_draft_invoices_draft_id
    ON followup_draft_invoices(draft_id);
CREATE INDEX IF NOT EXISTS idx_followup_draft_invoices_invoice_id
    ON followup_draft_invoices(invoice_id);
CREATE INDEX IF NOT EXISTS idx_followup_draft_invoices_result_id
    ON followup_draft_invoices(reconciliation_result_id);
CREATE INDEX IF NOT EXISTS idx_followup_events_draft_id
    ON followup_draft_events(draft_id);
"""

MESSAGE_DELIVERIES_DDL = """
CREATE TABLE IF NOT EXISTS message_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    followup_draft_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    recipient_phone_snapshot TEXT NOT NULL,
    message_body_snapshot TEXT NOT NULL,
    provider TEXT NOT NULL
        CHECK (provider IN ('whatsapp_cloud_api', 'mock')),
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED', 'SENDING', 'SENT', 'DELIVERED', 'READ',
                          'RETRY_PENDING', 'FAILED', 'UNKNOWN')),
    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(attempt_count) = 'integer' AND attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK (typeof(max_attempts) = 'integer' AND max_attempts >= 1),
    provider_message_id TEXT,
    last_http_status INTEGER,
    last_error_code TEXT,
    last_error_message TEXT
        CHECK (last_error_message IS NULL
               OR length(last_error_message) <= 300),
    next_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_attempt_at TEXT,
    sent_at TEXT,
    delivered_at TEXT,
    read_at TEXT,
    failed_at TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_note TEXT,
    resolution_outcome TEXT
        CHECK (resolution_outcome IS NULL
               OR resolution_outcome IN ('SENT_CONFIRMED', 'NOT_SENT')),
    FOREIGN KEY (followup_draft_id) REFERENCES followup_drafts(id)
        ON DELETE CASCADE,
    UNIQUE (idempotency_key)
);
"""

MESSAGE_DELIVERY_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_message_deliveries_draft_id
    ON message_deliveries(followup_draft_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_deliveries_draft_unique
    ON message_deliveries(followup_draft_id);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_status
    ON message_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_provider
    ON message_deliveries(provider);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_next_attempt
    ON message_deliveries(next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_provider_message_id
    ON message_deliveries(provider_message_id);
"""

SCHEMA = (
    SUPPLIERS_DDL
    + SYSTEM_META_DDL
    + SYSTEM_AUDIT_EVENTS_DDL
    + INVOICES_DDL
    + GSTR2B_IMPORTS_DDL
    + GSTR2B_ROWS_DDL
    + RECONCILIATION_RUNS_DDL
    + RECONCILIATION_RESULTS_DDL
    + FOLLOWUP_DRAFTS_DDL
    + FOLLOWUP_DRAFT_INVOICES_DDL
    + FOLLOWUP_EVENTS_DDL
    + FOLLOWUP_VERIFICATIONS_DDL
    + MESSAGE_DELIVERIES_DDL
    + INDEX_DDL
    + GSTR2B_INDEX_DDL
    + RECONCILIATION_INDEX_DDL
    + FOLLOWUP_INDEX_DDL
    + MESSAGE_DELIVERY_INDEX_DDL
)


def get_database_path() -> Path:
    """Return one stable database path, independent of the launch directory.

    Relative DATABASE_PATH values are resolved from the project root. This
    keeps ``milaan.db`` consistent when Streamlit is started from another
    working directory while still allowing an explicit absolute path.
    """
    configured = os.environ.get("DATABASE_PATH", "milaan.db").strip()
    if not configured:
        raise ValueError("DATABASE_PATH must not be empty")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def get_connection() -> sqlite3.Connection:
    path = get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _legacy_suppliers_table(conn: sqlite3.Connection) -> bool:
    """True if a suppliers table exists with the Task 1 (legacy) schema."""
    info = conn.execute("PRAGMA table_info(suppliers)").fetchall()
    if not info:
        return False
    cols = {row["name"] for row in info}
    return "name" in cols and "supplier_name" not in cols


def _migrate_legacy_suppliers_table(conn: sqlite3.Connection) -> None:
    """Copy data from the Task 1 suppliers(name) schema into the new schema.

    Runs inside a single explicit transaction so the rename + copy is
    atomic; failures roll back and raise (never silently swallowed).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE suppliers RENAME TO suppliers_legacy")
        conn.execute(SUPPLIERS_DDL)
        conn.execute(
            """INSERT INTO suppliers
                   (id, supplier_name, gstin, contact_phone, created_at)
               SELECT id, name, gstin, NULL, created_at FROM suppliers_legacy"""
        )
        conn.execute("DROP TABLE suppliers_legacy")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


_INVOICE_COLUMNS = (
    "id", "supplier_id", "invoice_number", "invoice_date", "tax_period",
    "taxable_value", "gst_rate", "cgst", "sgst", "igst", "total_gst",
    "invoice_total", "source", "validation_status", "case_status",
    "created_at",
)


def _invoices_table_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'invoices'"
    ).fetchone()
    return row["sql"] if row is not None else None


def _needs_validation_status_migration(conn: sqlite3.Connection) -> bool:
    """True if invoices exists with the Task 2 CHECK constraints (no
    'needs_review'), which SQLite cannot alter in place."""
    sql = _invoices_table_sql(conn)
    return sql is not None and "needs_review" not in sql


def _migrate_validation_status_check(conn: sqlite3.Connection) -> None:
    """Rebuild the invoices table to allow 'needs_review', preserving rows.

    Every statement runs in one explicit transaction; failure rolls back
    and raises. Foreign keys are disabled by the caller during init.
    """
    columns = ", ".join(_INVOICE_COLUMNS)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE invoices RENAME TO invoices_legacy")
        conn.execute(INVOICES_DDL)
        conn.execute(
            f"""INSERT INTO invoices ({columns})
                SELECT {columns} FROM invoices_legacy"""
        )
        conn.execute("DROP TABLE invoices_legacy")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _migrate_followup_approval_snapshots(conn: sqlite3.Connection) -> None:
    """Add approval snapshot columns to an existing Task 6 table.

    Additive ALTER TABLE only; no rebuild, no data loss. Existing approved
    rows without snapshots remain approved but delivery is blocked until a
    snapshot exists (see get_delivery_eligibility).
    """
    columns = _table_columns(conn, "followup_drafts")
    additions = (
        ("approved_text", "TEXT"),
        ("approved_recipient_phone", "TEXT"),
        ("approved_at", "TEXT"),
    )
    missing = [
        (name, declaration)
        for name, declaration in additions
        if name not in columns
    ]
    if not missing:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, declaration in missing:
            conn.execute(
                f"ALTER TABLE followup_drafts ADD COLUMN {name} {declaration}"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    """Create the schema if missing. Safe to call repeatedly; never drops
    or resets records. Migrates a Task 1 legacy suppliers table and the
    Task 2 invoices CHECK constraint (adding needs_review) in place, and
    adds Task 7 approval snapshot columns without rebuilding tables."""
    path = get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.isolation_level = None  # autocommit; explicit transactions below
        conn.execute("PRAGMA foreign_keys = OFF")
        if _legacy_suppliers_table(conn):
            _migrate_legacy_suppliers_table(conn)
        conn.executescript(SCHEMA)
        _migrate_followup_approval_snapshots(conn)
        if _needs_validation_status_migration(conn):
            _migrate_validation_status_check(conn)
            # Rebuilding drops the old table's indexes with it.
            conn.executescript(INDEX_DDL)
    finally:
        conn.close()


def is_db_ready() -> bool:
    try:
        with db() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        names = {row["name"] for row in tables}
        return "suppliers" in names and "invoices" in names
    except sqlite3.Error:
        return False


def format_paise(paise: int) -> str:
    """Render integer paise as a rupee string using integer arithmetic only.

    1_604_886 -> "16048.86"
    """
    if not isinstance(paise, int):
        raise TypeError("paise amounts must be integers")
    sign = "-" if paise < 0 else ""
    magnitude = abs(paise)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}"


def record_system_audit_event(
    *,
    actor: str,
    event_type: str,
    subject_type: str,
    subject_id: str | int = "",
    details: dict | None = None,
) -> None:
    """Persist a bounded, non-secret operational audit event."""
    actor = actor.strip() if isinstance(actor, str) else ""
    event_type = event_type.strip() if isinstance(event_type, str) else ""
    subject_type = subject_type.strip() if isinstance(subject_type, str) else ""
    if not actor or not event_type or not subject_type:
        raise ValueError("actor, event_type and subject_type are required")
    encoded = json.dumps(details or {}, sort_keys=True, separators=(",", ":"))
    if (len(actor) > 320 or len(event_type) > 80 or len(subject_type) > 80
            or len(str(subject_id)) > 160 or len(encoded) > 4000):
        raise ValueError("audit event exceeds field-size limits")
    with db() as conn:
        conn.execute(
            """INSERT INTO system_audit_events
                   (actor, event_type, subject_type, subject_id, details)
               VALUES (?, ?, ?, ?, ?)""",
            (actor, event_type, subject_type, str(subject_id), encoded),
        )


def get_system_audit_events(limit: int = 100) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    with db() as conn:
        rows = conn.execute(
            """SELECT actor, event_type, subject_type, subject_id, details,
                      created_at FROM system_audit_events
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "Actor": row["actor"],
            "Event": row["event_type"],
            "Subject": f"{row['subject_type']}:{row['subject_id']}",
            "Details": json.loads(row["details"]),
            "At": row["created_at"],
        }
        for row in rows
    ]


# --- Synthetic demo data ---------------------------------------------------
# Clearly marked synthetic: supplier names carry a "(Synthetic)" prefix,
# GSTINs are fabricated but checksum-valid so the demo exercises the real
# validation pipeline, and every invoice gets source='demo-seed'. One record
# is deliberately invalid (a non-existent calendar date) so the demo shows a
# validation failure without making every demo identifier malformed.
DEMO_SUPPLIERS = [
    ("(Synthetic) Demo Traders Pvt Ltd", "27AAACS1234A1Z2", "9900000011"),
    ("(Synthetic) Demo Bearings Co", "29AAACB5678B1ZP", "9900000022"),
    ("(Synthetic) Demo Kirana Stores", "24AAACD9012C1ZB", "9900000033"),
]

DEMO_INVOICES = [
    # (supplier gstin, invoice_number, invoice_date, tax_period,
    #  taxable_value, gst_rate, cgst, sgst, igst, total_gst,
    #  invoice_total, validation_status)
    ("27AAACS1234A1Z2", "INV-DEMO-001", "2026-03-05", "2026-03",
     1_250_000, 18, 112_500, 112_500, 0, 225_000, 1_475_000, "pending"),
    ("27AAACS1234A1Z2", "INV-DEMO-002", "2026-03-18", "2026-03",
     824_050, 12, 49_443, 49_443, 0, 98_886, 922_936, "pending"),
    ("27AAACS1234A1Z2", "INV-DEMO-003", "2026-04-02", "2026-04",
     3_000_000, 5, 75_000, 75_000, 0, 150_000, 3_150_000, "pending"),
    ("29AAACB5678B1ZP", "INV-DEMO-101", "2026-03-12", "2026-03",
     4_500_000, 18, 405_000, 405_000, 0, 810_000, 5_310_000, "pending"),
    ("29AAACB5678B1ZP", "INV-DEMO-102", "2026-03-25", "2026-03",
     1_600_000, 18, 0, 0, 288_000, 288_000, 1_888_000, "pending"),
    # Deliberately invalid record for validation-testing: 31 April does not
    # exist. The GSTIN itself is structurally valid.
    ("24AAACD9012C1ZB", "INV-DEMO-201", "2026-04-31", "2026-04",
     275_000, 12, 16_500, 16_500, 0, 33_000, 308_000, "invalid"),
]

_LEGACY_DEMO_GSTINS = {
    "27FAKE1000F1Z5": "27AAACS1234A1Z2",
    "29FAKE2000G1Z6": "29AAACB5678B1ZP",
    "24FAKE3000H1Z7": "24AAACD9012C1ZB",
}


def _repair_legacy_demo_identifiers(conn: sqlite3.Connection) -> None:
    """Upgrade earlier demo-seeded GSTINs without touching user records.

    Older builds seeded structurally invalid placeholder GSTINs, which made
    every demo invoice fail validation. The repair is limited to the exact
    synthetic supplier rows and the known demo invoice. It is a no-op on a
    fresh database and never runs outside seed_demo_data.
    """
    for old, new in _LEGACY_DEMO_GSTINS.items():
        conn.execute(
            """UPDATE suppliers SET gstin = ?
               WHERE gstin = ? AND supplier_name LIKE '(Synthetic)%'
                 AND NOT EXISTS (
                     SELECT 1 FROM suppliers WHERE gstin = ?
                 )""",
            (new, old, new),
        )
    conn.execute(
        """UPDATE invoices SET invoice_date = '2026-04-31'
           WHERE invoice_number = 'INV-DEMO-201'
             AND source = 'demo-seed'
             AND invoice_date = '2026-04-14'"""
    )


def seed_demo_data(conn: sqlite3.Connection | None = None) -> dict:
    """Idempotently insert synthetic demo suppliers and invoices.

    Every insert is guarded with WHERE NOT EXISTS on the natural keys
    (suppliers: gstin; invoices: supplier_id+invoice_number+tax_period),
    so seeding twice adds nothing and modifies nothing. Plain INSERT (not
    INSERT OR IGNORE) is used so CHECK/IntegrityError violations raise
    loudly instead of being silently ignored. All rows are inserted in
    one transaction; a failure rolls the whole seed back and raises.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        added = {"suppliers": 0, "invoices": 0}
        with conn:
            _repair_legacy_demo_identifiers(conn)
            for name, gstin, phone in DEMO_SUPPLIERS:
                cur = conn.execute(
                    """INSERT INTO suppliers (supplier_name, gstin, contact_phone)
                       SELECT ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM suppliers WHERE gstin = ?
                       )""",
                    (name, gstin, phone, gstin),
                )
                added["suppliers"] += cur.rowcount
            supplier_ids = {
                row["gstin"]: row["id"]
                for row in conn.execute(
                    "SELECT id, gstin FROM suppliers"
                ).fetchall()
            }
            for (gstin, invoice_number, invoice_date, tax_period,
                 taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                 invoice_total, validation_status) in DEMO_INVOICES:
                supplier_id = supplier_ids[gstin]
                cur = conn.execute(
                    """INSERT INTO invoices
                           (supplier_id, invoice_number, invoice_date, tax_period,
                            taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                            invoice_total, source, validation_status)
                       SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo-seed', ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM invoices
                           WHERE supplier_id = ? AND invoice_number = ?
                             AND tax_period = ?
                       )""",
                    (supplier_id, invoice_number, invoice_date, tax_period,
                     taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                     invoice_total, validation_status,
                     supplier_id, invoice_number, tax_period),
                )
                added["invoices"] += cur.rowcount
            conn.execute(
                """INSERT INTO system_meta (key, value)
                   VALUES ('seeded_demo', datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET value = datetime('now')"""
            )
        return added
    finally:
        if owns_conn:
            conn.close()


def get_suppliers() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, supplier_name, gstin, contact_phone, created_at
               FROM suppliers ORDER BY id"""
        ).fetchall()
    return [
        {
            "ID": r["id"],
            "Supplier": r["supplier_name"],
            "GSTIN": r["gstin"],
            "Phone": r["contact_phone"] or "",
            "Created at": r["created_at"],
        }
        for r in rows
    ]


def get_invoices() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT i.id, s.supplier_name AS supplier, i.invoice_number,
                      i.invoice_date, i.tax_period, i.taxable_value, i.gst_rate,
                      i.cgst, i.sgst, i.igst, i.total_gst, i.invoice_total,
                      i.source, i.validation_status, i.case_status
               FROM invoices i
               LEFT JOIN suppliers s ON s.id = i.supplier_id
               ORDER BY i.invoice_date, i.invoice_number"""
        ).fetchall()
    return [
        {
            "ID": r["id"],
            "Invoice": r["invoice_number"],
            "Supplier": r["supplier"],
            "Date": r["invoice_date"],
            "Tax period": r["tax_period"],
            "Taxable (Rs)": format_paise(r["taxable_value"]),
            "Rate %": r["gst_rate"],
            "CGST (Rs)": format_paise(r["cgst"]),
            "SGST (Rs)": format_paise(r["sgst"]),
            "IGST (Rs)": format_paise(r["igst"]),
            "Total GST (Rs)": format_paise(r["total_gst"]),
            "Invoice total (Rs)": format_paise(r["invoice_total"]),
            "Source": r["source"],
            "Validation": r["validation_status"],
            "Case": r["case_status"],
        }
        for r in rows
    ]


def get_totals() -> dict:
    with db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS invoice_count,
                      COALESCE(SUM(total_gst), 0) AS total_gst_paise
               FROM invoices"""
        ).fetchone()
    return {
        "invoice_count": row["invoice_count"],
        "total_gst_paise": row["total_gst_paise"],
    }


def get_validation_summary() -> dict:
    """Invoice counts per validation_status, including zero counts."""
    summary = {status: 0 for status in VALIDATION_STATUSES}
    with db() as conn:
        rows = conn.execute(
            """SELECT validation_status, COUNT(*) AS n
               FROM invoices GROUP BY validation_status"""
        ).fetchall()
    for row in rows:
        summary[row["validation_status"]] = row["n"]
    return summary


def invoice_exists(
    supplier_id: int,
    invoice_number: str,
    tax_period: str,
    *,
    exclude_invoice_id: int | None = None,
) -> bool:
    """Check the invoices natural key (supplier, number, tax period)."""
    query = (
        "SELECT 1 FROM invoices WHERE supplier_id = ? AND invoice_number = ? "
        "AND tax_period = ?"
    )
    params: list[object] = [supplier_id, invoice_number, tax_period]
    if exclude_invoice_id is not None:
        query += " AND id != ?"
        params.append(exclude_invoice_id)
    with db() as conn:
        return conn.execute(query, params).fetchone() is not None


def get_invoice(invoice_id: int) -> dict | None:
    """Load one invoice with its supplier's GSTIN, or None if missing."""
    with db() as conn:
        row = conn.execute(
            """SELECT i.*, s.gstin AS supplier_gstin
               FROM invoices i
               JOIN suppliers s ON s.id = i.supplier_id
               WHERE i.id = ?""",
            (invoice_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def update_invoice_validation(invoice_id: int, status: str) -> None:
    """Persist a validation status.

    Rejects statuses outside VALIDATION_STATUSES before touching SQL, so
    callers cannot bypass the schema whitelist; the CHECK constraint
    remains the final guard.
    """
    if status not in VALIDATION_STATUSES:
        raise ValueError(
            f"invalid validation status {status!r}; expected one of "
            f"{VALIDATION_STATUSES}"
        )
    with db() as conn:
        cursor = conn.execute(
            "UPDATE invoices SET validation_status = ? WHERE id = ?",
            (status, invoice_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"invoice {invoice_id} not found")


def revalidate_invoice(invoice_id: int) -> validation.ValidationResult:
    """Validate a stored invoice, persist its status, and return the result.

    Duplicate detection uses the same natural key as the database UNIQUE
    constraint, excluding the invoice itself.
    """
    record = get_invoice(invoice_id)
    if record is None:
        raise KeyError(f"invoice {invoice_id} not found")
    duplicate = invoice_exists(
        record["supplier_id"],
        record["invoice_number"],
        record["tax_period"],
        exclude_invoice_id=invoice_id,
    )
    result = validation.validate_invoice(record, duplicate_exists=duplicate)
    update_invoice_validation(invoice_id, result.status)
    return result


def revalidate_all_invoices() -> list[dict]:
    """Revalidate every stored invoice and persist each status.

    Returns one entry per invoice: {invoice_id, invoice_number, result}.
    Database errors propagate; they are never swallowed.
    """
    with db() as conn:
        ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM invoices ORDER BY id")
        ]
    entries = []
    for invoice_id in ids:
        record = get_invoice(invoice_id)
        result = revalidate_invoice(invoice_id)
        entries.append({
            "invoice_id": invoice_id,
            "invoice_number": record["invoice_number"],
            "result": result,
        })
    return entries


def save_invoice_import(parsed) -> dict:
    """Persist accepted purchase-invoice import rows in one transaction.

    Suppliers are reused by normalized GSTIN and created only when absent.
    Rows whose natural key already exists are counted as duplicates and are
    never overwritten. Each inserted row is validated with the same
    deterministic rules used elsewhere; its persisted validation_status is
    the result. No row is silently discarded: duplicates and rejected rows
    are reported in the return value.
    """
    imported = 0
    suppliers_created = 0
    duplicates = 0
    with db() as conn:
        for row in parsed.rows:
            if not row.accepted:
                continue
            gstin = row.supplier_gstin
            supplier = conn.execute(
                "SELECT id FROM suppliers WHERE gstin = ?", (gstin,)
            ).fetchone()
            if supplier is None:
                cursor = conn.execute(
                    """INSERT INTO suppliers
                           (supplier_name, gstin, contact_phone)
                       VALUES (?, ?, ?)""",
                    (
                        row.supplier_name,
                        gstin,
                        row.contact_phone or None,
                    ),
                )
                supplier_id = cursor.lastrowid
                suppliers_created += 1
            else:
                supplier_id = supplier["id"]

            existing = conn.execute(
                """SELECT 1 FROM invoices
                   WHERE supplier_id = ? AND invoice_number = ?
                     AND tax_period = ?""",
                (supplier_id, row.invoice_number, row.tax_period),
            ).fetchone()
            if existing is not None:
                duplicates += 1
                continue

            record = row.record()
            result = validation.validate_invoice(record)
            conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date,
                        tax_period, taxable_value, gst_rate, cgst, sgst,
                        igst, total_gst, invoice_total, source,
                        validation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'upload', ?)""",
                (
                    supplier_id,
                    row.invoice_number,
                    row.invoice_date,
                    row.tax_period,
                    row.taxable_value,
                    row.gst_rate,
                    row.cgst,
                    row.sgst,
                    row.igst,
                    row.total_gst,
                    row.invoice_total,
                    result.status,
                ),
            )
            imported += 1
    return {
        "imported": imported,
        "suppliers_created": suppliers_created,
        "duplicates": duplicates,
        "rejected": parsed.rejected_rows,
        "file_name": parsed.file_name,
        "file_hash": parsed.file_hash,
    }


# --- GSTR-2B staging (Task 4) ---------------------------------------------
# Ingestion only: these helpers never touch suppliers/invoices.

def _issues_json(issues) -> str:
    return json.dumps(
        [
            {"code": issue.code, "field": issue.field, "message": issue.message}
            for issue in issues
        ],
        ensure_ascii=False,
    )


def _insert_gstr2b_row(conn: sqlite3.Connection, import_id: int, row) -> None:
    """Insert one parsed row. Module-level so tests can simulate failures."""
    conn.execute(
        """INSERT INTO gstr2b_rows
               (import_id, row_number, supplier_gstin, invoice_number,
                invoice_date, tax_period, taxable_value, cgst, sgst, igst,
                total_gst, validation_status, errors, warnings, raw_data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            import_id,
            row.row_number,
            row.supplier_gstin,
            row.invoice_number,
            row.invoice_date,
            row.tax_period,
            row.taxable_value,
            row.cgst,
            row.sgst,
            row.igst,
            row.total_gst,
            row.validation_status,
            _issues_json(row.errors),
            _issues_json(row.warnings),
            row.raw_data,
        ),
    )


def save_gstr2b_import(parsed) -> dict:
    """Persist a parsed GSTR-2B batch and every source row atomically.

    `parsed` is a gstr2b.ParsedGstr2b. Rejected rows are stored too, with
    their row-level errors, so the audit trail is complete. Either the
    batch and all rows commit together or nothing is written; failures
    propagate and are never swallowed.
    """
    rows = list(parsed.rows)
    total = len(rows)
    accepted = sum(
        1 for row in rows if row.validation_status == "valid"
    )
    rejected = total - accepted

    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """INSERT INTO gstr2b_imports
                       (file_name, file_hash, file_size, source_format,
                        total_rows, accepted_rows, rejected_rows, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')""",
                (
                    parsed.file_name,
                    parsed.file_hash,
                    parsed.file_size,
                    parsed.source_format,
                    total,
                    accepted,
                    rejected,
                ),
            )
            import_id = cursor.lastrowid
            for row in rows:
                _insert_gstr2b_row(conn, import_id, row)
    finally:
        conn.close()

    return {
        "import_id": import_id,
        "file_name": parsed.file_name,
        "total_rows": total,
        "accepted_rows": accepted,
        "rejected_rows": rejected,
    }


def find_gstr2b_imports_by_hash(file_hash: str) -> list[dict]:
    """All batches previously imported from a file with this SHA-256."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, file_name, imported_at, source_format,
                      total_rows, accepted_rows, rejected_rows
               FROM gstr2b_imports WHERE file_hash = ?
               ORDER BY id DESC""",
            (file_hash,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_gstr2b_imports(limit: int = 50) -> list[dict]:
    """Batch history, newest first, for display."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, file_name, imported_at, source_format, total_rows,
                      accepted_rows, rejected_rows, file_hash
               FROM gstr2b_imports ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "Batch": row["id"],
            "Imported at": row["imported_at"],
            "File": row["file_name"],
            "Format": row["source_format"],
            "Rows": row["total_rows"],
            "Accepted": row["accepted_rows"],
            "Rejected": row["rejected_rows"],
            "SHA-256": row["file_hash"][:12] + "...",
        }
        for row in rows
    ]


def get_gstr2b_import(import_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM gstr2b_imports WHERE id = ?", (import_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_gstr2b_row(row_id: int) -> dict | None:
    """Load one staged GSTR-2B row for evidence display."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM gstr2b_rows WHERE id = ?", (row_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_gstr2b_rows(import_id: int) -> list[dict]:
    """Rows of one batch (including rejected rows) for display."""
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM gstr2b_rows WHERE import_id = ?
               ORDER BY row_number""",
            (import_id,),
        ).fetchall()

    def money(value) -> str:
        return "" if value is None else format_paise(value)

    def issue_text(raw: str) -> str:
        try:
            issues = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        return "; ".join(
            f"[{issue.get('code')}] {issue.get('field')}: "
            f"{issue.get('message')}"
            for issue in issues
        )

    return [
        {
            "Row": row["row_number"],
            "Status": row["validation_status"],
            "GSTIN": row["supplier_gstin"] or "",
            "Invoice": row["invoice_number"] or "",
            "Date": row["invoice_date"] or "",
            "Tax period": row["tax_period"] or "",
            "Taxable (Rs)": money(row["taxable_value"]),
            "CGST (Rs)": money(row["cgst"]),
            "SGST (Rs)": money(row["sgst"]),
            "IGST (Rs)": money(row["igst"]),
            "Total GST (Rs)": money(row["total_gst"]),
            "Issues": issue_text(row["errors"]),
        }
        for row in rows
    ]


# --- Reconciliation (Task 5) ----------------------------------------------
# Matching is deterministic (pure reconciliation module). A run snapshots its
# evidence; invoice and GSTR-2B source rows are only read, never updated.

_RECONCILIATION_STATUS_COUNTS = {
    reconciliation.STATUS_MATCHED: "matched_count",
    reconciliation.STATUS_AMOUNT_MISMATCH: "amount_mismatch_count",
    reconciliation.STATUS_MISSING_FROM_2B: "missing_count",
    reconciliation.STATUS_NEEDS_REVIEW: "needs_review_count",
    reconciliation.STATUS_NOT_ELIGIBLE: "not_eligible_count",
}


def _load_invoice_records() -> list[reconciliation.InvoiceRecord]:
    with db() as conn:
        rows = conn.execute(
            """SELECT i.id, i.invoice_number, i.tax_period, i.total_gst,
                      i.validation_status, s.gstin AS supplier_gstin
               FROM invoices i
               JOIN suppliers s ON s.id = i.supplier_id
               ORDER BY i.id"""
        ).fetchall()
    return [
        reconciliation.InvoiceRecord(
            invoice_id=row["id"],
            supplier_gstin=row["supplier_gstin"],
            invoice_number=row["invoice_number"],
            tax_period=row["tax_period"],
            total_gst=row["total_gst"],
            validation_status=row["validation_status"],
        )
        for row in rows
    ]


def _load_gstr2b_records(import_id: int) -> list[reconciliation.Gstr2bRecord]:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, supplier_gstin, invoice_number, tax_period,
                      total_gst, validation_status
               FROM gstr2b_rows WHERE import_id = ? ORDER BY id""",
            (import_id,),
        ).fetchall()
    return [
        reconciliation.Gstr2bRecord(
            row_id=row["id"],
            supplier_gstin=row["supplier_gstin"],
            invoice_number=row["invoice_number"],
            tax_period=row["tax_period"],
            total_gst=row["total_gst"],
            validation_status=row["validation_status"],
        )
        for row in rows
    ]


def _insert_reconciliation_result(
    conn: sqlite3.Connection,
    run_id: int,
    result: reconciliation.MatchResult,
) -> None:
    """Insert one result row. Module-level so tests can simulate failures."""
    conn.execute(
        """INSERT INTO reconciliation_results
               (run_id, invoice_id, gstr2b_row_id, match_status,
                match_method, amount_difference_paise, reason_codes,
                invoice_number, supplier_gstin, tax_period,
                invoice_total_gst_paise, gstr2b_invoice_number,
                gstr2b_total_gst_paise)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            result.invoice_id,
            result.gstr2b_row_id,
            result.match_status,
            result.match_method,
            result.amount_difference_paise,
            json.dumps(list(result.reason_codes)),
            result.invoice_number,
            result.supplier_gstin,
            result.tax_period,
            result.invoice_total_gst_paise,
            result.gstr2b_invoice_number,
            result.gstr2b_total_gst_paise,
        ),
    )


def reconcile_gstr2b_import(
    gstr2b_import_id: int,
    amount_tolerance_paise: int = 0,
) -> dict:
    """Run deterministic matching for one batch and persist a new run.

    Every stored invoice gets exactly one result. A run is refused when
    there is nothing usable to reconcile (no invoices, no batch rows, no
    valid GSTR-2B rows, or no eligible/valid invoice), so the UI never
    reports success for an empty comparison. Previous runs are kept and
    their ``is_latest`` flag is cleared; results are inserted atomically
    with the run.
    """
    config = reconciliation.ReconciliationConfig(amount_tolerance_paise)
    if not isinstance(gstr2b_import_id, int) or isinstance(gstr2b_import_id, bool):
        raise ValueError("gstr2b_import_id must be an integer batch id")
    if get_gstr2b_import(gstr2b_import_id) is None:
        raise ValueError(f"GSTR-2B import batch {gstr2b_import_id} not found")

    invoices = _load_invoice_records()
    if not invoices:
        raise ValueError("No purchase invoices exist; nothing to reconcile.")

    rows = _load_gstr2b_records(gstr2b_import_id)
    if not rows:
        raise ValueError(
            f"GSTR-2B batch {gstr2b_import_id} has no rows; reconciliation "
            "was not started."
        )
    if not any(
        row.validation_status == reconciliation.VALIDATION_VALID for row in rows
    ):
        raise ValueError(
            f"GSTR-2B batch {gstr2b_import_id} has no usable (valid) rows; "
            "reconciliation was not started."
        )

    results = reconciliation.reconcile_invoices(invoices, rows, config=config)
    if not any(
        result.match_status != reconciliation.STATUS_NOT_ELIGIBLE
        for result in results
    ):
        raise ValueError(
            "No validated ('valid') purchase invoices are eligible for "
            "reconciliation; reconciliation was not started."
        )

    counts = {field: 0 for field in _RECONCILIATION_STATUS_COUNTS.values()}
    for result in results:
        counts[_RECONCILIATION_STATUS_COUNTS[result.match_status]] += 1

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE reconciliation_runs SET is_latest = 0 "
                "WHERE is_latest = 1"
            )
            cursor = conn.execute(
                """INSERT INTO reconciliation_runs
                       (gstr2b_import_id, amount_tolerance_paise,
                        invoice_count, matched_count, amount_mismatch_count,
                        missing_count, needs_review_count, not_eligible_count,
                        is_latest)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    gstr2b_import_id,
                    config.amount_tolerance_paise,
                    len(results),
                    counts["matched_count"],
                    counts["amount_mismatch_count"],
                    counts["missing_count"],
                    counts["needs_review_count"],
                    counts["not_eligible_count"],
                ),
            )
            run_id = cursor.lastrowid
            for result in results:
                _insert_reconciliation_result(conn, run_id, result)
    finally:
        conn.close()

    return {
        "run_id": run_id,
        "gstr2b_import_id": gstr2b_import_id,
        "amount_tolerance_paise": config.amount_tolerance_paise,
        "invoice_count": len(results),
        **counts,
    }


def get_reconciliation_runs(limit: int = 50) -> list[dict]:
    """Run history, newest first, including which run is latest."""
    with db() as conn:
        rows = conn.execute(
            """SELECT r.*, g.file_name AS file_name
               FROM reconciliation_runs r
               JOIN gstr2b_imports g ON g.id = r.gstr2b_import_id
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "Run": row["id"],
            "Created at": row["created_at"],
            "Batch": row["gstr2b_import_id"],
            "File": row["file_name"],
            "Latest": bool(row["is_latest"]),
            "Tolerance (paise)": row["amount_tolerance_paise"],
            "Invoices": row["invoice_count"],
            "Matched": row["matched_count"],
            "Amount mismatch": row["amount_mismatch_count"],
            "Missing from 2B": row["missing_count"],
            "Needs review": row["needs_review_count"],
            "Not eligible": row["not_eligible_count"],
        }
        for row in rows
    ]


def get_reconciliation_run(run_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            """SELECT r.*, g.file_name AS file_name
               FROM reconciliation_runs r
               JOIN gstr2b_imports g ON g.id = r.gstr2b_import_id
               WHERE r.id = ?""",
            (run_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_latest_reconciliation_run() -> dict | None:
    with db() as conn:
        row = conn.execute(
            """SELECT r.*, g.file_name AS file_name
               FROM reconciliation_runs r
               JOIN gstr2b_imports g ON g.id = r.gstr2b_import_id
               WHERE r.is_latest = 1"""
        ).fetchone()
    return dict(row) if row is not None else None


def get_reconciliation_summary(run_id: int) -> dict | None:
    """Per-status counts and invoice-GST rupee totals for one run."""
    run = get_reconciliation_run(run_id)
    if run is None:
        return None
    by_status = {
        status: {"count": 0, "total_gst_paise": 0}
        for status in reconciliation.MATCH_STATUSES
    }
    with db() as conn:
        rows = conn.execute(
            """SELECT match_status, COUNT(*) AS n,
                      COALESCE(SUM(invoice_total_gst_paise), 0) AS total
               FROM reconciliation_results
               WHERE run_id = ? GROUP BY match_status""",
            (run_id,),
        ).fetchall()
    for row in rows:
        by_status[row["match_status"]] = {
            "count": row["n"],
            "total_gst_paise": row["total"],
        }
    return {
        "run": run,
        "by_status": by_status,
        "invoice_total_gst_paise": sum(
            item["total_gst_paise"] for item in by_status.values()
        ),
    }


def get_reconciliation_results(run_id: int) -> list[dict]:
    """Invoice-level evidence for one run, ready for display."""
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM reconciliation_results
               WHERE run_id = ? ORDER BY invoice_id""",
            (run_id,),
        ).fetchall()

    def money(value) -> str:
        return "" if value is None else format_paise(value)

    def difference(value) -> str:
        if value is None:
            return ""
        if value == 0:
            return "0.00"
        prefix = "+" if value > 0 else "-"
        return prefix + format_paise(abs(value))

    def reasons(raw: str) -> str:
        try:
            codes = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(codes, list):
            return str(codes)
        return "; ".join(str(code) for code in codes)

    return [
        {
            "Invoice #": row["invoice_id"],
            "Invoice number": row["invoice_number"],
            "GSTIN": row["supplier_gstin"] or "",
            "Tax period": row["tax_period"] or "",
            "Invoice GST (Rs)": money(row["invoice_total_gst_paise"]),
            "Status": row["match_status"],
            "Method": row["match_method"],
            "2B row": (
                str(row["gstr2b_row_id"])
                if row["gstr2b_row_id"] is not None else ""
            ),
            "2B invoice": row["gstr2b_invoice_number"] or "",
            "2B GST (Rs)": money(row["gstr2b_total_gst_paise"]),
            "Difference (Rs)": difference(row["amount_difference_paise"]),
            "Reasons": reasons(row["reason_codes"]),
        }
        for row in rows
    ]


# --- Supplier follow-up preparation (Task 6) ------------------------------
# Drafts only. There is deliberately no send/network function in this module;
# approval records intent for a future integration and nothing more.

def _parse_reason_codes(raw: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ("INVALID_REASON_CODES",)
    if not isinstance(parsed, list):
        return ("INVALID_REASON_CODES",)
    return tuple(str(item) for item in parsed)


def get_latest_reconciliation_run_for_import(
    gstr2b_import_id: int,
) -> dict | None:
    """Latest run for one batch (by id), independent of the global flag."""
    with db() as conn:
        row = conn.execute(
            """SELECT r.*, g.file_name AS file_name
               FROM reconciliation_runs r
               JOIN gstr2b_imports g ON g.id = r.gstr2b_import_id
               WHERE r.gstr2b_import_id = ?
               ORDER BY r.id DESC LIMIT 1""",
            (gstr2b_import_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _load_followup_candidates(run_id: int) -> list[followups.CandidateInvoice]:
    with db() as conn:
        rows = conn.execute(
            """SELECT rr.id AS result_id, rr.match_status, rr.match_method,
                      rr.gstr2b_row_id, rr.amount_difference_paise,
                      rr.reason_codes,
                      i.id AS invoice_id, i.invoice_number, i.invoice_date,
                      i.tax_period, i.taxable_value, i.total_gst,
                      i.validation_status,
                      s.id AS supplier_id, s.supplier_name,
                      s.gstin AS supplier_gstin, s.contact_phone
               FROM reconciliation_results rr
               JOIN invoices i ON i.id = rr.invoice_id
               JOIN suppliers s ON s.id = i.supplier_id
               WHERE rr.run_id = ?
               ORDER BY s.id, i.invoice_date, i.invoice_number, i.id""",
            (run_id,),
        ).fetchall()
    return [
        followups.CandidateInvoice(
            invoice_id=row["invoice_id"],
            invoice_number=row["invoice_number"],
            invoice_date=row["invoice_date"],
            tax_period=row["tax_period"],
            taxable_value_paise=row["taxable_value"],
            total_gst_paise=row["total_gst"],
            supplier_id=row["supplier_id"],
            supplier_name=row["supplier_name"],
            supplier_gstin=row["supplier_gstin"],
            contact_phone=row["contact_phone"],
            validation_status=row["validation_status"],
            result_id=row["result_id"],
            match_status=row["match_status"],
            match_method=row["match_method"],
            gstr2b_row_id=row["gstr2b_row_id"],
            amount_difference_paise=row["amount_difference_paise"],
            reason_codes=_parse_reason_codes(row["reason_codes"]),
        )
        for row in rows
    ]


def _plan_display(plan: followups.DraftPlan) -> dict:
    return {
        "supplier_id": plan.supplier_id,
        "supplier_name": plan.supplier_name,
        "supplier_gstin": plan.supplier_gstin or "",
        "contact_phone": plan.contact_phone or "",
        "channel_available": plan.channel_available,
        "status": plan.status,
        "review_reasons": list(plan.review_reasons),
        "invoices": [
            {
                "invoice_id": invoice.invoice_id,
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date,
                "tax_period": invoice.tax_period,
                "taxable_value_paise": invoice.taxable_value_paise,
                "total_gst_paise": invoice.total_gst_paise,
                "reconciliation_result_id": (
                    invoice.reconciliation_result_id
                ),
                "reason": invoice.reason,
            }
            for invoice in plan.invoices
        ],
        "total_taxable_value_paise": plan.total_taxable_value_paise,
        "total_gst_paise": plan.total_gst_paise,
        "inclusion_reason": plan.inclusion_reason,
        "draft_text": plan.draft_text,
        "language": plan.language,
    }


def get_followup_eligibility(gstr2b_import_id: int) -> dict:
    """Read-only preview of the latest run's follow-up candidates.

    Returns the resolved latest run, planned supplier groups, and every
    excluded invoice with its reason. Nothing is written.
    """
    if (
        not isinstance(gstr2b_import_id, int)
        or isinstance(gstr2b_import_id, bool)
    ):
        raise ValueError("gstr2b_import_id must be an integer batch id")
    if get_gstr2b_import(gstr2b_import_id) is None:
        raise ValueError(f"GSTR-2B import batch {gstr2b_import_id} not found")

    run = get_latest_reconciliation_run_for_import(gstr2b_import_id)
    if run is None:
        return {
            "run": None,
            "plans": [],
            "excluded": [],
            "eligible_invoice_count": 0,
        }

    candidates = _load_followup_candidates(run["id"])
    plans, excluded = followups.plan_followup_drafts(
        candidates,
        run_id=run["id"],
        import_id=gstr2b_import_id,
    )
    return {
        "run": run,
        "plans": [_plan_display(plan) for plan in plans],
        "excluded": [
            {
                "invoice_id": item.invoice_id,
                "invoice_number": item.invoice_number,
                "supplier_id": item.supplier_id,
                "supplier_name": item.supplier_name,
                "validation_status": item.validation_status,
                "match_status": item.match_status,
                "reason": item.reason,
            }
            for item in excluded
        ],
        "eligible_invoice_count": sum(
            len(plan.invoices) for plan in plans
        ),
    }


def _insert_followup_draft(
    conn: sqlite3.Connection,
    plan: followups.DraftPlan,
    run_id: int,
    import_id: int,
) -> int:
    """Insert one draft, its invoice associations and CREATED event."""
    cursor = conn.execute(
        """INSERT INTO followup_drafts
               (supplier_id, reconciliation_run_id, gstr2b_import_id,
                supplier_name, supplier_gstin, contact_phone,
                channel_available, draft_text, language, status,
                review_reasons, inclusion_reason, total_taxable_value_paise,
                total_gst_paise)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan.supplier_id,
            run_id,
            import_id,
            plan.supplier_name,
            plan.supplier_gstin,
            plan.contact_phone,
            1 if plan.channel_available else 0,
            plan.draft_text,
            plan.language,
            plan.status,
            json.dumps(list(plan.review_reasons)),
            plan.inclusion_reason,
            plan.total_taxable_value_paise,
            plan.total_gst_paise,
        ),
    )
    draft_id = cursor.lastrowid
    for invoice in plan.invoices:
        conn.execute(
            """INSERT INTO followup_draft_invoices
                   (draft_id, invoice_id, reconciliation_result_id,
                    invoice_number, invoice_date, tax_period,
                    taxable_value_paise, total_gst_paise, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft_id,
                invoice.invoice_id,
                invoice.reconciliation_result_id,
                invoice.invoice_number,
                invoice.invoice_date,
                invoice.tax_period,
                invoice.taxable_value_paise,
                invoice.total_gst_paise,
                invoice.reason,
            ),
        )
    _record_followup_event(
        conn,
        draft_id,
        event_type="CREATED",
        from_status=None,
        to_status=plan.status,
        note=plan.inclusion_reason,
    )
    return draft_id


def _record_followup_event(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    event_type: str,
    from_status: str | None,
    to_status: str,
    note: str = "",
) -> None:
    conn.execute(
        """INSERT INTO followup_draft_events
               (draft_id, event_type, from_status, to_status, note)
           VALUES (?, ?, ?, ?, ?)""",
        (draft_id, event_type, from_status, to_status, note),
    )


def generate_followup_drafts(gstr2b_import_id: int) -> dict:
    """Create drafts for the latest run of one batch, idempotently.

    Existing drafts for the same (run, supplier) are never replaced, so
    repeated generation cannot duplicate drafts or overwrite user edits.
    The draft, its invoice associations and the CREATED events commit in
    one transaction.
    """
    if (
        not isinstance(gstr2b_import_id, int)
        or isinstance(gstr2b_import_id, bool)
    ):
        raise ValueError("gstr2b_import_id must be an integer batch id")
    if get_gstr2b_import(gstr2b_import_id) is None:
        raise ValueError(f"GSTR-2B import batch {gstr2b_import_id} not found")

    run = get_latest_reconciliation_run_for_import(gstr2b_import_id)
    if run is None:
        raise ValueError(
            f"No reconciliation run exists for GSTR-2B batch "
            f"{gstr2b_import_id}; run reconciliation first."
        )

    candidates = _load_followup_candidates(run["id"])
    plans, excluded = followups.plan_followup_drafts(
        candidates,
        run_id=run["id"],
        import_id=gstr2b_import_id,
    )
    if not plans:
        raise ValueError(
            "No unambiguous MISSING_FROM_2B invoices are available for "
            "follow-up preparation in this run; no drafts were created."
        )

    created_ids: list[int] = []
    existing = 0
    conn = get_connection()
    try:
        with conn:
            existing_ids = {
                row["supplier_id"]
                for row in conn.execute(
                    "SELECT supplier_id FROM followup_drafts "
                    "WHERE reconciliation_run_id = ?",
                    (run["id"],),
                ).fetchall()
            }
            for plan in plans:
                if plan.supplier_id in existing_ids:
                    existing += 1
                    continue
                created_ids.append(
                    _insert_followup_draft(
                        conn, plan, run["id"], gstr2b_import_id
                    )
                )
    finally:
        conn.close()

    return {
        "run_id": run["id"],
        "gstr2b_import_id": gstr2b_import_id,
        "created_drafts": len(created_ids),
        "existing_drafts": existing,
        "eligible_invoices": sum(len(plan.invoices) for plan in plans),
        "excluded_invoices": len(excluded),
        "draft_ids": created_ids,
    }


def get_followup_drafts(
    *,
    gstr2b_import_id: int | None = None,
    run_id: int | None = None,
) -> list[dict]:
    query = """
        SELECT d.*,
               (SELECT COUNT(*) FROM followup_draft_invoices di
                WHERE di.draft_id = d.id) AS invoice_count
        FROM followup_drafts d
    """
    conditions: list[str] = []
    params: list[object] = []
    if gstr2b_import_id is not None:
        conditions.append("d.gstr2b_import_id = ?")
        params.append(gstr2b_import_id)
    if run_id is not None:
        conditions.append("d.reconciliation_run_id = ?")
        params.append(run_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY d.id DESC"
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "Draft": row["id"],
            "Run": row["reconciliation_run_id"],
            "Batch": row["gstr2b_import_id"],
            "Supplier": row["supplier_name"],
            "GSTIN": row["supplier_gstin"] or "",
            "Status": row["status"],
            "Channel": (
                "available" if row["channel_available"] else "unavailable"
            ),
            "Invoices": row["invoice_count"],
            "Taxable (Rs)": format_paise(row["total_taxable_value_paise"]),
            "GST (Rs)": format_paise(row["total_gst_paise"]),
            "Created": row["created_at"],
            "Updated": row["updated_at"],
            "Review reasons": ", ".join(
                _parse_reason_codes(row["review_reasons"])
            ),
        }
        for row in rows
    ]


def verify_followup_with_later_import(
    draft_id: int, later_import_id: int
) -> list[dict]:
    """Record whether an approved follow-up's invoices appear in a later batch.

    This is an evidence check only: it does not alter invoice validation,
    reconciliation results, draft status, or delivery state. Matching uses
    normalized supplier GSTIN + invoice number; multiple candidates are
    preserved as NEEDS_REVIEW instead of selecting one arbitrarily.
    """
    if not isinstance(draft_id, int) or isinstance(draft_id, bool):
        raise ValueError("draft_id must be an integer")
    if not isinstance(later_import_id, int) or isinstance(later_import_id, bool):
        raise ValueError("later_import_id must be an integer")
    draft = get_followup_draft(draft_id)
    if draft is None:
        raise ValueError(f"follow-up draft {draft_id} not found")
    if draft["status"] != followups.STATUS_APPROVED:
        raise ValueError("Only approved follow-up drafts can be verified")

    with db() as conn:
        later_import = conn.execute(
            "SELECT id FROM gstr2b_imports WHERE id = ?", (later_import_id,)
        ).fetchone()
        if later_import is None:
            raise ValueError(f"GSTR-2B import batch {later_import_id} not found")
        if later_import_id == draft["gstr2b_import_id"]:
            raise ValueError("Select a batch other than the original follow-up batch")

        source_rows = conn.execute(
            """SELECT di.invoice_id, di.reconciliation_result_id,
                      di.invoice_number, di.total_gst_paise,
                      rr.match_status, r.gstr2b_import_id AS original_import_id,
                      s.gstin AS supplier_gstin
               FROM followup_draft_invoices di
               JOIN reconciliation_results rr
                 ON rr.id = di.reconciliation_result_id
               JOIN reconciliation_runs r ON r.id = rr.run_id
               JOIN invoices i ON i.id = di.invoice_id
               JOIN suppliers s ON s.id = i.supplier_id
               WHERE di.draft_id = ?
               ORDER BY di.invoice_date, di.invoice_number, di.invoice_id""",
            (draft_id,),
        ).fetchall()
        if not source_rows:
            raise ValueError("This follow-up draft has no linked invoices")
        invalid_origin = [row for row in source_rows
                          if row["match_status"] != reconciliation.STATUS_MISSING_FROM_2B
                          or row["original_import_id"] != draft["gstr2b_import_id"]]
        if invalid_origin:
            raise ValueError("Draft evidence is not an original missing-from-2B result")

        later_rows = conn.execute(
            """SELECT id, supplier_gstin, invoice_number, tax_period, total_gst
               FROM gstr2b_rows
               WHERE import_id = ? AND validation_status = 'valid'
                 AND supplier_gstin IS NOT NULL AND invoice_number IS NOT NULL
                 AND total_gst IS NOT NULL
               ORDER BY id""",
            (later_import_id,),
        ).fetchall()

        outcomes = []
        for source in source_rows:
            gstin = reconciliation.normalize_gstin(source["supplier_gstin"])
            number = reconciliation.normalize_invoice_number(source["invoice_number"])
            if not gstin or not number:
                outcome, row_id, difference = "NEEDS_REVIEW", None, None
                reason = "Invoice lacks a usable GSTIN or invoice number match key."
            else:
                candidates = [
                    row for row in later_rows
                    if reconciliation.normalize_gstin(row["supplier_gstin"]) == gstin
                    and reconciliation.normalize_invoice_number(row["invoice_number"]) == number
                ]
                if not candidates:
                    outcome, row_id, difference = "NOT_FOUND", None, None
                    reason = (
                        "No matching row was found in this later imported batch; "
                        "this does not establish supplier non-filing."
                    )
                elif len(candidates) != 1:
                    outcome, row_id, difference = "NEEDS_REVIEW", None, None
                    reason = "Multiple rows share the supplier and invoice key; no row was selected."
                else:
                    candidate = candidates[0]
                    row_id = candidate["id"]
                    difference = int(source["total_gst_paise"]) - int(candidate["total_gst"])
                    if difference == 0:
                        outcome = "PRESENT"
                        reason = "A unique later-batch row matches the supplier, invoice number, and GST amount."
                    else:
                        outcome = "AMOUNT_MISMATCH"
                        reason = "A unique later-batch row matches the supplier and invoice number, but GST amount differs."

            conn.execute(
                """INSERT OR IGNORE INTO followup_verifications
                       (followup_draft_id, invoice_id, original_result_id,
                        original_import_id, later_import_id, outcome,
                        later_row_id, amount_difference_paise, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (draft_id, source["invoice_id"], source["reconciliation_result_id"],
                 source["original_import_id"], later_import_id, outcome,
                 row_id, difference, reason),
            )
            saved = conn.execute(
                """SELECT * FROM followup_verifications
                   WHERE followup_draft_id = ? AND invoice_id = ?
                     AND later_import_id = ?""",
                (draft_id, source["invoice_id"], later_import_id),
            ).fetchone()
            outcomes.append(dict(saved))
        return outcomes


def get_followup_verifications(draft_id: int | None = None) -> list[dict]:
    query = """SELECT v.*, d.supplier_name, g.file_name AS later_file_name
               FROM followup_verifications v
               JOIN followup_drafts d ON d.id = v.followup_draft_id
               JOIN gstr2b_imports g ON g.id = v.later_import_id"""
    params: tuple[object, ...] = ()
    if draft_id is not None:
        query += " WHERE v.followup_draft_id = ?"
        params = (draft_id,)
    query += " ORDER BY v.id DESC"
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_followup_draft(draft_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM followup_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft["review_reasons"] = list(
        _parse_reason_codes(row["review_reasons"])
    )
    draft["channel_available"] = bool(row["channel_available"])
    return draft


def get_followup_draft_invoices(draft_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM followup_draft_invoices WHERE draft_id = ?
               ORDER BY invoice_date, invoice_number, invoice_id""",
            (draft_id,),
        ).fetchall()
    return [
        {
            "Invoice #": row["invoice_id"],
            "Result #": row["reconciliation_result_id"],
            "Invoice number": row["invoice_number"],
            "Date": row["invoice_date"],
            "Tax period": row["tax_period"],
            "Taxable (Rs)": format_paise(row["taxable_value_paise"]),
            "GST (Rs)": format_paise(row["total_gst_paise"]),
            "Reason": row["reason"],
        }
        for row in rows
    ]


def get_followup_draft_events(draft_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM followup_draft_events WHERE draft_id = ?
               ORDER BY id""",
            (draft_id,),
        ).fetchall()
    return [
        {
            "Event": row["event_type"],
            "From": row["from_status"] or "",
            "To": row["to_status"],
            "Note": row["note"],
            "At": row["created_at"],
        }
        for row in rows
    ]


def get_all_followup_draft_events(limit: int = 200) -> list[dict]:
    """Draft status transitions across all drafts, newest first."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    with db() as conn:
        rows = conn.execute(
            """SELECT e.*, d.supplier_name AS supplier_name
               FROM followup_draft_events e
               LEFT JOIN followup_drafts d ON d.id = e.draft_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "Draft": row["draft_id"],
            "Supplier": row["supplier_name"] or "",
            "Event": row["event_type"],
            "From": row["from_status"] or "",
            "To": row["to_status"],
            "Note": row["note"],
            "At": row["created_at"],
        }
        for row in rows
    ]


def update_followup_draft_text(draft_id: int, draft_text: str) -> None:
    """Persist an edit; only DRAFT/NEEDS_REVIEW drafts are editable."""
    if not isinstance(draft_text, str) or not draft_text.strip():
        raise ValueError("draft text must be a non-empty string")
    draft = get_followup_draft(draft_id)
    if draft is None:
        raise KeyError(f"follow-up draft {draft_id} not found")
    if draft["status"] not in (
        followups.STATUS_DRAFT,
        followups.STATUS_NEEDS_REVIEW,
    ):
        raise ValueError(
            f"draft {draft_id} is {draft['status']} and can no longer be "
            "edited"
        )
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """UPDATE followup_drafts
                   SET draft_text = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (draft_text, draft_id),
            )
            _record_followup_event(
                conn,
                draft_id,
                event_type="EDITED",
                from_status=draft["status"],
                to_status=draft["status"],
                note="Draft text edited",
            )
    finally:
        conn.close()


def get_followup_approval_problems(draft_id: int) -> list[str]:
    """Re-verify evidence before approval. Empty list means approvable."""
    draft = get_followup_draft(draft_id)
    if draft is None:
        return ["DRAFT_NOT_FOUND"]
    problems: list[str] = []
    if draft["status"] != followups.STATUS_DRAFT:
        problems.append(f"STATUS_{draft['status']}")
    if not draft["channel_available"]:
        problems.append("DELIVERY_CHANNEL_UNAVAILABLE")
    if draft["review_reasons"]:
        problems.append("REVIEW_REASONS_PRESENT")

    latest = get_latest_reconciliation_run_for_import(
        draft["gstr2b_import_id"]
    )
    if latest is None or latest["id"] != draft["reconciliation_run_id"]:
        problems.append("STALE_RECONCILIATION_RUN")

    with db() as conn:
        rows = conn.execute(
            """SELECT di.invoice_id, di.reconciliation_result_id,
                      i.validation_status, i.supplier_id,
                      s.gstin AS supplier_gstin, s.contact_phone,
                      rr.run_id, rr.match_status, rr.match_method,
                      rr.gstr2b_row_id, rr.amount_difference_paise,
                      rr.reason_codes
               FROM followup_draft_invoices di
               JOIN invoices i ON i.id = di.invoice_id
               JOIN suppliers s ON s.id = i.supplier_id
               JOIN reconciliation_results rr
                    ON rr.id = di.reconciliation_result_id
               WHERE di.draft_id = ?""",
            (draft_id,),
        ).fetchall()

    if not rows:
        problems.append("NO_SUPPORTING_EVIDENCE")
    for row in rows:
        if row["validation_status"] != followups.VALIDATION_VALID:
            problems.append(f"INVOICE_NOT_VALID:{row['invoice_id']}")
        if row["supplier_id"] != draft["supplier_id"]:
            problems.append(f"SUPPLIER_CHANGED:{row['invoice_id']}")
        current_gstin = "".join(
            (row["supplier_gstin"] or "").split()
        ).upper()
        snapshot_gstin = "".join(
            (draft["supplier_gstin"] or "").split()
        ).upper()
        if current_gstin != snapshot_gstin:
            problems.append(f"SUPPLIER_GSTIN_CHANGED:{row['invoice_id']}")
        if not (row["contact_phone"] or "").strip():
            problems.append(f"CONTACT_PHONE_REMOVED:{row['invoice_id']}")
        if row["run_id"] != draft["reconciliation_run_id"]:
            problems.append(f"RESULT_RUN_MISMATCH:{row['invoice_id']}")
        if row["match_status"] != followups.MATCH_MISSING:
            problems.append(f"RESULT_NOT_MISSING:{row['invoice_id']}")
        if row["gstr2b_row_id"] is not None:
            problems.append(f"ROW_ATTACHED:{row['invoice_id']}")
        if row["amount_difference_paise"] is not None:
            problems.append(f"DIFFERENCE_PRESENT:{row['invoice_id']}")
        if row["match_method"] != followups.METHOD_NONE:
            problems.append(f"METHOD_NOT_NONE:{row['invoice_id']}")
        if _parse_reason_codes(row["reason_codes"]) != (
            followups.REASON_NO_2B_ROW_FOUND,
        ):
            problems.append(f"EVIDENCE_NOT_UNAMBIGUOUS:{row['invoice_id']}")
    return problems


def approve_followup_draft(draft_id: int, *, confirmed: bool) -> None:
    """Approve a draft for a FUTURE integration. Nothing is sent.

    Requires explicit confirmation, an available delivery channel, clean
    review reasons, the batch's latest reconciliation run, and unchanged
    eligible invoice/result evidence.
    """
    if confirmed is not True:
        raise ValueError(
            "Approval requires explicit confirmation; no message was sent."
        )
    draft = get_followup_draft(draft_id)
    if draft is None:
        raise KeyError(f"follow-up draft {draft_id} not found")
    if draft["status"] != followups.STATUS_DRAFT:
        raise ValueError(
            f"draft {draft_id} is {draft['status']}; only DRAFT drafts can "
            "be approved"
        )
    problems = get_followup_approval_problems(draft_id)
    if problems:
        raise ValueError(
            "Draft cannot be approved: " + ", ".join(problems)
            + ". No message was sent."
        )
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """UPDATE followup_drafts
                   SET status = 'APPROVED',
                       approved_text = ?,
                       approved_recipient_phone = ?,
                       approved_at = datetime('now'),
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    draft["draft_text"],
                    (draft["contact_phone"] or "").strip() or None,
                    draft_id,
                ),
            )
            _record_followup_event(
                conn,
                draft_id,
                event_type="STATUS_CHANGED",
                from_status=followups.STATUS_DRAFT,
                to_status=followups.STATUS_APPROVED,
                note="Approved for delivery review; "
                     "no message sent",
            )
    finally:
        conn.close()


def skip_followup_draft(draft_id: int) -> None:
    """Mark a draft as skipped. Nothing is sent."""
    draft = get_followup_draft(draft_id)
    if draft is None:
        raise KeyError(f"follow-up draft {draft_id} not found")
    if draft["status"] == followups.STATUS_SKIPPED:
        raise ValueError(f"draft {draft_id} is already SKIPPED")
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """UPDATE followup_drafts
                   SET status = 'SKIPPED', updated_at = datetime('now')
                   WHERE id = ?""",
                (draft_id,),
            )
            _record_followup_event(
                conn,
                draft_id,
                event_type="STATUS_CHANGED",
                from_status=draft["status"],
                to_status=followups.STATUS_SKIPPED,
                note="Skipped by user; no message sent",
            )
    finally:
        conn.close()


# --- WhatsApp delivery persistence (Task 7) -------------------------------
# No provider networking here. Real/mock dispatch lives in delivery.py; this
# module only stores fail-closed state transitions and never tokens or raw
# HTTP responses.

DELIVERY_PROVIDERS = ("whatsapp_cloud_api", "mock")

_BLOCKING_DELIVERY_STATUSES = (
    "QUEUED",
    "SENDING",
    "RETRY_PENDING",
    "UNKNOWN",
    "SENT",
    "DELIVERED",
    "READ",
)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_phone_digits(value: str | None) -> str | None:
    """Return E.164-style digits for a plausible phone, else None.

    Accepts an optional leading '+', spaces, hyphens and parentheses.
    Requires 8-15 digits. A syntactically plausible number does NOT prove
    ownership or consent.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("+"):
        text = text[1:]
    if not text or any(
        character not in "0123456789 -()" for character in text
    ):
        return None
    digits = "".join(
        character for character in text if character.isdigit()
    )
    if not 8 <= len(digits) <= 15:
        return None
    return digits


def get_delivery_eligibility(draft_id: int) -> dict:
    """Revalidate an approved draft against current database state.

    Empty problems means a delivery may be prepared. The approval snapshot
    (text and recipient) must exist; later changes never silently alter it.
    """
    draft = get_followup_draft(draft_id)
    if draft is None:
        return {
            "draft": None,
            "problems": ["DRAFT_NOT_FOUND"],
            "eligible": False,
            "approved_text": None,
            "approved_recipient_phone": None,
            "current_contact_phone": None,
        }

    problems: list[str] = []
    if draft["status"] != followups.STATUS_APPROVED:
        problems.append("DRAFT_NOT_APPROVED")
    if draft["review_reasons"]:
        problems.append("REVIEW_REASONS_PRESENT")

    approved_text = draft.get("approved_text")
    approved_phone = draft.get("approved_recipient_phone")
    approved_at = draft.get("approved_at")
    if not approved_text or not approved_at:
        problems.append("APPROVAL_SNAPSHOT_MISSING")
    elif draft["draft_text"] != approved_text:
        problems.append("APPROVED_TEXT_CHANGED")
    if not approved_phone:
        problems.append("APPROVAL_RECIPIENT_MISSING")

    with db() as conn:
        supplier = conn.execute(
            """SELECT supplier_name, gstin, contact_phone
               FROM suppliers WHERE id = ?""",
            (draft["supplier_id"],),
        ).fetchone()
        evidence_rows = conn.execute(
            """SELECT di.invoice_id, di.reconciliation_result_id,
                      i.validation_status, i.supplier_id,
                      s.gstin AS supplier_gstin,
                      rr.run_id, rr.match_status, rr.match_method,
                      rr.gstr2b_row_id, rr.amount_difference_paise,
                      rr.reason_codes
               FROM followup_draft_invoices di
               JOIN invoices i ON i.id = di.invoice_id
               JOIN suppliers s ON s.id = i.supplier_id
               JOIN reconciliation_results rr
                    ON rr.id = di.reconciliation_result_id
               WHERE di.draft_id = ?""",
            (draft_id,),
        ).fetchall()

    current_phone = supplier["contact_phone"] if supplier is not None else None
    if supplier is None:
        problems.append("SUPPLIER_MISSING")
    else:
        snapshot_gstin = "".join(
            (draft["supplier_gstin"] or "").split()
        ).upper()
        current_gstin = "".join(
            (supplier["gstin"] or "").split()
        ).upper()
        if current_gstin != snapshot_gstin:
            problems.append("SUPPLIER_GSTIN_CHANGED")

    approved_digits = normalize_phone_digits(approved_phone)
    current_digits = normalize_phone_digits(current_phone)
    if approved_phone and approved_digits is None:
        problems.append("APPROVED_RECIPIENT_INVALID")
    if not (current_phone or "").strip():
        problems.append("CONTACT_PHONE_MISSING")
    elif current_digits is None:
        problems.append("RECIPIENT_PHONE_INVALID")
    elif approved_digits is not None and current_digits != approved_digits:
        problems.append("RECIPIENT_PHONE_CHANGED")

    latest = get_latest_reconciliation_run_for_import(
        draft["gstr2b_import_id"]
    )
    if latest is None or latest["id"] != draft["reconciliation_run_id"]:
        problems.append("STALE_RECONCILIATION_RUN")

    if not evidence_rows:
        problems.append("NO_SUPPORTING_EVIDENCE")
    for row in evidence_rows:
        if row["validation_status"] != followups.VALIDATION_VALID:
            problems.append(f"INVOICE_NOT_VALID:{row['invoice_id']}")
        if row["supplier_id"] != draft["supplier_id"]:
            problems.append(f"SUPPLIER_CHANGED:{row['invoice_id']}")
        if row["run_id"] != draft["reconciliation_run_id"]:
            problems.append(f"RESULT_RUN_MISMATCH:{row['invoice_id']}")
        if row["match_status"] != followups.MATCH_MISSING:
            problems.append(f"RESULT_NOT_MISSING:{row['invoice_id']}")
        if row["gstr2b_row_id"] is not None:
            problems.append(f"ROW_ATTACHED:{row['invoice_id']}")
        if row["amount_difference_paise"] is not None:
            problems.append(f"DIFFERENCE_PRESENT:{row['invoice_id']}")
        if row["match_method"] != followups.METHOD_NONE:
            problems.append(f"METHOD_NOT_NONE:{row['invoice_id']}")
        if _parse_reason_codes(row["reason_codes"]) != (
            followups.REASON_NO_2B_ROW_FOUND,
        ):
            problems.append(f"EVIDENCE_NOT_UNAMBIGUOUS:{row['invoice_id']}")

    return {
        "draft": draft,
        "problems": problems,
        "eligible": not problems,
        "approved_text": approved_text,
        "approved_recipient_phone": approved_phone,
        "current_contact_phone": current_phone,
    }


def get_message_delivery(delivery_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM message_deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_message_delivery_by_idempotency_key(key: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM message_deliveries WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_blocking_delivery_for_draft(draft_id: int) -> dict | None:
    placeholders = ", ".join("?" for _ in _BLOCKING_DELIVERY_STATUSES)
    with db() as conn:
        row = conn.execute(
            f"""SELECT * FROM message_deliveries
                WHERE followup_draft_id = ? AND status IN ({placeholders})
                ORDER BY id DESC LIMIT 1""",
            (draft_id, *_BLOCKING_DELIVERY_STATUSES),
        ).fetchone()
    return dict(row) if row is not None else None


def get_message_deliveries(
    *,
    gstr2b_import_id: int | None = None,
    draft_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    query = """
        SELECT m.*, d.supplier_name, d.supplier_gstin, d.gstr2b_import_id
        FROM message_deliveries m
        JOIN followup_drafts d ON d.id = m.followup_draft_id
    """
    conditions: list[str] = []
    params: list[object] = []
    if gstr2b_import_id is not None:
        conditions.append("d.gstr2b_import_id = ?")
        params.append(gstr2b_import_id)
    if draft_id is not None:
        conditions.append("m.followup_draft_id = ?")
        params.append(draft_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def create_message_delivery(
    draft_id: int,
    *,
    provider: str,
    idempotency_key: str,
    max_attempts: int = 3,
) -> dict:
    """Create a QUEUED delivery with frozen recipient/body snapshots.

    The approved draft is revalidated first. The unique idempotency key is
    the final duplicate guard; concurrent inserts of the same key return the
    existing record instead of creating another one.
    """
    if provider not in DELIVERY_PROVIDERS:
        raise ValueError(f"unsupported delivery provider {provider!r}")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must be a non-empty string")
    if len(idempotency_key) > 200:
        raise ValueError("idempotency_key is too long")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 10
    ):
        raise ValueError("max_attempts must be an integer between 1 and 10")

    existing = get_message_delivery_by_idempotency_key(idempotency_key)
    if existing is not None:
        return {"delivery": existing, "created": False}

    eligibility = get_delivery_eligibility(draft_id)
    if not eligibility["eligible"]:
        raise ValueError(
            "Delivery blocked: " + ", ".join(eligibility["problems"])
        )
    blocking = get_blocking_delivery_for_draft(draft_id)
    if blocking is not None:
        raise ValueError(
            "Delivery blocked: an accepted or unresolved delivery already "
            f"exists for this draft (id {blocking['id']}, "
            f"status {blocking['status']})"
        )

    recipient = eligibility["approved_recipient_phone"]
    body = eligibility["approved_text"]
    conn = get_connection()
    conflict = False
    row = None
    created = False
    try:
        try:
            with conn:
                cursor = conn.execute(
                    """INSERT INTO message_deliveries
                           (followup_draft_id, idempotency_key,
                            recipient_phone_snapshot, message_body_snapshot,
                            provider, status, attempt_count, max_attempts)
                       VALUES (?, ?, ?, ?, ?, 'QUEUED', 0, ?)
                       ON CONFLICT(idempotency_key) DO NOTHING""",
                    (
                        draft_id,
                        idempotency_key,
                        recipient,
                        body,
                        provider,
                        max_attempts,
                    ),
                )
                created = cursor.rowcount == 1
                row = conn.execute(
                    """SELECT * FROM message_deliveries
                       WHERE idempotency_key = ?""",
                    (idempotency_key,),
                ).fetchone()
        except sqlite3.IntegrityError:
            conflict = True
    finally:
        conn.close()
    if conflict:
        existing = get_message_delivery_by_idempotency_key(idempotency_key)
        if existing is not None:
            return {"delivery": existing, "created": False}
        raise ValueError(
            "Delivery blocked: a delivery record already exists for this "
            "draft"
        )
    if row is None:
        raise RuntimeError("could not create or load delivery record")
    delivery = dict(row)
    return {"delivery": delivery, "created": created}


def claim_message_delivery(
    delivery_id: int, *, now_text: str | None = None
) -> bool:
    """Atomically move QUEUED/RETRY_PENDING to SENDING exactly once."""
    now = now_text or _utc_now_text()
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """UPDATE message_deliveries
                   SET status = 'SENDING',
                       attempt_count = attempt_count + 1,
                       last_attempt_at = ?,
                       updated_at = ?,
                       next_attempt_at = NULL,
                       provider_message_id = NULL,
                       last_http_status = NULL,
                       last_error_code = NULL,
                       last_error_message = NULL
                   WHERE id = ?
                     AND status IN ('QUEUED', 'RETRY_PENDING')
                     AND attempt_count < max_attempts
                     AND (status = 'QUEUED'
                          OR next_attempt_at IS NULL
                          OR next_attempt_at <= ?)""",
                (now, now, delivery_id, now),
            )
    finally:
        conn.close()
    return cursor.rowcount == 1


def finish_message_delivery_attempt(
    delivery_id: int,
    *,
    status: str,
    provider_message_id: str | None = None,
    http_status: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    next_attempt_at: str | None = None,
) -> None:
    """Record the outcome of a send attempt currently in SENDING.

    DELIVERED/READ are rejected here: they require authenticated provider
    evidence, and no webhook receiver is implemented.
    """
    if status not in SEND_RESULT_STATUSES:
        raise ValueError(
            f"send attempts may only record {SEND_RESULT_STATUSES}, "
            f"not {status!r}"
        )
    if status == "SENT":
        provider_message_id = (provider_message_id or "").strip()
        if not provider_message_id or len(provider_message_id) > 200:
            raise ValueError(
                "SENT requires a provider message ID (max 200 characters)"
            )
    code = (error_code or "").strip()[:100] or None
    message = (error_message or "").strip()[:300] or None
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """UPDATE message_deliveries
                   SET status = ?,
                       provider_message_id = ?,
                       last_http_status = ?,
                       last_error_code = ?,
                       last_error_message = ?,
                       next_attempt_at = ?,
                       updated_at = datetime('now'),
                       sent_at = CASE WHEN ? = 'SENT'
                                      THEN datetime('now')
                                      ELSE sent_at END,
                       failed_at = CASE WHEN ? = 'FAILED'
                                        THEN datetime('now')
                                        ELSE failed_at END
                   WHERE id = ? AND status = 'SENDING'""",
                (
                    status,
                    provider_message_id,
                    http_status,
                    code,
                    message,
                    next_attempt_at,
                    status,
                    status,
                    delivery_id,
                ),
            )
    finally:
        conn.close()
    if cursor.rowcount != 1:
        raise ValueError(
            f"delivery {delivery_id} is not in SENDING state"
        )


def resolve_unknown_delivery(
    delivery_id: int,
    *,
    outcome: str,
    resolved_by: str,
    note: str,
    provider_message_id: str | None = None,
) -> None:
    """Manual reconciliation of an UNKNOWN outcome.

    Only an operator, using evidence from Meta, may resolve it. Confirmed
    acceptance records SENT (never DELIVERED); confirmed non-acceptance makes
    a bounded retry possible.
    """
    if outcome not in ("SENT_CONFIRMED", "NOT_SENT"):
        raise ValueError("outcome must be SENT_CONFIRMED or NOT_SENT")
    by = (resolved_by or "").strip()
    reason = (note or "").strip()
    if not by or len(by) > 200:
        raise ValueError("resolved_by is required (max 200 characters)")
    if not reason or len(reason) > 500:
        raise ValueError("resolution note is required (max 500 characters)")
    if outcome == "SENT_CONFIRMED":
        provider_id = (provider_message_id or "").strip()
        if not provider_id or len(provider_id) > 200:
            raise ValueError(
                "a provider message ID is required to confirm acceptance"
            )
    else:
        provider_id = None

    conn = get_connection()
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM message_deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"delivery {delivery_id} not found")
            if row["status"] != "UNKNOWN":
                raise ValueError(
                    f"delivery {delivery_id} is {row['status']}, not UNKNOWN"
                )
            if outcome == "SENT_CONFIRMED":
                conn.execute(
                    """UPDATE message_deliveries
                       SET status = 'SENT',
                           provider_message_id = ?,
                           sent_at = COALESCE(sent_at, datetime('now')),
                           failed_at = NULL,
                           next_attempt_at = NULL,
                           last_error_code = NULL,
                           last_error_message = NULL,
                           resolved_at = datetime('now'),
                           resolved_by = ?,
                           resolution_note = ?,
                           resolution_outcome = ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (provider_id, by, reason, outcome, delivery_id),
                )
            elif row["attempt_count"] < row["max_attempts"]:
                conn.execute(
                    """UPDATE message_deliveries
                       SET status = 'RETRY_PENDING',
                           next_attempt_at = datetime('now'),
                           resolved_at = datetime('now'),
                           resolved_by = ?,
                           resolution_note = ?,
                           resolution_outcome = ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (by, reason, outcome, delivery_id),
                )
            else:
                conn.execute(
                    """UPDATE message_deliveries
                       SET status = 'FAILED',
                           failed_at = datetime('now'),
                           last_error_code = 'UNKNOWN_NOT_SENT',
                           last_error_message = 'Manually resolved as not sent after an unknown outcome',
                           next_attempt_at = NULL,
                           resolved_at = datetime('now'),
                           resolved_by = ?,
                           resolution_note = ?,
                           resolution_outcome = ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (by, reason, outcome, delivery_id),
                )
    finally:
        conn.close()
