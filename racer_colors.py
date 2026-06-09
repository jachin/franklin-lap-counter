import colorsys
import random
import re
from typing import Any

TOTAL_COLOR_SCHEMES = 1000
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RacerColorScheme = tuple[str, str]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def build_color_schemes(count: int = TOTAL_COLOR_SCHEMES) -> list[RacerColorScheme]:
    schemes: list[RacerColorScheme] = []

    for index in range(count):
        hue = index / count

        sat_seed = ((index * 37) % 100) / 100.0
        val_seed = ((index * 53) % 100) / 100.0

        primary_sat = _clamp01(0.55 + (0.30 * sat_seed))
        primary_val = _clamp01(0.70 + (0.22 * val_seed))
        pr, pg, pb = colorsys.hsv_to_rgb(hue, primary_sat, primary_val)

        stripe_hue = (hue + 0.08 + (0.10 * ((index * 29) % 100) / 100.0)) % 1.0
        stripe_sat = _clamp01(0.45 + (0.35 * ((index * 71) % 100) / 100.0))
        stripe_val = _clamp01(0.55 + (0.30 * ((index * 19) % 100) / 100.0))
        sr, sg, sb = colorsys.hsv_to_rgb(stripe_hue, stripe_sat, stripe_val)

        schemes.append((_rgb_to_hex(pr, pg, pb), _rgb_to_hex(sr, sg, sb)))

    return schemes


COLOR_SCHEMES = build_color_schemes()


def _is_hex_color(value: Any) -> bool:
    return isinstance(value, str) and HEX_COLOR_RE.fullmatch(value) is not None


def assign_random_scheme(
    existing_assignments: dict[int, RacerColorScheme],
) -> RacerColorScheme:
    used = set(existing_assignments.values())

    if len(used) < len(COLOR_SCHEMES):
        available = [scheme for scheme in COLOR_SCHEMES if scheme not in used]
        return random.choice(available)

    return random.choice(COLOR_SCHEMES)


def parse_racer_color_assignments(raw: Any) -> dict[int, RacerColorScheme]:
    if not isinstance(raw, dict):
        return {}

    parsed: dict[int, RacerColorScheme] = {}
    for raw_racer_id, raw_scheme in raw.items():
        try:
            racer_id = int(raw_racer_id)
        except (TypeError, ValueError):
            continue

        if racer_id <= 0:
            continue

        # Backward compatibility: previously values were scheme indices.
        if isinstance(raw_scheme, int) and 0 <= raw_scheme < len(COLOR_SCHEMES):
            parsed[racer_id] = COLOR_SCHEMES[raw_scheme]
            continue

        if not isinstance(raw_scheme, dict):
            continue

        primary = raw_scheme.get("primary")
        secondary = raw_scheme.get("secondary")
        if not (_is_hex_color(primary) and _is_hex_color(secondary)):
            continue

        # Explicit cast after validation to satisfy static type narrowing.
        primary_hex = str(primary).lower()
        secondary_hex = str(secondary).lower()
        parsed[racer_id] = (primary_hex, secondary_hex)

    return parsed
