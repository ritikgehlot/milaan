"""Bill photo -> validated invoice fields.

Three independent signals guard the two dangerous fields (GSTIN, tax):
1. GSTIN check digit + tax arithmetic (deterministic, from validation.py)
2. A second, independent "double read" of the critical fields
3. Model-reported per-field confidence (weakest signal; used only as a gate)

A GSTIN is never inferred, completed or "corrected" by the agent. If any
signal fails, the field goes back to the trader as a targeted question.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import validation

from .config import get_settings
from .llm import LLMClient, LLMUnavailable, image_part, parse_json_object

CRITICAL_FIELDS = ("supplier_gstin", "invoice_number", "invoice_date", "total_gst")
CONFIDENCE_THRESHOLD = 0.80
SUPPORTED_RATES = (0, 5, 12, 18, 28)

EXTRACT_PROMPT = """You read Indian GST purchase invoices (tax invoices / bills) from photos.
The photo was sent by the BUYER (a shopkeeper). Extract the SELLER's details.

Return ONLY a JSON object with these keys:
{
 "is_bill": true|false,              // false if this is not a GST invoice/bill at all
 "supplier_name": string|null,       // seller / issuer of the bill
 "supplier_gstin": string|null,      // SELLER's GSTIN exactly as printed, 15 chars
 "buyer_gstin": string|null,         // buyer's GSTIN if printed
 "invoice_number": string|null,      // exactly as printed, keep slashes/dashes
 "invoice_date": "YYYY-MM-DD"|null,
 "taxable_value": "1234.56"|null,    // total taxable value before tax, rupees
 "cgst": "0.00"|null, "sgst": "0.00"|null, "igst": "0.00"|null,
 "total_gst": "0.00"|null,           // total tax amount
 "invoice_total": "0.00"|null,       // grand total
 "gst_rate": 18|null,                // single GST rate in percent; null if multiple rates
 "description": string|null,         // 3-8 words: what goods/services were bought
 "confidence": {"supplier_gstin":0-1,"invoice_number":0-1,"invoice_date":0-1,
                "total_gst":0-1,"taxable_value":0-1,"invoice_total":0-1},
 "boxes": {"supplier_gstin":[ymin,xmin,ymax,xmax], "invoice_number":[...], "total_gst":[...],
           "invoice_date":[...]}       // 0-1000 normalized coordinates of each printed value
}
Rules:
- NEVER guess or complete a character you cannot read. Use null and low confidence instead.
- Amounts as plain decimal strings without commas or currency symbols.
- If the photo is blurry or cut off, lower the confidence of affected fields honestly.
"""

DOUBLE_READ_PROMPT = """Look only at this invoice photo. Read three printed values character by
character, independently: the SELLER's GSTIN (15 characters), the invoice number, and the
total GST/tax amount. If any character is unreadable, return null for that value.
Return ONLY JSON: {"supplier_gstin": string|null, "invoice_number": string|null,
"total_gst": "0.00"|null}"""


@dataclass
class Extraction:
    status: str                     # 'VALID' | 'NEEDS_INPUT' | 'REJECTED'
    fields: dict = field(default_factory=dict)   # normalized (paise ints)
    uncertain: list[str] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    usage: list[dict] = field(default_factory=list)
    boxes: dict = field(default_factory=dict)

    @property
    def ask_field(self) -> str | None:
        return self.uncertain[0] if self.uncertain else None


# ---- parsing helpers --------------------------------------------------------

def money_to_paise(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
    text = text.replace(" ", "")
    if text in {"", "-", "null", "None"}:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    if amount != amount.quantize(Decimal("0.01")):
        return None
    return int(amount * 100)


def parse_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
                "%d %b %Y", "%d-%b-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def clean_gstin(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(r"[\s\-]", "", str(value)).upper()
    return text or None


def find_gstin_in_text(text: str) -> str | None:
    m = re.search(r"\b([0-9]{2}[A-Za-z0-9]{13})\b", re.sub(r"[\s\-]", "", text or ""))
    if m:
        return m.group(1).upper()
    m = re.search(r"([0-9]{2}[A-Za-z]{5}[0-9]{4}[A-Za-z][0-9A-Za-z][Zz][0-9A-Za-z])", text or "")
    return m.group(1).upper() if m else None


def derive_rate(taxable: int | None, total_gst: int | None) -> int | None:
    if not taxable or total_gst is None:
        return None
    for rate in SUPPORTED_RATES:
        expected = (taxable * rate * 2 + 100) // 200  # round half up
        if abs(expected - total_gst) <= 1:
            return rate
    return None


def normalize_fields(raw: dict) -> dict:
    taxable = money_to_paise(raw.get("taxable_value"))
    cgst = money_to_paise(raw.get("cgst")) or 0
    sgst = money_to_paise(raw.get("sgst")) or 0
    igst = money_to_paise(raw.get("igst")) or 0
    total_gst = money_to_paise(raw.get("total_gst"))
    if total_gst is None and (cgst or sgst or igst):
        total_gst = cgst + sgst + igst
    invoice_total = money_to_paise(raw.get("invoice_total"))
    invoice_date = parse_date(raw.get("invoice_date"))
    rate = raw.get("gst_rate")
    try:
        rate = int(rate) if rate is not None else None
    except (TypeError, ValueError):
        rate = None
    if rate not in SUPPORTED_RATES:
        rate = derive_rate(taxable, total_gst)
    number = raw.get("invoice_number")
    return {
        "supplier_name": (raw.get("supplier_name") or "").strip() or None,
        "supplier_gstin": clean_gstin(raw.get("supplier_gstin")),
        "buyer_gstin": clean_gstin(raw.get("buyer_gstin")),
        "invoice_number": str(number).strip() if number else None,
        "invoice_date": invoice_date,
        "tax_period": invoice_date[:7] if invoice_date else None,
        "taxable_paise": taxable,
        "cgst_paise": cgst,
        "sgst_paise": sgst,
        "igst_paise": igst,
        "total_gst_paise": total_gst,
        "invoice_total_paise": invoice_total,
        "gst_rate": rate,
        "description": (raw.get("description") or "").strip() or None,
    }


# ---- deterministic checks -----------------------------------------------------

FIELD_FOR_CODE = {
    "GSTIN_FORMAT": "supplier_gstin", "GSTIN_STATE_CODE": "supplier_gstin",
    "GSTIN_CHECKSUM": "supplier_gstin", "GSTIN_MISSING": "supplier_gstin",
    "INVALID_DATE": "invoice_date", "INVALID_TAX_PERIOD": "invoice_date",
    "RATE_MISMATCH": "total_gst", "TAX_TOTAL_MISMATCH": "total_gst",
    "INVOICE_TOTAL_MISMATCH": "total_gst", "CGST_SGST_MISMATCH": "total_gst",
    "TAX_STRUCTURE_CONFLICT": "total_gst", "ROUNDING_TOLERANCE": "total_gst",
}


def check_fields(f: dict, trader_gstin: str | None = None) -> list[dict]:
    """Run MILAAN's existing deterministic validator over extracted fields."""
    problems: list[dict] = []
    record = {
        "supplier_gstin": f.get("supplier_gstin"),
        "invoice_number": f.get("invoice_number"),
        "invoice_date": f.get("invoice_date"),
        "tax_period": f.get("tax_period"),
        "taxable_value": f.get("taxable_paise"),
        "cgst": f.get("cgst_paise"), "sgst": f.get("sgst_paise"), "igst": f.get("igst_paise"),
        "total_gst": f.get("total_gst_paise"),
        "invoice_total": f.get("invoice_total_paise"),
        "gst_rate": f.get("gst_rate"),
    }
    multi_rate = record["gst_rate"] is None
    if multi_rate:
        record["gst_rate"] = 0  # validate structure only; skip the rate check below
    result = validation.validate_invoice(record)
    for issue in (*result.errors, *result.warnings):
        if multi_rate and issue.code == "RATE_MISMATCH":
            continue
        if issue.code == "REQUIRED_FIELD":
            fld = {"taxable_value": "taxable_value", "invoice_total": "invoice_total",
                   "total_gst": "total_gst", "tax_period": "invoice_date"}.get(issue.field, issue.field)
        else:
            fld = FIELD_FOR_CODE.get(issue.code, issue.field)
        problems.append({"code": issue.code, "field": fld, "message": issue.message})
    if multi_rate and f.get("taxable_paise") and f.get("total_gst_paise") is not None:
        problems.append({"code": "MULTI_RATE", "field": "gst_rate",
                         "message": "No single GST rate fits; rate check skipped (multi-rate bill)."})
    if trader_gstin and f.get("supplier_gstin") and f["supplier_gstin"] == trader_gstin:
        problems.append({"code": "BUYER_SELLER_SWAPPED", "field": "supplier_gstin",
                         "message": "Extracted seller GSTIN equals the trader's own GSTIN."})
    if f.get("invoice_date"):
        try:
            if date.fromisoformat(f["invoice_date"]) > date.today():
                problems.append({"code": "FUTURE_DATE", "field": "invoice_date",
                                 "message": "Invoice date is in the future."})
        except ValueError:
            pass
    return problems


