from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QDialog
)

from .abstract_device import DeviceState
from .DeviceManager import StreamingSerialEditor, StreamingSerialRuntime
from .WorkspaceTheme import apply_workspace_theme, palette_is_dark
from .DeviceNameDialog import DeviceNameDialog
from .device import alicat_mfc, mfm
try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None


class CollapsibleDeviceCard(QFrame):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceCard")
        self.setFrameShape(QFrame.StyledPanel)

        self.toggle = QPushButton(f"▶  {title}")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setObjectName("deviceCardHeader")
        self.toggle.clicked.connect(self._set_expanded)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 6, 10, 9)
        self.body_layout.setSpacing(6)
        self.body.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.body)

    def _set_expanded(self, expanded: bool) -> None:
        text = self.toggle.text()[3:]
        self.toggle.setText(f"{'▼' if expanded else '▶'}  {text}")
        self.body.setVisible(expanded)


class DevicesWorkspace(QWidget):
    """Embedded device management and control page.

    This page owns the generic serial polling/save timer that previously lived in
    the Device Manager dialog, so serial devices keep operating without a popup.
    """

    def __init__(self, app, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self._cards: dict[str, CollapsibleDeviceCard] = {}
        self._signature: tuple = ()
        self._port_controls: dict[str, tuple[QComboBox, object]] = {}

        title = QLabel("Devices")
        title.setObjectName("workspaceTitle")

        add_mfc = QPushButton("Add MFC")
        add_mfc.clicked.connect(self.add_mfc_device)
        add_mfm = QPushButton("Add MFM")
        add_mfm.clicked.connect(self.add_mfm_device)
        add_serial = QPushButton("Add Serial Device")
        add_serial.clicked.connect(self.add_serial_device)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_devices)

        toolbar = QHBoxLayout()
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(add_mfc)
        toolbar.addWidget(add_mfm)
        toolbar.addWidget(add_serial)
        toolbar.addWidget(refresh)

        self.cards_widget = QWidget()
        self.cards_widget.setObjectName("cardsWidget")
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(2, 2, 2, 2)
        self.cards_layout.setSpacing(7)
        self.cards_layout.addStretch()

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.cards_widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(7)
        layout.addLayout(toolbar)
        layout.addWidget(scroll, 1)

        self.setStyleSheet(
            "QFrame#deviceCard { border: 1px solid palette(mid); border-radius: 7px; }"
            "QPushButton#deviceCardHeader { text-align: left; font-weight: 700;"
            " border: 0; padding: 8px; background: palette(alternate-base); }"
            "QLabel#workspaceTitle { font-size: 16px; font-weight: 700; }"
        )

        self._theme_name = ""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        self.rebuild()

    def rebuild(self) -> None:
        expanded = {
            name: card.toggle.isChecked()
            for name, card in self._cards.items()
        }
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            card = item.widget()
            if card is not None:
                card.deleteLater()
        self._cards.clear()
        self._port_controls.clear()

        registry = getattr(self.app, "device_registry", None)
        snapshots = registry.snapshots() if registry is not None else {}

        ni = getattr(self.app, "NIDAQ_Device", None)
        if ni is not None:
            self._add_summary_card("NI", ni, "NI DAQ", expanded.get("NI", False))

        for name, widget in sorted(getattr(self.app, "mfcs", {}).items()):
            self._add_alicat_card(
                name,
                widget,
                snapshots.get(name),
                expanded.get(name, False),
            )

        for name, widget in self._mfm_widgets():
            self._add_mfm_card(
                name,
                widget,
                expanded.get(name, False),
            )

        for name, runtime in sorted(
            getattr(self.app, "generic_serial_devices", {}).items()
        ):
            self._add_serial_card(name, runtime, expanded.get(name, False))

        self._signature = self._device_signature()

    def _add_summary_card(self, name, runtime, kind, expanded) -> None:
        card = CollapsibleDeviceCard(f"{name}  |  {kind}")
        snapshot = runtime.snapshot()
        grid = QGridLayout()
        grid.addWidget(QLabel("State"), 0, 0)
        grid.addWidget(QLabel(snapshot.state.value), 0, 1)
        card.body_layout.addLayout(grid)
        card.toggle.setChecked(expanded)
        card._set_expanded(expanded)
        self._insert_card(name, card)

    def _acquisition_is_active(self) -> bool:
        """Return True only when the NI acquisition device is actually running."""
        ni_device = getattr(self.app, "NIDAQ_Device", None)
        if ni_device is None:
            return False
        try:
            return ni_device.snapshot().state == DeviceState.RUNNING
        except Exception:
            return False

    def _add_alicat_card(self, name, widget, snapshot, expanded) -> None:
        """Render Alicat controls backed by the existing tested widget methods."""
        card = CollapsibleDeviceCard(f"{name}  |  Alicat MFC")
        registry = getattr(self.app, "device_registry", None)
        runtime = registry.get(name) if registry is not None else None

        status = QLabel(
            snapshot.state.value if snapshot is not None else "DISCONNECTED"
        )

        port = QComboBox()
        port.setEditable(True)
        port.addItems(
            [
                widget.comport_input.itemText(index)
                for index in range(widget.comport_input.count())
            ]
        )
        self._populate_port_combo(
            port, widget.comport_input.currentText()
        )
        self._port_controls[name] = (port, widget)

        gas = QComboBox()
        gas.addItems(
            [
                widget.gas_input.itemText(index)
                for index in range(widget.gas_input.count())
            ]
        )
        gas.setCurrentText(widget.gas_input.currentText())

        flow = QLineEdit(widget.dil_rate_input.text())
        flow.setPlaceholderText("Flow setpoint")

        grid = QGridLayout()
        for row, (label, control) in enumerate(
            (
                ("Status", status),
                ("COM Port", port),
                ("Gas", gas),
                ("Flow Setpoint", flow),
            )
        ):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(control, row, 1)

        connected = bool(
            runtime is not None
            and runtime.state != DeviceState.DISCONNECTED
        )
        connect_button = QPushButton(
            "Disconnect" if connected else "Connect"
        )
        set_flow_button = QPushButton("Set Flow")
        stop_flow_button = QPushButton("Stop Flow")
        remove_button = QPushButton("Remove MFC")

        def sync_widget_inputs() -> None:
            widget.comport_input.setCurrentText(port.currentText())
            widget.gas_input.setCurrentText(gas.currentText())
            widget.dil_rate_input.setText(flow.text().strip())

        def refresh_interfaces() -> None:
            self._refresh_all_interfaces()
            QTimer.singleShot(0, self.rebuild)

        def toggle_connection() -> None:
            sync_widget_inputs()
            current_runtime = (
                registry.get(name) if registry is not None else None
            )
            is_connected = bool(
                current_runtime is not None
                and current_runtime.state != DeviceState.DISCONNECTED
            )
            try:
                # The legacy widget method is the tested connection path. Set its
                # button to the desired final state exactly once.
                widget.mfc_connection_btn.setChecked(not is_connected)
                widget.establish_connection()

                updated_runtime = (
                    registry.get(name) if registry is not None else None
                )
                if updated_runtime is not None and not is_connected:
                    if self._acquisition_is_active():
                        updated_runtime.start()
                    else:
                        updated_runtime.stop()
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    f"{name} connection failed",
                    str(exc),
                )
            refresh_interfaces()

        def set_flow() -> None:
            sync_widget_inputs()
            try:
                # This is the same runtime path used by the old bottom tab and
                # supports changing gas and setpoint during acquisition.
                widget.set_flow_rate()
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    f"{name} flow update failed",
                    str(exc),
                )
            refresh_interfaces()

        def stop_flow() -> None:
            sync_widget_inputs()
            try:
                widget.stop_flow_rate()
                flow.setText(widget.dil_rate_input.text())
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    f"{name} stop-flow failed",
                    str(exc),
                )
            refresh_interfaces()

        connect_button.clicked.connect(toggle_connection)
        set_flow_button.clicked.connect(set_flow)
        stop_flow_button.clicked.connect(stop_flow)
        remove_button.clicked.connect(
            lambda: self._remove_legacy_device(name, widget)
        )

        controls = QHBoxLayout()
        controls.addWidget(connect_button)
        controls.addWidget(set_flow_button)
        controls.addWidget(stop_flow_button)
        controls.addWidget(remove_button)
        controls.addStretch()

        card.body_layout.addLayout(grid)
        card.body_layout.addLayout(controls)


        card.toggle.setChecked(expanded)
        card._set_expanded(expanded)
        self._insert_card(name, card)

    def _add_serial_card(self, name, runtime, expanded) -> None:
        card = CollapsibleDeviceCard(f"{name}  |  Streaming Serial")
        snapshot = runtime.snapshot()
        grid = QGridLayout()
        grid.addWidget(QLabel("State"), 0, 0)
        grid.addWidget(QLabel(snapshot.state.value), 0, 1)
        grid.addWidget(QLabel("Port"), grid.rowCount(), 0)
        grid.addWidget(QLabel(runtime.config.port), grid.rowCount() - 1, 1)
        grid.addWidget(QLabel("Baud"), grid.rowCount(), 0)
        grid.addWidget(QLabel(str(runtime.config.baud_rate)), grid.rowCount() - 1, 1)

        connect = QPushButton(
            "Disconnect" if runtime.connected else "Connect"
        )
        edit = QPushButton("Edit")
        remove = QPushButton("Remove")

        def toggle_connection() -> None:
            try:
                if runtime.connected:
                    runtime.disconnect()
                else:
                    runtime.connect()
            except Exception as exc:
                QMessageBox.critical(self, "Serial connection failed", str(exc))
            self.rebuild()

        connect.clicked.connect(toggle_connection)
        edit.clicked.connect(lambda: self.edit_serial_device(name, runtime))
        remove.clicked.connect(lambda: self.remove_serial_device(name, runtime))

        controls = QHBoxLayout()
        controls.addWidget(connect)
        controls.addWidget(edit)
        controls.addWidget(remove)
        controls.addStretch()

        card.body_layout.addLayout(grid)
        card.body_layout.addLayout(controls)
        card.toggle.setChecked(expanded)
        card._set_expanded(expanded)
        self._insert_card(name, card)

    @staticmethod
    def _add_metric_rows(grid: QGridLayout, snapshot) -> None:
        age = snapshot.age_seconds()
        values = (
            ("Status", snapshot.state.value),
            ("Reads", str(snapshot.sequence)),
            ("Run Reads", str(snapshot.run_sequence)),
            ("Read Hz", f"{snapshot.read_frequency_hz:.3f}"),
            ("Saved Samples", str(snapshot.saved_samples_this_run)),
            ("Save Hz", f"{snapshot.sample_save_frequency_hz:.3f}"),
            ("Last Read", "-" if age is None else f"{age:.1f} s ago"),
            ("Last Save", snapshot.last_save_local or "-"),
        )
        for row, (label, value) in enumerate(values):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(QLabel(value), row, 1)

    def _insert_card(self, name: str, card: CollapsibleDeviceCard) -> None:
        self._cards[name] = card
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

    @staticmethod
    def _available_ports() -> list[str]:
        if list_ports is None:
            return []
        return [port.device for port in list_ports.comports()]

    @staticmethod
    def _populate_port_combo(combo: QComboBox, current: str) -> None:
        ports = DevicesWorkspace._available_ports()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(ports)
        if current and current not in ports:
            combo.addItem(current)
        combo.setCurrentText(current)
        combo.blockSignals(False)

    def refresh_devices(self) -> None:
        """Refresh COM-port choices in place without rebuilding device cards."""
        ports = self._available_ports()

        for _name, (card_combo, source_widget) in tuple(
            self._port_controls.items()
        ):
            source_combo = getattr(source_widget, "comport_input", None)
            current = card_combo.currentText()
            if not current and source_combo is not None:
                current = source_combo.currentText()

            card_combo.blockSignals(True)
            card_combo.clear()
            card_combo.addItems(ports)
            if current and current not in ports:
                card_combo.addItem(current)
            card_combo.setCurrentText(current)
            card_combo.blockSignals(False)

            if source_combo is not None:
                source_combo.blockSignals(True)
                source_combo.clear()
                source_combo.addItems(ports)
                if current and current not in ports:
                    source_combo.addItem(current)
                source_combo.setCurrentText(current)
                source_combo.blockSignals(False)

        self._refresh_all_interfaces()

    def _remove_bottom_tab_for_device(self, name: str, widget: QWidget) -> None:
        host = getattr(self.app, "device_tab_widget", None)
        if host is None:
            return
        for index in reversed(range(host.count())):
            page = host.widget(index)
            title = host.tabText(index).strip().lower()
            owns_widget = page is widget or page.isAncestorOf(widget)
            if owns_widget or title == name.strip().lower():
                host.removeTab(index)
                if page is not widget:
                    page.setParent(None)
                    page.deleteLater()

    def _refresh_all_interfaces(self) -> None:
        status = getattr(self.app, "device_status_label", None)
        if status is not None:
            status.refresh()

        manager = getattr(self.app, "device_manager_dialog", None)
        if manager is not None:
            manager.refresh()

    def _remove_new_legacy_device_tabs(self) -> None:
        input_page = getattr(self.app, "input_settings_widget", None)
        if input_page is None:
            return
        tabs = _find_tab_widget(input_page)
        if tabs is None:
            return
        _remove_legacy_device_tabs(tabs, input_page)

    def _legacy_control_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = []
        widgets.extend(getattr(self.app, "mfcs", {}).values())
        widgets.extend(widget for _name, widget in self._mfm_widgets())
        return widgets

    def _mfm_widgets(self) -> list[tuple[str, QWidget]]:
        widgets: list[tuple[str, QWidget]] = []
        for name, device in sorted(getattr(self.app, "device_arr", {}).items()):
            if device.__class__.__name__.lower() == "mfm":
                widgets.append((name, device))
        return widgets

    @staticmethod
    def _connection_button(widget):
        for attribute in (
            "mfc_connection_btn",
            "mfm_connection_btn",
            "connection_btn",
        ):
            button = getattr(widget, attribute, None)
            if button is not None:
                return button
        return None

    @classmethod
    def _widget_connected(cls, widget) -> bool:
        button = cls._connection_button(widget)
        return bool(button is not None and button.isChecked())

    def _add_mfm_card(self, name: str, widget: QWidget, expanded: bool) -> None:
        card = CollapsibleDeviceCard(f"{name}  |  Alicat MFM")
        grid = QGridLayout()

        source_port = getattr(widget, "comport_input", None)
        port = QComboBox()
        port.setEditable(True)
        current_port = source_port.currentText() if source_port is not None else ""
        self._populate_port_combo(port, current_port)
        self._port_controls[name] = (port, widget)
        grid.addWidget(QLabel("COM Port"), 0, 0)
        grid.addWidget(port, 0, 1)

        source_gas = getattr(widget, "gas_input", None)
        gas = None
        if source_gas is not None:
            gas = QComboBox()
            gas.addItems(
                [source_gas.itemText(i) for i in range(source_gas.count())]
            )
            gas.setCurrentText(source_gas.currentText())
            grid.addWidget(QLabel("Gas"), 1, 0)
            grid.addWidget(gas, 1, 1)

        status = QLabel("Connected" if self._widget_connected(widget) else "Disconnected")
        grid.addWidget(QLabel("State"), 2, 0)
        grid.addWidget(status, 2, 1)

        connect = QPushButton(
            "Disconnect" if self._widget_connected(widget) else "Connect"
        )
        remove = QPushButton("Remove MFM")

        def sync_inputs() -> None:
            if source_port is not None:
                source_port.setCurrentText(port.currentText())
            if source_gas is not None and gas is not None:
                source_gas.setCurrentText(gas.currentText())

        def toggle_connection() -> None:
            sync_inputs()
            button = self._connection_button(widget)
            method = getattr(widget, "establish_connection", None)
            if button is None or method is None:
                QMessageBox.warning(
                    self,
                    "MFM control unavailable",
                    "The current MFM widget does not expose a connection control.",
                )
                return
            button.setChecked(not self._widget_connected(widget))
            method()
            self.refresh_devices()

        connect.clicked.connect(toggle_connection)
        remove.clicked.connect(lambda: self._remove_legacy_device(name, widget))

        controls = QHBoxLayout()
        controls.addWidget(connect)
        controls.addWidget(remove)
        controls.addStretch()
        card.body_layout.addLayout(grid)
        card.body_layout.addLayout(controls)
        card.toggle.setChecked(expanded)
        card._set_expanded(expanded)
        self._insert_card(name, card)

    def _legacy_action(self, object_name: str) -> Optional[QAction]:
        for action in self.app.menuBar().findChildren(QAction):
            if action.objectName() == object_name:
                return action

        # Removed menus may no longer be children of the visible menu bar, but
        # MainMenu remains an application child.
        for action in self.app.findChildren(QAction):
            if action.objectName() == object_name:
                return action
        return None

    def _trigger_legacy_add(self, object_name: str, label: str) -> None:
        action = self._legacy_action(object_name)
        if action is None:
            QMessageBox.critical(
                self,
                f"Cannot add {label}",
                f"The existing {label} creation action was not found.",
            )
            return

        action.trigger()

        def finalize_add() -> None:
            host = getattr(self.app, "device_tab_widget", None)
            if host is not None:
                host.setVisible(False)
                host.setParent(self.app)
            self.rebuild()
            self._refresh_all_interfaces()

        QTimer.singleShot(0, finalize_add)
        QTimer.singleShot(25, finalize_add)

    # def add_mfc_device(self) -> None:
    #     self._trigger_legacy_add("Add MFC", "MFC")

    def add_mfc_device(self) -> None:
        dialog = DeviceNameDialog("Add MFC")
        if dialog.exec() != QDialog.Accepted:
            return
        name = dialog.device_name.strip()
        if not name:
            self.app.inform_user("Device name can not be empty.")
            return

        if name in self.app.device_arr:
            self.app.inform_user("Device names must be unique.")
            return

        device = alicat_mfc(self.app, None, name,)

        self.app.device_arr[name] = device
        self.app.mfcs[name] = device

        self.rebuild()
        self._refresh_all_interfaces()

    def add_mfm_device(self) -> None:
        dialog = DeviceNameDialog("Add MFM")
        if dialog.exec() != QDialog.Accepted:
            return

        name = dialog.device_name.strip()
        if not name:
            self.app.inform_user("Device name can not be empty.")
            return

        if name in self.app.device_arr:
            self.app.inform_user("Device names must be unique.")
            return

        device = mfm(self.app, name,)

        self.app.device_arr[name] = device
        self.app.mfms[name] = device

        self.rebuild()
        self._refresh_all_interfaces()

    # def add_mfm_device(self) -> None:
    #     self._trigger_legacy_add("Add MFM", "MFM")

    def _remove_legacy_device(self, name: str, widget: QWidget) -> None:
        registry = getattr(self.app, "device_registry", None)
        if registry is not None and registry.get(name) is not None:
            registry.unregister(name, disconnect=True)

        for container_name in ("mfcs", "mfms", "device_arr"):
            container = getattr(self.app, container_name, None)
            if isinstance(container, dict):
                container.pop(name, None)

        # Best effort disconnect through the existing widget API.
        try:
            button = getattr(widget, "mfc_connection_btn", None)
            if button is not None and button.isChecked():
                button.setChecked(False)
                widget.establish_connection()
        except Exception as exc:
            self.app.notify(f"{name} removal warning: {exc}", "warning")

        self._remove_bottom_tab_for_device(name, widget)
        widget.setParent(None)
        widget.deleteLater()
        self.rebuild()
        self._refresh_all_interfaces()

    def add_serial_device(self) -> None:
        dialog = StreamingSerialEditor(self)
        if dialog.exec() != QDialog.Accepted:
            return

        try:
            config = dialog.get_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid device", str(exc))
            return

        devices = getattr(self.app, "generic_serial_devices", None)
        if devices is None:
            devices = {}
            self.app.generic_serial_devices = devices

        if config.name in devices:
            QMessageBox.warning(
                self,
                "Duplicate name",
                "Device name already exists.",
            )
            return

        runtime = StreamingSerialRuntime(config, self.app.notify)
        devices[config.name] = runtime

        registry = getattr(self.app, "device_registry", None)
        if registry is not None:
            registry.register(runtime)

        # Refresh both embedded workspace and any existing Device Manager dialog.
        self.rebuild()
        self._refresh_all_interfaces()

    def edit_serial_device(self, name, runtime) -> None:
        dialog = StreamingSerialEditor(self, runtime.config)
        if dialog.exec() != QDialog.Accepted:
            return

        try:
            config = dialog.get_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid device", str(exc))
            return

        was_connected = runtime.connected
        runtime.disconnect()
        registry = getattr(self.app, "device_registry", None)
        if registry is not None and registry.get(name) is runtime:
            registry.unregister(name, disconnect=False)

        runtime.config = config
        runtime.name = config.name
        devices = self.app.generic_serial_devices
        if config.name != name:
            del devices[name]
            devices[config.name] = runtime

        if registry is not None:
            registry.register(runtime)
        if was_connected and config.enabled:
            runtime.connect()

        self.rebuild()
        self._refresh_all_interfaces()

    def remove_serial_device(self, name, runtime) -> None:
        registry = getattr(self.app, "device_registry", None)
        if registry is not None and registry.get(name) is runtime:
            registry.unregister(name, disconnect=True)
        else:
            runtime.disconnect()

        self.app.generic_serial_devices.pop(name, None)
        self.rebuild()
        self._refresh_all_interfaces()

    def _tick(self) -> None:
        theme_name = "dark" if palette_is_dark(self.app) else "light"
        if theme_name != self._theme_name:
            console = getattr(self.app, "panel", None)
            if console is not None:
                self._theme_name = apply_workspace_theme(
                    self.app,
                    self,
                    console,
                )

        saving = bool(getattr(self.app, "save_bool", False))
        prefix = getattr(self.app, "common_path", None)
        save_begin = getattr(self.app, "save_begin_time", time.time())
        elapsed_origin = time.monotonic() - max(0.0, time.time() - save_begin)

        for runtime in tuple(
            getattr(self.app, "generic_serial_devices", {}).values()
        ):
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

        signature = self._device_signature()
        if signature != self._signature:
            self.rebuild()

    def _device_signature(self) -> tuple:
        registry = getattr(self.app, "device_registry", None)
        snapshots = registry.snapshots() if registry is not None else {}
        snapshot_signature = tuple(
            sorted(
                (
                    name,
                    snapshot.device_type,
                    snapshot.state.value,
                )
                for name, snapshot in snapshots.items()
            )
        )
        mfc_signature = tuple(sorted(getattr(self.app, "mfcs", {}).keys()))
        mfm_signature = tuple(name for name, _widget in self._mfm_widgets())
        serial_signature = tuple(
            sorted(getattr(self.app, "generic_serial_devices", {}).keys())
        )
        return (
            snapshot_signature,
            mfc_signature,
            mfm_signature,
            serial_signature,
        )

