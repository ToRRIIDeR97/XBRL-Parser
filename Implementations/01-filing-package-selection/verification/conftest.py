import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("XBRL_ENV", "test")
os.environ.setdefault("XBRL_SEC_USER_AGENT", "Test Suite test@example.com")
