"""Offline 'fixture' extractor: returns the stored reading for a known demo image.

This is a disclosed MOCK used only when no LLM key is configured (offline
rehearsal, CI tests). Live runs use the vision model in extract.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import extract


class FixtureExtractor:
    def __init__(self, *dirs: Path):
        self.index: dict[str, dict] = {}
        for d in dirs:
            for jpg in Path(d).rglob("*.jpg"):
                side = jpg.with_suffix(".json")
                if side.exists():
                    self.index[hashlib.sha256(jpg.read_bytes()).hexdigest()] = json.loads(side.read_text("utf-8"))["fixture"]

    def __call__(self, image: bytes) -> extract.Extraction:
        fx = self.index.get(hashlib.sha256(image).hexdigest())
        if fx is None:
            return extract.finalize({"is_bill": False})
        return extract.extract_from_fixture(fx)
