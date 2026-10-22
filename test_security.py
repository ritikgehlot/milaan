"""Production access-policy and operational-audit tests."""

import os
import tempfile
import unittest
from pathlib import Path

import db
import security


class SecurityConfigTests(unittest.TestCase):
    def test_production_requires_auth_by_default(self):
        config = security.load_security_config({"APP_ENV": "production"})
        self.assertTrue(config.auth_required)
        self.assertTrue(config.production)

    def test_development_allows_local_demo_by_default(self):
        config = security.load_security_config({"APP_ENV": "development"})
        self.assertFalse(config.auth_required)

    def test_domain_allowlist(self):
        config = security.load_security_config({
            "APP_ENV": "production",
            "MILAAN_ALLOWED_EMAIL_DOMAINS": "example.com, operations.example",
        })
        self.assertTrue(security.email_is_allowed("a@example.com", config.allowed_email_domains))
        self.assertFalse(security.email_is_allowed("a@other.example", config.allowed_email_domains))

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "MILAAN_AUTH_REQUIRED"):
            security.load_security_config({"MILAAN_AUTH_REQUIRED": "maybe"})


class SystemAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self.tmp.name) / "audit.db")
        db.init_db()

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.old_path

    def test_event_is_persisted_with_structured_details(self):
        db.record_system_audit_event(
            actor="person@example.com", event_type="gstr2b_imported",
            subject_type="gstr2b_import", subject_id=7,
            details={"accepted_rows": 2},
        )
        events = db.get_system_audit_events()
        self.assertEqual(events[0]["Actor"], "person@example.com")
        self.assertEqual(events[0]["Subject"], "gstr2b_import:7")
        self.assertEqual(events[0]["Details"], {"accepted_rows": 2})

    def test_audit_event_requires_bounded_identifiers(self):
        with self.assertRaises(ValueError):
            db.record_system_audit_event(
                actor="", event_type="event", subject_type="object"
            )
