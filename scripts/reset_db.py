#!/usr/bin/env python3
"""Delete and recreate the DealSieve database.

Usage: python scripts/reset_db.py   (or: dealsieve reset)
Respects DEALSIEVE_DB_PATH (default data/dealsieve.db).
"""

from __future__ import annotations

import os
from pathlib import Path

from dealsieve.persistence import Repo


def main() -> int:
    db_path = Path(os.environ.get("DEALSIEVE_DB_PATH", "data/dealsieve.db"))
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            candidate.unlink()
            print(f"removed {candidate}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    repo = Repo(db_path)
    repo.init_schema()
    print(f"initialized empty database at {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