def _find_tab_widget(widget: QWidget) -> Optional[QTabWidget]:
    current = widget.parentWidget()
    while current is not None:
        if isinstance(current, QTabWidget):
            return current
        current = current.parentWidget()
    return None


def _remove_widget_from_layout(layout, target: QWidget) -> bool:
    if layout is None:
        return False
    for index in reversed(range(layout.count())):
        item = layout.itemAt(index)
        widget = item.widget()
        child_layout = item.layout()
        if widget is target:
            layout.takeAt(index)
            target.setParent(None)
            return True
        if child_layout is not None and _remove_widget_from_layout(child_layout, target):
            return True
    return False


def _remove_legacy_device_tabs(tab_widget: QTabWidget, input_page: QWidget) -> None:
    for index in reversed(range(tab_widget.count())):
        page = tab_widget.widget(index)
        title = tab_widget.tabText(index).lower()
        if page is input_page:
            continue
        if any(token in title for token in ("thorlabs", "laser")):
            tab_widget.removeTab(index)
            page.deleteLater()
        elif any(token in title for token in ("alicat", "mfc", "mfm")):
            tab_widget.removeTab(index)
            page.hide()


def _remove_legacy_device_menus(app) -> None:
    menu_bar = app.menuBar()
    for action in list(menu_bar.actions()):
        text = action.text().replace("&", "").strip().lower()
        if text in {"add devices", "remove devices"}:
            menu_bar.removeAction(action)


