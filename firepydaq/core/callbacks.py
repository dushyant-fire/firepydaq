from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


NotifyCallback = Callable[[str, str], None]
StateCallback = Callable[[object], None]


def _ignore_notification(_text: str, _level: str) -> None:
    return


def _ignore_state(_state: object) -> None:
    return


@dataclass
class EngineCallbacks:
    """GUI-neutral callbacks supplied by a GUI, CLI, or service client."""

    notify: NotifyCallback = _ignore_notification
    state_changed: StateCallback = _ignore_state
