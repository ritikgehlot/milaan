"""Synthetic GST bill photo generator (for the demo and the extraction eval).

All suppliers, GSTINs and amounts are synthetic. GSTINs are built with a
valid check digit so they exercise the validator, but they are generated,
not taken from any real business.
"""

from __future__ import annotations

import io
import json
import math
import random
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import validation

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts", "/Library/Fonts", "/System/Library/Fonts"]


def font(size: int, bold: bool = False, mono: bool = False):
    names = (["DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"] if mono else
             ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]) + ["arial.ttf", "Arial.ttf"]
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    first14 = f"{state}{pan}{entity}Z"
    return first14 + validation.gstin_checksum(first14)


def corrupt_gstin(g: str, rng: random.Random) -> str:
    """Flip one character (a typical OCR / misprint error); result fails the checksum."""
    chars = list(g)
    i = rng.choice([3, 5, 7, 8, 9, 10])
    pool = "0123456789" if chars[i].isdigit() else "ABCDEFGHJKLMNPRSTUVWXYZ"
    chars[i] = rng.choice([c for c in pool if c != chars[i]])
    out = "".join(chars)
    return out if not validation.validate_gstin(out).is_format_valid else corrupt_gstin(g, rng)


def money(p: int) -> str:
    return f"{p // 100:,}.{p % 100:02d}"


