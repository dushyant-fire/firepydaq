from __future__ import annotations

import threading
from typing import Iterable, Optional

from .abstract_device import AbstractDevice, DeviceSnapshot


class DeviceRegistry:
    """Thread-safe owner of the devices participating in a FirePyDAQ run."""

    def __init__(self) -> None:
        self._devices: dict[str, AbstractDevice] = {}
        self._lock = threading.RLock()

    def register(self, device: AbstractDevice, *, replace: bool = False) -> None:
        with self._lock:
            if device.name in self._devices and not replace:
                if self._devices[device.name] is device:
                    return
                raise ValueError(f"Device already registered: {device.name}")
            self._devices[device.name] = device

    def unregister(self, name: str, *, disconnect: bool = True) -> None:
        with self._lock:
            device = self._devices.pop(name, None)
        if device is not None and disconnect:
            device.disconnect()

    def get(self, name: str) -> Optional[AbstractDevice]:
        with self._lock:
            return self._devices.get(name)

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._devices.keys())

    def devices(self) -> tuple[AbstractDevice, ...]:
        with self._lock:
            return tuple(self._devices.values())

    def snapshots(self) -> dict[str, DeviceSnapshot]:
        return {device.name: device.snapshot() for device in self.devices()}

    def start_all(self) -> None:
        started: list[AbstractDevice] = []
        try:
            for device in self.devices():
                device.start()
                started.append(device)
        except Exception:
            for device in reversed(started):
                try:
                    device.stop()
                except Exception:
                    pass
            raise

    def stop_all(self) -> None:
        for device in reversed(self.devices()):
            try:
                device.stop()
            except Exception:
                pass

    def disconnect_all(self) -> None:
        for device in reversed(self.devices()):
            try:
                device.disconnect()
            except Exception:
                pass

    def settings_to_dict(self) -> dict[str, dict]:
        return {
            device.name: device.settings_to_dict()
            for device in self.devices()
        }
