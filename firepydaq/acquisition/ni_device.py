from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .abstract_device import AbstractDevice, DeviceState
from ..api.EchoNIDAQTask import CreateDAQTask


class NIDaqDevice(AbstractDevice):
    """AbstractDevice adapter for hardware-timed NI analog input.

    The existing FirePyDAQ acquisition loop remains the block consumer. Every
    successful block read is also published as the latest immutable snapshot.
    """

    def __init__(self, parent, name: str = "NI") -> None:
        super().__init__(name=name, device_type="ni_daq")
        self.parent = parent
        self._driver = CreateDAQTask(parent, name)
        self.config_path: Optional[Path] = None
        self.sampling_rate = 0.0
        self.samples_per_read = 0

    def __getattr__(self, name: str):
        # Preserve the existing FirePyDAQ API during migration.
        driver = object.__getattribute__(self, "_driver")
        return getattr(driver, name)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._driver.ailabel_map.keys())

    def connect(self, config_path: str | Path | None = None) -> None:
        requested_path = Path(config_path) if config_path is not None else self.config_path
        if (
            self.state in (DeviceState.CONNECTED, DeviceState.RUNNING)
            and requested_path == self.config_path
            and self._driver.aitask is not None
        ):
            return

        if config_path is not None:
            self.config_path = Path(config_path)
        if self.config_path is None:
            raise ValueError("NI configuration file is required.")

        try:
            self._driver.CreateFromConfig(self.config_path)
            self._set_state(DeviceState.CONNECTED)
        except Exception as exc:
            self._set_error(exc)
            raise

        registry = getattr(self.parent, "device_registry", None)
        if registry is not None:
            registry.register(self, replace=True)

    def disconnect(self) -> None:
        self.stop()
        try:
            self._driver.close()
        finally:
            self._clear_snapshot(DeviceState.DISCONNECTED)

    def start(
        self,
        sampling_rate: float | None = None,
        samples_per_read: int | None = None,
        *,
        save_tdms: bool = False,
        save_tdms_path: str = "PreSavedData_AI.tdms",
    ) -> None:
        if self.state == DeviceState.RUNNING:
            return
        if self.state == DeviceState.DISCONNECTED:
            self.connect()

        if sampling_rate is not None:
            self.sampling_rate = float(sampling_rate)
        if samples_per_read is not None:
            self.samples_per_read = int(samples_per_read)
        if self.sampling_rate <= 0 or self.samples_per_read <= 0:
            raise ValueError("NI sampling rate and samples per read must be positive.")

        try:
            self._driver.StartAIContinuousTask(
                self.sampling_rate,
                self.samples_per_read,
                save_tdms=save_tdms,
                save_tdms_path=save_tdms_path,
            )
            self._set_state(DeviceState.RUNNING)
        except Exception as exc:
            self._set_error(exc)
            raise

    def stop(self) -> None:
        self._driver.stop_ai()
        if self.state != DeviceState.DISCONNECTED:
            self._set_state(DeviceState.CONNECTED)

    def read_block(self) -> np.ndarray:
        if self.state != DeviceState.RUNNING:
            raise RuntimeError("NI analog-input task is not running.")

        try:
            raw = self._driver.threadaitask()
            values = np.asarray(raw)
            if values.ndim == 1:
                values = values[np.newaxis, :]
            self._publish(self._latest_values(values))
            return values
        except Exception as exc:
            self._set_error(exc)
            raise

    def threadaitask(self):
        """Compatibility alias used by the current acquisition loop."""
        return self.read_block()

    def CreateFromConfig(self, config_path) -> None:
        """Compatibility alias that now performs AbstractDevice connect."""
        self.connect(config_path)

    def StartAIContinuousTask(
        self,
        SamplingRate,
        HowManySample,
        save_tdms: bool = False,
        save_tdms_path: str = "PreSavedData_AI.tdms",
    ) -> None:
        """Compatibility alias that now performs AbstractDevice start."""
        self.start(
            SamplingRate,
            HowManySample,
            save_tdms=save_tdms,
            save_tdms_path=save_tdms_path,
        )

    def settings_to_dict(self) -> dict[str, Any]:
        return {
            "Type": "ni_daq",
            "ConfigFile": str(self.config_path) if self.config_path else "",
            "SamplingRate": self.sampling_rate,
            "SamplesPerRead": self.samples_per_read,
            "AnalogOutputEnabled": False,
        }

    def _latest_values(self, block: np.ndarray) -> Mapping[str, Any]:
        latest = block[:, -1] if block.shape[1] else np.array([])
        labels = self.labels
        return {
            labels[index] if index < len(labels) else f"channel_{index}": float(value)
            for index, value in enumerate(latest)
        }
