from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time
from typing import Optional

import numpy as np

from .acquisition_mode import AcquisitionMode
from .callbacks import EngineCallbacks
from .run_state import AcquisitionState, RunState, SaveState, local_now
from .save_manager import DataBlock




@dataclass(frozen=True)
class NiCycleResult:
    acquisition_time: np.ndarray
    values: np.ndarray
    absolute_time: tuple[str, ...]
    block: DataBlock
    submitted: bool
    read_elapsed_s: float
    block_duration_s: float
    processing_overrun: bool


class AcquisitionEngine:
    """GUI-independent device, timing, health, and saving coordinator."""

    def __init__(
        self,
        *,
        device_registry,
        callbacks: Optional[EngineCallbacks] = None,
        save_manager=None,
        health_manager=None,
        event_logger=None,
        mode: AcquisitionMode = AcquisitionMode.FULL,
    ) -> None:
        self.device_registry = device_registry
        self.callbacks = callbacks or EngineCallbacks()
        self.save_manager = save_manager
        self.health_manager = health_manager
        self.event_logger = event_logger
        self.mode = AcquisitionMode(mode)
        self._lock = threading.RLock()
        self._state = RunState()
        self._output_prefix: Optional[Path] = None
        self._last_health_write = 0.0
        self._cycle_scheduler = None
        self._cycle_callback = None
        self._cycle_delay_ms = 1

    def set_health_manager(self, health_manager) -> None:
        """Bind or replace the health manager after application startup."""
        self.health_manager = health_manager

    def set_save_manager(self, save_manager) -> None:
        """Bind or replace the save manager after application startup."""
        self.save_manager = save_manager

    @property
    def state(self) -> RunState:
        with self._lock:
            return self._state

    @property
    def acquiring(self) -> bool:
        return self.state.acquisition == AcquisitionState.RUNNING

    @property
    def saving(self) -> bool:
        return self.state.saving == SaveState.SAVING

    def set_mode(self, mode: AcquisitionMode) -> None:
        if self.acquiring or self.saving:
            raise RuntimeError("Stop acquisition and saving before changing mode.")
        self.mode = AcquisitionMode(mode)
        self.callbacks.notify(f"Acquisition mode: {self.mode.value}", "info")

    def start_acquisition(self) -> None:
        if self.acquiring:
            return

        started = []
        try:
            for device in self._participating_devices():
                device.start()
                started.append(device)
        except Exception as exc:
            for device in reversed(started):
                try:
                    device.stop()
                except Exception:
                    pass
            self._set_error(exc)
            raise

        self._set_state(
            replace(
                self.state,
                acquisition=AcquisitionState.RUNNING,
                acquisition_started_local=local_now(),
                acquisition_elapsed_s=0.0,
                save_time_origin_s=0.0,
                last_error=None,
            )
        )
        self.callbacks.notify("Acquisition started.", "success")

    def stop_acquisition(self) -> None:
        if self.saving:
            self.stop_save()

        self._set_state(
            replace(self.state, acquisition=AcquisitionState.IDLE,))

        errors = []
        for device in self._participating_devices():
            try:
                # Temporary migration boundary. The legacy runpyDAQ loop still
                # owns the active NI task and stops it after its loop exits.
                # if device.device_type == "ni_daq":
                #     continue
                device.stop()
            except Exception as exc:
                errors.append(f"{device.name}: {exc}")

        self._set_state(
            replace(
                self.state,
                acquisition=AcquisitionState.IDLE,
                last_error="; ".join(errors) if errors else None,
            )
        )
        self.update_health()
        self.callbacks.notify(
            "Acquisition stopped."
            + (" Device stop warnings: " + "; ".join(errors) if errors else ""),
            "warning" if errors else "success",
        )

    def configure_cycle_scheduler(
        self,
        *,
        scheduler,
        callback,
        delay_ms=1,
    ):

        self._scheduler = scheduler
        self._cycle_callback = callback
        self._cycle_delay_ms = delay_ms

    def schedule_next_cycle(self):
        if not self.acquiring:
            return False

        self._scheduler(
            self._cycle_delay_ms,
            self._cycle_callback,
        )

        return True

    def stop_ni_device(self, ni_device):

        if hasattr(ni_device, "aitask"):
            ni_device.aitask.stop()
            ni_device.aitask.close()

        if hasattr(ni_device, "aotask"):
            ni_device.aotask.stop()
            ni_device.aotask.close()

    def start_save(self, output_prefix: str | Path, *, manifest=None) -> None:
        if self.saving:
            return
        if not self.acquiring:
            raise RuntimeError("Start acquisition before saving.")
        if self.save_manager is None:
            raise RuntimeError("AcquisitionEngine has no SaveManager.")

        self._output_prefix = Path(output_prefix)
        started_devices = []
        try:
            self.save_manager.start(self._output_prefix, manifest=manifest)
            for device in self._participating_devices():
                device.start_run()
                started_devices.append(device)
        except Exception as exc:
            for device in started_devices:
                device.stop_run()
            try:
                self.save_manager.stop()
            except Exception:
                pass
            self._set_error(exc)
            raise

        self._set_state(
            replace(
                self.state,
                saving=SaveState.SAVING,
                save_started_local=local_now(),
                save_time_origin_s=self.state.acquisition_elapsed_s,
                last_error=None,
            )
        )
        self.update_health()
        self.callbacks.notify("Saving started.", "success")

    def stop_save(self) -> None:
        if not self.saving:
            return

        self._set_state(replace(self.state, saving=SaveState.FINALIZING))
        error = None
        try:
            for device in self._participating_devices():
                device.stop_run()
            if self.save_manager is not None:
                result = self.save_manager.stop()
                if result is not None and not result.completed:
                    raise RuntimeError(result.error or "Save finalization failed.")
        except Exception as exc:
            error = exc

        self._set_state(
            replace(
                self.state,
                saving=SaveState.ERROR if error else SaveState.IDLE,
                last_error=str(error) if error else None,
            )
        )
        self.update_health()

        if error is not None:
            self.callbacks.notify(f"Save finalization failed: {error}", "error")
            raise error
        self.callbacks.notify("Saving stopped.", "success")

    @property
    def last_block_duration(self) -> float:
        return self.state.last_block_duration_s

    def read_ni_cycle(
        self,
        ni_device,
        *,
        labels,
        datetime_format: str,
    ) -> Optional[NiCycleResult]:
        """Read and process one available NI hardware block.

        The engine owns NI availability checking, the hardware read, acquisition
        timing, DataBlock construction, save submission, and NI save accounting.
        The GUI remains responsible only for dashboard buffering and plotting.
        """
        if self.mode == AcquisitionMode.SERIAL_ONLY or not self.acquiring:
            return None

        sample_count = int(ni_device.numberOfSamples)
        sample_rate_hz = float(ni_device.aitask.timing.samp_clk_rate)
        available = int(ni_device.aitask._in_stream.avail_samp_per_chan)
        if available < sample_count:
            return None

        read_started = time.perf_counter()
        try:
            values = np.asarray(ni_device.threadaitask())
            if self.health_manager is not None:
                self.health_manager.good_read("NI")

            acquisition_time, absolute_time, block = self.build_ni_block(
                values=values,
                sample_count=sample_count,
                sample_rate_hz=sample_rate_hz,
                labels=labels,
                datetime_format=datetime_format,
            )

            submitted = False
            if self.saving:
                submitted = self.submit_ni_block(block)
                if not submitted:
                    raise RuntimeError(
                        "SaveManager rejected an NI block; acquisition must stop "
                        "to prevent silent data loss."
                    )
                ni_device.register_saved_samples(len(block.elapsed_s))

            read_elapsed_s = time.perf_counter() - read_started
            block_duration_s = self.last_block_duration
            return NiCycleResult(
                acquisition_time=acquisition_time,
                values=values,
                absolute_time=absolute_time,
                block=block,
                submitted=submitted,
                read_elapsed_s=read_elapsed_s,
                block_duration_s=block_duration_s,
                processing_overrun=(
                    block_duration_s > 0.0
                    and read_elapsed_s > block_duration_s
                ),
            )
        except Exception:
            if self.health_manager is not None:
                try:
                    self.health_manager.error("NI")
                except Exception:
                    pass
            raise

    def build_ni_block(
        self,
        *,
        values,
        sample_count: int,
        sample_rate_hz: float,
        labels,
        datetime_format: str,
    ) -> tuple[np.ndarray, tuple[str, ...], DataBlock]:
        """Build one NI block with continuous acquisition and save-relative time.

        Returns acquisition-relative times for plotting, absolute timestamps, and a
        DataBlock whose elapsed time starts at zero for each Save session.
        """
        sample_count = int(sample_count)
        sample_rate_hz = float(sample_rate_hz)
        if sample_count <= 0:
            raise ValueError("sample_count must be positive.")
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive.")

        with self._lock:
            state = self._state
            sample_offsets = (
                np.arange(sample_count, dtype=np.float64) / sample_rate_hz
            )
            acquisition_times = state.acquisition_elapsed_s + sample_offsets
            save_times = acquisition_times - state.save_time_origin_s
            block_duration = sample_count/sample_rate_hz
            self._state = replace(
                state,
                acquisition_elapsed_s=(
                    state.acquisition_elapsed_s
                    + sample_count / sample_rate_hz
                ),
                last_block_duration_s=block_duration,
            )
            changed_state = self._state

        self.callbacks.state_changed(changed_state)

        block_end = datetime.now()
        reverse_offsets = (
            np.arange(sample_count - 1, -1, -1, dtype=np.float64)
            / sample_rate_hz
        )
        absolute_times = tuple(
            (block_end - timedelta(seconds=float(offset))).strftime(
                datetime_format
            )
            for offset in reverse_offsets
        )

        block = DataBlock(
            elapsed_s=np.array(save_times, dtype=np.float64, copy=True),
            absolute_time=absolute_times,
            values=np.array(values, copy=True),
            labels=tuple(labels),
        )
        return acquisition_times, absolute_times, block

    def submit_ni_block(self, block: DataBlock) -> bool:
        """Submit one NI block to SaveManager when a Save session is active."""
        if not self.saving or self.save_manager is None:
            return False
        submit = getattr(self.save_manager, "submit_block", None)
        if submit is None:
            raise RuntimeError(
                "Engine save_manager does not implement submit_block(). "
                "Pass the real SaveManager, not LegacySaveAdapter."
            )
        return bool(submit(block))

    def collect_snapshots(self):
        """Return current immutable device snapshots.

        This method intentionally does not write serial CSV rows. Serial persistence
        still belongs to the current serial writer until that writer is migrated.
        """
        return self.device_registry.snapshots()

    def capture_serial_snapshots(self) -> int:
        """Submit current serial snapshots through SaveManager during saving."""
        if not self.saving or self.save_manager is None:
            return 0
        submit = getattr(self.save_manager, "submit_serial_snapshots", None)
        if submit is None:
            raise RuntimeError(
                "Engine save_manager does not implement submit_serial_snapshots()."
            )
        return int(submit(self.collect_snapshots()))

    def snapshots(self):
        return self.collect_snapshots()

    # def update_health(self) -> None:
    #     manager = self.health_manager
    #     if manager is None:
    #         return
    #     write_registry = getattr(manager, "write_registry", None)
    #     if callable(write_registry):
    #         write_registry(self.device_registry)
    #         return
    #     write = getattr(manager, "write", None)
    #     if callable(write):
    #         write()

    def update_health(self, force=False):
        manager = self.health_manager
        if manager is None:
            return

        now = time.monotonic()
        if (
            not force
            and now - self._last_health_write < 1.0
        ):
            return
        self._last_health_write = now
        write_registry = getattr(manager, "write_registry", None,)

        if callable(write_registry):
            write_registry(
                self.device_registry
            )
            return

        write = getattr(manager, "write", None,)

        if callable(write):
            write()

    def _participating_devices(self):
        return [
            device
            for device in self.device_registry.devices()
            if not (
                self.mode == AcquisitionMode.SERIAL_ONLY
                and device.device_type == "ni_daq"
            )
        ]

    def _set_state(self, state: RunState) -> None:
        with self._lock:
            self._state = state
        self.callbacks.state_changed(state)

    def _set_error(self, exc: BaseException) -> None:
        self._set_state(
            replace(
                self.state,
                acquisition=AcquisitionState.ERROR,
                saving=SaveState.ERROR if self.saving else self.state.saving,
                last_error=f"{type(exc).__name__}: {exc}",
            )
        )
        self.callbacks.notify(self.state.last_error or str(exc), "error")
