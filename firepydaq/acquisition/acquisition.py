"""
FIREpyDAQ acquisition.py replacement.

Key changes
-----------
- Acquisition blocks are written by one background writer through a bounded queue.
- Data are persisted immediately as atomic Parquet chunks. The full experiment is
  never accumulated in RAM for saving.
- Only the most recent 1200 samples/channel are retained for the local dashboard.
- Final Parquet consolidation streams row groups and does not concatenate all data
  in memory.
- Queue overload, writer errors, duplicate paths, dashboard startup and shutdown,
  and application shutdown are handled explicitly.

Install
-------
1. Keep the original module as firepydaq/acquisition/acquisition_legacy.py.
2. Save this file as firepydaq/acquisition/acquisition.py.
"""

from __future__ import annotations

import glob
import json
import multiprocessing as mp
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PySide6.QtCore import QTimer

from .acquisition_legacy import application as _LegacyApplication
from .acquisition_legacy import (
    create_dash_app,
    error_logger,
    firepydaq_logger,
)

import ctypes
from .DeviceHealth_Chunks import DeviceHealthManager, ChunkManifestManager
from .device_registry import DeviceRegistry
from .alicat_device import AlicatDevice
from .abstract_device import DeviceState
from ..utilities.firepydaq_path import (get_firepydaq_dir, get_active_run_dir, )
from ..utilities.serial_runtime import SerialDeviceManager
from ..utilities.DAQUtils import AlicatGases
from firepydaq.core.save_manager import (
    DataBlock,
    SaveManager,
)


FIREPYDAQ_DIR = get_firepydaq_dir()

DASHBOARD_BUFFER_SAMPLES = 1200
WRITER_QUEUE_BLOCKS = 32
WRITER_STOP_TIMEOUT_S = 30.0
PARQUET_COMPRESSION = "zstd"

