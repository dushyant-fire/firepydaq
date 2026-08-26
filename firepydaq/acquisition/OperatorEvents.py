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
    """Thread-safe, append-only operator event CSV writer."""

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
                self._next_event_number = self._read_next_event_number(output_path)
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
    save_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)

        self.draft_label = QLabel("Draft")
        self.draft_label.setMinimumWidth(56)
        self.draft_label.setStyleSheet(
            "font-weight: 700; color: #4f5961;"
        )

        self.category_input = QComboBox()
        self.category_input.addItems(DEFAULT_CATEGORIES)

        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("Describe the operator event")

        self.save_button = QPushButton("Save")
        self.remove_button = QPushButton("Remove")
        self.save_button.setMaximumWidth(58)
        self.remove_button.setMaximumWidth(70)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 3, 5, 3)
        layout.setSpacing(5)
        layout.addWidget(self.draft_label)
        layout.addWidget(self.category_input)
        layout.addWidget(self.message_input, 1)
        layout.addWidget(self.save_button)
        layout.addWidget(self.remove_button)

        self.save_button.clicked.connect(
            lambda: self.save_requested.emit(self)
        )
        self.remove_button.clicked.connect(
            lambda: self.remove_requested.emit(self)
        )
        self.message_input.returnPressed.connect(
            lambda: self.save_requested.emit(self)
        )

    def set_draft_number(self, number: int) -> None:
        self.draft_label.setText(f"Draft {number}")

    def set_busy(self, busy: bool) -> None:
        enabled = not busy
        self.category_input.setEnabled(enabled)
        self.message_input.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)


class OperatorEventsWidget(QWidget):
    """Structured operator events with multiple numbered drafts."""

    event_saved = Signal(dict)
    event_file_changed = Signal(str)

    def __init__(
        self,
        operator_getter: Callable[[], str],
        output_prefix_getter: Callable[[], Optional[str | Path]],
        elapsed_origin_getter: Callable[[], Optional[float]],
        notify: Callable[[str, str], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.operator_getter = operator_getter
        self.output_prefix_getter = output_prefix_getter
        self.elapsed_origin_getter = elapsed_origin_getter
        self.notify = notify
        self.logger = OperatorEventLogger()
        self._drafts: list[EventDraftRow] = []

        title = QLabel("Operator Events")
        title.setStyleSheet("font-weight: 600;")

        self.path_label = QLabel(
            "Start acquisition to initialize the event file."
        )
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setWordWrap(False)

        add_button = QPushButton("Add draft")
        add_button.setMaximumWidth(84)
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

        draft_scroll = QScrollArea()
        draft_scroll.setWidgetResizable(True)
        draft_scroll.setWidget(self.draft_container)
        draft_scroll.setMinimumHeight(64)
        draft_scroll.setMaximumHeight(124)
        draft_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(3)
        layout.addLayout(header_layout)
        layout.addWidget(self.path_label)
        layout.addWidget(draft_scroll)

        self.add_draft()

    def configure_for_current_test(self) -> Path:
        output_prefix = self.output_prefix_getter()
        if output_prefix is None:
            raise RuntimeError("The current test output path is not available.")

        output_path = self.logger.configure(
            output_prefix,
            self.elapsed_origin_getter(),
        )

        self.event_file_changed.emit(
            self._relative_data_path(output_path)
        )
        relative_path = self._relative_data_path(output_path)
        self.path_label.setText(relative_path)
        self.path_label.setToolTip(str(output_path))
        self.path_label.update()
        self.event_file_changed.emit(relative_path)
        return output_path

    @staticmethod
    def _relative_data_path(path: Path) -> str:
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

    def add_draft(self) -> None:
        draft = EventDraftRow(self)
        draft.save_requested.connect(self._save_draft)
        draft.remove_requested.connect(self._remove_draft)
        self._drafts.append(draft)
        self.draft_layout.insertWidget(
            self.draft_layout.count() - 1,
            draft,
        )
        self._renumber_drafts()
        draft.message_input.setFocus()

    def _renumber_drafts(self) -> None:
        for index, draft in enumerate(self._drafts, start=1):
            draft.set_draft_number(index)

    def _remove_draft(self, draft: EventDraftRow) -> None:
        if draft in self._drafts:
            self._drafts.remove(draft)
        draft.setParent(None)
        draft.deleteLater()

        if not self._drafts:
            self.add_draft()
        else:
            self._renumber_drafts()

        self.draft_container.adjustSize()
        self.draft_container.updateGeometry()
        self.updateGeometry()

    def _save_draft(self, draft: EventDraftRow) -> None:
        draft.set_busy(True)
        try:
            self.configure_for_current_test()
            row = self.logger.append(
                operator=self.operator_getter(),
                category=draft.category_input.currentText(),
                message=draft.message_input.text(),
            )
        except Exception as exc:
            draft.set_busy(False)
            QMessageBox.warning(
                self,
                "Operator event not saved",
                str(exc),
            )
            return

        # Emit while the widget and signal connections are still alive.
        self.event_saved.emit(dict(row))

        # Remove the saved draft, then retain one empty draft if needed.
        self._remove_draft(draft)

        self.notify(
            f"Operator event {row['EventNumber']} saved: {row['Message']}",
            "observation",
        )
