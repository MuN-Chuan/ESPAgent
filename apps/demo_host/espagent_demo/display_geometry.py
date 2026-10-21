from __future__ import annotations

from dataclasses import dataclass

PORTRAIT = "portrait"
LANDSCAPE = "landscape"
DISPLAY_ORIENTATIONS = frozenset({PORTRAIT, LANDSCAPE})


@dataclass(frozen=True)
class DisplayGeometry:
    orientation: str
    width: int
    height: int


def normalize_orientation(value: object) -> str:
    orientation = str(value or PORTRAIT).lower()
    if orientation not in DISPLAY_ORIENTATIONS:
        raise ValueError("orientation must be portrait or landscape")
    return orientation


def display_geometry(orientation: object = PORTRAIT) -> DisplayGeometry:
    normalized = normalize_orientation(orientation)
    if normalized == LANDSCAPE:
        return DisplayGeometry(normalized, 320, 240)
    return DisplayGeometry(normalized, 240, 320)