def uncertain_fields(f: dict, raw: dict, problems: list[dict], second: dict | None) -> list[str]:
    out: list[str] = []

    def add(name: str) -> None:
        if name not in out:
            out.append(name)

    conf = raw.get("confidence") or {}
    for name in CRITICAL_FIELDS:
        key = {"total_gst": "total_gst_paise"}.get(name, name)
        if f.get(key) in (None, ""):
            add(name)
            continue
        try:
            if float(conf.get(name, 1.0)) < CONFIDENCE_THRESHOLD:
                add(name)
        except (TypeError, ValueError):
            pass
    for p in problems:
        if p["code"] in ("MULTI_RATE",):
            continue
        if p["field"] in ("supplier_gstin", "invoice_number", "invoice_date", "total_gst"):
            add(p["field"])
        elif p["field"] in ("taxable_value", "invoice_total", "cgst", "sgst", "igst"):
            add("total_gst")
    if second:
        if clean_gstin(second.get("supplier_gstin")) != f.get("supplier_gstin"):
            add("supplier_gstin")
        n1 = re.sub(r"\s", "", (f.get("invoice_number") or "")).upper()
        n2 = re.sub(r"\s", "", str(second.get("invoice_number") or "")).upper()
        if n1 != n2:
            add("invoice_number")
        if money_to_paise(second.get("total_gst")) != f.get("total_gst_paise"):
            add("total_gst")
    order = {"supplier_gstin": 0, "total_gst": 1, "invoice_number": 2, "invoice_date": 3}
    return sorted(out, key=lambda x: order.get(x, 9))


