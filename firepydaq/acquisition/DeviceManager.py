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

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


@dataclass
class StreamingSerialConfig:
    """Persistent configuration for one line-oriented serial stream."""

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
        return [
            item.strip()
            for item in self.columns.split(",")
            if item.strip()
        ]


class StreamingSerialRuntime:
    """Read and record one streaming serial device without blocking NI reads."""

    def __init__(
        self,
        config: StreamingSerialConfig,
        notify: Callable[[str, str], None],
    ) -> None:
        self.config = config
        self.notify = notify

        self.serial_port = None
        self.connected = False
        self.last_error = ""
        self.last_values: dict[str, object] = {}
        self.last_local_time = ""
        self.read_count = 0
        self.error_count = 0

        self._receive_buffer = bytearray()
        self._output_file = None
        self._csv_writer: Optional[csv.DictWriter] = None
        self._output_path: Optional[Path] = None
        self._elapsed_origin = 0.0
        self._last_save = 0.0
        self._last_fsync = 0.0

    @property
    def recording(self) -> bool:
        return self._output_file is not None

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path

    def connect(self) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is required. Run: poetry add pyserial"
            )

        self.disconnect()
        self.serial_port = serial.Serial(
            port=self.config.port,
            baudrate=self.config.baud_rate,
            timeout=0,
        )
        self.connected = True
        self.last_error = ""
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

    def poll(self) -> list[dict[str, object]]:
        if not self.connected or self.serial_port is None:
            return []

        rows: list[dict[str, object]] = []
        try:
            bytes_waiting = int(
                getattr(self.serial_port, "in_waiting", 0)
            )
            if bytes_waiting:
                self._receive_buffer.extend(
                    self.serial_port.read(bytes_waiting)
                )

            while b"\n" in self._receive_buffer:
                raw_line, _, remainder = self._receive_buffer.partition(b"\n")
                self._receive_buffer = bytearray(remainder)
                raw_line = raw_line.rstrip(b"\r")
                if not raw_line:
                    continue

                text = raw_line.decode(
                    self.config.encoding,
                    errors="replace",
                ).strip()
                parsed = self._parse(text)

                self.last_values = parsed
                self.last_local_time = (
                    datetime.now()
                    .astimezone()
                    .isoformat(timespec="milliseconds")
                )
                self.read_count += 1
                rows.append(parsed)
                self._save_if_due(parsed)

        except Exception as exc:
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.connected = False

        return rows

    def start_recording(
        self,
        output_prefix: Path,
        elapsed_origin: float,
    ) -> Optional[Path]:
        if self.recording:
            return self._output_path

        safe_device_name = _safe_name(self.config.name)
        prefix = Path(output_prefix)
        output_path = prefix.with_name(
            f"{prefix.name}_{safe_device_name}_serial.csv"
        )

        if output_path.exists():
            raise FileExistsError(
                f"Serial output already exists: {output_path}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_file = output_path.open(
            "x",
            newline="",
            encoding="utf-8",
        )

        fieldnames = [
            "LocalTime",
            "ElapsedTime",
            *self.config.column_names(),
        ]
        writer = csv.DictWriter(
            output_file,
            fieldnames=fieldnames,
        )
        writer.writeheader()
        output_file.flush()
        os.fsync(output_file.fileno())

        self._output_file = output_file
        self._csv_writer = writer
        self._output_path = output_path
        self._elapsed_origin = elapsed_origin
        self._last_save = 0.0
        self._last_fsync = time.monotonic()
        return output_path

    def stop_recording(self) -> Optional[Path]:
        """Close an active CSV and announce its final location exactly once."""
        if self._output_file is None:
            return None

        output_path = self._output_path
        try:
            self._output_file.flush()
            os.fsync(self._output_file.fileno())
        finally:
            self._output_file.close()
            self._output_file = None
            self._csv_writer = None

        if output_path is not None:
            self.notify(
                f"Serial data saved: {_relative_data_path(output_path)}",
                "success",
            )

        return output_path

    def _parse(self, text: str) -> dict[str, object]:
        columns = self.config.column_names()
        values = [
            part.strip()
            for part in text.split(self.config.delimiter)
        ]

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
        if self._csv_writer is None or self._output_file is None:
            return

        now = time.monotonic()
        save_period = 1.0 / max(
            self.config.save_frequency_hz,
            0.001,
        )
        if now - self._last_save < save_period:
            return

        row = {
            "LocalTime": (
                datetime.now()
                .astimezone()
                .isoformat(timespec="milliseconds")
            ),
            "ElapsedTime": (
                f"{max(0.0, now - self._elapsed_origin):.6f}"
            ),
        }
        row.update(values)
        self._csv_writer.writerow(row)
        self._output_file.flush()

        if now - self._last_fsync >= 5.0:
            os.fsync(self._output_file.fileno())
            self._last_fsync = now

        self._last_save = now


