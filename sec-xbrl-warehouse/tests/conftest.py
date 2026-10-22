from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("XBRL_ENV", "test")
os.environ.setdefault("XBRL_SEC_USER_AGENT", "Test Suite test@example.com")
# Cleanup tests opt out explicitly; other tests must not inherit local deletion policy.
os.environ.setdefault("XBRL_RETAIN_FILING_SOURCE_FILES", "true")