# ---- extraction entrypoints ---------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finalize(raw: dict, *, second: dict | None = None, trader_gstin: str | None = None,
             usage: list[dict] | None = None) -> Extraction:
    if not raw.get("is_bill", True):
        return Extraction(status="REJECTED", raw=raw, usage=usage or [],
                          problems=[{"code": "NOT_A_BILL", "field": "*",
                                     "message": "Photo is not a GST bill."}])
    f = normalize_fields(raw)
    problems = check_fields(f, trader_gstin)
    unsure = uncertain_fields(f, raw, problems, second)
    status = "NEEDS_INPUT" if unsure else "VALID"
    return Extraction(status=status, fields=f, uncertain=unsure, problems=problems,
                      raw=raw, usage=usage or [], boxes=raw.get("boxes") or {})


def extract_with_llm(image: bytes, mime: str, llm: LLMClient, *,
                     trader_gstin: str | None = None, double_read: bool = True) -> Extraction:
    s = get_settings()
    messages = [{"role": "user", "content": [{"type": "text", "text": EXTRACT_PROMPT},
                                             image_part(image, mime)]}]
    raw, usage1 = llm.json_chat(messages, model=s.vision_model, max_tokens=1200)
    usages = [usage1]
    second = None
    if double_read and raw.get("is_bill", True):
        try:
            m2 = [{"role": "user", "content": [{"type": "text", "text": DOUBLE_READ_PROMPT},
                                               image_part(image, mime)]}]
            second, usage2 = llm.json_chat(m2, model=s.vision_model, max_tokens=300, temperature=0.0)
            usages.append(usage2)
        except LLMUnavailable:
            second = None  # absence of the second read is itself a reason to be careful
            raw.setdefault("confidence", {})
            raw["confidence"]["supplier_gstin"] = min(
                float(raw["confidence"].get("supplier_gstin", 1.0)), 0.79)
    return finalize(raw, second=second, trader_gstin=trader_gstin, usage=usages)


