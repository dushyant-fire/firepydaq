from __future__ import annotations

from enum import Enum


class AcquisitionMode(str, Enum):
    """Hardware participation modes supported by the acquisition engine."""

    FULL = "FULL"
    SERIAL_ONLY = "SERIAL_ONLY"
