"""Extraction evaluation on synthetic bill photos (needs LLM_API_KEY).

  python -m agent.eval.extraction_eval --generate      # (re)build the image set (already shipped)
  python -m agent.eval.extraction_eval --run [--limit 20] [--price-in 0.30 --price-out 2.50]

Measures, per field and per degradation:
  * field accuracy vs what is PRINTED on the paper
  * the safety metric: dangerous errors (wrong GSTIN or wrong tax) that passed
    every gate un-flagged ("unsafe pass") — the number that must be 0
  * gate burden: correct fields flagged anyway (= extra questions to the trader)
  * smudged fields: did the model guess, and did the gates catch it
  * latency and tokens -> cost per bill (prices are inputs, USD per 1M tokens)

Images are synthetic renders with simulated photo damage, not real bill
photographs; real-photo accuracy will be lower. This is stated with results.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from pathlib import Path

from agent import extract
from agent.config import ROOT, get_settings
from agent.demo.billgen import BillSpec, corrupt_gstin, make_gstin, write_bill
from agent.llm import LLMClient, LLMUnavailable

SET_DIR = ROOT / "agent" / "eval" / "bills_eval"
KINDS = ["clean", "rotated", "blur", "low_light", "noise", "jpeg", "crumpled", "partial_crop",
         "smudge_gstin", "smudge_total"]
ITEMS = [("Cement OPC 53 Grade 50kg", "2523", 33000), ("TMT Bar Fe500 (kg)", "7214", 6150),
         ("CPVC Pipe 1in 3m", "3917", 41000), ("PVC Ball Valve 1in", "8481", 18500),
         ("FR Wire 1.5sqmm 90m", "8544", 165000), ("Wall Putty 40kg", "3214", 72000),
         ("SS Hinges 4in", "8302", 4500), ("Mortise Lock Set", "8301", 68000),
         ("Tile Adhesive 20kg", "3214", 39000), ("GI Wire 12g (kg)", "7217", 9800)]
NAMES = ["Marwar Traders", "Suryanagri Hardware", "Bhawani Steel Co", "Chopasni Pipe House", "Kailash Electricals",
         "Mehrangarh Building Supply", "Paota Sanitary Mart", "Sardarpura Paints", "Umaid Cement Depot",
         "Ratanada Glass & Ply"]


def generate(n_per_kind: int = 4, seed: int = 2026) -> None:
    rng = random.Random(seed)
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"

    def pan():
        return "".join(rng.choice(letters) for _ in range(3)) + rng.choice("CFP") + rng.choice(letters) + \
            f"{rng.randrange(1000, 9999)}" + rng.choice(letters)
    buyer = make_gstin("08", "AAQFR4417L")
    k = 0

    def spec(**kw):
        nonlocal k
        k += 1
        name = rng.choice(NAMES)
        items = [(d, h, rng.randrange(5, 120), r) for d, h, r in rng.sample(ITEMS, rng.randrange(1, 4))]
        num = rng.choice([f"{name[:2].upper()}/{rng.randrange(1, 999):03d}", f"INV-{rng.randrange(1000, 9999)}",
                          f"{name[:3].upper()}/26-27/{rng.randrange(1, 9999):04d}"])
        return BillSpec(name, make_gstin("08", pan()), "Jodhpur, Rajasthan", num,
                        f"2026-{rng.randrange(7, 11):02d}-{rng.randrange(1, 29):02d}", items,
                        gst_rate=rng.choice([18, 18, 18, 12, 28]), interstate=rng.random() < 0.15,
                        buyer_gstin=buyer, style="thermal" if rng.random() < 0.15 else "a4", **kw)
    for kind in KINDS:
        for i in range(n_per_kind):
            write_bill(spec(), SET_DIR / f"{kind}_{i:02d}", kind=kind, seed=seed + k)
    for i in range(3):  # supplier misprinted their own GSTIN on the paper (checksum fails)
        s = spec()
        s.printed_gstin = corrupt_gstin(s.supplier_gstin, rng)
        write_bill(s, SET_DIR / f"misprint_gstin_{i:02d}", kind="clean", seed=seed + k)
    for i in range(3):  # paper itself has an arithmetic error in the tax line
        s = spec()
        s.printed_tax_override = s.totals()["tax"] + rng.choice([-1, 1]) * rng.randrange(500, 5000)
        write_bill(s, SET_DIR / f"arith_error_{i:02d}", kind="clean", seed=seed + k)
    from PIL import Image, ImageDraw
    from agent.demo.billgen import to_jpeg
    for i in range(3):
        img = Image.new("RGB", (900, 1200), tuple(rng.randrange(80, 220) for _ in range(3)))
        d = ImageDraw.Draw(img)
        for _ in range(30):
            x, y = rng.randrange(900), rng.randrange(1200)
            d.rectangle((x, y, x + rng.randrange(20, 200), y + rng.randrange(20, 200)),
                        outline=tuple(rng.randrange(256) for _ in range(3)), width=4)
        (SET_DIR / f"not_a_bill_{i:02d}.jpg").write_bytes(to_jpeg(img))
        (SET_DIR / f"not_a_bill_{i:02d}.json").write_text(json.dumps({"truth": {"is_bill": False},
                                                                      "fixture": {"is_bill": False},
                                                                      "degradation": "not_a_bill"}))
    print(f"generated {len(list(SET_DIR.glob('*.jpg')))} images in {SET_DIR}")


def _num(s):
    return re.sub(r"\s+", "", str(s or "")).upper()


def run(limit: int | None, price_in: float, price_out: float, double_read: bool) -> dict:
    s = get_settings()
    llm = LLMClient(s)
    if not llm.enabled:
        raise SystemExit("Set LLM_API_KEY (and LLM_BASE_URL / LLM_MODEL) in .env first.")
    files = sorted(SET_DIR.glob("*.jpg"))[: limit or None]
    per = []
    for jpg in files:
        meta = json.loads(jpg.with_suffix(".json").read_text("utf-8"))
        truth, kind = meta["truth"], meta.get("degradation")
        t0 = time.time()
        try:
            ex = extract.extract_with_llm(jpg.read_bytes(), "image/jpeg", llm, double_read=double_read)
        except LLMUnavailable as exc:
            per.append({"file": jpg.name, "kind": kind, "error": str(exc)[:200]})
            continue
        dt = time.time() - t0
        tokens_in = sum(int(u.get("prompt_tokens") or 0) for u in ex.usage)
        tokens_out = sum(int(u.get("completion_tokens") or 0) for u in ex.usage)
        rec = {"file": jpg.name, "kind": kind, "seconds": round(dt, 2), "tokens_in": tokens_in,
               "tokens_out": tokens_out, "status": ex.status, "uncertain": ex.uncertain}
        if not truth.get("is_bill", True):
            rec["is_bill_correct"] = ex.status == "REJECTED"
            per.append(rec)
            continue
        rec["is_bill_correct"] = ex.status != "REJECTED"
        f = ex.fields
        printed_tax = meta.get("printed_tax_override")
        exp_tax = printed_tax if printed_tax is not None else extract.money_to_paise(truth["total_gst"])
        checks = {
            "supplier_gstin": f.get("supplier_gstin") == truth["printed_gstin"],
            "invoice_number": _num(f.get("invoice_number")) == _num(truth["invoice_number"]),
            "invoice_date": f.get("invoice_date") == truth["invoice_date"],
            "total_gst": f.get("total_gst_paise") == exp_tax,
            "taxable_value": f.get("taxable_paise") == extract.money_to_paise(truth["taxable_value"]),
        }
        rec["correct"] = checks
        # dangerous field = what we would act on: a GSTIN or tax amount that is wrong vs the paper,
        # or (for a misprinted/arith-error paper) any value that is not flagged for the trader.
        dangerous = []
        for fld in ("supplier_gstin", "total_gst"):
            wrong = not checks[fld]
            smudged = (kind == "smudge_gstin" and fld == "supplier_gstin") or (kind == "smudge_total" and fld == "total_gst")
            paper_bad = (fld == "supplier_gstin" and jpg.name.startswith("misprint")) or \
                        (fld == "total_gst" and jpg.name.startswith("arith"))
            flagged = fld in ex.uncertain
            if (wrong or smudged or paper_bad) and not flagged:
                dangerous.append(fld)
        rec["unsafe_pass"] = dangerous
        rec["false_flags"] = [fld for fld in ex.uncertain if fld in checks and checks[fld]
                              and not (kind or "").startswith("smudge") and not jpg.name.startswith(("misprint", "arith"))]
        per.append(rec)
        print(f"{jpg.name:<26} {ex.status:<11} unsure={ex.uncertain} unsafe={dangerous} {dt:.1f}s")
    bills = [r for r in per if "correct" in r]
    fields = ["supplier_gstin", "invoice_number", "invoice_date", "total_gst", "taxable_value"]
    clean_like = [r for r in bills if not (r["kind"] or "").startswith("smudge")]
    summary = {
        "images": len(per), "bills": len(bills), "errors": sum(1 for r in per if "error" in r),
        "is_bill_accuracy": round(sum(1 for r in per if r.get("is_bill_correct")) / max(1, len(per) - sum(1 for r in per if "error" in r)), 3),
        "field_accuracy_excl_smudged": {f: round(sum(1 for r in clean_like if r["correct"][f]) / max(1, len(clean_like)), 3) for f in fields},
        "unsafe_pass_bills": sum(1 for r in bills if r["unsafe_pass"]),
        "unsafe_pass_detail": [{"file": r["file"], "fields": r["unsafe_pass"]} for r in bills if r["unsafe_pass"]],
        "auto_accepted_bills": sum(1 for r in bills if r["status"] == "VALID"),
        "auto_accepted_fully_correct": sum(1 for r in bills if r["status"] == "VALID" and all(r["correct"].values())),
        "bills_with_a_reask": sum(1 for r in bills if r["uncertain"]),
        "false_flags_total": sum(len(r["false_flags"]) for r in bills),
        "by_degradation": {},
        "avg_seconds_per_bill": round(sum(r["seconds"] for r in bills) / max(1, len(bills)), 2),
        "avg_tokens_in": round(sum(r["tokens_in"] for r in bills) / max(1, len(bills))),
        "avg_tokens_out": round(sum(r["tokens_out"] for r in bills) / max(1, len(bills))),
        "model": s.vision_model, "double_read": double_read,
        "note": "Synthetic rendered bills with simulated photo damage; not real photographs.",
    }
    summary["cost_usd_per_bill"] = round((summary["avg_tokens_in"] * price_in + summary["avg_tokens_out"] * price_out) / 1e6, 6)
    for kind in sorted({r["kind"] for r in bills}):
        rs = [r for r in bills if r["kind"] == kind]
        summary["by_degradation"][kind] = {
            "n": len(rs), "all_fields_correct": sum(1 for r in rs if all(r["correct"].values())),
            "reasked": sum(1 for r in rs if r["uncertain"]), "unsafe_pass": sum(1 for r in rs if r["unsafe_pass"])}
    out = ROOT / "agent_data" / "eval" / "extraction_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "per_image": per}, indent=2))
    print(json.dumps(summary, indent=2))
    print("saved", out)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-double-read", action="store_true")
    ap.add_argument("--price-in", type=float, default=0.30, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=2.50, help="USD per 1M output tokens")
    a = ap.parse_args(argv)
    if a.generate:
        generate()
    if a.run:
        run(a.limit, a.price_in, a.price_out, not a.no_double_read)
    if not (a.generate or a.run):
        ap.print_help()


if __name__ == "__main__":
    main()