def _safe_name(value: str) -> str:
    cleaned = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        str(value),
    ).strip("._")
    return cleaned or "serial_device"


def _relative_data_path(path: Path) -> str:
    """Return a path relative to ExperimentData or CalibrationData."""
    parts = path.parts
    for index, part in enumerate(parts):
        normalized = part.lower().replace("_", "").replace("-", "")
        if (
            "experimentdata" in normalized
            or "calibrationdata" in normalized
        ):
            remainder = parts[index + 1 :]
            return str(Path(*remainder)) if remainder else path.name

    return str(Path(path.parent.name) / path.name)


class StreamingSerialEditor(QDialog):
    """Configuration dialog for one streaming serial device."""

    def __init__(
        self,
        parent=None,
        config: Optional[StreamingSerialConfig] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Streaming Serial Device")
        self.setMinimumWidth(440)

        current = config or StreamingSerialConfig(
            name="SerialDevice",
            port="",
        )

        self.name_input = QLineEdit(current.name)

        self.port_input = QComboBox()
        self.port_input.setEditable(True)
        available_ports = (
            [port.device for port in list_ports.comports()]
            if list_ports is not None
            else []
        )
        self.port_input.addItems(available_ports)
        self.port_input.setCurrentText(current.port)

        self.baud_input = QComboBox()
        self.baud_input.setEditable(True)
        self.baud_input.addItems(
            [
                "1200",
                "2400",
                "4800",
                "9600",
                "19200",
                "38400",
                "57600",
                "115200",
                "230400",
            ]
        )
        self.baud_input.setCurrentText(str(current.baud_rate))

        self.delimiter_input = QLineEdit(current.delimiter)
        self.columns_input = QLineEdit(current.columns)
        self.columns_input.setPlaceholderText(
            "temperature,humidity,pressure"
        )

        self.timeout_input = QDoubleSpinBox()
        self.timeout_input.setRange(0.01, 10.0)
        self.timeout_input.setDecimals(2)
        self.timeout_input.setValue(current.read_timeout_s)
        self.timeout_input.setSuffix(" s")

        self.save_rate_input = QDoubleSpinBox()
        self.save_rate_input.setRange(0.01, 1000.0)
        self.save_rate_input.setDecimals(2)
        self.save_rate_input.setValue(current.save_frequency_hz)
        self.save_rate_input.setSuffix(" Hz")

        self.enabled_input = QCheckBox()
        self.enabled_input.setChecked(current.enabled)

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
            encoding="utf-8",
            enabled=self.enabled_input.isChecked(),
        )


