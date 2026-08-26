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
from ..utilities.serial_csv_writer import SerialCsvWriter
from ..utilities.DAQUtils import AlicatGases


FIREPYDAQ_DIR = get_firepydaq_dir()

DASHBOARD_BUFFER_SAMPLES = 1200
WRITER_QUEUE_BLOCKS = 32
WRITER_STOP_TIMEOUT_S = 30.0
PARQUET_COMPRESSION = "zstd"

@dataclass(frozen=True)
class _DataBlock:
    elapsed_s: np.ndarray
    absolute_time: tuple[str, ...]
    values: np.ndarray
    labels: tuple[str, ...]


class _ChunkWriter:
    """Single background writer with a bounded in-memory handoff queue.

    The queue contains at most WRITER_QUEUE_BLOCKS acquisition blocks. Each block
    is removed from the queue and written to disk immediately. Atomic rename keeps
    the dashboard from opening a partially written chunk.
    """

    def __init__(self, chunk_dir: Path, messages: queue.Queue):
        self.chunk_dir = chunk_dir
        self.messages = messages
        self.items: queue.Queue = queue.Queue(maxsize=WRITER_QUEUE_BLOCKS)
        self.stop_token = object()
        self.thread: Optional[threading.Thread] = None
        self.count = 0
        self.running = False
        self.failed = False

    def start(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.chunk_dir.rglob("chunk_*.parquet"))
        if existing:
            raise FileExistsError(
                f"Chunk directory is not empty: {self.chunk_dir}. "
                "Use a new test name or recover/remove the existing chunks."
            )
        self.running = True
        self.thread = threading.Thread(
            target=self._run,
            name="firepydaq-parquet-writer",
            daemon=False,
        )
        self.thread.start()

    def put(self, block: _DataBlock) -> bool:
        if not self.running or self.failed:
            return False
        try:
            self.items.put_nowait(block)
            return True
        except queue.Full:
            self._message("error", "Writer queue is full; stopping acquisition to prevent silent data loss.")
            return False

    def stop(self, timeout: float = WRITER_STOP_TIMEOUT_S) -> bool:
        if not self.running:
            return True
        try:
            self.items.put(self.stop_token, timeout=timeout)
        except queue.Full:
            self._message("error", "Writer queue did not drain before shutdown.")
            return False

        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self._message("error", "Writer did not stop; chunk files remain recoverable.")
                return False
        self.running = False
        return not self.failed

    def _message(self, level: str, text: str) -> None:
        try:
            self.messages.put_nowait((level, text))
        except queue.Full:
            pass

    def _run(self) -> None:
        try:
            while True:
                item = self.items.get()
                try:
                    if item is self.stop_token:
                        return
                    self._write_block(item)
                finally:
                    self.items.task_done()
        except Exception as exc:
            self.failed = True
            self._message("error", f"Writer failed: {exc}")
            firepydaq_logger.exception("Background writer failed")
        finally:
            self.running = False

    def _write_block(self, block: _DataBlock) -> None:
        values = np.asarray(block.values)
        if values.ndim == 1:
            values = values[np.newaxis, :]

        n = len(block.elapsed_s)
        if len(block.absolute_time) != n or values.shape[1] != n:
            raise ValueError(
                "Acquisition block dimensions disagree: "
                f"time={n}, absolute_time={len(block.absolute_time)}, values={values.shape}."
            )

        arrays = {
            "AbsoluteTime": pa.array(block.absolute_time, type=pa.string()),
            "Time": pa.array(block.elapsed_s, type=pa.float64()),
        }
        for index, label in enumerate(block.labels):
            if index < values.shape[0]:
                arrays[label] = pa.array(values[index], type=pa.float32())

        table = pa.table(arrays)
        final_path = self.chunk_dir / f"chunk_{self.count:08d}.parquet"
        temporary_path = final_path.with_suffix(".parquet.tmp")
        pq.write_table(table, temporary_path, compression=PARQUET_COMPRESSION)
        os.replace(temporary_path, final_path)
        self.count += 1


