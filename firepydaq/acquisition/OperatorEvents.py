from __future__ import annotations

import csv
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

EVENT_FIELDS = (
    "EventNumber",
    "LocalTime",
    "ElapsedTime",
    "Operator",
    "Category",
    "Message",
)

DEFAULT_CATEGORIES = (
    "Observation",
    "Equipment",
    "Process Change",
    "Anomaly",
    "Safety",
    "Other",
)


class OperatorEventLogger:
    """Thread-safe, append-only operator-event CSV writer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._output_path: Optional[Path] = None
        self._origin_monotonic: Optional[float] = None
        self._next_event_number = 1

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path

    def configure(
        self,
        output_prefix: str | Path,
        origin_monotonic: Optional[float],
    ) -> Path:
        prefix = Path(output_prefix)
        output_path = prefix.with_name(f"{prefix.name}_events.csv")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            if self._output_path != output_path:
                self._output_path = output_path
                self._next_event_number = self._read_next_event_number(
                    output_path
                )
            self._origin_monotonic = origin_monotonic

        return output_path

    def append(self, operator: str, category: str, message: str) -> dict:
        operator = operator.strip()
        category = category.strip() or "Observation"
        message = message.strip()

        if not operator:
            raise ValueError("Operator name is required.")
        if not message:
            raise ValueError("Event message is required.")

        with self._lock:
            if self._output_path is None:
                raise RuntimeError("Start acquisition before saving events.")

            now_monotonic = time.monotonic()
            elapsed_time = (
                max(0.0, now_monotonic - self._origin_monotonic)
                if self._origin_monotonic is not None
                else 0.0
            )
            row = {
                "EventNumber": self._next_event_number,
                "LocalTime": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
                "ElapsedTime": f"{elapsed_time:.6f}",
                "Operator": operator,
                "Category": category,
                "Message": message,
            }

            new_file = not self._output_path.exists()
            with self._output_path.open(
                "a",
                newline="",
                encoding="utf-8",
            ) as stream:
                writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
                stream.flush()
                os.fsync(stream.fileno())

            self._next_event_number += 1
            return row

    @staticmethod
    def _read_next_event_number(path: Path) -> int:
        if not path.exists():
            return 1

        highest = 0
        try:
            with path.open("r", newline="", encoding="utf-8") as stream:
                for row in csv.DictReader(stream):
                    try:
                        highest = max(
                            highest,
                            int(row.get("EventNumber", 0)),
                        )
                    except (TypeError, ValueError):
                        continue
        except OSError:
            return 1

        return highest + 1


class EventDraftRow(QFrame):
    """One unsaved event draft with a permanent creation identity."""

    saved = Signal(object)
    removed = Signal(object)

    def __init__(self, draft_id: int, parent=None) -> None:
        super().__init__(parent)
        self.draft_id = draft_id
        self.created_local = datetime.now().astimezone()
        self.created_monotonic = time.monotonic()

        self.setFrameShape(QFrame.StyledPanel)

        creation_time = self.created_local.strftime("%H:%M:%S")
        self.draft_label = QLabel(
            f"Event {self.draft_id}  {creation_time}"
        )
        self.draft_label.setMinimumWidth(130)
        self.draft_label.setStyleSheet(
            "font-weight: 700; color: #4f5961;"
        )
        self.draft_label.setToolTip(
            "Event draft opened: "
            + self.created_local.isoformat(timespec="seconds")
        )

        self.category = QComboBox()
        self.category.addItems(DEFAULT_CATEGORIES)

        self.message = QLineEdit()
        self.message.setPlaceholderText("Describe the operator event")

        self.save_button = QPushButton("Save")
        self.remove_button = QPushButton("Remove")
        self.save_button.setMaximumWidth(58)
        self.remove_button.setMaximumWidth(68)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 3, 5, 3)
        layout.setSpacing(5)
        layout.addWidget(self.draft_label)
        layout.addWidget(self.category)
        layout.addWidget(self.message, 1)
        layout.addWidget(self.save_button)
        layout.addWidget(self.remove_button)

        self.save_button.clicked.connect(lambda: self.saved.emit(self))
        self.remove_button.clicked.connect(lambda: self.removed.emit(self))
        self.message.returnPressed.connect(lambda: self.saved.emit(self))

    def set_busy(self, busy: bool) -> None:
        enabled = not busy
        for widget in (
            self.category,
            self.message,
            self.save_button,
            self.remove_button,
        ):
            widget.setEnabled(enabled)


class OperatorEventsWidget(QWidget):
    """Multiple operator-event drafts with stable IDs and creation times."""

    event_saved = Signal(dict)
    event_file_changed = Signal(str)

    def __init__(
        self,
        operator_getter: Callable[[], str],
        output_prefix_getter,
        elapsed_origin_getter,
        notify,
        parent=None,
        publish_event=None,
    ) -> None:
        super().__init__(parent)
        self.operator_getter = operator_getter
        self.output_prefix_getter = output_prefix_getter
        self.elapsed_origin_getter = elapsed_origin_getter
        self.notify = notify
        self.logger = OperatorEventLogger()
        self._drafts: list[EventDraftRow] = []
        self._next_draft_id = 1
        self.publish_event = publish_event

        title = QLabel("Operator Events")
        title.setStyleSheet("font-weight: 600;")

        self.path_label = QLabel("Start acquisition to initialize the event file.")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setWordWrap(False)

        add_button = QPushButton("Log Event")
        add_button.setMaximumWidth(82)
        add_button.clicked.connect(self.add_draft)

        header_layout = QHBoxLayout()
        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(add_button)

        self.draft_container = QWidget()
        self.draft_layout = QVBoxLayout(self.draft_container)
        self.draft_layout.setContentsMargins(0, 0, 0, 0)
        self.draft_layout.setSpacing(3)
        self.draft_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.draft_container)
        scroll.setMinimumHeight(64)
        scroll.setMaximumHeight(180)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        layout.addLayout(header_layout)
        layout.addWidget(self.path_label)
        layout.addWidget(scroll)

    def configure_for_current_test(self) -> Path:
        output_prefix = self.output_prefix_getter()
        if output_prefix is None:
            raise RuntimeError("The current test output path is not available.")

        path = self.logger.configure(
            output_prefix,
            self.elapsed_origin_getter(),
        )
        relative_path = self._relative_path(path)
        self.path_label.setText(relative_path)

        self.event_file_changed.emit(relative_path)

        self.path_label.setToolTip(str(path))
        return path

    @staticmethod
    def _relative_path(path: Path) -> str:
        parts = path.parts
        for index, part in enumerate(parts):
            normalized = part.lower().replace("_", "").replace("-", "")
            if (
                "experimentdata" in normalized
                or "calibrationdata" in normalized
            ):
                return str(Path(*parts[index + 1 :]))
        return str(Path(path.parent.name) / path.name)

    def add_draft(self) -> None:
        draft = EventDraftRow(
            draft_id=self._next_draft_id,
            parent=self,
        )
        self._next_draft_id += 1

        draft.saved.connect(self._save_draft)
        draft.removed.connect(self._remove_draft)
        self._drafts.append(draft)
        
        self.draft_layout.insertWidget(
            self.draft_layout.count() - 1,
            draft,
        )
        draft.message.setFocus()

    def _remove_draft(self, draft: EventDraftRow) -> None:
        if draft in self._drafts:
            self._drafts.remove(draft)
        draft.deleteLater()

    def _save_draft(self, draft: EventDraftRow) -> None:
        draft.set_busy(True)
        try:
            self.configure_for_current_test()
            row = self.logger.append(
                self.operator_getter(),
                draft.category.currentText(),
                draft.message.text(),
            )
        except Exception as exc:
            draft.set_busy(False)
            QMessageBox.warning(
                self,
                "Operator event not saved",
                str(exc),
            )
            return

        if draft in self._drafts:
            self._drafts.remove(draft)
        draft.deleteLater()

        self.publish_event(row)
        self.event_saved.emit(row)
        self.notify(
            f"Operator event {row['EventNumber']} saved: {row['Message']}",
            "observation",
        )
