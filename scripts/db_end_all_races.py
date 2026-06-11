#!/usr/bin/env python3
"""End all in-progress Franklin races without deleting race history."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import LapDatabase


def main() -> int:
    db = LapDatabase("lap_counter.db")
    try:
        assert db.conn is not None
        rows = db.conn.execute(
            "SELECT id FROM races WHERE status = 'in_progress' ORDER BY id DESC"
        ).fetchall()
        if not rows:
            print("No in-progress races to end.")
            return 0

        race_ids = [int(row["id"]) for row in rows]
        for race_id in race_ids:
            db.end_race(race_id)

        print(f"Marked {len(race_ids)} in-progress race(s) completed: {race_ids}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
