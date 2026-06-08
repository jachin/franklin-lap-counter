import json
import logging
from pathlib import Path
from typing import Any

from race.race import RaceEndMode


def load_initial_config(
    config_path: Path,
) -> tuple[int, RaceEndMode, list[dict[str, Any]]]:
    total_laps = 10
    race_end_mode = RaceEndMode.LAST_CAR
    contestants_data: list[dict[str, Any]] = []

    if not config_path.exists():
        return total_laps, race_end_mode, contestants_data

    try:
        raw_data = json.loads(config_path.read_text())
    except Exception as exc:
        logging.error("Failed to load config: %s", exc)
        return total_laps, race_end_mode, contestants_data

    config_data = raw_data if isinstance(raw_data, dict) else {}

    raw_total_laps = config_data.get("total_laps", total_laps)
    try:
        parsed_total_laps = int(raw_total_laps)
        if parsed_total_laps > 0:
            total_laps = parsed_total_laps
    except (TypeError, ValueError):
        logging.warning("Invalid total_laps in config: %r", raw_total_laps)

    race_end_mode_raw = str(config_data.get("race_end_mode", race_end_mode.value))
    try:
        race_end_mode = RaceEndMode(race_end_mode_raw)
    except ValueError:
        logging.warning("Invalid race_end_mode in config: %r", race_end_mode_raw)

    raw_contestants = config_data.get("contestants", contestants_data)
    if isinstance(raw_contestants, list):
        contestants_data = [c for c in raw_contestants if isinstance(c, dict)]
    else:
        logging.warning(
            "Invalid contestants in config: expected list, got %s",
            type(raw_contestants).__name__,
        )

    return total_laps, race_end_mode, contestants_data
