"""The judge demo: one trader (Ramesh, Jodhpur), one month (October 2026), seven suppliers.

Every cause in the diagnosis layer appears exactly once:
  Marudhar Cement     -> MATCHED (2 bills)
  Shree Balaji Pipes  -> FORMAT_VARIANT (bill 'SBP/26-27/0412' vs 2B 'SBP-412') -> no chase
  Thar Steel          -> QRMP_TIMING (quarterly filer)                         -> deliberately no chase
  Mahadev Hardware    -> NON_FILING (2 bills)  -> chase -> appears in Nov 2B -> RECOVERED
  Rathore Electricals -> AMOUNT_MISMATCH (₹4,500 on bill vs ₹4,050 in 2B) -> chase -> amended -> RECOVERED
  Jodhpur Paints      -> GSTIN_CONFLICT (2B has it under their Gujarat GSTIN) -> trader decides
  Shri Ram Bhojnalaya -> INELIGIBLE §17(5) food -> never chased

All names, GSTINs and amounts are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from .billgen import BillSpec, make_gstin, write_bill

TRADER_GSTIN = make_gstin("08", "AAQFR4417L")
TRADER_NAME = "Ramesh Hardware & Building Materials"

S = {
    "marudhar": ("Marudhar Cement Agency", make_gstin("08", "AAKFM2231Q"), "Pal Road, Jodhpur"),
    "balaji": ("Shree Balaji Pipes & Fittings", make_gstin("08", "AAMFS7710D"), "Basni Industrial Area, Jodhpur"),
    "thar": ("Thar Steel Traders", make_gstin("08", "AAJFT5520H"), "Mandore Road, Jodhpur"),
    "mahadev": ("Mahadev Hardware Mart", make_gstin("08", "ABBFM3398N"), "Sojati Gate, Jodhpur"),
    "rathore": ("Rathore Electricals", make_gstin("08", "AAHFR6643B"), "Ratanada, Jodhpur"),
    "jps": ("Jodhpur Paints & Sanitary", make_gstin("08", "AACFJ4410P"), "Shastri Nagar, Jodhpur"),
    "bhojnalaya": ("Shri Ram Bhojnalaya", make_gstin("08", "ABCPR9021E"), "Station Road, Jodhpur"),
}
JPS_GUJARAT = make_gstin("24", "AACFJ4410P")


def _b(key, number, dt, items, rate=18, **kw) -> BillSpec:
    name, gstin, addr = S[key]
    return BillSpec(name, gstin, addr, number, dt, items, gst_rate=rate, buyer_name=TRADER_NAME,
                    buyer_gstin=TRADER_GSTIN, **kw)


BILLS = {
    # seeded (sent "earlier in the month")
    "01_marudhar_1187": (_b("marudhar", "MCA/1187", "2026-10-03",
                            [("Cement OPC 53 Grade 50kg", "2523", 120, 33000)]), "clean"),
    "02_balaji_0412": (_b("balaji", "SBP/26-27/0412", "2026-10-08",
                          [("CPVC Pipe 1 inch 3m", "3917", 60, 41000), ("CPVC Elbow 1 inch", "3917", 200, 2800)]), "rotated"),
    "03_thar_088": (_b("thar", "TST/088", "2026-10-11", [("TMT Bar Fe500 12mm (kg)", "7214", 900, 6150)]), "low_light"),
    "04_mahadev_551": (_b("mahadev", "MHM/2026/551", "2026-10-05",
                          [("SS Door Hinges 4 inch", "8302", 300, 4500), ("Mortise Lock Set", "8301", 40, 68000)]), "crumpled"),
    "05_mahadev_583": (_b("mahadev", "MHM/2026/583", "2026-10-21",
                          [("Brass Door Handle", "8302", 120, 21000)]), "clean"),
    "06_jps_0977": (_b("jps", "JPS/0977", "2026-10-19",
                       [("Wall Putty 40kg", "3214", 50, 72000), ("CP Bath Fittings Set", "8481", 10, 245000)]), "clean"),
    "07_bhojnalaya_lunch": (_b("bhojnalaya", "SRB-2291", "2026-10-15",
                               [("Staff lunch thali", "9963", 25, 12000)], rate=5,
                               description="Staff lunch thali (food)"), "clean"),
    # live during the pitch
    "L1_marudhar_1214": (_b("marudhar", "MCA/1214", "2026-10-17",
                            [("Cement OPC 53 Grade 50kg", "2523", 80, 33000), ("Cement PPC 50kg", "2523", 40, 31000)]), "clean"),
    "L2_rathore_3341": (_b("rathore", "RE-3341", "2026-10-14",
                           [("FR Wire 1.5 sqmm 90m", "8544", 10, 165000), ("Modular Switch 6A", "8536", 500, 1700)]),
                        "smudge_gstin"),
}
SEEDED = [k for k in BILLS if not k.startswith("L")]
LIVE = [k for k in BILLS if k.startswith("L")]


def _row(spec: BillSpec, number: str | None = None, gstin: str | None = None, tax_override: int | None = None):
    t = spec.totals()
    tax = t["tax"] if tax_override is None else tax_override
    taxable = t["taxable"] if tax_override is None else round(tax * 100 / spec.gst_rate)
    d = "-".join(reversed(spec.invoice_date.split("-")))
    inv = {"inum": number or spec.invoice_number, "typ": "R", "dt": d, "val": (taxable + tax) / 100,
           "pos": "08", "rev": "N", "itcavl": "Y", "rsn": "", "diffprcnt": 1, "srctyp": "", "irn": "",
           "irngendate": "", "txval": taxable / 100, "igst": 0.0, "cgst": (tax // 2) / 100,
           "sgst": (tax - tax // 2) / 100, "cess": 0.0}
    return gstin or spec.supplier_gstin, spec.supplier_name, inv


def portal_2b(rtnprd: str, rows: list[tuple], amend_rows: list[tuple] | None = None) -> dict:
    """GSTR-2B JSON in the portal's docdata.b2b[] layout (synthetic)."""
    def group(rs):
        by: dict[str, dict] = {}
        for ctin, name, inv in rs:
            by.setdefault(ctin, {"ctin": ctin, "trdnm": name, "supfildt": "", "supprd": rtnprd, "inv": []})["inv"].append(inv)
        return list(by.values())
    doc = {"chksum": "synthetic", "data": {"gstin": TRADER_GSTIN, "rtnprd": rtnprd, "version": "1.0",
                                           "gendt": "14-" + rtnprd[:2] + "-" + rtnprd[2:],
                                           "docdata": {"b2b": group(rows)}}}
    if amend_rows:
        doc["data"]["docdata"]["b2ba"] = group(amend_rows)
    return doc


