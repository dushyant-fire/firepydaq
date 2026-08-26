from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import threading
import time
from typing import Any, Mapping, Optional


class DeviceState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    RUNNING = "RUNNING"
    STALE = "STALE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class DeviceSnapshot:
    name: str
    device_type: str
    sequence: int
    local_time: Optional[str]
    monotonic_time: Optional[float]
    values: Mapping[str, Any]
    state: DeviceState
    error: Optional[str] = None

    @property
    def has_value(self) -> bool:
        return self.sequence > 0 and bool(self.values)

    def age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if self.monotonic_time is None:
            return None
        return max(0.0, (time.monotonic() if now is None else now) - self.monotonic_time)


class AbstractDevice(ABC):
    def __init__(self, name: str, device_type: str) -> None:
        self.name = name
        self.device_type = device_type
        self._snapshot_lock = threading.RLock()
        self._sequence = 0
        self._local_time: Optional[str] = None
        self._monotonic_time: Optional[float] = None
        self._values: dict[str, Any] = {}
        self._state = DeviceState.DISCONNECTED
        self._error: Optional[str] = None

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def settings_to_dict(self) -> dict[str, Any]: ...

    def snapshot(self) -> DeviceSnapshot:
        with self._snapshot_lock:
            return DeviceSnapshot(
                name=self.name,
                device_type=self.device_type,
                sequence=self._sequence,
                local_time=self._local_time,
                monotonic_time=self._monotonic_time,
                values=deepcopy(self._values),
                state=self._state,
                error=self._error,
            )

    @property
    def state(self) -> DeviceState:
        with self._snapshot_lock:
            return self._state

    def _publish(self, values: Mapping[str, Any]) -> None:
        with self._snapshot_lock:
            self._sequence += 1
            self._values = dict(values)
            self._local_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
            self._monotonic_time = time.monotonic()
            self._state = DeviceState.RUNNING
            self._error = None

    def _set_error(self, exc: BaseException) -> None:
        with self._snapshot_lock:
            self._state = DeviceState.ERROR
            self._error = f"{type(exc).__name__}: {exc}"

    def _set_state(self, state: DeviceState) -> None:
        with self._snapshot_lock:
            self._state = state
            if state != DeviceState.ERROR:
                self._error = None

    def _clear_snapshot(self, state: DeviceState = DeviceState.DISCONNECTED) -> None:
        with self._snapshot_lock:
            self._sequence = 0
            self._local_time = None
            self._monotonic_time = None
            self._values = {}
            self._state = state
            self._error = None
