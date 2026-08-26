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
    """Immutable latest-value snapshot produced by one device."""

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


class AbstractDevice(ABC):
    """Common non-blocking contract for every FirePyDAQ device.

    Device-specific reads happen in the concrete implementation. Acquisition code
    consumes only ``snapshot()``, which is thread-safe and must never perform I/O.
    """

    def __init__(self, name: str, device_type: str) -> None:
        self.name = name
        self.device_type = device_type
        self._snapshot_lock = threading.Lock()
        self._sequence = 0
        self._local_time: Optional[str] = None
        self._monotonic_time: Optional[float] = None
        self._values: dict[str, Any] = {}
        self._state = DeviceState.DISCONNECTED
        self._error: Optional[str] = None

    @abstractmethod
    def connect(self) -> None:
        """Open communication resources. Must be idempotent."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close communication resources. Must be idempotent."""

    def start(self) -> None:
        """Start acquisition activity after connection, if needed."""
        self._set_state(DeviceState.RUNNING)

    def stop(self) -> None:
        """Stop acquisition activity without assuming process shutdown."""
        if self.state != DeviceState.DISCONNECTED:
            self._set_state(DeviceState.CONNECTED)

    def snapshot(self) -> DeviceSnapshot:
        """Return the latest value without device I/O or blocking."""
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

    @abstractmethod
    def settings_to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable device configuration."""

    def _publish(self, values: Mapping[str, Any]) -> None:
        now = time.monotonic()
        local_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._snapshot_lock:
            self._sequence += 1
            self._values = dict(values)
            self._local_time = local_time
            self._monotonic_time = now
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