class AlicatEditor(QDialog):
    """Compact editor backed by the existing Alicat MFC widget."""

    def __init__(self, device, parent=None) -> None:
        super().__init__(parent)
        self.device = device
        self.setWindowTitle(f"Alicat MFC - {device.dev_id}")
        self.setMinimumWidth(420)

        self.port_input = QComboBox()
        self.port_input.setEditable(True)
        self.port_input.addItems(
            [
                device.comport_input.itemText(index)
                for index in range(device.comport_input.count())
            ]
        )
        self.port_input.setCurrentText(
            device.comport_input.currentText()
        )

        self.gas_input = QComboBox()
        self.gas_input.addItems(
            [
                device.gas_input.itemText(index)
                for index in range(device.gas_input.count())
            ]
        )
        self.gas_input.setCurrentText(device.gas_input.currentText())

        self.flow_input = QLineEdit(device.dil_rate_input.text())

        form = QFormLayout()
        form.addRow("COM port", self.port_input)
        form.addRow("Gas", self.gas_input)
        form.addRow("Flow setpoint", self.flow_input)

        connect_button = QPushButton("Connect / Disconnect")
        set_flow_button = QPushButton("Set flow")
        stop_flow_button = QPushButton("Stop flow")

        connect_button.clicked.connect(self._toggle_connection)
        set_flow_button.clicked.connect(self._set_flow)
        stop_flow_button.clicked.connect(device.stop_flow_rate)

        controls = QHBoxLayout()
        controls.addWidget(connect_button)
        controls.addWidget(set_flow_button)
        controls.addWidget(stop_flow_button)

        close_buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(controls)
        layout.addWidget(close_buttons)

    def _synchronize(self) -> None:
        self.device.comport_input.setCurrentText(
            self.port_input.currentText()
        )
        self.device.gas_input.setCurrentText(
            self.gas_input.currentText()
        )
        self.device.dil_rate_input.setText(self.flow_input.text())

    def _toggle_connection(self) -> None:
        self._synchronize()
        self.device.mfc_connection_btn.toggle()
        self.device.establish_connection()

    def _set_flow(self) -> None:
        self._synchronize()
        self.device.set_flow_rate()


