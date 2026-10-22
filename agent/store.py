"""Persistent agent state (SQLite). Separate `agent_*` tables, same style as db.py.

One row per purchase invoice in `agent_invoices`, carrying its case state.
Money is always integer paise.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import get_settings

# ---- case states -----------------------------------------------------------
NEEDS_INPUT = "NEEDS_INPUT"          # extraction uncertain / GSTIN to confirm
INVALID = "INVALID"                  # bill fails arithmetic after confirmation
PENDING_2B = "PENDING_2B"            # recorded, waiting for the period's 2B
MATCHED = "MATCHED"                  # terminal: present in 2B
INELIGIBLE = "INELIGIBLE"            # terminal: §17(5) / ITC not available
QRMP_WAIT = "QRMP_WAIT"              # quarterly filer, legitimately absent
TO_CHASE = "TO_CHASE"                # diagnosed, awaiting trader approval
CHASED = "CHASED"                    # message sent, waiting
PROMISED = "PROMISED"                # supplier replied, promised action
RECOVERED = "RECOVERED"              # terminal: appeared in a later 2B
ESCALATED = "ESCALATED"              # terminal: chase limit hit, handed over
CHANNEL_BLOCKED = "CHANNEL_BLOCKED"  # send failed; call script given
SKIPPED = "SKIPPED"                  # terminal: trader chose not to chase

TERMINAL = {MATCHED, INELIGIBLE, RECOVERED, ESCALATED, SKIPPED}
AT_RISK = {TO_CHASE, CHASED, PROMISED, ESCALATED, CHANNEL_BLOCKED, SKIPPED,
           QRMP_WAIT}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_traders (
    phone TEXT PRIMARY KEY, name TEXT, lang TEXT NOT NULL DEFAULT 'hi',
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_phone TEXT NOT NULL, gstin TEXT NOT NULL, name TEXT,
    phone TEXT, filing_hint TEXT, consent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (trader_phone, gstin));
CREATE TABLE IF NOT EXISTS agent_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_phone TEXT NOT NULL,
    media_sha TEXT, image_path TEXT,
    supplier_name TEXT, supplier_gstin TEXT,
    invoice_number TEXT, invoice_date TEXT, tax_period TEXT,
    taxable_paise INTEGER, cgst_paise INTEGER, sgst_paise INTEGER,
    igst_paise INTEGER, total_gst_paise INTEGER, invoice_total_paise INTEGER,
    gst_rate INTEGER, description TEXT,
    extraction_json TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT 'photo',
    state TEXT NOT NULL, state_reason TEXT NOT NULL DEFAULT '',
    diagnosis TEXT, pending_field TEXT,
    eligibility TEXT NOT NULL DEFAULT 'ELIGIBLE',
    chase_count INTEGER NOT NULL DEFAULT 0,
    last_chased_at TEXT, next_check_at TEXT, recovered_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ai_trader_state ON agent_invoices(trader_phone, state);
CREATE TABLE IF NOT EXISTS agent_2b_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trader_phone TEXT NOT NULL,
    file_name TEXT, file_hash TEXT, source_format TEXT, periods TEXT,
    accepted INTEGER NOT NULL, rejected INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_2b_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, import_id INTEGER NOT NULL,
    supplier_gstin TEXT, supplier_name TEXT, invoice_number TEXT,
    invoice_date TEXT, tax_period TEXT, return_period TEXT, taxable_paise INTEGER,
    total_gst_paise INTEGER, itc_available INTEGER NOT NULL DEFAULT 1,
    valid INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS agent_2b_history (
    trader_phone TEXT NOT NULL, supplier_gstin TEXT NOT NULL,
    tax_period TEXT NOT NULL,
    PRIMARY KEY (trader_phone, supplier_gstin, tax_period));
CREATE TABLE IF NOT EXISTS agent_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trader_phone TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS agent_outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trader_phone TEXT NOT NULL,
    to_phone TEXT NOT NULL, role TEXT NOT NULL, supplier_gstin TEXT,
    invoice_ids TEXT NOT NULL DEFAULT '[]', body TEXT NOT NULL, media_url TEXT,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    provider_id TEXT, error TEXT, idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, role TEXT NOT NULL,
    direction TEXT NOT NULL, body TEXT, media_type TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trader_phone TEXT, turn_id TEXT,
    kind TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trace_turn ON agent_trace(turn_id);
"""

