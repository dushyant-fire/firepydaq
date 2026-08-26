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
    run_sequence: int
    read_frequency_hz: float
    saved_samples_this_run: int
    sample_save_frequency_hz: float
    local_time: Optional[str]
    monotonic_time: Optional[float]
    last_save_local: Optional[str]
    last_save_monotonic: Optional[float]
    values: Mapping[str, Any]
    state: DeviceState
    error: Optional[str] = None

    @property
    def has_value(self) -> bool:
        return self.sequence > 0 and bool(self.values)

    def age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if self.monotonic_time is None:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, current - self.monotonic_time)

    def save_age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_save_monotonic is None:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, current - self.last_save_monotonic)


class AbstractDevice(ABC):
    """Authoritative runtime state and metrics for every FirePyDAQ device."""

    _FREQUENCY_ALPHA = 0.10

    def __init__(self, name: str, device_type: str) -> None:
        self.name = name
        self.device_type = device_type
        self._snapshot_lock = threading.RLock()

        self._sequence = 0
        self._run_sequence = 0
        self._run_active = False

        self._read_frequency_hz = 0.0
        self._last_publish_time: Optional[float] = None

        self._saved_samples_this_run = 0
        self._sample_save_frequency_hz = 0.0
        self._last_save_time: Optional[float] = None
        self._last_save_local: Optional[str] = None

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

    def start_run(self) -> None:
        """Reset metrics that belong to one Save session."""
        with self._snapshot_lock:
            self._run_sequence = 0
            self._saved_samples_this_run = 0
            self._sample_save_frequency_hz = 0.0
            self._last_save_time = None
            self._last_save_local = None
            self._run_active = True

    def stop_run(self) -> None:
        """Freeze current Save-session metrics until the next Save begins."""
        with self._snapshot_lock:
            self._run_active = False

    def register_saved_samples(self, count: int) -> None:
        """Record samples confirmed accepted by a storage writer.

        ``count`` is samples, not write operations. NI therefore supplies the
        number of samples in a block, while Alicat and line-oriented serial
        devices normally supply one.
        """
        sample_count = int(count)
        if sample_count <= 0:
            return

        with self._snapshot_lock:
            if not self._run_active:
                return

            now = time.monotonic()
            if self._last_save_time is not None:
                elapsed = now - self._last_save_time
                if elapsed > 0:
                    instant_rate = sample_count / elapsed
                    self._sample_save_frequency_hz = self._smooth_frequency(
                        self._sample_save_frequency_hz,
                        instant_rate,
                    )

            self._saved_samples_this_run += sample_count
            self._last_save_time = now
            self._last_save_local = (
                datetime.now()
                .astimezone()
                .isoformat(timespec="milliseconds")
            )

    def snapshot(self) -> DeviceSnapshot:
        with self._snapshot_lock:
            return DeviceSnapshot(
                name=self.name,
                device_type=self.device_type,
                sequence=self._sequence,
                run_sequence=self._run_sequence,
                read_frequency_hz=self._read_frequency_hz,
                saved_samples_this_run=self._saved_samples_this_run,
                sample_save_frequency_hz=self._sample_save_frequency_hz,
                local_time=self._local_time,
                monotonic_time=self._monotonic_time,
                last_save_local=self._last_save_local,
                last_save_monotonic=self._last_save_time,
                values=deepcopy(self._values),
                state=self._state,
                error=self._error,
            )

    @property
    def state(self) -> DeviceState:
        with self._snapshot_lock:
            return self._state

    def _publish(self, values: Mapping[str, Any]) -> None:
        """Register one successful device update/read."""
        with self._snapshot_lock:
            now = time.monotonic()
            if self._last_publish_time is not None:
                elapsed = now - self._last_publish_time
                if elapsed > 0:
                    self._read_frequency_hz = self._smooth_frequency(
                        self._read_frequency_hz,
                        1.0 / elapsed,
                    )

            self._sequence += 1
            if self._run_active:
                self._run_sequence += 1

            self._values = dict(values)
            self._local_time = (
                datetime.now()
                .astimezone()
                .isoformat(timespec="milliseconds")
            )
            self._last_publish_time = now
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

    def _clear_snapshot(
        self,
        state: DeviceState = DeviceState.DISCONNECTED,
    ) -> None:
        with self._snapshot_lock:
            self._sequence = 0
            self._run_sequence = 0
            self._run_active = False
            self._read_frequency_hz = 0.0
            self._last_publish_time = None
            self._saved_samples_this_run = 0
            self._sample_save_frequency_hz = 0.0
            self._last_save_time = None
            self._last_save_local = None
            self._local_time = None
            self._monotonic_time = None
            self._values = {}
            self._state = state
            self._error = None

    @classmethod
    def _smooth_frequency(cls, previous: float, current: float) -> float:
        if previous <= 0:
            return current
        alpha = cls._FREQUENCY_ALPHA
        return ((1.0 - alpha) * previous) + (alpha * current)
