from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .OperatorEvents import EVENT_FIELDS, OperatorEventsWidget

MESSAGE_COLORS = {
    "info": "#006c75",
    "success": "#16803a",
    "warning": "#b26a00",
    "error": "#c62828",
    "observation": "#2459a9",
    "default": "#555f66",
}

SUPPRESSED_LOG_PREFIXES = (
    "Last time entry:",
    "Total samples/chan:",
    "Actual Hz:",
    "Writer queue at ",
)

WINDOWS_PATH_PATTERN = re.compile(
    r"[A-Za-z]:\\[^\r\n;]+"
)


class SectionFrame(QFrame):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("operationsSection")
        self.setFrameShape(QFrame.StyledPanel)

        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(7, 5, 7, 6)
        self.box.setSpacing(3)

        title_label = QLabel(title)
        title_label.setObjectName("operationsSectionTitle")
        self.box.addWidget(title_label)


class SystemLogWidget(QTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Important system messages appear here")
        self.document().setMaximumBlockCount(300)
        self.setMinimumHeight(120)
        self.setMaximumHeight(185)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def add_message(self, message_type: str, text: str) -> None:
        clean_text = str(text).strip()
        if any(
            clean_text.startswith(prefix)
            for prefix in SUPPRESSED_LOG_PREFIXES
        ):
            return

        clean_text = self._make_paths_relative(clean_text)
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = MESSAGE_COLORS.get(
            message_type,
            MESSAGE_COLORS["default"],
        )
        safe_text = html.escape(clean_text).replace("\n", "<br>")

        self.moveCursor(QTextCursor.End)
        self.insertHtml(
            f"<span style='color:{color}'>"
            f"<b>[{timestamp}]</b> {safe_text}</span><br>"
        )
        self.moveCursor(QTextCursor.End)

    @classmethod
    def _make_paths_relative(cls, text: str) -> str:
        return WINDOWS_PATH_PATTERN.sub(
            lambda match: cls._relative_windows_path(match.group(0)),
            text,
        )

    @staticmethod
    def _relative_windows_path(raw_path: str) -> str:
        trailing = ""
        while raw_path and raw_path[-1] in ".,":
            trailing = raw_path[-1] + trailing
            raw_path = raw_path[:-1]

        path = PureWindowsPath(raw_path)
        parts = path.parts
        for index, part in enumerate(parts):
            normalized = part.lower().replace("_", "").replace("-", "")
            if (
                "experimentdata" in normalized
                or "calibrationdata" in normalized
            ):
                remainder = parts[index + 1 :]
                relative = str(PureWindowsPath(*remainder))
                return relative + trailing

        return path.name + trailing


class OperationsConsole(QWidget):
    """System log and operator-event UI with disk-backed refresh."""

    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.setMinimumWidth(440)
        self.setMaximumWidth(620)
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Preferred,
        )
        self.setStyleSheet(
            "QFrame#operationsSection {"
            " background: palette(base);"
            " border: 1px solid palette(mid);"
            " border-radius: 7px;"
            "}"
            "QLabel#operationsSectionTitle {"
            " font-weight: 700;"
            " font-size: 12px;"
            "}"
        )

        system_section = SectionFrame("SYSTEM LOG")
        self.system_log = SystemLogWidget()
        clear_row = QHBoxLayout()
        clear_row.addStretch()
        clear_button = QPushButton("Clear")
        clear_button.setMaximumWidth(58)
        clear_button.clicked.connect(self.system_log.clear)
        clear_row.addWidget(clear_button)
        system_section.box.addWidget(self.system_log)
        system_section.box.addLayout(clear_row)

        event_section = SectionFrame("OPERATOR EVENTS")
        self.recent_events = QListWidget()
        self.recent_events.setMinimumHeight(55)
        self.recent_events.setMaximumHeight(180)
        self.recent_events.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )

        self.operator_events = OperatorEventsWidget(
            operator_getter=lambda: app.name_input.text().strip(),
            output_prefix_getter=lambda: getattr(
                app,
                "common_path",
                None,
            ),
            elapsed_origin_getter=lambda: getattr(
                app,
                "acquisition_start_monotonic",
                None,
            ),
            notify=app.notify,
            parent=self,
        )
        self.operator_events.event_saved.connect(self._on_event_saved)
        self.operator_events.event_file_changed.connect(
            self._on_event_file_changed
        )

        event_section.box.addWidget(self.recent_events)
        event_section.box.addWidget(self.operator_events)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(system_section)
        layout.addWidget(event_section)
        layout.addStretch()

        self._loaded_event_path: Path | None = None
        self._loaded_mtime_ns: int | None = None

        # Disk refresh provides a reliable second path even if another component
        # writes the event CSV or a Qt signal is missed during UI replacement.
        self.event_refresh_timer = QTimer(self)
        self.event_refresh_timer.timeout.connect(
            self.refresh_operator_events_from_disk
        )
        self.event_refresh_timer.start(500)

    def _on_event_saved(self, row: dict) -> None:
        self._upsert_event(row)
        self._loaded_event_path = self.operator_events.logger.output_path
        if self._loaded_event_path is not None:
            try:
                self._loaded_mtime_ns = (
                    self._loaded_event_path.stat().st_mtime_ns
                )
            except OSError:
                self._loaded_mtime_ns = None

        self.recent_events.viewport().update()
        self.recent_events.updateGeometry()

    def _on_event_file_changed(self, _relative_path: str) -> None:
        current_path = self.operator_events.logger.output_path
        if current_path != self._loaded_event_path:
            self._loaded_event_path = current_path
            self._loaded_mtime_ns = None
            self.recent_events.clear()
        self.refresh_operator_events_from_disk(force=True)

    def refresh_operator_events_from_disk(
        self,
        force: bool = False,
    ) -> None:
        path = self.operator_events.logger.output_path
        if path is None or not path.exists():
            return

        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return

        if (
            not force
            and path == self._loaded_event_path
            and mtime_ns == self._loaded_mtime_ns
        ):
            return

        rows = self._read_event_rows(path)
        self.recent_events.setUpdatesEnabled(False)
        try:
            self.recent_events.clear()
            for row in reversed(rows[-12:]):
                self.recent_events.addItem(self._make_event_item(row))
        finally:
            self.recent_events.setUpdatesEnabled(True)

        self._loaded_event_path = path
        self._loaded_mtime_ns = mtime_ns
        self.recent_events.viewport().update()

    @staticmethod
    def _read_event_rows(path: Path) -> list[dict]:
        import csv

        rows: list[dict] = []
        try:
            with path.open("r", newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != EVENT_FIELDS:
                    return []
                rows.extend(reader)
        except OSError:
            return []
        return rows

    def _upsert_event(self, row: dict) -> None:
        event_number = str(row.get("EventNumber", ""))
        for index in range(self.recent_events.count()):
            item = self.recent_events.item(index)
            if item.data(Qt.UserRole) == event_number:
                self.recent_events.takeItem(index)
                break

        self.recent_events.insertItem(0, self._make_event_item(row))
        while self.recent_events.count() > 12:
            self.recent_events.takeItem(
                self.recent_events.count() - 1
            )

    @staticmethod
    def _make_event_item(row: dict) -> QListWidgetItem:
        local_time = str(row.get("LocalTime", ""))
        display_time = local_time[11:19] if len(local_time) >= 19 else local_time
        event_number = str(row.get("EventNumber", ""))
        category = str(row.get("Category", ""))
        message = str(row.get("Message", ""))

        item = QListWidgetItem(
            f"#{event_number}  {display_time}  {category}: {message}"
        )
        item.setData(Qt.UserRole, event_number)
        item.setToolTip(
            f"{local_time} | {row.get('Operator', '')} | "
            f"Elapsed {row.get('ElapsedTime', '')} s"
        )
        return item

    # Backward compatibility with the old NotificationPanel interface.
    def add_message(self, message_type: str, text: str) -> None:
        self.system_log.add_message(message_type, text)

    def clear(self) -> None:
        self.system_log.clear()

    def toPlainText(self) -> str:
        return self.system_log.toPlainText()

    def setAlignment(self, *_args, **_kwargs) -> None:
        pass
