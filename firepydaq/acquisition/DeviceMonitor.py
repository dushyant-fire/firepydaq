from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class DeviceMonitorDialog(QDialog):
    """Read-only AbstractDevice health monitor."""

    HEADERS = (
        "Name",
        "Type",
        "State",
        "Reads",
        "Run Reads",
        "Read Hz",
        "Saved Samples",
        "Save Hz",
        "Last Read",
        "Last Save",
        "Last Error",
    )

    def __init__(self, app, parent=None) -> None:
        super().__init__(parent or app)
        self.app = app
        self.setWindowTitle("Device Health Monitor")
        self.resize(1120, 460)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        for column in range(len(self.HEADERS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(
            len(self.HEADERS) - 1,
            QHeaderView.Stretch,
        )

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def refresh(self) -> None:
        registry = getattr(self.app, "device_registry", None)
        snapshots = registry.snapshots() if registry is not None else {}
        self.table.setRowCount(len(snapshots))

        for row, (name, snapshot) in enumerate(sorted(snapshots.items())):
            read_age = snapshot.age_seconds()
            values = (
                name,
                snapshot.device_type,
                snapshot.state.value,
                str(snapshot.sequence),
                str(snapshot.run_sequence),
                f"{snapshot.read_frequency_hz:.3f}",
                str(snapshot.saved_samples_this_run),
                f"{snapshot.sample_save_frequency_hz:.3f}",
                "-" if read_age is None else f"{read_age:.1f} s ago",
                snapshot.last_save_local or "-",
                snapshot.error or "",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()


def show_device_monitor(app) -> None:
    if not hasattr(app, "device_monitor_dialog"):
        app.device_monitor_dialog = DeviceMonitorDialog(app)
    app.device_monitor_dialog.refresh()
    app.device_monitor_dialog.show()
    app.device_monitor_dialog.raise_()
    app.device_monitor_dialog.activateWindow()
