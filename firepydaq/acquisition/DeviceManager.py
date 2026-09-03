from __future__ import annotations

import csv
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .abstract_device import AbstractDevice, DeviceState

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


@dataclass
class StreamingSerialConfig:
    name: str
    port: str
    baud_rate: int = 9600
    delimiter: str = ","
    columns: str = "value"
    read_timeout_s: float = 0.25
    save_frequency_hz: float = 5.0
    encoding: str = "utf-8"
    enabled: bool = True

    def column_names(self) -> list[str]:
        return [item.strip() for item in self.columns.split(",") if item.strip()]


class StreamingSerialRuntime(AbstractDevice):
    """Non-blocking line-oriented serial reader with independent CSV saving."""

    def __init__(
        self,
        config: StreamingSerialConfig,
        notify: Callable[[str, str], None],
            ) -> None:
        super().__init__(name=config.name, device_type="streaming_serial")
        self.config = config
        self.notify = notify
        self.serial_port = None
        self.connected = False
        self.last_error = ""
        self.last_values: dict[str, object] = {}
        self.last_local_time = ""
        self.read_count = 0
        self.error_count = 0
        self._rx = bytearray()
        self._file = None
        self._writer: Optional[csv.DictWriter] = None
        self._output_path: Optional[Path] = None
        self._elapsed_origin = 0.0
        self._last_save = 0.0
        self._last_fsync = 0.0

    @property
    def recording(self) -> bool:
        return self._file is not None

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path

    def connect(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is required. Run: poetry add pyserial")
        self.disconnect()
        self.serial_port = serial.Serial(
            port=self.config.port,
            baudrate=self.config.baud_rate,
            timeout=0,
        )
        self.connected = True
        self.last_error = ""
        self._set_state(DeviceState.CONNECTED)
        self.notify(
            f"{self.config.name} connected on {self.config.port}",
            "success",
        )

    def disconnect(self) -> None:
        self.stop_recording()
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
        self.serial_port = None
        self.connected = False
        self._set_state(DeviceState.DISCONNECTED)
        was_connected = self.connected
        if was_connected:
            self.notify(
                f"{self.config.name} disconnected",
                "warning",
            )

    def start(self) -> None:
        snapshot = self.snapshot()
        if self.connected:
            if snapshot.has_value:
                self._set_state(DeviceState.CONNECTED)
            else:
                self._set_state(DeviceState.DISCONNECTED)

    def _pause_snapshot(self):
        with self._snapshot_lock:
            self._state = DeviceState.CONNECTED

    def stop(self) -> None:
        if self.connected:
            self._pause_snapshot()

    def snapshot(self):
        return super().snapshot()

    def settings_to_dict(self) -> dict[str, object]:
        return {
            "Type": self.device_type,
            "name": self.config.name,
            "port": self.config.port,
            "baud_rate": self.config.baud_rate,
            "delimiter": self.config.delimiter,
            "columns": self.config.columns,
            "read_timeout_s": self.config.read_timeout_s,
            "save_frequency_hz": self.config.save_frequency_hz,
            "encoding": self.config.encoding,
            "enabled": self.config.enabled,
        }

    def poll(self) -> list[dict[str, object]]:
        if not self.connected or self.serial_port is None:
            return []

        rows: list[dict[str, object]] = []
        try:
            waiting = int(getattr(self.serial_port, "in_waiting", 0))
            if waiting:
                self._rx.extend(self.serial_port.read(waiting))

            while b"\n" in self._rx:
                raw, _, remainder = self._rx.partition(b"\n")
                self._rx = bytearray(remainder)
                raw = raw.rstrip(b"\r")
                if not raw:
                    continue

                text = raw.decode(
                    self.config.encoding,
                    errors="replace",
                ).strip()
                parsed = self._parse(text)
                self.last_values = parsed
                self.last_local_time = (
                    datetime.now().astimezone().isoformat(timespec="milliseconds")
                )
                self.read_count += 1
                self._publish(parsed)
                self._save_if_due(parsed)
                rows.append(parsed)

        except Exception as exc:
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._set_error(exc)
            self.connected = False

        return rows

    def start_recording(self, output_prefix: Path, elapsed_origin: float) -> Path:
        if self.recording and self._output_path is not None:
            return self._output_path

        prefix = Path(output_prefix)
        path = prefix.with_name(
            f"{prefix.name}_{_safe_name(self.config.name)}_serial.csv"
        )
        if path.exists():
            raise FileExistsError(f"Serial output already exists: {path}")

        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("x", newline="", encoding="utf-8")
        fields = ["LocalTime", "ElapsedTime", *self.config.column_names()]
        self._writer = csv.DictWriter(self._file, fieldnames=fields)
        self._writer.writeheader()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._output_path = path
        self._elapsed_origin = elapsed_origin
        self._last_save = 0.0
        self._last_fsync = time.monotonic()
        return path

    def stop_recording(self) -> Optional[Path]:
        if self._file is None:
            return None

        path = self._output_path
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
        finally:
            self._file.close()
            self._file = None
            self._writer = None

        if path is not None:
            self.notify(
                f"Serial CSV: {_relative_data_path(path)}",
                "success",
            )
        return path

    def _parse(self, text: str) -> dict[str, object]:
        columns = self.config.column_names()
        values = [part.strip() for part in text.split(self.config.delimiter)]
        if len(values) != len(columns):
            raise ValueError(
                f"Expected {len(columns)} fields but received "
                f"{len(values)}: {text!r}"
            )

        result: dict[str, object] = {}
        for key, value in zip(columns, values):
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
        return result

    def _save_if_due(self, values: dict[str, object]) -> None:
        if self._writer is None or self._file is None:
            return

        now = time.monotonic()
        period = 1.0 / max(self.config.save_frequency_hz, 0.001)
        if now - self._last_save < period:
            return

        row = {
            "LocalTime": datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            "ElapsedTime": f"{max(0.0, now - self._elapsed_origin):.6f}",
            **values,
        }
        self._writer.writerow(row)
        self.register_saved_samples(1)
        self._file.flush()
        if now - self._last_fsync >= 5.0:
            os.fsync(self._file.fileno())
            self._last_fsync = now
        self._last_save = now


def _safe_name(value: str) -> str:
    return (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
        or "serial_device"
    )


def _relative_data_path(path: Path) -> str:
    for index, part in enumerate(path.parts):
        normalized = part.lower().replace("_", "").replace("-", "")
        if "experimentdata" in normalized or "calibrationdata" in normalized:
            remainder = path.parts[index + 1 :]
            return str(Path(*remainder)) if remainder else path.name
    return str(Path(path.parent.name) / path.name)


class StreamingSerialEditor(QDialog):
    def __init__(
        self,
        parent=None,
        config: Optional[StreamingSerialConfig] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Streaming Serial Device")
        self.setMinimumWidth(440)
        cfg = config or StreamingSerialConfig(name="SerialDevice", port="")

        self.name_input = QLineEdit(cfg.name)
        self.port_input = QComboBox()
        self.port_input.setEditable(True)
        if list_ports is not None:
            self.port_input.addItems([port.device for port in list_ports.comports()])
        self.port_input.setCurrentText(cfg.port)

        self.baud_input = QComboBox()
        self.baud_input.setEditable(True)
        self.baud_input.addItems(
            ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"]
        )
        self.baud_input.setCurrentText(str(cfg.baud_rate))
        self.delimiter_input = QLineEdit(cfg.delimiter)
        self.columns_input = QLineEdit(cfg.columns)
        self.columns_input.setPlaceholderText("temperature,humidity,pressure")
        self.timeout_input = QDoubleSpinBox()
        self.timeout_input.setRange(0.01, 10.0)
        self.timeout_input.setValue(cfg.read_timeout_s)
        self.timeout_input.setSuffix(" s")
        self.save_rate_input = QDoubleSpinBox()
        self.save_rate_input.setRange(0.01, 1000.0)
        self.save_rate_input.setValue(cfg.save_frequency_hz)
        self.save_rate_input.setSuffix(" Hz")
        self.enabled_input = QCheckBox()
        self.enabled_input.setChecked(cfg.enabled)

        form = QFormLayout()
        form.addRow("Device name", self.name_input)
        form.addRow("COM port", self.port_input)
        form.addRow("Baud rate", self.baud_input)
        form.addRow("Delimiter", self.delimiter_input)
        form.addRow("Column names", self.columns_input)
        form.addRow("Read timeout", self.timeout_input)
        form.addRow("Save frequency", self.save_rate_input)
        form.addRow("Enabled", self.enabled_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def get_config(self) -> StreamingSerialConfig:
        name = self.name_input.text().strip()
        port = self.port_input.currentText().strip()
        columns = self.columns_input.text().strip()
        if not name or not port:
            raise ValueError("Device name and COM port are required.")
        if not [item for item in columns.split(",") if item.strip()]:
            raise ValueError("At least one column name is required.")
        return StreamingSerialConfig(
            name=name,
            port=port,
            baud_rate=int(self.baud_input.currentText()),
            delimiter=self.delimiter_input.text(),
            columns=columns,
            read_timeout_s=self.timeout_input.value(),
            save_frequency_hz=self.save_rate_input.value(),
            enabled=self.enabled_input.isChecked(),
        )


class AlicatEditor(QDialog):
    def __init__(self, device, registry=None, parent=None) -> None:
        super().__init__(parent)
        self.device = device
        self.registry = registry
        self.setWindowTitle(f"Alicat MFC - {device.dev_id}")

        self.port_input = QComboBox()
        self.port_input.setEditable(True)
        self.port_input.addItems(
            [device.comport_input.itemText(i) for i in range(device.comport_input.count())]
        )
        self.port_input.setCurrentText(device.comport_input.currentText())
        self.gas_input = QComboBox()
        self.gas_input.addItems(
            [device.gas_input.itemText(i) for i in range(device.gas_input.count())]
        )
        self.gas_input.setCurrentText(device.gas_input.currentText())
        self.flow_input = QLineEdit(device.dil_rate_input.text())

        form = QFormLayout()
        form.addRow("COM port", self.port_input)
        form.addRow("Gas", self.gas_input)
        form.addRow("Flow setpoint", self.flow_input)

        connect_button = QPushButton("Connect / Disconnect")
        set_button = QPushButton("Set flow")
        stop_button = QPushButton("Stop flow")
        connect_button.clicked.connect(self._toggle_connection)
        set_button.clicked.connect(self._set_flow)
        stop_button.clicked.connect(device.stop_flow_rate)
        controls = QHBoxLayout()
        controls.addWidget(connect_button)
        controls.addWidget(set_button)
        controls.addWidget(stop_button)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(controls)
        layout.addWidget(close)

    def _runtime(self):
        return self.registry.get(self.device.dev_id) if self.registry else None

    def _sync(self) -> None:
        self.device.comport_input.setCurrentText(self.port_input.currentText())
        self.device.gas_input.setCurrentText(self.gas_input.currentText())
        self.device.dil_rate_input.setText(self.flow_input.text())

    def _toggle_connection(self) -> None:
        self._sync()
        runtime = self._runtime()
        if runtime is not None:
            runtime.stop()
        self.device.mfc_connection_btn.toggle()
        self.device.establish_connection()
        if runtime is not None and self.device.mfc_connection_btn.isChecked():
            runtime.start()

    def _set_flow(self) -> None:
        self._sync()
        self.device.set_flow_rate()


class DeviceManagerDialog(QDialog):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Device Manager")
        self.resize(860, 480)
        if not hasattr(app, "generic_serial_devices"):
            app.generic_serial_devices = {}

        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(
            [
                "Enabled",
                "Name",
                "Type",
                "Connection",
                "Reads",
                "Run Reads",
                "Read Hz",
                "Saved Samples",
                "Save Hz",
                "Last Read",
                "Last Save",
                "Last Error",
            ]
        )
        header = self.table.horizontalHeader()
        for column in range(11):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeToContents,
            )
        header.setSectionResizeMode(11, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit_selected)

        buttons = QHBoxLayout()
        for text, slot in (
            ("Add streaming serial", self.add_serial),
            ("Edit / control", self.edit_selected),
            ("Remove", self.remove_selected),
            ("Connect", self.connect_selected),
            ("Disconnect", self.disconnect_selected),
            ("Refresh", self.refresh),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Double-click a device to edit or control it."))
        layout.addWidget(self.table)
        layout.addLayout(buttons)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)
        self.refresh()

    def _rows(self) -> list[tuple[str, str, object]]:
        rows = [
            (name, "Alicat MFC", device)
            for name, device in getattr(self.app, "mfcs", {}).items()
        ]
        rows.extend(
            (name, "Streaming Serial", runtime)
            for name, runtime in self.app.generic_serial_devices.items()
        )
        return rows

    def _selected(self) -> Optional[tuple[str, str, object]]:
        index = self.table.currentRow()
        rows = self._rows()
        return rows[index] if 0 <= index < len(rows) else None

    def _registry(self):
        return getattr(self.app, "device_registry", None)

    def refresh(self) -> None:
        rows = self._rows()
        self.table.setRowCount(len(rows))

        for row_index, (name, device_type, device) in enumerate(rows):
            if device_type == "Alicat MFC":
                registry = self._registry()
                runtime = registry.get(name) if registry is not None else None
                snapshot = runtime.snapshot() if runtime is not None else None
                enabled = True
            else:
                snapshot = device.snapshot()
                enabled = device.config.enabled

            if snapshot is None:
                values = (
                    "Yes" if enabled else "No",
                    name,
                    device_type,
                    DeviceState.DISCONNECTED.value,
                    "0",
                    "0",
                    "0.000",
                    "0",
                    "0.000",
                    "-",
                    "-",
                    "",
                )
            else:
                read_age = snapshot.age_seconds()
                last_read = (
                    "-" if read_age is None else f"{read_age:.1f} s ago"
                )
                last_save = (
                    "-"
                    if snapshot.last_save_local is None
                    else snapshot.last_save_local[11:19]
                )
                values = (
                    "Yes" if enabled else "No",
                    name,
                    device_type,
                    snapshot.state.value,
                    str(snapshot.sequence),
                    str(snapshot.run_sequence),
                    f"{snapshot.read_frequency_hz:.3f}",
                    str(snapshot.saved_samples_this_run),
                    f"{snapshot.sample_save_frequency_hz:.3f}",
                    last_read,
                    last_save,
                    snapshot.error or "",
                )

            for column_index, value in enumerate(values):
                self.table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(value),
                )

    def add_serial(self) -> None:
        dialog = StreamingSerialEditor(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            config = dialog.get_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid device", str(exc))
            return
        if config.name in self.app.generic_serial_devices:
            QMessageBox.warning(self, "Duplicate name", "Device name already exists.")
            return
        runtime = StreamingSerialRuntime(config, self.app.notify)
        self.app.generic_serial_devices[config.name] = runtime
        if self._registry() is not None:
            self._registry().register(runtime)
        self.refresh()

    def edit_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, kind, device = selected
        if kind == "Alicat MFC":
            AlicatEditor(device, self._registry(), self).exec()
            return

        dialog = StreamingSerialEditor(self, device.config)
        if dialog.exec() != QDialog.Accepted:
            return
        config = dialog.get_config()
        was_connected = device.connected
        device.disconnect()
        registry = self._registry()
        if registry is not None and registry.get(name) is device:
            registry.unregister(name, disconnect=False)
        device.config = config
        device.name = config.name
        if config.name != name:
            del self.app.generic_serial_devices[name]
            self.app.generic_serial_devices[config.name] = device
        if registry is not None:
            registry.register(device)
        if was_connected and config.enabled:
            device.connect()
            device.start()
        self.refresh()

    def remove_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, kind, device = selected
        if kind != "Streaming Serial":
            QMessageBox.information(
                self,
                "Existing device",
                "Use the existing Remove Devices command for Alicat removal.",
            )
            return
        registry = self._registry()
        if registry is not None and registry.get(name) is device:
            registry.unregister(name, disconnect=True)
        else:
            device.disconnect()
        del self.app.generic_serial_devices[name]
        self.refresh()

    def connect_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, kind, device = selected
        try:
            if kind == "Streaming Serial":
                device.connect()
                device.start()
            else:
                runtime = self._registry().get(name) if self._registry() else None
                if runtime is not None:
                    runtime.stop()
                if not device.mfc_connection_btn.isChecked():
                    device.mfc_connection_btn.setChecked(True)
                    device.establish_connection()
                if runtime is not None:
                    runtime.start()
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
        self.refresh()

    def disconnect_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, kind, device = selected
        try:
            if kind == "Streaming Serial":
                device.disconnect()
            else:
                runtime = self._registry().get(name) if self._registry() else None
                if runtime is not None:
                    runtime.stop()
                if device.mfc_connection_btn.isChecked():
                    device.mfc_connection_btn.setChecked(False)
                    device.establish_connection()
        except Exception as exc:
            QMessageBox.critical(self, "Disconnection failed", str(exc))
        self.refresh()

    def _tick(self) -> None:
        saving = (hasattr(self.app, "engine") and self.app.engine.saving)
        prefix = getattr(self.app, "common_path", None)
        save_begin = getattr(self.app, "save_begin_time", time.time())
        elapsed_origin = time.monotonic() - max(0.0, time.time() - save_begin)

        for runtime in tuple(self.app.generic_serial_devices.values()):
            if runtime.connected:
                runtime.poll()
            if (
                saving
                and prefix
                and runtime.connected
                and runtime.config.enabled
                and not runtime.recording
            ):
                try:
                    runtime.start_recording(Path(prefix), elapsed_origin)
                except Exception as exc:
                    runtime.last_error = f"{type(exc).__name__}: {exc}"
            elif not saving and runtime.recording:
                runtime.stop_recording()

        if self.isVisible():
            self.refresh()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()


def install_device_manager(app) -> None:
    if not hasattr(app, "generic_serial_devices"):
        app.generic_serial_devices = {}
    if not hasattr(app, "device_manager_dialog"):
        app.device_manager_dialog = DeviceManagerDialog(app)
    app.device_manager_dialog.show()
    app.device_manager_dialog.raise_()
    app.device_manager_dialog.activateWindow()
