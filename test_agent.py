"""Tests for the WhatsApp agent layer (agent/). Offline: no network, no keys."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="milaan-agent-test-"))
os.environ["AGENT_DB_PATH"] = str(TMP / "agent.db")
os.environ["AGENT_MEDIA_DIR"] = str(TMP / "media")
os.environ["LLM_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["TWILIO_ACCOUNT_SID"] = ""

from agent import diagnose, extract, store, twob  # noqa: E402
from agent.channel import ConsoleChannel, valid_twilio_signature, split_body  # noqa: E402
from agent.config import get_settings  # noqa: E402
from agent.core import Agent  # noqa: E402
from agent.demo import scenario  # noqa: E402
from agent.demo.fixtures import FixtureExtractor  # noqa: E402
from agent.demo.seed import DEMO_DIR, main as seed_main  # noqa: E402
from agent.llm import LLMClient, LLMReply, LLMUnavailable  # noqa: E402
from agent import planner  # noqa: E402

TRADER, SUPPLIER = "+919000000001", "+919000000002"


def fresh_agent(channel=None, llm=None):
    for suffix in ("", "-wal", "-shm"):
        p = Path(os.environ["AGENT_DB_PATH"] + suffix)
        if p.exists():
            p.unlink()
    store.reset_initialized()
    seed_main(["--trader", TRADER, "--supplier", SUPPLIER, "--quiet"])
    s = get_settings()
    s.trader_phones = (TRADER,)
    ch = channel or ConsoleChannel(echo=False)
    return Agent(channel=ch, settings=s, llm=llm, fixture_extractor=FixtureExtractor(DEMO_DIR)), ch


def photo(agent, name):
    agent.handle(TRADER, "", [(str(DEMO_DIR / "bills" / name), "image/jpeg")])


def file(agent, name):
    agent.handle(TRADER, "", [(str(DEMO_DIR / name), "application/json")], [name])


def to(ch, phone):
    return [m for m in ch.sent if m["to"] == phone]


class ExtractionSafetyTests(unittest.TestCase):
    def base(self, **over):
        raw = {"is_bill": True, "supplier_name": "X", "supplier_gstin": scenario.S["mahadev"][1],
               "invoice_number": "A/1", "invoice_date": "2026-10-05", "taxable_value": "1000.00",
               "cgst": "90.00", "sgst": "90.00", "igst": "0.00", "total_gst": "180.00",
               "invoice_total": "1180.00", "gst_rate": 18,
               "confidence": {k: 0.95 for k in extract.CRITICAL_FIELDS}}
        raw.update(over)
        return raw

    def test_clean_bill_valid(self):
        self.assertEqual(extract.finalize(self.base()).status, "VALID")

    def test_unreadable_gstin_is_asked_never_guessed(self):
        ex = extract.finalize(self.base(supplier_gstin="08ABBFM33?8N1Z1"))
        self.assertEqual(ex.status, "NEEDS_INPUT")
        self.assertEqual(ex.ask_field, "supplier_gstin")

    def test_checksum_failure_blocks(self):
        g = scenario.S["mahadev"][1]
        bad = g[:5] + ("X" if g[5] != "X" else "Y") + g[6:]
        ex = extract.finalize(self.base(supplier_gstin=bad))
        self.assertIn("supplier_gstin", ex.uncertain)

    def test_arithmetic_check_catches_ocr_digit_error(self):
        ex = extract.finalize(self.base(total_gst="160.00", cgst="80.00", sgst="80.00", invoice_total="1160.00"))
        self.assertIn("total_gst", ex.uncertain)

    def test_double_read_disagreement_is_uncertain(self):
        second = {"supplier_gstin": scenario.S["mahadev"][1], "invoice_number": "A/7", "total_gst": "180.00"}
        ex = extract.finalize(self.base(), second=second)
        self.assertEqual(ex.uncertain, ["invoice_number"])

    def test_low_confidence_is_uncertain(self):
        raw = self.base()
        raw["confidence"]["total_gst"] = 0.4
        self.assertIn("total_gst", extract.finalize(raw).uncertain)

    def test_not_a_bill(self):
        self.assertEqual(extract.finalize({"is_bill": False}).status, "REJECTED")

    def test_blocked_credit_food_but_not_cement(self):
        self.assertEqual(extract.classify_eligibility("Staff lunch thali")[0], "BLOCKED")
        self.assertEqual(extract.classify_eligibility("Cement OPC 53 Grade")[0], "ELIGIBLE")
        self.assertEqual(extract.classify_eligibility("CPVC pipe, elbow")[0], "ELIGIBLE")


class DiagnosisTests(unittest.TestCase):
    def inv(self, **kw):
        d = {"id": 1, "supplier_gstin": scenario.S["mahadev"][1], "invoice_number": "MHM/2026/551",
             "tax_period": "2026-10", "total_gst_paise": 732600, "state": "PENDING_2B", "chase_count": 0}
        d.update(kw)
        return d

    def row(self, **kw):
        r = {"id": 9, "supplier_gstin": scenario.S["mahadev"][1], "invoice_number": "MHM/2026/551",
             "total_gst_paise": 732600, "itc_available": 1, "return_period": "2026-10"}
        r.update(kw)
        return r

    def decide(self, inv, rows, history=("2026-08", "2026-09"), hint=None, known=None):
        return diagnose.diagnose_invoice(inv, rows, history=list(history), filing_hint=hint,
                                         known_gstins=known if known is not None else {inv["supplier_gstin"]},
                                         latest_return_period="2026-10")

    def test_exact_match(self):
        self.assertEqual(self.decide(self.inv(), [self.row()]).cause, "MATCHED")

    def test_format_variant_not_chased(self):
        d = self.decide(self.inv(invoice_number="SBP/26-27/0412"), [self.row(invoice_number="SBP-412")])
        self.assertEqual((d.cause, d.new_state), ("FORMAT_VARIANT", "MATCHED"))

    def test_format_variant_requires_same_amount(self):
        d = self.decide(self.inv(invoice_number="SBP/26-27/0412"),
                        [self.row(invoice_number="SBP-412", total_gst_paise=100)])
        self.assertEqual(d.cause, "NON_FILING")

    def test_different_prefix_not_merged(self):
        self.assertFalse(diagnose.numbers_equivalent("ABC-412", "XYZ-412"))

    def test_rounding_within_one_rupee(self):
        self.assertEqual(self.decide(self.inv(), [self.row(total_gst_paise=732550)]).cause, "ROUNDING")

    def test_amount_mismatch_chased(self):
        d = self.decide(self.inv(), [self.row(total_gst_paise=700000)])
        self.assertEqual((d.cause, d.new_state), ("AMOUNT_MISMATCH", "TO_CHASE"))

    def test_itc_not_available(self):
        self.assertEqual(self.decide(self.inv(), [self.row(itc_available=0)]).new_state, "INELIGIBLE")

    def test_qrmp_suppressed(self):
        d = self.decide(self.inv(), [], history=("2026-03", "2026-06", "2026-09"))
        self.assertEqual((d.cause, d.new_state), ("QRMP_TIMING", "QRMP_WAIT"))

    def test_qrmp_hint_overrides(self):
        self.assertEqual(self.decide(self.inv(), [], hint="qrmp").cause, "QRMP_TIMING")

    def test_unknown_gstin_never_contacted(self):
        d = self.decide(self.inv(), [], known=set())
        self.assertEqual((d.cause, d.new_state), ("UNKNOWN_GSTIN", "NEEDS_INPUT"))

    def test_gstin_conflict(self):
        other = scenario.JPS_GUJARAT
        d = self.decide(self.inv(supplier_gstin=scenario.S["jps"][1], invoice_number="JPS/0977"),
                        [self.row(supplier_gstin=other, invoice_number="JPS/0977")])
        self.assertEqual(d.cause, "GSTIN_CONFLICT")

    def test_genuine_non_filing(self):
        self.assertEqual(self.decide(self.inv(), []).cause, "NON_FILING")

    def test_recovered_after_chase(self):
        d = self.decide(self.inv(state="CHASED", chase_count=1), [self.row(return_period="2026-11")])
        self.assertEqual(d.new_state, "RECOVERED")

    def test_no_2b_yet(self):
        inv = self.inv(tax_period="2026-11")
        self.assertIsNone(self.decide(inv, []))

    def test_itc_deadline(self):
        self.assertEqual(diagnose.itc_deadline("2026-10"), "2027-11-30")
        self.assertEqual(diagnose.itc_deadline("2027-02"), "2027-11-30")


class PortalJsonTests(unittest.TestCase):
    def test_parse_b2b_and_b2ba(self):
        scenario.build(DEMO_DIR)
        p = twob.parse_any((DEMO_DIR / "GSTR2B_NOV2026.json").read_bytes(), "x.json")
        self.assertEqual(p.source_format, "portal_json")
        self.assertEqual(p.return_periods, ["2026-11"])
        self.assertEqual(len(p.rows), 3)
        self.assertTrue(all(r["total_gst_paise"] > 0 for r in p.rows))

    def test_itcavl_n(self):
        doc = {"data": {"rtnprd": "102026", "docdata": {"b2b": [{"ctin": scenario.S["thar"][1], "inv": [
            {"inum": "1", "dt": "01-10-2026", "txval": 100, "cgst": 9, "sgst": 9, "igst": 0, "itcavl": "N"}]}]}}}
        p = twob.parse_any(json.dumps(doc).encode(), "a.json")
        self.assertFalse(p.rows[0]["itc_available"])


class AgentFlowTests(unittest.TestCase):
    def test_full_demo_cycle(self):
        agent, ch = fresh_agent()
        photo(agent, "L1_marudhar_1214.jpg")
        photo(agent, "L2_rathore_3341.jpg")
        agent.handle(TRADER, scenario.S["rathore"][1])
        file(agent, "GSTR2B_OCT2026.json")
        jps = [i for i in store.get_invoices(TRADER) if i["invoice_number"] == "JPS/0977"][0]
        self.assertEqual(jps["state"], "NEEDS_INPUT")
        agent.handle(TRADER, f"2b {jps['id']}")
        self.assertEqual(to(ch, SUPPLIER), [], "nothing may reach a supplier before consent")
        agent.handle(TRADER, "haan")
        self.assertEqual(len(to(ch, SUPPLIER)), 2)
        agent.handle(SUPPLIER, "agle hafte kar dunga")
        file(agent, "GSTR2B_NOV2026.json")
        invs = store.get_invoices(TRADER)
        states = {i["invoice_number"]: i["state"] for i in invs}
        self.assertEqual(states["MHM/2026/551"], "RECOVERED")
        self.assertEqual(states["MHM/2026/583"], "RECOVERED")
        self.assertEqual(states["RE-3341"], "RECOVERED")
        self.assertEqual(states["SBP/26-27/0412"], "MATCHED")
        self.assertEqual(states["TST/088"], "QRMP_WAIT")
        self.assertEqual(states["SRB-2291"], "INELIGIBLE")
        recovered = sum(i["total_gst_paise"] for i in invs if i["state"] == "RECOVERED")
        self.assertEqual(recovered, 1636200)

    def test_ok_is_not_consent(self):
        agent, ch = fresh_agent()
        file(agent, "GSTR2B_OCT2026.json")
        agent.handle(TRADER, "ok")
        agent.handle(TRADER, "theek hai dekhte hain")
        self.assertEqual(to(ch, SUPPLIER), [])

    def test_nahi_cancels(self):
        agent, ch = fresh_agent()
        file(agent, "GSTR2B_OCT2026.json")
        agent.handle(TRADER, "nahi")
        agent.handle(TRADER, "haan")
        self.assertEqual(to(ch, SUPPLIER), [])

    def test_policy_recheck_at_send_time(self):
        agent, ch = fresh_agent()
        file(agent, "GSTR2B_OCT2026.json")
        mh = [i for i in store.get_invoices(TRADER) if i["invoice_number"].startswith("MHM")]
        for i in mh:  # state changes between draft and approval
            store.update_invoice(i["id"], state="SKIPPED")
        agent.handle(TRADER, "haan")
        bodies = [m["body"] for m in to(ch, SUPPLIER)]
        self.assertFalse(any("MHM/2026/551" in b for b in bodies))

    def test_skip_supplier(self):
        agent, ch = fresh_agent()
        file(agent, "GSTR2B_OCT2026.json")
        agent.handle(TRADER, "skip 1")
        agent.handle(TRADER, "haan")
        self.assertEqual(to(ch, SUPPLIER), [])
        states = {i["invoice_number"]: i["state"] for i in store.get_invoices(TRADER)}
        self.assertEqual(states["MHM/2026/551"], "SKIPPED")

    def test_send_failure_gives_call_script(self):
        agent, ch = fresh_agent(channel=ConsoleChannel(echo=False, fail_for={SUPPLIER}))
        file(agent, "GSTR2B_OCT2026.json")
        agent.handle(TRADER, "haan")
        states = {i["invoice_number"]: i["state"] for i in store.get_invoices(TRADER)}
        self.assertEqual(states["MHM/2026/551"], "CHANNEL_BLOCKED")
        self.assertTrue(any("📞" in m["body"] for m in to(ch, TRADER)))

    def test_duplicate_photo(self):
        agent, ch = fresh_agent()
        photo(agent, "L1_marudhar_1214.jpg")
        n = len(store.get_invoices(TRADER))
        photo(agent, "L1_marudhar_1214.jpg")
        self.assertEqual(len(store.get_invoices(TRADER)), n)
        self.assertIn("♻️", to(ch, TRADER)[-1]["body"])

    def test_unknown_sender_ignored(self):
        agent, ch = fresh_agent()
        before = len(ch.sent)
        agent.handle("+919999999999", "hello")
        self.assertEqual(len(ch.sent), before)

    def test_escalation_after_max_chases(self):
        agent, ch = fresh_agent()
        file(agent, "GSTR2B_OCT2026.json")
        for _ in range(3):
            agent.handle(TRADER, "haan")
            agent.handle(TRADER, "#advance 8")
        agent.handle(TRADER, "#advance 8")
        states = {i["invoice_number"]: i["state"] for i in store.get_invoices(TRADER)}
        self.assertEqual(states["MHM/2026/551"], "ESCALATED")
        self.assertTrue(any("🚨" in m["body"] for m in to(ch, TRADER)))

    def test_llm_outage_queues_retry(self):
        class DownLLM(LLMClient):
            enabled = True
            def chat(self, *a, **k):
                raise LLMUnavailable("down")
        agent, ch = fresh_agent(llm=DownLLM(get_settings()))
        agent.fixture_extractor = None
        photo(agent, "L1_marudhar_1214.jpg")
        self.assertIn("⏳", to(ch, TRADER)[-1]["body"])
        agent.fixture_extractor = FixtureExtractor(DEMO_DIR)  # service is back
        agent.tick(TRADER)
        self.assertTrue(any(i["invoice_number"] == "MCA/1214" for i in store.get_invoices(TRADER)))


class FakeLLM(LLMClient):
    """Scripted OpenAI-compatible responses."""
    def __init__(self, replies):
        super().__init__(get_settings())
        self.replies = list(replies)
        self.calls = []

    @property
    def enabled(self):
        return True

    def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return self.replies.pop(0)


class PlannerTests(unittest.TestCase):
    def test_no_send_tool_exposed(self):
        names = {t["function"]["name"] for t in planner.TOOLS}
        self.assertFalse(any("send" in n for n in names))

    def test_tool_loop(self):
        llm = FakeLLM([
            LLMReply("", [{"id": "c1", "type": "function", "function": {"name": "get_status", "arguments": "{}"}}]),
            LLMReply("आपके 7 बिल दर्ज हैं [📄बिल से]"),
        ])
        agent, ch = fresh_agent(llm=llm)
        agent.handle(TRADER, "bhai mera kitna GST fasa hua hai abhi")
        self.assertEqual(to(ch, TRADER)[-1]["body"], "आपके 7 बिल दर्ज हैं [📄बिल से]")
        tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("PENDING_2B", tool_msg["content"])

    def test_planner_sets_phone(self):
        llm = FakeLLM([
            LLMReply("", [{"id": "c1", "type": "function", "function": {
                "name": "set_supplier_phone", "arguments": json.dumps({"supplier": "Balaji", "phone": "9876543210"})}}]),
            LLMReply("सेव कर दिया"),
        ])
        agent, ch = fresh_agent(llm=llm)
        agent.handle(TRADER, "balaji pipes wale ka number 98765 43210 hai")
        self.assertEqual(store.get_supplier(TRADER, scenario.S["balaji"][1])["phone"], "+919876543210")

    def test_planner_failure_falls_back(self):
        class Down(FakeLLM):
            def chat(self, *a, **k):
                raise LLMUnavailable("x")
        agent, ch = fresh_agent(llm=Down([]))
        agent.handle(TRADER, "kitna paisa atka hai")
        self.assertIn("📒", to(ch, TRADER)[-1]["body"])

    def test_supplier_intent_keywords(self):
        self.assertEqual(planner.keyword_supplier_intent("kal kar dunga", "2026-10-22"), ("PROMISED", "2026-10-23"))
        self.assertEqual(planner.keyword_supplier_intent("ho gaya bhai", "2026-10-22")[0], "ALREADY_FILED")
        self.assertEqual(planner.keyword_supplier_intent("wrong number", "2026-10-22")[0], "WRONG_PERSON")


class ChannelTests(unittest.TestCase):
    def test_twilio_signature(self):
        import base64, hashlib, hmac
        url, params = "https://x.ngrok.app/whatsapp", {"From": "whatsapp:+91900", "Body": "hi"}
        payload = url + "".join(k + params[k] for k in sorted(params))
        sig = base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()
        self.assertTrue(valid_twilio_signature("tok", url, params, sig))
        self.assertFalse(valid_twilio_signature("tok", url, {**params, "Body": "haan"}, sig))

    def test_split_long_body(self):
        parts = split_body("x\n" * 2000)
        self.assertTrue(all(len(p) <= 1550 for p in parts))


class SimulatorTests(unittest.TestCase):
    def test_offline_demo_uses_no_llm(self):
        import contextlib
        import io
        from unittest import mock
        from agent import simulate
        os.environ["LLM_API_KEY"] = "dummy-key-must-not-be-used"
        try:
            with mock.patch("agent.llm.requests.post", side_effect=AssertionError("network call")), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                simulate.main(["--script", "demo", "--offline"])
        finally:
            os.environ["LLM_API_KEY"] = ""
        self.assertIn("16,362", out.getvalue())
        with store.conn() as c:
            names = [r[0] for r in c.execute("SELECT name FROM agent_trace WHERE name LIKE 'extract_bill%'")]
        # 7 seeded + 3 sent during the demo, all through the fixture reader; no vision call, no error
        self.assertEqual(names, ["extract_bill[fixture-mock]"] * 10)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