_lock = threading.RLock()
_initialized: set[str] = set()


def db_path() -> Path:
    return get_settings().db_path


@contextmanager
def conn():
    """One serialized connection per use. All agent DB access in a process goes
    through a re-entrant lock, so concurrent webhook threads never collide."""
    path = db_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        key = str(path)
        try:
            if key not in _initialized:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                _initialized.add(key)
            yield c
            c.commit()
        finally:
            c.close()


def reset_initialized() -> None:
    _initialized.clear()


# ---- clock (offset lets the demo fast-forward weeks, disclosed as such) ----

def now() -> datetime:
    with conn() as c:
        row = c.execute("SELECT value FROM agent_meta WHERE key='clock_offset_days'").fetchone()
    offset = float(row["value"]) if row else 0.0
    return datetime.now(timezone.utc) + timedelta(days=offset)


def now_text() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def advance_clock(days: float) -> float:
    with conn() as c:
        row = c.execute("SELECT value FROM agent_meta WHERE key='clock_offset_days'").fetchone()
        total = (float(row["value"]) if row else 0.0) + days
        c.execute("INSERT OR REPLACE INTO agent_meta(key, value) VALUES ('clock_offset_days', ?)",
                  (str(total),))
    return total


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ---- traders & suppliers ----------------------------------------------------

def ensure_trader(phone: str, lang: str = "hi") -> dict:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO agent_traders(phone, lang, created_at) VALUES (?,?,?)",
                  (phone, lang, now_text()))
        return dict(c.execute("SELECT * FROM agent_traders WHERE phone=?", (phone,)).fetchone())


def set_trader_lang(phone: str, lang: str) -> None:
    with conn() as c:
        c.execute("UPDATE agent_traders SET lang=? WHERE phone=?", (lang, phone))


def get_trader(phone: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM agent_traders WHERE phone=?", (phone,)).fetchone()
    return dict(r) if r else None


def upsert_supplier(trader: str, gstin: str, name: str | None = None,
                    phone: str | None = None, filing_hint: str | None = None,
                    consent: int | None = None) -> dict:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO agent_suppliers(trader_phone, gstin, name, created_at) "
                  "VALUES (?,?,?,?)", (trader, gstin, name, now_text()))
        if name:
            c.execute("UPDATE agent_suppliers SET name=COALESCE(name, ?) WHERE trader_phone=? AND gstin=?",
                      (name, trader, gstin))
        if phone is not None:
            c.execute("UPDATE agent_suppliers SET phone=? WHERE trader_phone=? AND gstin=?",
                      (phone, trader, gstin))
        if filing_hint is not None:
            c.execute("UPDATE agent_suppliers SET filing_hint=? WHERE trader_phone=? AND gstin=?",
                      (filing_hint, trader, gstin))
        if consent is not None:
            c.execute("UPDATE agent_suppliers SET consent=? WHERE trader_phone=? AND gstin=?",
                      (consent, trader, gstin))
        return dict(c.execute("SELECT * FROM agent_suppliers WHERE trader_phone=? AND gstin=?",
                              (trader, gstin)).fetchone())


def get_suppliers(trader: str) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM agent_suppliers WHERE trader_phone=? ORDER BY name",
                               (trader,)))


def get_supplier(trader: str, gstin: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM agent_suppliers WHERE trader_phone=? AND gstin=?",
                      (trader, gstin)).fetchone()
    return dict(r) if r else None


def find_supplier_by_phone(phone: str) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM agent_suppliers WHERE phone=?", (phone,)))


# ---- invoices ---------------------------------------------------------------

INVOICE_FIELDS = (
    "media_sha", "image_path", "supplier_name", "supplier_gstin", "invoice_number",
    "invoice_date", "tax_period", "taxable_paise", "cgst_paise", "sgst_paise",
    "igst_paise", "total_gst_paise", "invoice_total_paise", "gst_rate", "description",
    "extraction_json", "provenance", "state", "state_reason", "diagnosis",
    "pending_field", "eligibility", "chase_count", "last_chased_at",
    "next_check_at", "recovered_at",
)


