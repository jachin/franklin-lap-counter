#!/usr/bin/env python3
"""
Database module for RC Lap Counter.
Manages races, laps, and race-control audit actions using SQLite.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class LapDatabase:
    """Manages race and lap data in SQLite database"""

    def __init__(self, db_path: str = "lap_counter.db"):
        self.db_path: Path = Path(db_path)
        self.conn: sqlite3.Connection | None = None
        self._init_database()

    def _init_database(self) -> None:
        """Initialize database connection and create tables if needed"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row  # Enable column access by name

        cursor = self.conn.cursor()

        # Create races table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS races (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
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
                race_time REAL NOT NULL,
                lap_number INTEGER NOT NULL,
                lap_time REAL,
                timestamp TEXT NOT NULL,
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
        cursor.execute("PRAGMA table_info(race_control_actions)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if "command_id" not in existing_columns:
            cursor.execute(
                "ALTER TABLE race_control_actions ADD COLUMN command_id TEXT"
            )

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_race_control_actions_command_id
            ON race_control_actions(command_id)
        """)

        self.conn.commit()

    def create_race(self, notes: str | None = None) -> int:
        """Create a new race and return its ID"""
        assert self.conn is not None
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO races (start_time, status, notes)
            VALUES (?, 'in_progress', ?)
        """,
            (datetime.now().isoformat(), notes),
        )
        self.conn.commit()
        race_id = cursor.lastrowid
        assert race_id is not None
        return race_id

    def end_race(self, race_id: int) -> None:
        """Mark a race as completed"""
        assert self.conn is not None
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE races
            SET end_time = ?, status = 'completed'
            WHERE id = ?
        """,
            (datetime.now().isoformat(), race_id),
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
            ORDER BY start_time DESC
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
        race_time: float,
        lap_number: int,
        lap_time: float | None = None,
    ) -> int:
        """Add a lap to the database"""
        assert self.conn is not None
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO laps
            (race_id, racer_id, sensor_id, race_time, lap_number, lap_time, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                race_id,
                racer_id,
                sensor_id,
                race_time,
                lap_number,
                lap_time,
                datetime.now().isoformat(),
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
            ORDER BY timestamp ASC
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

        # Get total laps per racer
        cursor.execute(
            """
            SELECT racer_id, COUNT(*) as lap_count, MAX(lap_number) as max_lap
            FROM laps
            WHERE race_id = ?
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