class DeviceManagerDialog(QDialog):
    """Central management UI for Alicat and streaming serial devices."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Device Manager")
        self.resize(860, 480)

        if not hasattr(self.app, "generic_serial_devices"):
            self.app.generic_serial_devices = {}

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            [
                "Enabled",
                "Name",
                "Type",
                "Connection",
                "Reads",
                "Last error",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit_selected)

        add_button = QPushButton("Add streaming serial")
        edit_button = QPushButton("Edit / control")
        remove_button = QPushButton("Remove")
        connect_button = QPushButton("Connect")
        disconnect_button = QPushButton("Disconnect")
        refresh_button = QPushButton("Refresh")

        add_button.clicked.connect(self.add_serial)
        edit_button.clicked.connect(self.edit_selected)
        remove_button.clicked.connect(self.remove_selected)
        connect_button.clicked.connect(self.connect_selected)
        disconnect_button.clicked.connect(self.disconnect_selected)
        refresh_button.clicked.connect(self.refresh)

        controls = QHBoxLayout()
        for button in (
            add_button,
            edit_button,
            remove_button,
            connect_button,
            disconnect_button,
            refresh_button,
        ):
            controls.addWidget(button)
        controls.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Double-click a device to open its configuration and controls."
            )
        )
        layout.addWidget(self.table)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)
        self.refresh()

    def _device_rows(self) -> list[tuple[str, str, object]]:
        rows: list[tuple[str, str, object]] = []
        rows.extend(
            (name, "Alicat MFC", device)
            for name, device in getattr(self.app, "mfcs", {}).items()
        )
        rows.extend(
            (name, "Streaming Serial", runtime)
            for name, runtime in self.app.generic_serial_devices.items()
        )
        return rows

    def refresh(self) -> None:
        rows = self._device_rows()
        self.table.setRowCount(len(rows))

        for row_index, (name, device_type, device) in enumerate(rows):
            is_alicat = device_type == "Alicat MFC"
            enabled = True if is_alicat else device.config.enabled
            connected = (
                hasattr(device, "loop")
                if is_alicat
                else device.connected
            )
            reads = "" if is_alicat else str(device.read_count)
            error = "" if is_alicat else device.last_error

            values = (
                "Yes" if enabled else "No",
                name,
                device_type,
                "Connected" if connected else "Disconnected",
                reads,
                error,
            )
            for column_index, value in enumerate(values):
                self.table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(value),
                )

    def _selected(self) -> Optional[tuple[str, str, object]]:
        row = self.table.currentRow()
        rows = self._device_rows()
        if row < 0 or row >= len(rows):
            return None
        return rows[row]

    def add_serial(self) -> None:
        dialog = StreamingSerialEditor(self)
        if dialog.exec() != QDialog.Accepted:
            return

        try:
            config = dialog.get_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid device", str(exc))
            return

        if (
            config.name in self.app.generic_serial_devices
            or config.name in getattr(self.app, "device_arr", {})
        ):
            QMessageBox.warning(
                self,
                "Duplicate name",
                f"A device named {config.name!r} already exists.",
            )
            return

        self.app.generic_serial_devices[config.name] = (
            StreamingSerialRuntime(config, self.app.notify)
        )
        self.refresh()

    def edit_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return

        name, device_type, device = selected
        if device_type == "Alicat MFC":
            AlicatEditor(device, self).exec()
            return

        dialog = StreamingSerialEditor(self, device.config)
        if dialog.exec() != QDialog.Accepted:
            return

        try:
            config = dialog.get_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid device", str(exc))
            return

        was_connected = device.connected
        device.disconnect()
        device.config = config

        if config.name != name:
            del self.app.generic_serial_devices[name]
            self.app.generic_serial_devices[config.name] = device

        if was_connected and config.enabled:
            try:
                device.connect()
            except Exception as exc:
                device.last_error = f"{type(exc).__name__}: {exc}"

        self.refresh()

    def remove_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return

        name, device_type, device = selected
        if device_type != "Streaming Serial":
            QMessageBox.information(
                self,
                "Existing device",
                "Use the existing Remove Devices command for Alicat removal.",
            )
            return

        device.disconnect()
        del self.app.generic_serial_devices[name]
        self.refresh()

    def connect_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return

        _name, device_type, device = selected
        try:
            if device_type == "Streaming Serial":
                device.connect()
            elif not device.mfc_connection_btn.isChecked():
                device.mfc_connection_btn.setChecked(True)
                device.establish_connection()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Connection failed",
                str(exc),
            )
        self.refresh()

    def disconnect_selected(self) -> None:
        selected = self._selected()
        if selected is None:
            return

        _name, device_type, device = selected
        if device_type == "Streaming Serial":
            device.disconnect()
        elif device.mfc_connection_btn.isChecked():
            device.mfc_connection_btn.setChecked(False)
            device.establish_connection()
        self.refresh()

    def _tick(self) -> None:
        saving = bool(getattr(self.app, "save_bool", False))
        output_prefix = getattr(self.app, "common_path", None)
        save_begin_time = getattr(
            self.app,
            "save_begin_time",
            time.time(),
        )
        elapsed_origin = time.monotonic() - max(
            0.0,
            time.time() - save_begin_time,
        )

        for runtime in tuple(self.app.generic_serial_devices.values()):
            if runtime.connected:
                runtime.poll()

            if (
                saving
                and output_prefix
                and runtime.connected
                and runtime.config.enabled
                and not runtime.recording
            ):
                try:
                    runtime.start_recording(
                        Path(output_prefix),
                        elapsed_origin,
                    )
                except Exception as exc:
                    runtime.last_error = f"{type(exc).__name__}: {exc}"

            elif not saving and runtime.recording:
                # stop_recording() performs the final flush/fsync and posts the
                # relative output path to the application's System Log.
                runtime.stop_recording()

        if self.isVisible():
            self.refresh()

    def closeEvent(self, event) -> None:
        # Keep the manager and timer alive while hidden. The timer owns generic
        # streaming reads and save-state synchronization.
        event.ignore()
        self.hide()


def install_device_manager(app) -> None:
    """Install and show one reusable Device Manager dialog."""
    if not hasattr(app, "generic_serial_devices"):
        app.generic_serial_devices = {}

    if not hasattr(app, "device_manager_dialog"):
        app.device_manager_dialog = DeviceManagerDialog(app)

    app.device_manager_dialog.show()
    app.device_manager_dialog.raise_()
    app.device_manager_dialog.activateWindow()