class application(_LegacyApplication):
    def _write_device_health_from_registry(self):
        """Persist every registered AbstractDevice from current snapshots."""
        manager = getattr(self, "device_health", None)
        registry = getattr(self, "device_registry", None)
        if manager is None or registry is None:
            return
        manager.write_registry(registry)

    """Drop-in legacy GUI subclass with bounded-memory, disk-backed recording."""

    def __init__(self):
        super().__init__()

        import threading

        if not hasattr(self, "vis_lock"):
            self.vis_lock = threading.Lock()

        self._writer: Optional[_ChunkWriter] = None
        self._writer_started = False
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
        self._serial_writer: Optional[SerialCsvWriter] = None
        self._serial_last_saved_sequence: dict[str, int] = {}
        self._serial_elapsed_origin: Optional[float] = None

        self.device_health = None
        self.manifest = None
        self.last_device_health_write = 0.0

    def _drain_writer_messages(self) -> None:
        while True:
            try:
                level, text = self._writer_messages.get_nowait()
            except queue.Empty:
                return
            self.notify(text, level)

    def _start_safe_writer(self) -> None:
        if self._writer_started:
            return

        chunk_dir = chunk_dir = get_active_run_dir(self.common_path)

        chunk_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        writer = _ChunkWriter(chunk_dir, self._writer_messages)
        writer.start()
        self._writer = writer
        self._writer_started = True
        firepydaq_logger.info("Disk-backed Parquet writer started: %s", chunk_dir)

    def _finalize_safe_writer(self) -> None:
        with self._finalize_lock:
            if not self._writer_started or self._writer is None:
                return

            writer = self._writer
            writer_ok = writer.stop()
            self._writer_started = False
            self._drain_writer_messages()

            chunks = sorted(writer.chunk_dir.glob("chunk_*.parquet"))
            if not chunks:
                self.notify("No data chunks were written.", "warning")
                return

            final_path = Path(str(self.common_path) + ".parquet")
            temporary_path = final_path.with_suffix(final_path.suffix + ".tmp")

            if final_path.exists() or temporary_path.exists():
                self.notify(
                    f"Final output already exists; raw chunks remain in {writer.chunk_dir}",
                    "error",
                )
                return

            parquet_writer = None
            rows = 0
            try:
                for chunk in chunks:
                    table = pq.read_table(chunk)
                    if parquet_writer is None:
                        parquet_writer = pq.ParquetWriter(
                            temporary_path,
                            table.schema,
                            compression=PARQUET_COMPRESSION,
                        )
                    parquet_writer.write_table(table)
                    rows += table.num_rows
                if parquet_writer is not None:
                    parquet_writer.close()
                    parquet_writer = None
                os.replace(temporary_path, final_path)

                for device in self.device_registry.devices():
                    device.stop_run()

                csv_path = final_path.with_suffix(".csv")
                try:
                    pq.read_table(final_path).to_pandas().to_csv(
                                       csv_path,
                                       index=False,
                                   )

                    final_rows = pq.ParquetFile(final_path).metadata.num_rows

                    if final_rows != rows:
                        raise RuntimeError(
                            f"Row mismatch. chunks={rows}, final={final_rows}"
                        )
                    if self.manifest is not None:
                        self.manifest.finalize(
                            final_path,
                            csv_path,
                            final_rows,
                        )
                        
                        final_manifest_path = (
                            FIREPYDAQ_DIR
                            / f"{final_path.stem}_manifest.json"
                        )

                        with open(final_manifest_path, "w", encoding="utf-8",) as fp:
                            json.dump(self.manifest.manifest, fp, indent=2,)
                except Exception as exc:
                    self.notify(
                        f"CSV export failed: {exc}",
                        "warning",
                    )

                status = "success" if writer_ok else "warning"

                if writer_ok:
                    shutil.rmtree(writer.chunk_dir, ignore_errors=True,)
                    self.notify(
                        f"Finalized {rows:,} samples to {final_path}; temporary chunks removed.",
                        status,
                    )
                else:
                    self.notify(
                        f"Finalized {rows:,} samples to {final_path}; chunks retained because the writer reported an error.",
                        status,
                    )
            except Exception as exc:
                if parquet_writer is not None:
                    parquet_writer.close()
                temporary_path.unlink(missing_ok=True)
                self.notify(
                    f"Final consolidation failed: {exc}. Raw chunks remain in {writer.chunk_dir}",
                    "error",
                )
                firepydaq_logger.exception("Final Parquet consolidation failed")

    @staticmethod
    def _append_dashboard_buffer(existing, new, max_samples=DASHBOARD_BUFFER_SAMPLES):
        new = np.asarray(new)
        existing = np.asarray(existing)
        if new.ndim == 1:
            combined = np.concatenate((existing.reshape(-1), new.reshape(-1)))
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
        self._snapshot_serial_devices()
        no_samples = self.NIDAQ_Device.numberOfSamples
        self.ActualSamplingRate = self.NIDAQ_Device.aitask.timing.samp_clk_rate
        samples_available = self.NIDAQ_Device.aitask._in_stream.avail_samp_per_chan

        # if self.mfcs != {}:
        #     self.alicat_locks = {}
        #     for mfcname, al_mfc in self.mfcs.items():
        #         lock = threading.Lock()
        #         self.alicat_locks[mfcname] = lock
        #         lock.acquire()
        #         self.all_mfcData[mfcname] = al_mfc.GetFlows()
        #         if self.device_health is not None:
        #             self.device_health.good_read(mfcname)

        if samples_available >= no_samples:
            try:
                read_started = time.perf_counter()
                self.ydata_new = np.asarray(self.NIDAQ_Device.threadaitask())

                if self.device_health is not None:
                    self.device_health.good_read("NI")
                if self.NIDAQ_Device.ao_counter > 0:
                    self.written_data = self.NIDAQ_Device.threadaotask(
                        AO_initials=[0] * self.NIDAQ_Device.ao_counter
                    )

                # if self.mfcs != {}:
                #     for lock in self.alicat_locks.values():
                #         if lock.locked():
                #             lock.release()

                block_duration = no_samples / self.ActualSamplingRate
                previous_time = float(self.xdata[-1]) if len(self.xdata) else 0.0
                if previous_time == 0.0:
                    self.xdata_new = np.arange(no_samples, dtype=np.float64) / self.ActualSamplingRate
                else:
                    self.xdata_new = previous_time + (
                        np.arange(1, no_samples + 1, dtype=np.float64) / self.ActualSamplingRate
                    )

                block_end = datetime.now()
                offsets = np.arange(no_samples - 1, -1, -1, dtype=np.float64) / self.ActualSamplingRate
                self.abs_timestamp = [
                    (block_end - timedelta(seconds=float(offset))).strftime(self.dt_format)
                    for offset in offsets
                ]

                if self.save_bool and self._writer_started and self._writer is not None:
                    block = _DataBlock(
                        elapsed_s=np.array(self.xdata_new, dtype=np.float64, copy=True),
                        absolute_time=tuple(self.abs_timestamp),
                        values=np.array(self.ydata_new, copy=True),
                        labels=tuple(self.labels_to_save),
                    )
                    qsize = self._writer.items.qsize()
                    qmax = self._writer.items.maxsize

                    fill = qsize / qmax

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
                    success = self._writer.put(block)
                    if success:
                        self.NIDAQ_Device.register_saved_samples(len(block.elapsed_s))
                        if self.manifest is not None:
                            self.manifest.update_chunk(len(block.elapsed_s))
                    else:
                        self.save_bool = False
                        self.ContinueAcquisition = False

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

                read_elapsed = time.perf_counter() - read_started
                if read_elapsed > block_duration:
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

                    self._write_device_health_from_registry()

                    self.last_device_health_write = (
                        time.monotonic()
                    )

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
                self.ContinueAcquisition = False
                self.inform_user(f"{exc_type}{exc_value}")
                traceback.print_tb(exc_traceback)

        if self.ContinueAcquisition and self.running:
            QTimer.singleShot(1, self.runpyDAQ)
        else:
            self.run_counter = 0
            if self._writer_started:
                self._finalize_safe_writer()
            self._stop_serial_writer()
            self._stop_serial_workers()
            self.notify("Acquisition stopped.", "info")
            self.acquisition_button.setText("Start Acquisition")
            self.save_button.setEnabled(False)
            self._stop_dashboard()

    @error_logger("SaveData")
    def save_data(self):
        if self.save_button.isChecked():
            self.save_button.setText("Stop")
            self.save_bool = True
            for device in self.device_registry.devices():
                device.start_run()
            self.run_counter = 0
            self.set_up()

            if not self.is_valid_path(self.json_file):
                self.json_file = self.save_dir + self.json_file
            if not self.is_valid_path(self.common_path):
                self.common_path = self.save_dir + self.common_path

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

            with json_path.open(
                "x",
                encoding="utf-8",
            ) as outfile:

                json.dump(
                    settings_copy,
                    outfile,
                    indent=4,
                )

            self.initiate_dataArrays()

            health_dir = (".firepydaq")
            
            self.device_health = DeviceHealthManager(health_dir)
            self.last_device_health_write = 0.0

            ## registering device health
            # NI DAQ
            self.device_health.register(
                "NI",
                "DAQ"
            )

            # Alicats
            if hasattr(self, "mfcs"):
                for mfcname in self.mfcs.keys():
                    self.device_health.register(
                        mfcname,
                        "ALICAT"
                    )
                configured_devices.extend(list(self.mfcs.keys()))

            # User-added devices
            if hasattr(self, "devices"):
                for devicename in self.devices.keys():
                    self.device_health.register(
                        devicename,
                        "DEVICE"
                    )

            chunk_dir = get_active_run_dir(self.common_path)
            chunk_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.manifest = ChunkManifestManager(
                chunk_dir,
                self.settings,
                self.labels_to_save,
            )

            self.manifest.write()

            self._start_safe_writer()
            self._start_serial_writer()
            self.save_begin_time = time.time()
            if hasattr(self, "operator_events"):
                try:
                    self.acquisition_start_monotonic = time.monotonic()
                    self.operator_events.configure_for_current_test()
                    if hasattr(self.panel, "recent_events"):
                        self.panel.recent_events.clear()
                except Exception as exc:
                    self.notify(
                        f"Operator event initialization failed: {exc}",
                        "warning",
                    )
            firepydaq_logger.info("Safe saving initiated")

            if hasattr(self, "NIDAQ_Device"):
                configured_devices.insert(0, "NI")

            self.notify(
                "Configured devices: "
                + ", ".join(configured_devices),
                "info",
            )
            self.notify(f"Saving Data in {self.settings['Test Name']}", "info")

            if self.dashboard:
                self.settings["Data File"] = self.common_path + ".parquet"
                self._start_dashboard()
        else:
            self.save_button.setText("Save")
            self.save_bool = False
            self.panel.operator_events.path_label.clear()
            self.panel.recent_events.clear()
            self.notify("Saving stopped", "info")
            self._finalize_safe_writer()
            self._stop_serial_writer()
            self._stop_dashboard()

    def closeEvent(self, *args, **kwargs):
        self._stop_serial_workers()
        self.running = False
        self.ContinueAcquisition = False
        self.save_bool = False
        self._writer_message_timer.stop()
        if self._writer_started:
            self._finalize_safe_writer()
        self._stop_serial_writer()
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

    def _start_serial_writer(self):
        if self._serial_writer is not None:
            return
        writer = SerialCsvWriter(
            output_prefix=Path(self.common_path),
            messages=self._writer_messages,
        )
        writer.start()
        self._serial_writer = writer
        self._serial_last_saved_sequence = {}
        self._serial_elapsed_origin = time.monotonic()

    def _stop_serial_writer(self):
        writer = self._serial_writer
        if writer is None:
            return
        writer_ok = writer.stop()
        paths = writer.output_paths()
        self._serial_writer = None
        if paths:
            self.notify(
                "Alicat data saved separately: "
                + ", ".join(str(path) for path in paths),
                "info" if writer_ok else "warning",
            )

    def _snapshot_serial_devices(self):
        for name, snapshot in self.device_registry.snapshots().items():
            if (
                snapshot.device_type != "alicat"
                or snapshot.state != DeviceState.RUNNING
                or not snapshot.has_value
                or snapshot.monotonic_time is None
                or snapshot.age_seconds() > 2.0
            ):
                continue

            writer = self._serial_writer
            if not self.save_bool or writer is None:
                continue

            last_sequence = self._serial_last_saved_sequence.get(name, 0)
            if snapshot.sequence <= last_sequence:
                continue

            origin = self._serial_elapsed_origin
            elapsed_s = (
                0.0
                if origin is None
                else max(0.0, snapshot.monotonic_time - origin)
            )
            writer_snapshot = {
                "value": dict(snapshot.values),
                "local_time": snapshot.local_time,
                "monotonic_time": snapshot.monotonic_time,
                "sequence": snapshot.sequence,
                "error": snapshot.error,
            }
            if writer.put(name, writer_snapshot, elapsed_s):
                self._serial_last_saved_sequence[name] = snapshot.sequence
                device = self.device_registry.get(name)
                if device is not None:
                    device.register_saved_samples(1)

    def _stop_serial_workers(self):
        try:
            self.device_registry.stop_all()
        except Exception as exc:
            firepydaq_logger.warning("Device stop warning: %s", exc)
        finally:
            self._serial_workers_started = False
        self._write_device_health_from_registry()

