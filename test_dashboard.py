"""Dashboard API: built from the agent DB, local-only unless a token is given."""
import os
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="milaan-dash-"))
os.environ["AGENT_DB_PATH"] = str(TMP / "d.db")
os.environ["LLM_API_KEY"] = ""

from flask import Flask  # noqa: E402

from agent import store  # noqa: E402
from agent.demo.seed import main as seed_main  # noqa: E402
from agent.web import bp  # noqa: E402


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.reset_initialized()
        seed_main(["--trader", "+919000000001", "--supplier", "+919000000002", "--quiet"])
        app = Flask(__name__)
        app.register_blueprint(bp)
        cls.c = app.test_client()

    def test_payload(self):
        d = self.c.get("/api/dashboard").get_json()
        self.assertFalse(d["empty"])
        self.assertEqual(len(d["invoices"]), 7)
        self.assertNotIn("9000000001", d["trader"]["phone"][3:-4])  # masked

    def test_page(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 200)

    def test_remote_needs_token(self):
        h = {"Cf-Connecting-Ip": "1.2.3.4"}
        os.environ.pop("DASHBOARD_TOKEN", None)
        self.assertEqual(self.c.get("/api/dashboard", headers=h).status_code, 403)
        os.environ["DASHBOARD_TOKEN"] = "abc"
        self.assertEqual(self.c.get("/api/dashboard?t=wrong", headers=h).status_code, 403)
        self.assertEqual(self.c.get("/api/dashboard?t=abc", headers=h).status_code, 200)
        os.environ.pop("DASHBOARD_TOKEN", None)


if __name__ == "__main__":
    unittest.main()
