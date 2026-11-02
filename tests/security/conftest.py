"""Puts every docs/layers/*/files folder on sys.path, so the security tests import the real
reference modules (action.py, dispatcher.py, ...) under their plain names."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

for files in sorted((ROOT / "docs" / "layers").glob("*/files")):
    sys.path.insert(0, str(files))
