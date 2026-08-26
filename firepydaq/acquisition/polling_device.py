from __future__ import annotations

from collections.abc import Callable, Mapping
import threading
import time
from typing import Any, Optional

from .abstract_device import AbstractDevice, DeviceState


class PollingDevice(AbstractDevice):
    """Run a blocking device read function in an isolated worker thread.

    The worker publishes immutable snapshots. Optional health callbacks allow the
    existing DeviceHealthManager to remain the authoritative GUI health source.
    """

    def __init__(
        self,
        name: str,
        device_type: str,
        read_fn: Callable[[], Any],
        *,
        interval_s: float = 0.2,
        connect_fn: Optional[Callable[[], None]] = None,
        disconnect_fn: Optional[Callable[[], None]] = None,
        settings_fn: Optional[Callable[[], dict]] = None,
        health_manager_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(name=name, device_type=device_type)
        self._read_fn = read_fn
        self._connect_fn = connect_fn
        self._disconnect_fn = disconnect_fn
        self._settings_fn = settings_fn
        self._health_manager_getter = health_manager_getter
        self._interval_s = max(float(interval_s), 0.01)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def connect(self) -> None:
        if self._connect_fn is not None:
            self._connect_fn()
        self._set_state(DeviceState.CONNECTED)

    def disconnect(self) -> None:
        self.stop()
        if self._disconnect_fn is not None:
            self._disconnect_fn()
        self._set_state(DeviceState.DISCONNECTED)

    def start(self) -> None:
        if self.running:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"firepydaq-device-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self._interval_s * 4.0))
        self._thread = None

        if self.state != DeviceState.DISCONNECTED:
            self._set_state(DeviceState.CONNECTED)

    def settings_to_dict(self) -> dict[str, Any]:
        settings = self._settings_fn() if self._settings_fn else {}
        return {
            "Type": self.device_type,
            "PollingIntervalSeconds": self._interval_s,
            **settings,
        }

    def _run(self) -> None:
        next_read = time.monotonic()
        while not self._stop_event.is_set():
            delay = next_read - time.monotonic()
            if delay > 0 and self._stop_event.wait(delay):
                return

            try:
                raw_value = self._read_fn()
                self._publish(self._normalize(raw_value))
                self._report_good_read()
            except Exception as exc:
                self._set_error(exc)
                self._report_error()

            next_read = max(
                next_read + self._interval_s,
                time.monotonic(),
            )

    def _health_manager(self):
        if self._health_manager_getter is None:
            return None
        try:
            return self._health_manager_getter()
        except Exception:
            return None

    def _report_good_read(self) -> None:
        manager = self._health_manager()
        if manager is not None:
            try:
                manager.good_read(self.name)
            except Exception:
                pass

    def _report_error(self) -> None:
        manager = self._health_manager()
        if manager is not None:
            try:
                manager.error(self.name)
            except Exception:
                pass

    @staticmethod
    def _normalize(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, (list, tuple)):
            return {
                str(index): item
                for index, item in enumerate(value)
            }
        return {"value": value}