def extract_from_fixture(sidecar: dict, trader_gstin: str | None = None) -> Extraction:
    """Offline mock: uses a ground-truth sidecar instead of a model. Disclosed as a mock."""
    return finalize(dict(sidecar), second=None, trader_gstin=trader_gstin)


# ---- targeted re-ask: crop the exact field the trader must check ----------------

def crop_field(image: bytes, box: list | None, out_dir: Path) -> Path | None:
    if not box or len(box) != 4:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image)).convert("RGB")
        w, h = img.size
        ymin, xmin, ymax, xmax = [max(0, min(1000, float(v))) for v in box]
        if ymax <= ymin or xmax <= xmin:
            return None
        pad_x, pad_y = 0.08 * w, 0.04 * h
        crop = img.crop((max(0, xmin / 1000 * w - pad_x), max(0, ymin / 1000 * h - pad_y),
                         min(w, xmax / 1000 * w + pad_x), min(h, ymax / 1000 * h + pad_y)))
        if crop.width < 40 or crop.height < 15:
            return None
        crop = crop.resize((crop.width * 2, crop.height * 2))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"crop-{uuid.uuid4().hex[:12]}.jpg"
        crop.save(path, "JPEG", quality=88)
        return path
    except Exception:
        return None


# ---- §17(5) blocked-credit screen (conservative; ambiguous -> CA) ---------------

BLOCKED_17_5 = [
    ("17(5)(a) motor vehicle", r"\b(car|motor ?car|motor vehicle|scooter|motorcycle|bike|suv|sedan)\b"),
    ("17(5)(b)(i) food & beverages", r"\b(food|restaurant|catering|caterer|meal|tiffin|snacks?|"
                                     r"sweets?|mithai|beverages?|tea|coffee|lunch|dinner|hotel bill)\b"),
    ("17(5)(b)(i) beauty / health", r"\b(salon|spa|beauty|cosmetic|plastic surgery|parlour|parlor)\b"),
    ("17(5)(b)(ii) club / fitness", r"\b(club membership|membership fee|gym|fitness centre|health club)\b"),
    ("17(5)(b)(iii) insurance", r"\b(life insurance|health insurance|mediclaim)\b"),
    ("17(5)(b)(iv) travel benefits", r"\b(holiday package|tour package|leave travel|vacation)\b"),
    ("17(5)(c)/(d) works contract for own building", r"\b(works contract|construction service|"
                                                     r"civil work for own|building construction service)\b"),
    ("17(5)(h) gifts / free samples", r"\b(gifts?|diwali gift|free sample)\b"),
]


def classify_eligibility(description: str | None) -> tuple[str, str]:
    """Returns ('ELIGIBLE'|'BLOCKED', clause). Goods for resale (cement, pipes,
    hardware...) are never matched by these patterns."""
    text = (description or "").lower()
    for clause, pattern in BLOCKED_17_5:
        if re.search(pattern, text):
            return "BLOCKED", clause
    return "ELIGIBLE", ""