def insert_invoice(trader: str, **fields: Any) -> int:
    data = {k: v for k, v in fields.items() if k in INVOICE_FIELDS}
    data.setdefault("state", PENDING_2B)
    ts = now_text()
    cols = ["trader_phone", *data.keys(), "created_at", "updated_at"]
    vals = [trader, *data.values(), ts, ts]
    with conn() as c:
        cur = c.execute(f"INSERT INTO agent_invoices({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        vals)
        return int(cur.lastrowid)


def update_invoice(invoice_id: int, **fields: Any) -> None:
    data = {k: v for k, v in fields.items() if k in INVOICE_FIELDS}
    if not data:
        return
    data["updated_at"] = now_text()
    sets = ", ".join(f"{k}=?" for k in data)
    with conn() as c:
        c.execute(f"UPDATE agent_invoices SET {sets} WHERE id=?", (*data.values(), invoice_id))


def get_invoice(invoice_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM agent_invoices WHERE id=?", (invoice_id,)).fetchone()
    return dict(r) if r else None


def get_invoices(trader: str, states: Iterable[str] | None = None,
                 period: str | None = None) -> list[dict]:
    q = "SELECT * FROM agent_invoices WHERE trader_phone=?"
    args: list[Any] = [trader]
    if states:
        states = list(states)
        q += f" AND state IN ({','.join('?' * len(states))})"
        args += states
    if period:
        q += " AND tax_period=?"
        args.append(period)
    q += " ORDER BY id"
    with conn() as c:
        return _rows(c.execute(q, args))


def find_invoice_by_media(trader: str, sha: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM agent_invoices WHERE trader_phone=? AND media_sha=?",
                      (trader, sha)).fetchone()
    return dict(r) if r else None


def find_duplicate_invoice(trader: str, gstin: str, number: str, period: str,
                           exclude_id: int | None = None) -> dict | None:
    with conn() as c:
        r = c.execute(
            "SELECT * FROM agent_invoices WHERE trader_phone=? AND upper(supplier_gstin)=upper(?) "
            "AND upper(trim(invoice_number))=upper(trim(?)) AND tax_period=? AND id != ?",
            (trader, gstin, number, period, exclude_id or -1)).fetchone()
    return dict(r) if r else None


# ---- GSTR-2B ----------------------------------------------------------------

def save_2b_import(trader: str, file_name: str, file_hash: str, source_format: str,
                   rows: list[dict], rejected: int) -> int:
    periods = sorted({r["return_period"] for r in rows if r.get("return_period")})
    with conn() as c:
        cur = c.execute(
            "INSERT INTO agent_2b_imports(trader_phone, file_name, file_hash, source_format, periods, "
            "accepted, rejected, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (trader, file_name, file_hash, source_format, ",".join(periods), len(rows), rejected,
             now_text()))
        import_id = int(cur.lastrowid)
        for r in rows:
            c.execute(
                "INSERT INTO agent_2b_rows(import_id, supplier_gstin, supplier_name, invoice_number, "
                "invoice_date, tax_period, return_period, taxable_paise, total_gst_paise, "
                "itc_available, valid) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (import_id, r["supplier_gstin"], r.get("supplier_name"), r["invoice_number"],
                 r.get("invoice_date"), r["tax_period"], r["return_period"], r.get("taxable_paise"),
                 r["total_gst_paise"], 1 if r.get("itc_available", True) else 0))
            c.execute("INSERT OR IGNORE INTO agent_2b_history VALUES (?,?,?)",
                      (trader, r["supplier_gstin"], r["return_period"]))
    return import_id


def get_all_2b_rows(trader: str) -> list[dict]:
    """Every accepted 2B row this trader has ever imported (latest import wins on ties)."""
    with conn() as c:
        return _rows(c.execute(
            "SELECT r.*, i.created_at AS imported_at FROM agent_2b_rows r "
            "JOIN agent_2b_imports i ON i.id=r.import_id WHERE i.trader_phone=? ORDER BY r.import_id, r.id",
            (trader,)))


def get_2b_imports(trader: str) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM agent_2b_imports WHERE trader_phone=? ORDER BY id",
                               (trader,)))


