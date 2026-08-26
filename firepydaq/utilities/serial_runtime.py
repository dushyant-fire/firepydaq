from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable, Optional


class _PollingDevice:
    def __init__(self, name: str, read_fn: Callable[[], Any], interval_s: float,
                 health_manager_getter: Callable[[], Any]):
        self.name = name
        self.read_fn = read_fn
        self.interval_s = max(float(interval_s), 0.01)
        self.health_manager_getter = health_manager_getter
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._snapshot = {"value": None, "local_time": None,
                          "monotonic_time": None, "sequence": 0, "error": None}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run,
                                        name=f"firepydaq-serial-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._snapshot)

    def _health(self):
        try:
            return self.health_manager_getter()
        except Exception:
            return None

    def _run(self) -> None:
        next_read = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now < next_read:
                self._stop_event.wait(next_read - now)
                continue
            read_time = time.monotonic()
            local_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
            try:
                value = self.read_fn()
                with self._lock:
                    self._snapshot = {"value": deepcopy(value),
                                      "local_time": local_time,
                                      "monotonic_time": read_time,
                                      "sequence": self._snapshot["sequence"] + 1,
                                      "error": None}
                health = self._health()
                if health is not None:
                    health.good_read(self.name)
            except Exception as exc:
                with self._lock:
                    self._snapshot["error"] = f"{type(exc).__name__}: {exc}"
                health = self._health()
                if health is not None:
                    try:
                        health.error(self.name)
                    except Exception:
                        pass
            next_read = max(next_read + self.interval_s, time.monotonic())


class SerialDeviceManager:
    def __init__(self, health_manager_getter: Callable[[], Any]):
        self.health_manager_getter = health_manager_getter
        self._devices: dict[str, _PollingDevice] = {}
        self._lock = threading.Lock()

    def register_polling(self, name: str, read_fn: Callable[[], Any],
                         interval_s: float = 0.2) -> None:
        with self._lock:
            if name in self._devices:
                raise ValueError(f"Serial device already registered: {name}")
            self._devices[name] = _PollingDevice(name, read_fn, interval_s,
                                                  self.health_manager_getter)

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._devices.keys())

    def start_all(self) -> None:
        with self._lock:
            devices = tuple(self._devices.values())
        for device in devices:
            device.start()

    def stop_all(self) -> None:
        with self._lock:
            devices = tuple(self._devices.values())
        for device in devices:
            device.stop()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            items = tuple(self._devices.items())
        return {name: device.snapshot() for name, device in items}
