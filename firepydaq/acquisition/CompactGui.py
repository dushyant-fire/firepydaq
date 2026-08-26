from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QSizePolicy

STATUS_COLORS = {
    "ONLINE": "#16803a", "CONNECTED": "#16803a", "STALE": "#b77900",
    "ERROR": "#c62828", "DISCONNECTED": "#687076", "INITIALIZING": "#687076",
}


class DeviceStatusLabel(QLabel):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("padding: 2px 6px;")

    def refresh(self):
        fragments, details = [], []
        for name, status, detail in collect_devices(self.app):
            color = STATUS_COLORS.get(status, STATUS_COLORS["DISCONNECTED"])
            fragments.append(
                f"<span style='color:{color};font-weight:700'>●</span> {name}: {status.title()}"
            )
            details.append(f"{name}: {status.title()}{' | ' + detail if detail else ''}")
        self.setText("   ".join(fragments) if fragments else "No devices configured")
        self.setToolTip("\n".join(details))


class QueueStatusLabel(QLabel):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMinimumWidth(78)
        self.setAlignment(QtAlignRight)
        self.setStyleSheet("padding: 2px 6px; font-weight: 700;")

    def refresh(self):
        text, level = queue_status(self.app)
        color = level_color(level)
        self.setText(f"Queue: {text}")
        self.setStyleSheet(f"padding: 2px 6px; font-weight: 700; color: {color};")


class DataHealthLabel(QLabel):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMinimumWidth(105)
        self.setAlignment(QtAlignRight)
        self.setStyleSheet("padding: 2px 6px; font-weight: 700;")

    def refresh(self):
        queue_text, queue_level = queue_status(self.app)
        text, level, detail = data_health(self.app, queue_level)
        color = level_color(level)
        self.setText(f"Data: {text}")
        self.setToolTip(detail)
        self.setStyleSheet(f"padding: 2px 6px; font-weight: 700; color: {color};")


# Avoid importing the full Qt enum namespace just for AlignRight.
try:
    from PySide6.QtCore import Qt
    QtAlignRight = Qt.AlignRight | Qt.AlignVCenter
except Exception:
    QtAlignRight = 0


def level_color(level):
    return {"OK": "#16803a", "WARNING": "#b77900", "ERROR": "#c62828"}[level]


def queue_status(app):
    writer = getattr(app, "_writer", None)
    if writer is None or not getattr(app, "_writer_started", False):
        return "Idle", "OK"
    try:
        fill = writer.items.qsize() / writer.items.maxsize
    except Exception:
        return "Unknown", "WARNING"
    if getattr(writer, "failed", False) or fill >= 0.90:
        return f"{fill:.0%}", "ERROR"
    if fill >= 0.75:
        return f"{fill:.0%}", "WARNING"
    return f"{fill:.0%}", "OK"


def data_health(app, queue_level):
    writer = getattr(app, "_writer", None)
    if writer is not None and getattr(writer, "failed", False):
        return "Writer error", "ERROR", "Background writer reported failure"
    if queue_level == "ERROR":
        return "At risk", "ERROR", "Writer queue is critically full"
    states = []
    health = getattr(app, "device_health", None)
    if health is not None:
        try:
            with health._lock:
                states = [str(v.get("status", "")).upper() for v in health.devices.values()]
        except Exception:
            pass
    if "ERROR" in states:
        return "Device error", "ERROR", "At least one registered device reports ERROR"
    if "STALE" in states or queue_level == "WARNING":
        return "Warning", "WARNING", "A device is stale or the writer queue is elevated"
    if getattr(app, "save_bool", False):
        return "Healthy", "OK", "Saving active; no current writer/device error"
    return "Idle", "OK", "Saving is not active"


def collect_devices(app):
    result = {}
    health = getattr(app, "device_health", None)
    if health is not None:
        try:
            health.update_stale_states()
            with health._lock:
                snapshot = {name: dict(value) for name, value in health.devices.items()}
            for name, data in snapshot.items():
                result[name] = (
                    str(data.get("status", "INITIALIZING")).upper(),
                    f"Reads {data.get('read_count', 0)}, errors {data.get('error_count', 0)}",
                )
        except Exception:
            pass
    if "NI" not in result:
        ready = hasattr(app, "NIDAQ_Device")
        result["NI"] = ("CONNECTED" if ready else "DISCONNECTED", "NI task state")
    for name, device in getattr(app, "mfcs", {}).items():
        if name not in result:
            connected = hasattr(device, "loop") and hasattr(device, "MFC")
            result[name] = ("CONNECTED" if connected else "DISCONNECTED", "Alicat MFC")
    for name, runtime in getattr(app, "generic_serial_devices", {}).items():
        error = str(getattr(runtime, "last_error", ""))
        connected = bool(getattr(runtime, "connected", False))
        status = "ERROR" if error and not connected else ("CONNECTED" if connected else "DISCONNECTED")
        result[name] = (status, error or f"Reads {getattr(runtime, 'read_count', 0)}")
    return [(name, status, detail) for name, (status, detail) in sorted(result.items())]


def install_compact_gui(app) -> None:
    app.main_layout.setContentsMargins(8, 8, 8, 8)
    app.main_layout.setSpacing(5)
    if hasattr(app, "input_layout"):
        app.input_layout.setContentsMargins(4, 4, 6, 4)
        app.input_layout.setHorizontalSpacing(7)
        app.input_layout.setVerticalSpacing(5)
        app.input_layout.setColumnStretch(0, 0)
        app.input_layout.setColumnStretch(1, 0)

    for name in ("name_input", "exp_input", "test_type_input", "sample_rate_input",
                 "config_file_edit", "formulae_file_edit"):
        widget = getattr(app, name, None)
        if widget is not None:
            widget.setMinimumWidth(235)
            widget.setMaximumWidth(360)
            widget.setMinimumHeight(28)
    if hasattr(app, "test_input"):
        app.test_input.setMinimumWidth(175)
        app.test_input.setMaximumWidth(275)
    for name in ("test_btn", "config_input", "formulae_input"):
        widget = getattr(app, name, None)
        if widget is not None:
            widget.setMinimumWidth(68); widget.setMaximumWidth(76)
    for name in ("acquisition_button", "save_button"):
        widget = getattr(app, name, None)
        if widget is not None:
            widget.setMinimumWidth(180); widget.setMaximumWidth(230); widget.setMinimumHeight(32)

    panel = getattr(app, "panel", None)
    if panel is not None:
        panel.setMinimumWidth(440)
        panel.setMaximumWidth(620)

    # Devices are flexible on the left. Queue and Data remain pinned right.
    app.device_status_label = DeviceStatusLabel(app)
    app.queue_status_label = QueueStatusLabel(app)
    app.data_health_label = DataHealthLabel(app)
    bar = app.statusBar()
    bar.setSizeGripEnabled(True)
    bar.addWidget(app.device_status_label, 1)
    bar.addPermanentWidget(app.queue_status_label)
    bar.addPermanentWidget(app.data_health_label)

    app.compact_status_timer = QTimer(app)
    app.compact_status_timer.timeout.connect(app.device_status_label.refresh)
    app.compact_status_timer.timeout.connect(app.queue_status_label.refresh)
    app.compact_status_timer.timeout.connect(app.data_health_label.refresh)
    app.compact_status_timer.start(1000)
    app.device_status_label.refresh(); app.queue_status_label.refresh(); app.data_health_label.refresh()