class application(_LegacyApplication):
    def _write_device_health_from_registry(self):
        """Persist every registered AbstractDevice from current snapshots."""
        manager = getattr(self, "device_health", None)
        registry = getattr(self, "device_registry", None)
        if manager is None or registry is None:
            return
        write_registry = getattr(manager, "write_registry", None)
        if callable(write_registry):
            write_registry(registry)
        else:
            manager.write()

    """Drop-in legacy GUI subclass with bounded-memory, disk-backed recording."""

    def __init__(self):
        super().__init__()

        import threading

        if not hasattr(self, "vis_lock"):
            self.vis_lock = threading.Lock()

        self._writer: object = None
        self._writer_started = False
        self.elapsed_time_offset = 0.0
        self.save_time_offset = 0.0
        self._finalize_lock = threading.Lock()
        self._writer_messages: queue.Queue = queue.Queue(maxsize=100)
        self._writer_message_timer = QTimer(self)
        self._writer_message_timer.timeout.connect(self._drain_writer_messages)
        self._writer_message_timer.start(250)
        self._dashboard_process = None
        self.queue_warning_75_sent = False
        self.queue_warning_90_sent = False

        # Unified non-blocking device registry. NI remains on its hardware-
        # timed path during this migration; Alicat and streaming serial devices
        # are consumed through immutable snapshots.
        self.device_registry = DeviceRegistry()
        self._serial_workers_started = False

        self.device_health = None
        self.manifest = None
        self.last_device_health_write = 0.0

        from firepydaq.core import (AcquisitionEngine, EngineCallbacks,)

        from firepydaq.core.legacy_save_adapter import (LegacySaveAdapter,)

        self.save_manager = SaveManager(
            active_run_dir_getter=get_active_run_dir,
            firepydaq_dir=FIREPYDAQ_DIR,
            notify=self.notify,
            device_registry=self.device_registry,
            )

        self.engine = AcquisitionEngine(
            device_registry=self.device_registry,
            callbacks=EngineCallbacks(
                notify=self.notify,
                state_changed=self._on_engine_state_changed,
            ),
            save_manager=self.save_manager,
            health_manager=self.device_health,
        )

        self.engine.configure_cycle_scheduler(
            scheduler=QTimer.singleShot,
            callback=self.runpyDAQ,
            delay_ms=1,
        )

        print("Registry:", self.device_registry)

        print("Engine:", self.engine)

    def _on_engine_state_changed(self, state,):
        pass

    def _drain_writer_messages(self) -> None:
        for level, message in self.save_manager.drain_messages():
            self.notify(message, level)

    def _start_safe_writer(self) -> None:
        """Compatibility wrapper; SaveManager owns writer startup."""
        if self.save_manager.active:
            return
        self.save_manager.start(
            self.common_path,
            manifest=self.manifest,
        )
        self._writer = self.save_manager.writer
        self._writer_started = True
        firepydaq_logger.info(
            "Disk-backed Parquet writer started: %s",
            self._writer.chunk_dir,
        )

    def _finalize_safe_writer(self):
        """Compatibility wrapper; SaveManager owns final consolidation."""
        result = self.save_manager.stop()
        self._writer = None
        self._writer_started = False
        return result

    @staticmethod
    def _append_dashboard_buffer(existing, new, max_samples=DASHBOARD_BUFFER_SAMPLES):
        new = np.asarray(new)
        existing = np.asarray(existing)
        if new.ndim == 1:
            combined = np.concatenate((existing.reshape(-1), new.reshape(-1)))

            max_samples = int(max_samples)

            return combined[-max_samples:]

        if existing.ndim != 2 or existing.shape[0] != new.shape[0]:
            combined = new
        else:
            combined = np.concatenate((existing, new), axis=1)

        return combined[:, -max_samples:]

    def _stop_dashboard(self) -> None:
        process = getattr(self, "dash_thread", None) or self._dashboard_process
        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        except Exception as exc:
            firepydaq_logger.warning("Dashboard shutdown failed: %s", exc)
        finally:
            self._dashboard_process = None
            if hasattr(self, "dash_thread"):
                self.dash_thread = None

    def _start_dashboard(self) -> None:
        self._stop_dashboard()
        mp.freeze_support()
        process = mp.Process(
            target=create_dash_app,
            kwargs={"jsonpath": self.json_file},
            name="firepydaq-dashboard",
            daemon=True,
        )
        process.start()
        self._dashboard_process = process
        self.dash_thread = process
        self.notify("Launching Dashboard on http://127.0.0.1:1222", "info")

    def runpyDAQ(self):
        self._ensure_serial_workers()
        self.engine.capture_serial_snapshots()
        # self.engine.collect_snapshots()
        self.engine.update_health()
        cycle = self.engine.read_ni_cycle(
            self.NIDAQ_Device,
            labels=self.labels_to_save,
            datetime_format=self.dt_format,
        )

        if cycle is not None:
            try:
                self.ActualSamplingRate = float(
                    self.NIDAQ_Device.aitask.timing.samp_clk_rate
                )
                self.xdata_new = cycle.acquisition_time
                self.ydata_new = cycle.values
                self.abs_timestamp = list(cycle.absolute_time)

                fill = self.save_manager.queue_fill_ratio
                if self.engine.saving:
                    if fill > 0.90 and not self.queue_warning_90_sent:
                        self.notify(
                            f"Writer queue at {fill:.0%} capacity",
                            "warning",
                        )
                        self.queue_warning_90_sent = True
                    elif fill > 0.75 and not self.queue_warning_75_sent:
                        self.notify(
                            f"Writer queue at {fill:.0%} capacity",
                            "warning",
                        )
                        self.queue_warning_75_sent = True
                    elif fill < 0.50:
                        self.queue_warning_75_sent = False
                        self.queue_warning_90_sent = False

                self.xdata = self._append_dashboard_buffer(self.xdata, self.xdata_new)
                self.ydata = self._append_dashboard_buffer(self.ydata, self.ydata_new)

                if hasattr(self, "data_vis_tab"):
                    if not hasattr(self.data_vis_tab, "dev_edit"):
                        self.data_vis_tab.set_labels(self.config_file)

                    # The visualization worker releases vis_lock after consuming
                    # the arrays. Use a plain Lock because that release can occur
                    # from the worker thread. An RLock is thread-owned and raises
                    # "cannot release un-acquired lock" in that situation.
                    plot_slot_acquired = self.vis_lock.acquire(blocking=False)
                    if plot_slot_acquired:
                        try:
                            if np.asarray(self.ydata).ndim == 1:
                                n = min(len(self.xdata), len(self.ydata))
                                x_plot = np.array(self.xdata[-n:], copy=True)
                                y_plot = np.array(self.ydata[-n:], copy=True)
                            else:
                                selection = self.data_vis_tab.get_curr_selection()
                                selected_y = np.asarray(self.ydata[selection])
                                n = min(len(self.xdata), len(selected_y))
                                x_plot = np.array(self.xdata[-n:], copy=True)
                                y_plot = np.array(selected_y[-n:], copy=True)

                            if n > 0:
                                self.data_vis_tab.set_data_and_plot(x_plot, y_plot)
                            else:
                                self.vis_lock.release()
                        except Exception:
                            # set_data_and_plot did not accept the update, so its
                            # worker cannot release the lock. Release it here.
                            if self.vis_lock.locked():
                                self.vis_lock.release()
                            raise

                block_duration = max(cycle.block_duration_s, 1e-6)
                if cycle.processing_overrun:
                    self.notify(
                        "Data-loss warning: acquisition processing exceeded one hardware block duration.",
                        "warning",
                    )

                if (
                        self.device_health is not None
                        and time.monotonic() -
                        self.last_device_health_write
                        > 5.0
                        ):
                    self.device_health.update_stale_states()
                    self.engine.update_health()
                    self.last_device_health_write = (time.monotonic())

                last_time = float(self.xdata_new[-1])
                if int(last_time // 5) != int((last_time - block_duration) // 5):
                    total = self.NIDAQ_Device.aitask.in_stream.total_samp_per_chan_acquired
                    self.notify(
                        f"Last time entry: {last_time:.2f}, "
                        f"Total samples/chan: {total}, "
                        f"Actual Hz: {self.ActualSamplingRate:.2f}"
                    )
            except Exception:
                if self.device_health is not None:
                    try:
                        self.device_health.error("NI")
                    except Exception:
                        pass
                exc_type, exc_value, exc_traceback = sys.exc_info()
                self.inform_user(f"{exc_type}{exc_value}")
                traceback.print_tb(exc_traceback)

        # if self.engine.acquiring and self.running:
        #     QTimer.singleShot(1, self.runpyDAQ)
        if self.running and self.engine.schedule_next_cycle():
            return
        else:
            self.run_counter = 0
            if self.save_manager.active:
                self._finalize_safe_writer()
            self._stop_serial_workers()
            self.notify("Acquisition stopped.", "info")
            self.acquisition_button.setText("Start Acquisition")
            self.save_button.setEnabled(False)
            self.elapsed_time_offset = 0
            self._stop_dashboard()

    @error_logger("SaveData")
    def save_data(self):
        if self.save_button.isChecked():
            self.save_button.setText("Stop")
            for device in self.device_registry.devices():
                device.start_run()
            self.run_counter = 0
            self.set_up()

            if not self.is_valid_path(self.json_file):
                self.json_file = self.save_dir + self.json_file
            if not self.is_valid_path(self.common_path):
                self.common_path = self.save_dir + self.common_path

            self.notify(f"Engine saving: {self.engine.state.saving}", "info",)

            json_path = Path(self.json_file)
            json_path.parent.mkdir(parents=True, exist_ok=True)

            settings_copy = dict(self.settings)
            configured_devices = []

            devices = settings_copy.get("Devices", {},)

            if hasattr(self, "generic_serial_devices",):
                serial_devices = {}

                for name, runtime in self.generic_serial_devices.items():

                    config = runtime.config

                    serial_devices[name] = {
                        "name": config.name,
                        "port": config.port,
                        "baud_rate": config.baud_rate,
                        "delimiter": config.delimiter,
                        "columns": config.columns,
                        "read_timeout_s": config.read_timeout_s,
                        "save_frequency_hz": config.save_frequency_hz,
                        "encoding": config.encoding,
                        "enabled": config.enabled,
                        "Type": "streaming_serial",
                    }
                configured_devices.extend(list(self.generic_serial_devices.keys()))
                if serial_devices:
                    devices["SerialDevices"] = serial_devices

            settings_copy["Devices"] = devices

            with json_path.open("x", encoding="utf-8",) as outfile:
                json.dump(settings_copy, outfile, indent=4,)

            self.initiate_dataArrays()

            health_dir = (".firepydaq")
            self.device_health = DeviceHealthManager(health_dir)
            engine = getattr(self, "engine", None)
            if engine is not None:
                engine.set_health_manager(self.device_health)
            self.last_device_health_write = 0.0

            # registering device health
            # NI DAQ
            self.device_health.register("NI", "DAQ")

            # Alicats
            if hasattr(self, "mfcs"):
                for mfcname in self.mfcs.keys():
                    self.device_health.register(mfcname, "ALICAT")
                configured_devices.extend(list(self.mfcs.keys()))

            # User-added devices
            if hasattr(self, "devices"):
                for devicename in self.devices.keys():
                    self.device_health.register(devicename, "DEVICE")

            chunk_dir = get_active_run_dir(self.common_path)
            chunk_dir.mkdir(parents=True, exist_ok=True,)

            self.manifest = ChunkManifestManager(chunk_dir, self.settings, self.labels_to_save,)

            self.manifest.write()

            # self._start_safe_writer()
            self.engine.start_save(
                self.common_path,
                manifest=getattr(self, "manifest", None),
            )
            self.save_time_offset = self.elapsed_time_offset
            self.save_begin_time = time.time()
            if hasattr(self, "operator_events"):
                try:
                    self.acquisition_start_monotonic = time.monotonic()
                    self.operator_events.configure_for_current_test()
                    if hasattr(self.panel, "recent_events"):
                        self.panel.recent_events.clear()
                except Exception as exc:
                    self.notify(f"Operator event initialization failed: {exc}", "warning",)
            firepydaq_logger.info("Safe saving initiated")

            if hasattr(self, "NIDAQ_Device"):
                configured_devices.insert(0, "NI")

            self.notify("Configured devices: " + ", ".join(configured_devices), "info",)
            self.notify(f"Saving Data in {self.settings['Test Name']}", "info")

            if self.dashboard:
                self.settings["Data File"] = self.common_path + ".parquet"
                self._start_dashboard()
        else:
            self.save_button.setText("Save")
            self.panel.operator_events.path_label.clear()
            self.panel.recent_events.clear()
            self.notify("Saving stopped", "info")
            self.engine.stop_save()
            self.notify(f"Engine saving: {self.engine.state.saving}", "info",)

            # self._finalize_safe_writer()
            self._stop_dashboard()

    def closeEvent(self, *args, **kwargs):
        self._stop_serial_workers()
        self.running = False
        self._writer_message_timer.stop()
        if self.save_manager.active:
            self._finalize_safe_writer()
        self._stop_dashboard()
        super().closeEvent(*args, **kwargs)

    def _ensure_serial_workers(self):
        if self._serial_workers_started:
            return

        for name, widget in getattr(self, "mfcs", {}).items():
            if self.device_registry.get(name) is None:
                gas = widget.gas_input.currentText()
                gas_code = next(
                    (
                        code
                        for code, label in AlicatGases.items()
                        if label == gas
                    ),
                    gas,
                )
                self.device_registry.register(
                    AlicatDevice(
                        name=name,
                        port=widget.comport_input.currentText(),
                        gas=gas_code,
                        poll_interval_s=0.2,
                        notify=self.notify,
                    )
                )

        for runtime in getattr(self, "generic_serial_devices", {}).values():
            if self.device_registry.get(runtime.name) is None:
                self.device_registry.register(runtime)

        # Start only devices already connected by the Device Manager. A
        # disconnected Alicat is not polled and cannot create stale CSV rows.
        for device in self.device_registry.devices():
            if device.state in (DeviceState.CONNECTED, DeviceState.RUNNING):
                device.start()
        self._serial_workers_started = True

    def _stop_serial_workers(self):
        try:
            self.device_registry.stop_all()
        except Exception as exc:
            firepydaq_logger.warning("Device stop warning: %s", exc)
        finally:
            self._serial_workers_started = False
        self.engine.update_health()

