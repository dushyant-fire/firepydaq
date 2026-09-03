from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class AcquisitionState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


class SaveState(str, Enum):
    IDLE = "IDLE"
    SAVING = "SAVING"
    FINALIZING = "FINALIZING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class RunState:
    acquisition: AcquisitionState = AcquisitionState.IDLE
    saving: SaveState = SaveState.IDLE
    acquisition_started_local: Optional[str] = None
    save_started_local: Optional[str] = None
    last_error: Optional[str] = None
    acquisition_elapsed_s: float = 0.0
    save_time_origin_s: float = 0.0
    last_block_duration_s: float = 0.0


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")