def build(out: Path) -> dict:
    out = Path(out)
    bills_dir = out / "bills"
    paths = {}
    for i, (key, (spec, kind)) in enumerate(BILLS.items()):
        overrides = None
        if kind == "smudge_gstin":
            # what an honest reader reports for a smudged GSTIN: unreadable, low confidence
            overrides = {"supplier_gstin": spec.supplier_gstin[:8] + "???" + spec.supplier_gstin[11:],
                         "confidence": {"supplier_gstin": 0.31, "invoice_number": 0.95, "invoice_date": 0.95,
                                        "total_gst": 0.93, "taxable_value": 0.93, "invoice_total": 0.93}}
        paths[key] = str(write_bill(spec, bills_dir / key, kind=kind, seed=100 + i, fixture_overrides=overrides))
    # a photo that is not a bill
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (900, 700), (180, 200, 170))
    d = ImageDraw.Draw(img)
    for r in range(0, 300, 25):
        d.ellipse((450 - r, 350 - r, 450 + r, 350 + r), outline=(90, 120, 80), width=3)
    (bills_dir / "X_not_a_bill.jpg").write_bytes(__import__("agent.demo.billgen", fromlist=["to_jpeg"]).to_jpeg(img))
    (bills_dir / "X_not_a_bill.json").write_text(json.dumps({"truth": {"is_bill": False},
                                                             "fixture": {"is_bill": False}}), encoding="utf-8")

    sp = {k: v[0] for k, v in BILLS.items()}
    oct_rows = [
        _row(sp["01_marudhar_1187"]), _row(sp["L1_marudhar_1214"]),
        _row(sp["02_balaji_0412"], number="SBP-412"),
        _row(sp["L2_rathore_3341"], tax_override=405000),
        _row(sp["06_jps_0977"], gstin=JPS_GUJARAT),
        _row(sp["07_bhojnalaya_lunch"]),
    ]
    nov_rows = [_row(sp["04_mahadev_551"]), _row(sp["05_mahadev_583"])]
    nov_amend = [_row(sp["L2_rathore_3341"])]
    (out / "GSTR2B_OCT2026.json").write_text(json.dumps(portal_2b("102026", oct_rows), indent=1), encoding="utf-8")
    (out / "GSTR2B_NOV2026.json").write_text(json.dumps(portal_2b("112026", nov_rows, nov_amend), indent=1),
                                             encoding="utf-8")
    history = {S[k][1]: (["2026-03", "2026-06", "2026-09"] if k == "thar" else ["2026-07", "2026-08", "2026-09"])
               for k in ("marudhar", "balaji", "thar", "mahadev", "rathore", "jps", "bhojnalaya")}
    (out / "history.json").write_text(json.dumps(history, indent=1), encoding="utf-8")
    return {"bills": paths, "history": history}
