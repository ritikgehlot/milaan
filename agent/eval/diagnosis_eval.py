"""Diagnosis evaluation: MILAAN vs baselines on seeded reconciliation cases.

  python -m agent.eval.diagnosis_eval [--traders 20] [--seed 7]

Each synthetic trader-month has invoices whose TRUE cause is seeded:
present, formatting variance, rounding, QRMP timing, genuine non-filing,
amount mismatch, GSTIN conflict, our own GSTIN misread, ITC-not-available —
plus a HARD held-out case (QRMP supplier with only one period of history)
that MILAAN's rules are known to get wrong, so the error is measured, not hidden.

Systems (same data, same 2B):
  B0  status quo: no reconciliation until year-end -> no chases in-window
  B2  exact-string matcher (GSTIN + invoice number + exact GST), no diagnosis:
      everything unmatched is chased
  MILAAN  the agent's diagnose.run() through the real store

Correct action per cause:
  CHASE      non_filing, amount_mismatch
  ASK_TRADER gstin_conflict, misread_gstin  (never message before the trader confirms)
  NO_ACTION  present, format_variant, rounding, qrmp, qrmp_thin, itc_unavailable

Caveat (printed with results): the generator and the rules were written by the
same team; this measures whether the policy is implemented correctly on the
perturbations we know about. It is not a blind real-world test.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from collections import Counter
from pathlib import Path

CAUSES = [("present", 0.55), ("format_variant", 0.08), ("rounding", 0.04), ("qrmp", 0.06),
          ("qrmp_thin", 0.02), ("non_filing", 0.12), ("amount_mismatch", 0.05), ("gstin_conflict", 0.03),
          ("misread_gstin", 0.03), ("itc_unavailable", 0.02)]
CORRECT = {"non_filing": "CHASE", "amount_mismatch": "CHASE", "gstin_conflict": "ASK_TRADER",
           "misread_gstin": "ASK_TRADER"}
INNOCENT = {"present", "format_variant", "rounding", "qrmp", "qrmp_thin", "itc_unavailable"}
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"


def correct_action(cause: str) -> str:
    return CORRECT.get(cause, "NO_ACTION")


def gen_trader(rng: random.Random, n_invoices: int):
    from agent.demo.billgen import make_gstin, corrupt_gstin

    def pan():
        return "".join(rng.choice(LETTERS) for _ in range(3)) + rng.choice("CFP") + rng.choice(LETTERS) + \
            f"{rng.randrange(1000, 9999)}" + rng.choice(LETTERS)
    suppliers = []
    for _ in range(14):
        suppliers.append({"pan": pan(), "gstin": None, "qrmp": False})
    for s in suppliers:
        s["gstin"] = make_gstin("08", s["pan"])
    invoices, rows, history = [], [], {}
    for s in suppliers:
        history[s["gstin"]] = ["2026-07", "2026-08", "2026-09"]
    weights = [w for _, w in CAUSES]
    names = [c for c, _ in CAUSES]
    for k in range(n_invoices):
        cause = rng.choices(names, weights)[0]
        s = rng.choice(suppliers)
        prefix = "".join(rng.choice(LETTERS) for _ in range(rng.randint(2, 4)))
        serial = rng.randrange(1, 9999)
        number = rng.choice([f"{prefix}/{serial}", f"{prefix}-{serial:04d}", f"{prefix}/26-27/{serial:04d}"])
        taxable = rng.randrange(2000, 200000) * 100
        gst = taxable * 18 // 100
        inv = {"cause": cause, "supplier_gstin": s["gstin"], "invoice_number": number,
               "invoice_date": f"2026-10-{rng.randrange(1, 29):02d}", "total_gst_paise": gst}
        row = {"supplier_gstin": s["gstin"], "invoice_number": number, "total_gst_paise": gst,
               "itc_available": True}
        if cause == "present":
            rows.append(row)
        elif cause == "format_variant":
            row["invoice_number"] = rng.choice([f"{prefix}{serial}", f"{prefix}-{serial}", f"{prefix.lower()} {serial:05d}"])
            rows.append(row)
        elif cause == "rounding":
            row["total_gst_paise"] = gst + rng.choice([-1, 1]) * rng.randrange(1, 100)
            rows.append(row)
        elif cause in ("qrmp", "qrmp_thin"):
            g = make_gstin("08", pan())  # dedicated quarterly supplier
            inv["supplier_gstin"] = row["supplier_gstin"] = g
            history[g] = ["2026-03", "2026-06", "2026-09"] if cause == "qrmp" else ["2026-09"]
        elif cause == "non_filing":
            pass
        elif cause == "amount_mismatch":
            row["total_gst_paise"] = gst - rng.randrange(5, 40) * gst // 100
            rows.append(row)
        elif cause == "gstin_conflict":
            row["supplier_gstin"] = make_gstin(rng.choice(["24", "27", "06"]), s["pan"])
            rows.append(row)
        elif cause == "misread_gstin":
            inv["supplier_gstin"] = corrupt_gstin(s["gstin"], rng)
        elif cause == "itc_unavailable":
            row["itc_available"] = False
            rows.append(row)
        invoices.append(inv)
    # unrelated 2B rows (purchases without a bill in our books) add realistic noise
    for _ in range(rng.randrange(3, 8)):
        s = rng.choice(suppliers)
        rows.append({"supplier_gstin": s["gstin"], "invoice_number": f"ZZ/{rng.randrange(1, 9999)}",
                     "total_gst_paise": rng.randrange(1000, 90000) * 100, "itc_available": True})
    return invoices, rows, history


def b2_actions(invoices, rows):
    keys = {(r["supplier_gstin"], r["invoice_number"], r["total_gst_paise"]) for r in rows}
    return ["NO_ACTION" if (i["supplier_gstin"], i["invoice_number"], i["total_gst_paise"]) in keys else "CHASE"
            for i in invoices]


def milaan_actions(invoices, rows, history, trader="+910000000000"):
    from agent import diagnose, store
    store.reset_initialized()
    for i in invoices:
        i["id"] = store.insert_invoice(trader, supplier_gstin=i["supplier_gstin"], invoice_number=i["invoice_number"],
                                       invoice_date=i["invoice_date"], tax_period=i["invoice_date"][:7],
                                       total_gst_paise=i["total_gst_paise"], state="PENDING_2B")
    for g, periods in history.items():
        for p in periods:
            store.add_history(trader, g, p)
    store.save_2b_import(trader, "eval.json", "x", "portal_json",
                         [{**r, "tax_period": "2026-10", "return_period": "2026-10", "invoice_date": None,
                           "taxable_paise": None, "supplier_name": None} for r in rows], 0)
    diagnose.run(trader)
    out = []
    for i in invoices:
        st = store.get_invoice(i["id"])["state"]
        out.append({"TO_CHASE": "CHASE", "NEEDS_INPUT": "ASK_TRADER"}.get(st, "NO_ACTION"))
    return out


def score(invoices, actions):
    truth = [correct_action(i["cause"]) for i in invoices]
    chases = [k for k, a in enumerate(actions) if a == "CHASE"]
    warranted = [k for k, t in enumerate(truth) if t == "CHASE"]
    tp = sum(1 for k in chases if truth[k] == "CHASE")
    innocent_msgs = sum(1 for k in chases if invoices[k]["cause"] in INNOCENT)
    misdirected = sum(1 for k in chases if invoices[k]["cause"] == "misread_gstin")
    premature = sum(1 for k in chases if invoices[k]["cause"] == "gstin_conflict")
    at_risk_truth = sum(invoices[k]["total_gst_paise"] for k in warranted)
    at_risk_found = sum(invoices[k]["total_gst_paise"] for k in chases if truth[k] == "CHASE")
    per_cause = {}
    for c, _ in CAUSES:
        idx = [k for k, i in enumerate(invoices) if i["cause"] == c]
        if idx:
            per_cause[c] = round(sum(1 for k in idx if actions[k] == truth[k]) / len(idx), 3)
    return {
        "invoices": len(invoices),
        "action_accuracy": round(sum(1 for a, t in zip(actions, truth) if a == t) / len(truth), 4),
        "chases": len(chases),
        "chase_precision": round(tp / len(chases), 4) if chases else None,
        "chase_recall": round(tp / len(warranted), 4) if warranted else None,
        "messages_to_innocent_suppliers": innocent_msgs,
        "misdirected_messages_wrong_gstin": misdirected,
        "premature_chases_unconfirmed_gstin": premature,
        "at_risk_gst_identified_pct": round(100 * at_risk_found / at_risk_truth, 2) if at_risk_truth else None,
        "per_cause_accuracy": per_cause,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--traders", type=int, default=20)
    ap.add_argument("--invoices", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="agent_data/eval/diagnosis_results.json")
    a = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="milaan-eval-"))
    os.environ["AGENT_DB_PATH"] = str(tmp / "eval.db")
    rng = random.Random(a.seed)
    all_inv, acts = [], {"B0": [], "B2": [], "MILAAN": []}
    for t in range(a.traders):
        invoices, rows, history = gen_trader(rng, a.invoices)
        acts["B0"] += ["NO_ACTION"] * len(invoices)
        acts["B2"] += b2_actions(invoices, rows)
        acts["MILAAN"] += milaan_actions(invoices, rows, history, trader=f"+9100000{t:05d}")
        all_inv += invoices
    results = {name: score(all_inv, a_) for name, a_ in acts.items()}
    results["_meta"] = {"traders": a.traders, "invoices_per_trader": a.invoices, "seed": a.seed,
                        "cause_counts": dict(Counter(i["cause"] for i in all_inv)),
                        "caveat": "Synthetic, seeded by the same team that wrote the rules; not a blind test. "
                                  "qrmp_thin is a known hard case outside the rules."}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(results, indent=2))
    cols = ["action_accuracy", "chases", "chase_precision", "chase_recall", "messages_to_innocent_suppliers",
            "misdirected_messages_wrong_gstin", "premature_chases_unconfirmed_gstin", "at_risk_gst_identified_pct"]
    print(f"{'metric':<38}" + "".join(f"{n:>10}" for n in ("B0", "B2", "MILAAN")))
    for c in cols:
        print(f"{c:<38}" + "".join(f"{str(results[n][c]):>10}" for n in ("B0", "B2", "MILAAN")))
    print("\nper-cause action accuracy (MILAAN | B2):")
    for c, v in results["MILAAN"]["per_cause_accuracy"].items():
        print(f"  {c:<18} {v:>6} | {results['B2']['per_cause_accuracy'].get(c)}")
    print("\ncause counts:", results["_meta"]["cause_counts"])
    print("caveat:", results["_meta"]["caveat"])
    print("saved", a.out)


if __name__ == "__main__":
    main()
