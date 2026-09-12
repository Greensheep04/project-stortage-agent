#!/usr/bin/env python3
"""Create and initialize the isolated test database on the same PostgreSQL service.

Idempotent; safe to run repeatedly. Test runs also call this via conftest.
"""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from pi_market import db


def main() -> int:
    result = db.ensure_test_database()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
