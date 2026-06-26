#!/usr/bin/env python3
"""
Database module for RC Lap Counter.
Manages races, laps, and race-control audit actions using SQLite.
"""

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _epoch_now() -> float:
    return time.time()


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class LapDatabase:
    """Manages race and lap data in SQLite database"""

    def __init__(self, db_path: str = "franklin.db"):
        self.db_path: Path = Path(db_path)
        self.conn: sqlite3.Connection | None = None
        self._init_database()

    def _init_database(self) -> None:
        """Initialize database connection and create tables if needed"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row  # Enable column access by name

        cursor = self.conn.cursor()

        # Create preferences table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Create races table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS races (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_at REAL,
                end_at REAL,
                start_time TEXT,
                end_time TEXT,
                status TEXT NOT NULL DEFAULT 'in_progress',
                notes TEXT
            )
        """)

        # Create laps table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id INTEGER NOT NULL,
                racer_id INTEGER NOT NULL,
                sensor_id INTEGER NOT NULL,
                race_start_at REAL,
                lap_at REAL,
                recorded_at REAL,
                race_time REAL,
                lap_number INTEGER NOT NULL,
                lap_time REAL,
                timestamp TEXT,
                FOREIGN KEY (race_id) REFERENCES races(id)
            )
        """)

        # Create race-control action audit table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS race_control_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id INTEGER,
                command TEXT NOT NULL,
                command_id TEXT,
                accepted INTEGER NOT NULL,
                racer_id INTEGER,
                lap_number INTEGER,
                penalty_seconds INTEGER,
                reason TEXT,
                message TEXT,
                source TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (race_id) REFERENCES races(id)
            )
        """)

        # Create indexes for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_laps_race_id
            ON laps(race_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_laps_racer_id
            ON laps(race_id, racer_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_race_control_actions_race_id
            ON race_control_actions(race_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_race_control_actions_command
            ON race_control_actions(command)
        """)

        # Lightweight migrations for existing databases
        cursor.execute("PRAGMA table_info(races)")
        race_columns = {row[1] for row in cursor.fetchall()}
        if "start_at" not in race_columns:
            cursor.execute("ALTER TABLE races ADD COLUMN start_at REAL")
        if "end_at" not in race_columns:
            cursor.execute("ALTER TABLE races ADD COLUMN end_at REAL")

        cursor.execute("PRAGMA table_info(laps)")
        lap_columns = {row[1] for row in cursor.fetchall()}
        if "race_start_at" not in lap_columns:
            cursor.execute("ALTER TABLE laps ADD COLUMN race_start_at REAL")
        if "lap_at" not in lap_columns:
            cursor.execute("ALTER TABLE laps ADD COLUMN lap_at REAL")
        if "recorded_at" not in lap_columns:
            cursor.execute("ALTER TABLE laps ADD COLUMN recorded_at REAL")

        cursor.execute("PRAGMA table_info(race_control_actions)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if "command_id" not in existing_columns:
            cursor.execute(
                "ALTER TABLE race_control_actions ADD COLUMN command_id TEXT"
            )

        # Backfill epoch columns from historical text/relative columns where possible.
        cursor.execute(
            """
            UPDATE races
            SET start_at = strftime('%s', start_time)
            WHERE start_at IS NULL AND start_time IS NOT NULL
            """
        )
        cursor.execute(
            """
            UPDATE races
            SET end_at = strftime('%s', end_time)
            WHERE end_at IS NULL AND end_time IS NOT NULL
            """
        )
        cursor.execute(
            """
            UPDATE laps
            SET race_start_at = (
                SELECT r.start_at FROM races r WHERE r.id = laps.race_id
            )
            WHERE race_start_at IS NULL
              AND race_time IS NOT NULL
            """
        )
        cursor.execute(
            """
            UPDATE laps
            SET lap_at = race_start_at + race_time
            WHERE lap_at IS NULL
              AND race_start_at IS NOT NULL
              AND race_time IS NOT NULL
            """
        )
        cursor.execute(
            """
            UPDATE laps
            SET recorded_at = COALESCE(strftime('%s', timestamp), lap_at)
            WHERE recorded_at IS NULL
            """
        )

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_race_control_actions_command_id
            ON race_control_actions(command_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_laps_lap_at
            ON laps(lap_at)
        """)

        self.conn.commit()

    def create_race(
        self, notes: str | None = None, *, start_at: float | None = None
    ) -> int:
        """Create a new race and return its ID."""
        assert self.conn is not None
        cursor = self.conn.cursor()
        start_epoch = float(start_at) if start_at is not None else _epoch_now()
        cursor.execute(
            """
            INSERT INTO races (start_at, start_time, status, notes)
            VALUES (?, ?, 'in_progress', ?)
        """,
            (start_epoch, _epoch_to_iso(start_epoch), notes),
        )
        self.conn.commit()
        race_id = cursor.lastrowid
        assert race_id is not None
        return race_id

    def end_race(self, race_id: int, *, end_at: float | None = None) -> None:
        """Mark a race as completed."""
        assert self.conn is not None
        cursor = self.conn.cursor()
        end_epoch = float(end_at) if end_at is not None else _epoch_now()
        cursor.execute(
            """
            UPDATE races
            SET end_at = ?, end_time = ?, status = 'completed'
            WHERE id = ?
        """,
            (end_epoch, _epoch_to_iso(end_epoch), race_id),
        )
        self.conn.commit()

    def get_in_progress_race(self) -> dict[str, Any] | None:
        """Get the current in-progress race if one exists"""
        assert self.conn is not None
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM races
            WHERE status = 'in_progress'
            ORDER BY COALESCE(start_at, strftime('%s', start_time)) DESC
            LIMIT 1
        """
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def add_lap(
        self,
        race_id: int,
        racer_id: int,
        sensor_id: int,
        lap_number: int,
        lap_time: float | None = None,
        *,
        race_start_at: float | None = None,
        lap_at: float | None = None,
        recorded_at: float | None = None,
        race_time: float | None = None,
    ) -> int:
        """Add a lap to the database using epoch-native fields.

        Preferred inputs: `race_start_at`, `lap_at`, and `recorded_at`.
        `race_time` remains accepted for compatibility and is derived when omitted.
        """
        assert self.conn is not None
        cursor = self.conn.cursor()

        resolved_race_time = race_time
        if (
            resolved_race_time is None
            and lap_at is not None
            and race_start_at is not None
        ):
            resolved_race_time = float(lap_at) - float(race_start_at)

        resolved_recorded_at = (
            float(recorded_at)
            if recorded_at is not None
            else (float(lap_at) if lap_at is not None else _epoch_now())
        )

        cursor.execute(
            """
            INSERT INTO laps
            (
                race_id,
                racer_id,
                sensor_id,
                race_start_at,
                lap_at,
                recorded_at,
                race_time,
                lap_number,
                lap_time,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                race_id,
                racer_id,
                sensor_id,
                race_start_at,
                lap_at,
                resolved_recorded_at,
                resolved_race_time,
                lap_number,
                lap_time,
                _epoch_to_iso(resolved_recorded_at),
            ),
        )
        self.conn.commit()
        lap_id = cursor.lastrowid
        assert lap_id is not None
        return lap_id

    def get_race_laps(self, race_id: int) -> list[dict[str, Any]]:
        """Get all laps for a specific race"""
        assert self.conn is not None
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM laps
            WHERE race_id = ?
            ORDER BY COALESCE(lap_at, strftime('%s', timestamp), id) ASC
        """,
            (race_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_racer_laps(self, race_id: int, racer_id: int) -> list[dict[str, Any]]:
        """Get all laps for a specific racer in a race"""
        assert self.conn is not None
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM laps
            WHERE race_id = ? AND racer_id = ?
            ORDER BY lap_number ASC
        """,
            (race_id, racer_id),
        )
        return [dict(row) for row in cursor.fetchall()]

    def remove_lap(
        self, race_id: int, racer_id: int, lap_number: int | None = None
    ) -> dict[str, Any] | None:
        """Remove one lap for racer in a race.

        If lap_number is provided, remove that exact lap number.
        If lap_number is None, remove the latest recorded positive lap for racer.
        Returns the removed lap row as dict, or None if nothing matched.
        """
        assert self.conn is not None
        cursor = self.conn.cursor()

        if lap_number is None:
            cursor.execute(
                """
                SELECT * FROM laps
                WHERE race_id = ? AND racer_id = ? AND lap_number > 0
                ORDER BY lap_number DESC, id DESC
                LIMIT 1
            """,
                (race_id, racer_id),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM laps
                WHERE race_id = ? AND racer_id = ? AND lap_number = ?
                ORDER BY id DESC
                LIMIT 1
            """,
                (race_id, racer_id, lap_number),
            )

        row = cursor.fetchone()
        if not row:
            return None

        removed = dict(row)
        cursor.execute("DELETE FROM laps WHERE id = ?", (row["id"],))
        self.conn.commit()
        return removed

    def add_race_control_action(
        self,
        *,
        command: str,
        accepted: bool,
        payload: dict[str, Any],
        race_id: int | None = None,
    ) -> int:
        """Persist one race-control action audit row and return its ID."""
        assert self.conn is not None
        cursor = self.conn.cursor()

        cursor.execute(
            """
            INSERT INTO race_control_actions
            (
                race_id,
                command,
                command_id,
                accepted,
                racer_id,
                lap_number,
                penalty_seconds,
                reason,
                message,
                source,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                race_id,
                command,
                payload.get("command_id"),
                1 if accepted else 0,
                payload.get("racer_id"),
                payload.get("lap_number"),
                payload.get("penalty_seconds"),
                payload.get("reason"),
                payload.get("message"),
                payload.get("source"),
                json.dumps(payload, sort_keys=True),
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()
        action_id = cursor.lastrowid
        assert action_id is not None
        return action_id

    def get_race_control_actions(
        self, race_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return race-control audit rows newest-first, optionally filtered by race."""
        assert self.conn is not None
        cursor = self.conn.cursor()

        if race_id is None:
            cursor.execute(
                """
                SELECT * FROM race_control_actions
                ORDER BY created_at DESC, id DESC
            """
            )
        else:
            cursor.execute(
                """
                SELECT * FROM race_control_actions
                WHERE race_id = ?
                ORDER BY created_at DESC, id DESC
            """,
                (race_id,),
            )

        return [dict(row) for row in cursor.fetchall()]

    def get_race_stats(self, race_id: int) -> dict[int, dict[str, Any]]:
        """Get statistics for a race"""
        assert self.conn is not None
        cursor = self.conn.cursor()

        # Get total laps per racer (lap_number > 0 excludes the first detection/crossing)
        cursor.execute(
            """
            SELECT racer_id, COUNT(*) as lap_count, MAX(lap_number) as max_lap
            FROM laps
            WHERE race_id = ? AND lap_number > 0
            GROUP BY racer_id
        """,
            (race_id,),
        )
        racer_stats = {row["racer_id"]: dict(row) for row in cursor.fetchall()}

        # Get best lap time per racer
        cursor.execute(
            """
            SELECT racer_id, MIN(lap_time) as best_lap_time
            FROM laps
            WHERE race_id = ? AND lap_time IS NOT NULL
            GROUP BY racer_id
        """,
            (race_id,),
        )
        for row in cursor.fetchall():
            if row["racer_id"] in racer_stats:
                racer_stats[row["racer_id"]]["best_lap_time"] = row["best_lap_time"]

        return racer_stats

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get a JSON-decoded preference value by key"""
        if not self.conn:
            return default
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT value FROM preferences WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except Exception:
                    return row["value"]
        except sqlite3.OperationalError:
            pass
        return default

    def set_preference(self, key: str, value: Any) -> None:
        """Set a preference value, serialized as JSON"""
        if not self.conn:
            return
        serialized = json.dumps(value)
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO preferences (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, serialized),
        )
        self.conn.commit()

    def close(self) -> None:
        """Close database connection"""
        if self.conn:
            self.conn.close()

    def __enter__(self) -> "LapDatabase":
        """Context manager entry"""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit"""
        self.close()


if __name__ == "__main__":
    # Simple test
    with LapDatabase("test.db") as db:
        print("Testing database...")

        # Create a race
        race_id = db.create_race("Test race")
        print(f"Created race {race_id}")

        # Add some laps
        _ = db.add_lap(
            race_id,
            racer_id=1,
            sensor_id=1,
            race_time=10.5,
            lap_number=1,
            lap_time=10.5,
        )
        _ = db.add_lap(
            race_id, racer_id=1, sensor_id=1, race_time=20.3, lap_number=2, lap_time=9.8
        )
        _ = db.add_lap(
            race_id,
            racer_id=2,
            sensor_id=2,
            race_time=12.1,
            lap_number=1,
            lap_time=12.1,
        )

        # Get stats
        stats = db.get_race_stats(race_id)
        print(f"Race stats: {stats}")

        # Check for in-progress race
        in_progress = db.get_in_progress_race()
        print(f"In-progress race: {in_progress}")

        print("Database test complete!")