def install_main_workspace(app) -> None:
    """Install Input Settings and Devices tabs with a persistent console."""
    if hasattr(app, "devices_workspace"):
        return

    input_page = getattr(app, "input_settings_widget", None)
    console = getattr(app, "panel", None)
    if input_page is None or console is None:
        return

    tabs = _find_tab_widget(input_page)
    if tabs is None:
        return

    _remove_widget_from_layout(input_page.layout(), console)
    console.setParent(None)
    _remove_legacy_device_tabs(tabs, input_page)

    app.devices_workspace = DevicesWorkspace(app)
    tabs.addTab(app.devices_workspace, "Devices")
    app.devices_workspace._theme_name = apply_workspace_theme(
        app,
        app.devices_workspace,
        console,
    )

    # The existing Input Settings tab remains first. Tabs change only the left
    # work area; System Log and Operator Events stay permanently on the right.
    parent = tabs.parentWidget()
    parent_layout = parent.layout() if parent is not None else None
    if parent_layout is None:
        return

    _remove_widget_from_layout(parent_layout, tabs)
    splitter = QSplitter(Qt.Horizontal)
    splitter.setChildrenCollapsible(False)
    splitter.addWidget(tabs)
    splitter.addWidget(console)
    splitter.setStretchFactor(0, 3)
    splitter.setStretchFactor(1, 2)
    splitter.setSizes([740, 520])
    parent_layout.addWidget(splitter)
    app.main_workspace_splitter = splitter

    # Compact the existing input controls without changing validation or config.
    if hasattr(app, "input_layout"):
        app.input_layout.setContentsMargins(8, 8, 8, 8)
        app.input_layout.setHorizontalSpacing(8)
        app.input_layout.setVerticalSpacing(6)
    for name in (
        "name_input",
        "exp_input",
        "test_input",
        "test_type_input",
        "sample_rate_input",
        "config_file_edit",
        "formulae_file_edit",
    ):
        field = getattr(app, name, None)
        if field is not None:
            field.setMinimumWidth(210)
            field.setMaximumWidth(360)

    _remove_legacy_device_menus(app)