@dataclass
class BillSpec:
    supplier_name: str
    supplier_gstin: str
    supplier_addr: str
    invoice_number: str
    invoice_date: str  # YYYY-MM-DD
    items: list[tuple[str, str, int, int]]  # (desc, hsn, qty, rate_paise)
    gst_rate: int = 18
    interstate: bool = False
    buyer_name: str = "Ramesh Hardware & Building Materials"
    buyer_gstin: str = ""
    printed_gstin: str | None = None      # misprinted GSTIN on the paper (else = supplier_gstin)
    printed_tax_override: int | None = None  # print a wrong tax figure (arithmetic error on paper)
    style: str = "a4"                     # 'a4' | 'thermal'
    description: str = ""

    def totals(self) -> dict:
        taxable = sum(q * r for _, _, q, r in self.items)
        tax = int((Decimal(taxable) * self.gst_rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if self.interstate:
            cgst = sgst = 0
            igst = tax
        else:
            cgst = tax // 2
            sgst = tax - cgst
            igst = 0
        return {"taxable": taxable, "cgst": cgst, "sgst": sgst, "igst": igst, "tax": tax, "total": taxable + tax}

    def truth(self) -> dict:
        t = self.totals()
        return {
            "is_bill": True, "supplier_name": self.supplier_name, "supplier_gstin": self.supplier_gstin,
            "printed_gstin": self.printed_gstin or self.supplier_gstin,
            "buyer_gstin": self.buyer_gstin, "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date, "taxable_value": f"{t['taxable'] / 100:.2f}",
            "cgst": f"{t['cgst'] / 100:.2f}", "sgst": f"{t['sgst'] / 100:.2f}", "igst": f"{t['igst'] / 100:.2f}",
            "total_gst": f"{t['tax'] / 100:.2f}", "invoice_total": f"{t['total'] / 100:.2f}",
            "gst_rate": self.gst_rate, "description": self.description or ", ".join(i[0] for i in self.items[:2]),
        }


def render(spec: BillSpec) -> tuple[Image.Image, dict]:
    """Render a clean bill. Returns (image, boxes in 0-1000 coords)."""
    W, H = (1240, 1650) if spec.style == "a4" else (620, 1500)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    boxes: dict[str, list[int]] = {}
    t = spec.totals()
    m = 60 if spec.style == "a4" else 30
    s = 1.0 if spec.style == "a4" else 0.62

    def text(xy, txt, size, bold=False, mono=False, key=None, fill="black", anchor="la"):
        f = font(int(size * s), bold, mono)
        d.text(xy, txt, font=f, fill=fill, anchor=anchor)
        if key:
            bb = d.textbbox(xy, txt, font=f, anchor=anchor)
            boxes[key] = [int(bb[1] / H * 1000), int(bb[0] / W * 1000), int(bb[3] / H * 1000), int(bb[2] / W * 1000)]

    y = m
    text((W // 2, y), "TAX INVOICE", 30, bold=True, anchor="ma"); y += int(55 * s)
    text((W // 2, y), spec.supplier_name.upper(), 44, bold=True, anchor="ma"); y += int(65 * s)
    text((W // 2, y), spec.supplier_addr, 22, anchor="ma"); y += int(40 * s)
    text((W // 2, y), f"GSTIN: {spec.printed_gstin or spec.supplier_gstin}", 28, bold=True, mono=True,
         key="supplier_gstin", anchor="ma"); y += int(60 * s)
    d.line((m, y, W - m, y), fill="black", width=3); y += int(25 * s)
    text((m, y), f"Invoice No: {spec.invoice_number}", 26, bold=True, key="invoice_number")
    if spec.style == "a4":
        dd = "-".join(reversed(spec.invoice_date.split("-")))
        text((W - m, y), f"Date: {dd}", 26, key="invoice_date", anchor="ra")
        y += int(50 * s)
    else:
        y += int(45 * s)
        dd = "/".join(reversed(spec.invoice_date.split("-")))
        text((m, y), f"Date: {dd}", 26, key="invoice_date"); y += int(50 * s)
    text((m, y), f"Bill To: {spec.buyer_name}", 22); y += int(35 * s)
    text((m, y), f"Buyer GSTIN: {spec.buyer_gstin}", 22, mono=True); y += int(35 * s)
    text((m, y), "Place of Supply: " + ("Gujarat (24)" if spec.interstate else "Rajasthan (08)"), 22); y += int(50 * s)
    d.line((m, y, W - m, y), fill="black", width=2); y += int(15 * s)
    cols = [m, int(W * 0.50), int(W * 0.62), int(W * 0.72), int(W * 0.84)] if spec.style == "a4" else \
           [m, int(W * 0.48), int(W * 0.60), int(W * 0.70), int(W * 0.82)]
    for x, h in zip(cols, ["Description", "HSN", "Qty", "Rate", "Amount"]):
        text((x, y), h, 22, bold=True)
    y += int(40 * s)
    d.line((m, y, W - m, y), fill="gray", width=1); y += int(12 * s)
    for desc, hsn, q, r in spec.items:
        text((cols[0], y), desc[:30 if spec.style == "a4" else 20], 22)
        text((cols[1], y), hsn, 22)
        text((cols[2], y), str(q), 22)
        text((cols[3], y), money(r), 22)
        text((cols[4], y), money(q * r), 22)
        y += int(42 * s)
    y += int(10 * s)
    d.line((m, y, W - m, y), fill="black", width=2); y += int(20 * s)
    tax_printed = spec.printed_tax_override if spec.printed_tax_override is not None else t["tax"]
    rows = [("Taxable Value", t["taxable"], None)]
    if spec.interstate:
        rows.append((f"IGST @ {spec.gst_rate}%", tax_printed, None))
    else:
        rows.append((f"CGST @ {spec.gst_rate / 2:g}%", tax_printed // 2, None))
        rows.append((f"SGST @ {spec.gst_rate / 2:g}%", tax_printed - tax_printed // 2, None))
    rows.append(("Total GST", tax_printed, "total_gst"))
    rows.append(("GRAND TOTAL", t["taxable"] + tax_printed, "invoice_total"))
    for label, val, key in rows:
        text((int(W * 0.50), y), label, 24, bold=label in ("GRAND TOTAL", "Total GST"))
        text((W - m, y), f"₹ {money(val)}" if font(10).getbbox("₹") else money(val), 24,
             bold=label in ("GRAND TOTAL", "Total GST"), key=key, anchor="ra")
        y += int(45 * s)
    y += int(40 * s)
    text((m, y), "Certified that the particulars given above are true and correct.", 18); y += int(60 * s)
    text((W - m, y), f"For {spec.supplier_name}", 22, anchor="ra"); y += int(80 * s)
    text((W - m, y), "Authorised Signatory", 20, anchor="ra")
    return img, boxes


# ---- photo degradations ----------------------------------------------------------

def degrade(img: Image.Image, boxes: dict, kind: str, rng: random.Random) -> Image.Image:
    W, H = img.size
    if kind in ("smudge_gstin", "smudge_total"):
        key = "supplier_gstin" if kind == "smudge_gstin" else "total_gst"
        b = boxes.get(key)
        if b:
            d = ImageDraw.Draw(img, "RGBA")
            y0, x0, y1, x1 = b[0] / 1000 * H, b[1] / 1000 * W, b[2] / 1000 * H, b[3] / 1000 * W
            cx = x0 + (x1 - x0) * rng.uniform(0.45, 0.7)
            for _ in range(10):
                r = rng.uniform(10, 22)
                ox, oy = rng.uniform(-30, 30), rng.uniform(-4, 4)
                d.ellipse((cx + ox - r, (y0 + y1) / 2 + oy - r, cx + ox + r, (y0 + y1) / 2 + oy + r),
                          fill=(120, 90, 60, 150))
    if kind in ("blur", "photo_blur"):
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(1.6, 2.4)))
    if kind == "low_light":
        img = Image.eval(img, lambda v: int(v * 0.55 + 15))
    if kind == "noise":
        px = img.load()
        for _ in range(W * H // 25):
            x, y = rng.randrange(W), rng.randrange(H)
            g = rng.randrange(0, 255)
            px[x, y] = (g, g, g)
    if kind == "jpeg":
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=12)
        img = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    if kind == "crumpled":
        # sinusoidal displacement + uneven shading, like a folded thermal bill
        src = img.copy()
        out = Image.new("RGB", (W, H), "white")
        sp, op = src.load(), out.load()
        amp, freq = rng.uniform(4, 8), rng.uniform(0.006, 0.012)
        for y in range(H):
            dx = int(amp * math.sin(y * freq))
            shade = 0.82 + 0.18 * (0.5 + 0.5 * math.sin(y * 0.004 + 1))
            for x in range(W):
                sx = min(W - 1, max(0, x + dx))
                r, g, b = sp[sx, y]
                op[x, y] = (int(r * shade), int(g * shade), int(b * shade))
        img = out
    # every "photo" gets placed on a table with a slight rotation and perspective
    angle = {"rotated": rng.uniform(5, 9) * rng.choice([-1, 1])}.get(kind, rng.uniform(-1.8, 1.8))
    rot = img.rotate(angle, expand=True, fillcolor=(70, 60, 50), resample=Image.BICUBIC)
    bg = Image.new("RGB", (rot.width + 120, rot.height + 120), (70, 60, 50))
    bg.paste(rot, (60, 60))
    top = 0
    if kind == "partial_crop":
        top = int(bg.height * 0.06)
        bg = bg.crop((0, top, int(bg.width * 0.93), bg.height))
    final = bg.resize((bg.width * 3 // 4, bg.height * 3 // 4))
    # transform boxes into the final photo's coordinates
    a = math.radians(angle)
    cx, cy = W / 2, H / 2
    new_boxes = {}
    for k, (y0, x0, y1, x1) in boxes.items():
        pts = []
        for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            X, Y = px / 1000 * W - cx, py / 1000 * H - cy
            rx = X * math.cos(a) + Y * math.sin(a) + rot.width / 2 + 60
            ry = -X * math.sin(a) + Y * math.cos(a) + rot.height / 2 + 60 - top
            pts.append((rx * 0.75 / final.width * 1000, ry * 0.75 / final.height * 1000))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        new_boxes[k] = [int(min(ys)), int(min(xs)), int(max(ys)), int(max(xs))]
    boxes.clear()
    boxes.update(new_boxes)
    return final


def to_jpeg(img: Image.Image, quality: int = 82) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def write_bill(spec: BillSpec, out: Path, kind: str = "clean", seed: int = 0,
               fixture_overrides: dict | None = None) -> Path:
    """Write <out>.jpg plus <out>.json (ground truth + fixture-mode mock reading)."""
    rng = random.Random(seed)
    img, boxes = render(spec)
    photo = degrade(img, boxes, kind, rng)
    out.parent.mkdir(parents=True, exist_ok=True)
    jpg = out.with_suffix(".jpg")
    jpg.write_bytes(to_jpeg(photo))
    truth = spec.truth()
    # The fixture is what the offline mock "reads": it equals the printed paper, with
    # deliberate uncertainty where the photo is damaged. It is NOT used in the LLM eval.
    fixture = {k: v for k, v in truth.items() if k not in ("printed_gstin",)}
    fixture["supplier_gstin"] = truth["printed_gstin"]
    if spec.printed_tax_override is not None:
        p = spec.printed_tax_override
        fixture["total_gst"] = f"{p / 100:.2f}"
        fixture["cgst"] = f"{(p // 2) / 100:.2f}" if not spec.interstate else "0.00"
        fixture["sgst"] = f"{(p - p // 2) / 100:.2f}" if not spec.interstate else "0.00"
        fixture["igst"] = f"{p / 100:.2f}" if spec.interstate else "0.00"
        fixture["invoice_total"] = f"{(spec.totals()['taxable'] + p) / 100:.2f}"
    fixture["confidence"] = {k: 0.97 for k in ("supplier_gstin", "invoice_number", "invoice_date",
                                               "total_gst", "taxable_value", "invoice_total")}
    fixture["boxes"] = boxes
    if fixture_overrides:
        fixture.update(fixture_overrides)
    out.with_suffix(".json").write_text(json.dumps({"truth": truth, "fixture": fixture, "degradation": kind,
                                                    "printed_tax_override": spec.printed_tax_override},
                                                   indent=2, ensure_ascii=False), encoding="utf-8")
    return jpg
