import json
import logging
from pathlib import Path
from typing import Any

from database import LapDatabase
from race.race_mode import RaceMode
from race.race_state import RaceEndMode
from racer_colors import RacerColorScheme, parse_racer_color_assignments


def _parse_race_mode(raw_mode: Any, default: RaceMode) -> RaceMode:
    if isinstance(raw_mode, str):
        normalized = raw_mode.strip().lower()
        for mode in RaceMode:
            if normalized in (mode.name.lower(), mode.value.lower()):
                return mode
        if normalized == "race":
            return RaceMode.REAL
    logging.warning("Invalid race_mode in config: %r", raw_mode)
    return default


def load_initial_config(
    config_path: Path,
) -> tuple[
    RaceMode,
    int,
    RaceEndMode,
    list[dict[str, Any]],
    list[int],
    dict[int, RacerColorScheme],
]:
    db_path = config_path.parent / "franklin.db"
    db = LapDatabase(str(db_path))

    race_mode_val = db.get_preference("race_mode")
    total_laps_val = db.get_preference("total_laps")
    race_end_mode_val = db.get_preference("race_end_mode")
    contestants_val = db.get_preference("contestants")
    last_race_contestant_ids_val = db.get_preference("last_race_contestant_ids")
    racer_color_assignments_val = db.get_preference("racer_color_assignments")

    # If all DB preferences are None, attempt to migrate from JSON
    if (
        race_mode_val is None
        and total_laps_val is None
        and race_end_mode_val is None
        and contestants_val is None
        and last_race_contestant_ids_val is None
        and racer_color_assignments_val is None
    ):
        if config_path.exists():
            logging.info(
                "Database preferences are empty. Loading and migrating from %s",
                config_path,
            )
            try:
                raw_data = json.loads(config_path.read_text())
                if isinstance(raw_data, dict):
                    # Save each value to SQLite
                    for k, v in raw_data.items():
                        db.set_preference(k, v)

                    # Update local variables
                    race_mode_val = raw_data.get("race_mode")
                    total_laps_val = raw_data.get("total_laps")
                    race_end_mode_val = raw_data.get("race_end_mode")
                    contestants_val = raw_data.get("contestants")
                    last_race_contestant_ids_val = raw_data.get(
                        "last_race_contestant_ids"
                    )
                    racer_color_assignments_val = raw_data.get(
                        "racer_color_assignments"
                    )
            except Exception as exc:
                logging.error("Failed to migrate config from JSON: %s", exc)

    # Defaults to use if no value was loaded from either source
    race_mode = RaceMode.TRAINING
    total_laps = 10
    race_end_mode = RaceEndMode.LAST_CAR
    contestants_data: list[dict[str, Any]] = []
    last_race_contestant_ids: list[int] = []
    racer_color_assignments: dict[int, RacerColorScheme] = {}

    if race_mode_val is not None:
        race_mode = _parse_race_mode(race_mode_val, race_mode)

    if total_laps_val is not None:
        try:
            parsed_total_laps = int(total_laps_val)
            if parsed_total_laps > 0:
                total_laps = parsed_total_laps
        except (TypeError, ValueError):
            logging.warning("Invalid total_laps in config: %r", total_laps_val)

    if race_end_mode_val is not None:
        try:
            race_end_mode = RaceEndMode(str(race_end_mode_val))
        except ValueError:
            logging.warning("Invalid race_end_mode in config: %r", race_end_mode_val)

    if contestants_val is not None:
        if isinstance(contestants_val, list):
            contestants_data = [c for c in contestants_val if isinstance(c, dict)]
        else:
            logging.warning(
                "Invalid contestants in config: expected list, got %s",
                type(contestants_val).__name__,
            )

    if last_race_contestant_ids_val is not None:
        if isinstance(last_race_contestant_ids_val, list):
            parsed_ids: list[int] = []
            for raw_id in last_race_contestant_ids_val:
                try:
                    parsed_id = int(raw_id)
                    if parsed_id > 0:
                        parsed_ids.append(parsed_id)
                except (TypeError, ValueError):
                    continue
            last_race_contestant_ids = list(dict.fromkeys(parsed_ids))
        else:
            logging.warning(
                "Invalid last_race_contestant_ids in config: expected list, got %s",
                type(last_race_contestant_ids_val).__name__,
            )

    if racer_color_assignments_val is not None:
        racer_color_assignments = parse_racer_color_assignments(
            racer_color_assignments_val
        )

    db.close()
    return (
        race_mode,
        total_laps,
        race_end_mode,
        contestants_data,
        last_race_contestant_ids,
        racer_color_assignments,
    )


def write_config(
    config_path: Path,
    *,
    race_mode: RaceMode,
    total_laps: int,
    race_end_mode: RaceEndMode,
    contestants_data: list[dict[str, Any]],
    last_race_contestant_ids: list[int],
    racer_color_assignments: dict[int, RacerColorScheme],
) -> None:
    db_path = config_path.parent / "franklin.db"
    db = LapDatabase(str(db_path))

    normalized_last_race_contestant_ids = sorted(
        {
            int(racer_id)
            for racer_id in last_race_contestant_ids
            if isinstance(racer_id, int) and racer_id > 0
        }
    )

    try:
        db.set_preference("race_mode", race_mode.value)
        db.set_preference("total_laps", total_laps)
        db.set_preference("race_end_mode", race_end_mode.value)
        db.set_preference("contestants", contestants_data)
        db.set_preference(
            "last_race_contestant_ids", normalized_last_race_contestant_ids
        )
        db.set_preference(
            "racer_color_assignments",
            {
                str(racer_id): {"primary": colors[0], "secondary": colors[1]}
                for racer_id, colors in sorted(racer_color_assignments.items())
            },
        )
    except Exception as exc:
        logging.error("Failed to write preferences to database: %s", exc)
    finally:
        db.close()
