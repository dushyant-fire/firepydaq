from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy

from .abstract_device import DeviceState
from .MainWorkspace import install_main_workspace

STATUS_COLORS = {
    DeviceState.RUNNING.value: "#16803a",
    DeviceState.CONNECTED.value: "#16803a",
    DeviceState.STALE.value: "#b77900",
    DeviceState.ERROR.value: "#c62828",
    DeviceState.DISCONNECTED.value: "#687076",
}


class DeviceStatusLabel(QLabel):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("padding: 2px 6px;")

    def refresh(self):
        fragments = []
        details = []
        for name, status, detail in collect_devices(self.app):
            color = STATUS_COLORS.get(status, "#687076")
            fragments.append(
                f"<span style='color:{color};font-weight:700'>●</span> "
                f"{name}: {status.title()}"
            )
            details.append(f"{name}: {status.title()} | {detail}")
        self.setText("   ".join(fragments) if fragments else "No devices configured")
        self.setToolTip("\n".join(details))


class QueueStatusLabel(QLabel):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMinimumWidth(78)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def refresh(self):
        text, level = queue_status(self.app)
        self.setText(f"Queue: {text}")
        self.setStyleSheet(
            f"padding:2px 6px;font-weight:700;color:{level_color(level)};"
        )


class DataHealthLabel(QLabel):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMinimumWidth(105)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def refresh(self):
        _, queue_level = queue_status(self.app)
        text, level, detail = data_health(self.app, queue_level)
        self.setText(f"Data: {text}")
        self.setToolTip(detail)
        self.setStyleSheet(
            f"padding:2px 6px;font-weight:700;color:{level_color(level)};"
        )


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
        return "Writer error", "ERROR", "NI background writer failed"
    if queue_level == "ERROR":
        return "At risk", "ERROR", "NI writer queue is critically full"
    registry = getattr(app, "device_registry", None)
    states = [s.state for s in registry.snapshots().values()] if registry else []
    if any(state == DeviceState.ERROR for state in states):
        return "Device warning", "WARNING", "A non-NI device reports an error; NI saving remains independent"
    if queue_level == "WARNING":
        return "Warning", "WARNING", "NI writer queue is elevated"
    if (hasattr(app, "engine") and app.engine.saving):
        return "Healthy", "OK", "NI writer is healthy"
    return "Idle", "OK", "Saving is not active"


def collect_devices(app):
    results = []
    registry = getattr(app, "device_registry", None)
    if registry is not None:
        for name, snapshot in sorted(registry.snapshots().items()):
            results.append(
                (
                    name,
                    snapshot.state.value,
                    f"Reads {snapshot.sequence}; {snapshot.error or 'no error'}",
                )
            )
    return results


def install_compact_gui(app) -> None:
    install_main_workspace(app)
    app.device_status_label = DeviceStatusLabel(app)
    app.queue_status_label = QueueStatusLabel(app)
    app.data_health_label = DataHealthLabel(app)
    bar = app.statusBar()
    bar.addWidget(app.device_status_label, 1)
    bar.addPermanentWidget(app.queue_status_label)
    bar.addPermanentWidget(app.data_health_label)
    app.compact_status_timer = QTimer(app)
    for widget in (
        app.device_status_label,
        app.queue_status_label,
        app.data_health_label,
    ):
        app.compact_status_timer.timeout.connect(widget.refresh)
        widget.refresh()
    app.compact_status_timer.start(1000)