def add_history(trader: str, gstin: str, period: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO agent_2b_history VALUES (?,?,?)", (trader, gstin, period))


def supplier_history(trader: str, gstin: str) -> list[str]:
    with conn() as c:
        return [r["tax_period"] for r in c.execute(
            "SELECT tax_period FROM agent_2b_history WHERE trader_phone=? AND supplier_gstin=? "
            "ORDER BY tax_period", (trader, gstin))]


def known_gstins(trader: str) -> set[str]:
    with conn() as c:
        a = {r[0] for r in c.execute("SELECT supplier_gstin FROM agent_2b_history WHERE trader_phone=?",
                                     (trader,))}
        b = {r[0] for r in c.execute("SELECT gstin FROM agent_suppliers WHERE trader_phone=?",
                                     (trader,))}
    return a | b


# ---- pending confirmations --------------------------------------------------

def create_pending(trader: str, kind: str, payload: dict) -> int:
    with conn() as c:
        c.execute("UPDATE agent_pending SET status='SUPERSEDED', resolved_at=? "
                  "WHERE trader_phone=? AND kind=? AND status='PENDING'", (now_text(), trader, kind))
        cur = c.execute("INSERT INTO agent_pending(trader_phone, kind, payload, created_at) VALUES (?,?,?,?)",
                        (trader, kind, json.dumps(payload, ensure_ascii=False), now_text()))
        return int(cur.lastrowid)


def get_pending(trader: str, kind: str | None = None) -> dict | None:
    q = "SELECT * FROM agent_pending WHERE trader_phone=? AND status='PENDING'"
    args: list[Any] = [trader]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY id DESC LIMIT 1"
    with conn() as c:
        r = c.execute(q, args).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return d


def resolve_pending(pending_id: int, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE agent_pending SET status=?, resolved_at=? WHERE id=?",
                  (status, now_text(), pending_id))


# ---- outbound messages --------------------------------------------------------

def create_outbound(trader: str, to_phone: str, role: str, body: str, *,
                    supplier_gstin: str | None = None, invoice_ids: list[int] | None = None,
                    media_url: str | None = None, idempotency_key: str | None = None) -> dict:
    ts = now_text()
    with conn() as c:
        if idempotency_key:
            existing = c.execute("SELECT * FROM agent_outbound WHERE idempotency_key=?",
                                 (idempotency_key,)).fetchone()
            if existing:
                return dict(existing)
        cur = c.execute(
            "INSERT INTO agent_outbound(trader_phone, to_phone, role, supplier_gstin, invoice_ids, body, "
            "media_url, status, idempotency_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (trader, to_phone, role, supplier_gstin, json.dumps(invoice_ids or []), body, media_url,
             "QUEUED", idempotency_key, ts, ts))
        return dict(c.execute("SELECT * FROM agent_outbound WHERE id=?", (cur.lastrowid,)).fetchone())


def update_outbound(out_id: int, **fields: Any) -> None:
    allowed = {"status", "attempts", "provider_id", "error"}
    data = {k: v for k, v in fields.items() if k in allowed}
    data["updated_at"] = now_text()
    sets = ", ".join(f"{k}=?" for k in data)
    with conn() as c:
        c.execute(f"UPDATE agent_outbound SET {sets} WHERE id=?", (*data.values(), out_id))


def get_outbound(trader: str | None = None, role: str | None = None) -> list[dict]:
    q = "SELECT * FROM agent_outbound WHERE 1=1"
    args: list[Any] = []
    if trader:
        q += " AND trader_phone=?"
        args.append(trader)
    if role:
        q += " AND role=?"
        args.append(role)
    with conn() as c:
        return _rows(c.execute(q + " ORDER BY id", args))


# ---- conversation & trace --------------------------------------------------------

def log_message(phone: str, role: str, direction: str, body: str | None,
                media_type: str | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO agent_messages(phone, role, direction, body, media_type, created_at) "
                  "VALUES (?,?,?,?,?,?)", (phone, role, direction, body, media_type, now_text()))


def recent_messages(phone: str, limit: int = 12) -> list[dict]:
    with conn() as c:
        rows = _rows(c.execute("SELECT * FROM agent_messages WHERE phone=? ORDER BY id DESC LIMIT ?",
                               (phone, limit)))
    return list(reversed(rows))


def trace(trader: str | None, turn_id: str | None, kind: str, name: str,
          payload: Any = None) -> None:
    text = json.dumps(payload if payload is not None else {}, ensure_ascii=False, default=str)
    if len(text) > 8000:
        text = text[:8000] + "…"
    with conn() as c:
        c.execute("INSERT INTO agent_trace(trader_phone, turn_id, kind, name, payload, created_at) "
                  "VALUES (?,?,?,?,?,?)", (trader, turn_id, kind, name, text, now_text()))


def get_trace(trader: str | None = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM agent_trace"
    args: list[Any] = []
    if trader:
        q += " WHERE trader_phone=?"
        args.append(trader)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return _rows(c.execute(q, args))
