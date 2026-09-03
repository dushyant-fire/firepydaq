"""FIREpyDAQ acquisition application.

The Qt application shell and engine-integrated adapter are consolidated here. Hardware acquisition, timing, persistence, manifests, and device health remain owned by firepydaq.core and device classes.
"""
from __future__ import annotations

##########################################################################
# FIREpyDAQ - Facilitated Interface for Recording Experiments,
# a python-package for Data Acquisition.
# Copyright (C) 2024  Dushyant M. Chaudhari

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#########################################################################

import sys

# PyQT Related
from PySide6.QtCore import QTimer, QRegularExpression
from PySide6.QtGui import QIcon, Qt, QRegularExpressionValidator, QAction
from PySide6.QtWidgets import (
    QDialog, QMainWindow, QWidget, QVBoxLayout, QMenu,
    QTabWidget, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QMessageBox, QFileDialog, QScrollArea)
from .SaveSettingsDialog import SaveSettingsDialog
from .exception_list import UnfilledFieldError
from .MainMenu import MainMenu
from .SerialConfigPersistence import serial_devices_to_settings
from .CompactGui import install_compact_gui
import json
from .OperationsConsole import OperationsConsole

# Dashboard
from ..dashboard.app import create_dash_app
from ..dashboard.raw_stream import RawDataPublisher
from ..utilities.PostProcessing import PostProcessData

import time
from datetime import datetime, timedelta

# Threading and multiprocesses
import queue
import threading
import multiprocessing as mp

# Data related
import polars as pl
import numpy as np
import pyarrow.parquet as pq

# String/Files validations
import glob
import re
import os
from pathlib import Path

# NI related
from .ni_device import NIDaqDevice

from .abstract_device import DeviceState
from .device_registry import DeviceRegistry
from firepydaq.core.acquisition_mode import AcquisitionMode

# Error handling
import traceback
from ..utilities.ErrorUtils import error_logger, firepydaq_logger

# To make the application icon in the tab
# appear as the selected icon for Windows
if os.name == 'nt':
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ulfsri.firepydaq.010')  # noqa: E501


class _GuiApplication(QMainWindow):
    """
    The main acquisition GUI that can be compiled
    """

    def __init__(self):

        super().__init__()

        self.MakeMainWindow()
        self.InitialiseTabs()
        self.InitVars()
        install_compact_gui(self)

    def MakeMainWindow(self):
        """Creates Main window and adds menu options

        - Fixed Geometry: 900 x 600
        - Import theme styles from css assets
        - Initiate light theme for the Window and add logo
        """

        # Set window properties
        self.setGeometry(0, 0, 900, 700)
        self.resize(1180, 700)
        self.setMinimumSize(760, 520)
        self.setWindowTitle("Facilitated Interface for Recording Experiments (FIRE)")  # noqa: E501
        self.menu = MainMenu(self)
        self.setMenuBar(self.menu)

        self.assets_folder = os.path.dirname(os.path.dirname(os.path.realpath(__file__))) + os.path.sep + "assets"  # noqa: E501
        self.style_light = self.assets_folder + os.path.sep + "styles_light.css"  # noqa: E501
        self.style_dark = self.assets_folder + os.path.sep + "styles_dark.css"
        self.popup_light = self.assets_folder + os.path.sep + "popup_light.css"
        self.popup_dark = self.assets_folder + os.path.sep + "popup_dark.css"
        try:
            f = open(self.style_dark)
            str = f.read()
            self.setStyleSheet(str)
            f.close()
        except Exception:
            self.notify("Error loading stylesheets", "error")

        ico_path = self.assets_folder + os.path.sep + "FIREpyDAQDark.png"
        self.setWindowIcon(QIcon(ico_path))

        # One outer scroll area handles the complete FirePyDAQ interface.
        # Individual device tabs remain normal widgets, avoiding nested main-window
        # scrolling while still supporting smaller laptop displays.
        self.window_scroll = QScrollArea()
        self.window_scroll.setWidgetResizable(True)
        self.window_scroll.setFrameShape(QScrollArea.NoFrame)
        self.window_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.window_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.main_widget = QWidget()
        self.main_widget.setObjectName("MainWidget")
        self.main_widget.setMinimumWidth(720)
        self.main_layout = QVBoxLayout(self.main_widget)
        self.window_scroll.setWidget(self.main_widget)
        self.setCentralWidget(self.window_scroll)

        self.main_layout.setStretch(0, 1)
        self.main_layout.setStretch(1, 0)

    def InitVars(self):
        """Method that initiates various
        variables for later use.

        - Empty dicts:
            - device_arr
                Keeps a log of all Devices added by the user
            - settings
                Stores settings used for NI DAQ
            - lasers
                Stores info of Thorlabs CLD101X devices added by the user.
                Maximum 4 allowed.
            - mfms
                Stores info of all Alicat Mass Flow Meters added by the user.
                Maximum 4 allowed.
            - mfcs
                Stores info of Alicat Mass Flow Controllers added by the user.
                Maximum 4 allowed.
        - Empty lists:
            - labels_to_save
                Stores Labels to save, corresponding to the
                `Label` in NI config file,
                during acquisition
        - Booleans:
            - running = `True`
                If GUI is compiled. Default: True
            - acquiring_data = `False`
                Boolean which is `True` when acquisition is running
            - display = `False`
                Plots or Dashboard
            - dashboard = `False`
                If dashboard display is selected. Default: `False`
            - tab = `False`
                If Tabular plot display is selected. Default: `False`
        - Others:
            - re_StrAllowable = r'^[A-Za-z0-9_]+$'
                regex format for allowable strings for some input fields.
                Alphanumeric with underscores, no spaces allowed.
            - dt_format = "%Y-%m-%d %H:%M:%S:%f"
                Format for how Absolute time is saved during acquisition
            - fext = ".parquet"
                File format for collected NI data
            - curr_mode = "Light"
                GUI mode/Theme
        """
        # array holding all device objects
        self.device_arr = {}
        self.settings = {}
        self.lasers = {}
        self.mfms = {}
        self.mfcs = {}

        self.labels_to_save = []

        self.running = True
        self.acquiring_data = False
        self.display = False
        self.dashboard = False
        self.tab = False

        self.re_strAllowable = r'^[A-Za-z0-9_]+$'
        self.dt_format = "%Y-%m-%d %H:%M:%S:%f"
        self.fext = '.parquet'
        self.curr_mode = "Light"

    def InitialiseTabs(self):
        """Initiates tabs based on `input_content`
        """
        self.input_tab_widget = QTabWidget()
        self.input_tab_content = self.input_content()
        self.input_tab_widget.addTab(self.input_tab_content, "Input Settings")
        self.main_layout.addWidget(self.input_tab_widget)

    def input_content(self):
        """Creates input content for NI device by default

        Generated content
        ------------
        - name_input: QLineEdit
            Name for Operator
            `re_StrAllowable` pattern is checked in `set_up()`
        - test_input: QLineEdit
            Name of the test
            Either a path selected using the `test_btn` button
            or a string that matches `re_StrAllowable` pattern
        - test_btn: QPushButton
            Connects to `set_test_file`
        - proj_input: QLineEdit
            Name of the experiment
            `re_StrAllowable` pattern is checked in `set_up()`
        - test_type_input: QComboBox
            Either "Experiment" or "Calibration"
        - sample_rate_input: QLineEdit
            Sampling rate for NI device (Hz)
            Will only accept floats
        - config_file_edit: QLineEdit
            Select .csv NI Config File.
            The `config_input` button can be used to select a file.
        - config_input: QPushButton
            Connects to `set_config_file()`
        - formulae_file_edit: QLineEdit
            Select an optional .csv formulae file
            to post-process data when dashboard display is selected.

            A corresponding button can be used to select a file.
        - formulae_input: QPushButton
            Connects to `set_formulae_file`
        - acquisition_button: QPushButton
            Connects to `acquisition_begins()`
        - save_button: QPushButton
            Connects to `save_data()`
        - panel: QTextEdit
            Notification panel.
            Placeholder text: "Welcome User!".
            Provides notification of errors/warnings/operations.

            Will be cleared after each `save_button` click
        - log_obs_txt: QLineEdit
            A 25 (width) x 190 (height) for writing observations.
        - notif_log_btn: QPushButton
            Button Text: "Log Obs."
            Connects to `log_Obs()`
        """
        # Input Settings Layout
        self.input_settings_widget = QWidget()
        self.main_input_layout = QHBoxLayout(self.input_tab_widget)
        self.input_layout = QGridLayout()

        # Experimenter's Name
        self.name_label = QLabel("Operator name:")
        self.name_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.name_label, 0, 0)

        self.name_input = QLineEdit()
        self.name_input.setMaximumWidth(200)
        self.name_input.setPlaceholderText("Your name")
        self.input_layout.addWidget(self.name_input, 0, 1)

        # Experimenter's Name
        self.test_label = QLabel("Test name:")
        self.test_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.test_label, 3, 0)

        self.test_layout = QHBoxLayout()
        self.test_input = QLineEdit()
        self.test_btn = QPushButton("Select")
        self.test_btn.clicked.connect(self.set_test_file)
        self.test_input.setMaximumWidth(150)
        self.test_input.setPlaceholderText("Your Test's name")
        self.test_btn.setMaximumWidth(50)
        self.test_layout.addWidget(self.test_input)
        self.test_layout.addWidget(self.test_btn)
        self.input_layout.addLayout(self.test_layout, 3, 1)

        self.series_label = QLabel("Series Name:")
        self.series_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.series_label, 2, 0)

        self.series_input = QLineEdit()
        self.series_input.setMaximumWidth(200)
        self.series_input.setPlaceholderText("Test series' name")
        self.input_layout.addWidget(self.series_input, 2, 1)

        # Project Name
        self.exp_label = QLabel("Project name:")
        self.exp_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.exp_label, 1, 0)

        self.proj_input = QLineEdit()
        self.proj_input.setPlaceholderText("Your Project's name")
        self.proj_input.setMaximumWidth(200)
        self.input_layout.addWidget(self.proj_input, 1, 1)

        # Test type
        self.test_type_label = QLabel("Experiment Type:")
        self.test_type_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.test_type_label, 4, 0)

        self.test_type_input = QComboBox()
        self.test_type_input.addItem('Experiment')
        self.test_type_input.addItem('Calibration')
        self.test_type_input.setMaximumWidth(200)
        self.input_layout.addWidget(self.test_type_input, 4, 1)

        # Sampling Rate
        self.sample_rate_label = QLabel("Sampling Rate (Hz):")
        self.sample_rate_label.setToolTip("Will only accept floats")
        self.sample_rate_label.setToolTipDuration(500)
        self.sample_rate_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.sample_rate_label, 5, 0)

        self.sample_rate_input = QLineEdit()
        self.sample_rate_input.setMaximumWidth(200)
        self.sample_rate_input.setPlaceholderText("10")
        reg_ex_1 = QRegularExpression(r"[0-9]*\.[0-9]{0,4}")  # double
        self.sample_rate_input.setValidator(QRegularExpressionValidator(reg_ex_1))  # noqa: E501
        # .setValidator(QRegExpValidator(reg_ex_1))
        self.input_layout.addWidget(self.sample_rate_input, 5, 1)

        # Acquisition hardware participation mode
        self.acquisition_mode_label = QLabel("Acquisition Mode:")
        self.acquisition_mode_label.setMaximumWidth(200)
        self.input_layout.addWidget(self.acquisition_mode_label, 6, 0)

        self.acquisition_mode_input = QComboBox()
        self.acquisition_mode_input.setMaximumWidth(200)
        self.acquisition_mode_input.addItem(
            "Full (NI + Serial Devices)",
            AcquisitionMode.FULL,
        )
        self.acquisition_mode_input.addItem(
            "Serial Only",
            AcquisitionMode.SERIAL_ONLY,
        )
        self.acquisition_mode_input.setToolTip(
            "Full mode requires validated NI hardware. Serial Only runs connected "
            "Alicat and streaming serial devices without NI hardware."
        )
        self.input_layout.addWidget(self.acquisition_mode_input, 6, 1)

        # Configuration File Name
        self.config_label = QLabel("Select Configuration File:")
        self.input_layout.addWidget(self.config_label, 7, 0)
        self.config_label.setMaximumWidth(200)

        self.config_file_layout = QHBoxLayout()
        self.config_file_edit = QLineEdit()
        self.config_input = QPushButton("Select")
        self.config_input.clicked.connect(self.set_config_file)
        self.config_input.setMaximumWidth(50)
        self.config_file_edit.setMaximumWidth(150)
        self.config_file_edit.setPlaceholderText("Your Config file")
        self.config_file_layout.addWidget(self.config_file_edit)
        self.config_file_layout.addWidget(self.config_input)
        self.input_layout.addLayout(self.config_file_layout, 7, 1)

        # Formulae File Name
        self.formulae_label = QLabel("Select Formulae File:")
        self.input_layout.addWidget(self.formulae_label, 8, 0)
        self.formulae_label.setMaximumWidth(200)

        self.formulae_file_layout = QHBoxLayout()
        self.formulae_file_edit = QLineEdit()
        self.formulae_input = QPushButton("Select")
        self.formulae_input.clicked.connect(self.set_formulae_file)
        self.formulae_input.setMaximumWidth(50)
        self.formulae_file_edit.setMaximumWidth(150)
        self.formulae_file_edit.setPlaceholderText("Your Formula file")
        self.formulae_file_layout.addWidget(self.formulae_file_edit)
        self.formulae_file_layout.addWidget(self.formulae_input)
        self.input_layout.addLayout(self.formulae_file_layout, 8, 1)

        # Explicit NI hardware validation
        self.validate_ni_button = QPushButton("Validate NI Hardware")
        self.validate_ni_button.setMaximumWidth(180)
        self.validate_ni_button.clicked.connect(self.validate_ni_hardware)

        # Buttons to begin DAQ
        self.acquisition_button = QPushButton("Start Acquisition")
        self.acquisition_button.setCheckable(True)
        self.acquisition_button.clicked.connect(self.acquisition_begins)
        self.acquisition_button.setMaximumWidth(180)
        self.acquisition_button.setEnabled(False)

        self.save_button = QPushButton("Save")
        self.save_button.setEnabled(False)
        self.save_button.setCheckable(True)
        self.save_button.clicked.connect(self.save_data)
        self.save_button.setMaximumWidth(180)
        self.formulae_file = ""

        # Acquisition controls row
        self.controls_layout = QHBoxLayout()
        self.controls_layout.addWidget(self.validate_ni_button)
        self.controls_layout.addWidget(self.acquisition_button)
        self.controls_layout.addWidget(self.save_button)
        self.input_layout.addLayout(self.controls_layout, 9, 0, 1, 3,)

        self.config_file_edit.textChanged.connect(self._invalidate_ni_validation)
        self.acquisition_mode_input.currentIndexChanged.connect(
            self._on_acquisition_mode_changed
        )

        # self.save_bool = False

        # Unified operations console: device health, system messages,
        # recent saved operator events, and parallel event drafts.
        self.notifications_layout = QVBoxLayout()
        self.panel = OperationsConsole(self, parent=self)
        self.panel.setMaximumHeight(590)
        self.notifications_layout.addWidget(self.panel)
        self.operator_events = self.panel.operator_events
        self.operator_events.setEnabled(False)

        self.main_input_layout.addLayout(self.input_layout)
        self.main_input_layout.addLayout(self.notifications_layout)
        self.data_visualizer_layout = QHBoxLayout()
        self.main_input_layout.addLayout(self.data_visualizer_layout)
        self.input_settings_widget.setLayout(self.main_input_layout)

        return self.input_settings_widget

    def save_notifs(self):
        file_name, _ = QFileDialog.getSaveFileName(self, "Save File", "", "Text Files (*.txt);;All Files (*)")  # noqa E501
        if file_name:
            with open(file_name, 'w') as file:
                file.write(self.panel.toPlainText())

    def log_Obs(self):
        """Calls `notify` and clears the `notif_txt_edit`
        """
        text = self.log_obs_txt.text()
        if text:
            self.notify(text, type="observation")
            self.log_obs_txt.clear()

    def notify(self, text="", type="default"):
        """Method that logs observations written in `log_obs_txt`
        and adds to the top of the notification panel.

        Any observations written here will be added
        to the notification panel, and a time stamp (format HH:MM:SS)
        at which the "Log Obs." is clicked will be
        appended to the written text.

        Arguments
        _________
            type: str
                Type of event: is one of the folowing
                    "event", "info", "warning", "error", "success", "default"
            str: str
                Any string written in `notif_txt_edit`

        Example
        _______
            If written observation is "Ignition observed"

            Notification text update will be,
                `[13:23:56] Ignition Observed`
        """
        self.panel.add_message(type, text)

    def set_test_file(self):
        """ Method that opens a `SaveSettingsDialog`
        and asks for filename and folder to save the file in.
        """
        dlg_save_file = SaveSettingsDialog("Select File to Save Data")
        self.menu._style_popup(dlg_save_file)
        if dlg_save_file.exec() == QDialog.Accepted:
            self.common_path = dlg_save_file.file_path
            file_pq = self.common_path + ".parquet"
            file_json = self.common_path + ".json"
            folder_pq = dlg_save_file.folder_path
            if os.path.exists(folder_pq):
                if os.path.exists(file_pq) or os.path.exists(file_json):
                    self.inform_user("File to save in already exists.")
                else:
                    self.parquet_file = file_pq
                    self.json_file = file_json
                    self.test_input.setText(self.parquet_file)
        return

    def set_formulae_file(self):
        """ Method that opens a `QFileDialog` to open a
        .csv formulae file
        """
        dlg = QFileDialog(self, 'Select a File', None, "CSV files (*.csv)")
        f = ""
        if dlg.exec():
            filenames = dlg.selectedFiles()
            f = open(filenames[0], 'r')
        if not isinstance(f, str):
            self.formulae_file = f.name
            self.formulae_file_edit.setText(self.formulae_file)
        return

    def set_config_file(self):
        """ Method that opens a `QFileDialog` to open a
        .csv NI config file
        """
        dlg = QFileDialog(self, 'Select a File', None, "CSV files (*.csv)")
        f = ""
        if dlg.exec():
            filenames = dlg.selectedFiles()
            f = open(filenames[0], 'r')
        if not isinstance(f, str):
            self.config_file = f.name
            self.config_file_edit.setText(self.config_file)
        return

    def dev_arr_to_dict(self):
        """ Method to store all user-added devices
        in one dictionary to allow saving a global
        configuration for the GUI.
        """
        dict_dev = {}
        if self.lasers:
            dict_dev["Lasers"] = {}
            for laser in self.lasers:
                laser_item = self.lasers[laser]
                dict_dev["Lasers"][laser] = laser_item.settings_to_dict()
        if self.mfcs:
            dict_dev["MFCs"] = {}
            for mfc in self.mfcs:
                mfc_item = self.mfcs[mfc]
                dict_dev["MFCs"][mfc] = mfc_item.settings_to_dict()
        if self.mfms:
            dict_dev["MFMs"] = {}
            for mfm in self.mfms:
                mfm_item = self.mfms[mfm]
                dict_dev["MFMs"][mfm] = mfm_item.settings_to_dict()
        return dict_dev

    def is_valid_path(self, path):
        """ Method that checks if the input path
        is a valid path

        Parameters
        ----------
            path: str
                Path to check for validity

        Returns
        -------
            The return value. True for valid file path. False otherwise.
        """
        try:
            if os.path.isabs(path):
                if os.path.normpath(path):
                    return True
            return False
        except (TypeError, ValueError):
            return False

    def _selected_acquisition_mode(self) -> AcquisitionMode:
        widget = getattr(self, "acquisition_mode_input", None)
        if widget is None:
            return AcquisitionMode.FULL
        value = widget.currentData()
        return AcquisitionMode(value or AcquisitionMode.FULL)

    def _is_serial_only(self) -> bool:
        return self._selected_acquisition_mode() == AcquisitionMode.SERIAL_ONLY

    def _on_acquisition_mode_changed(self, *_args) -> None:
        mode = self._selected_acquisition_mode()
        if getattr(self, "_last_mode", None) == mode:
            return
        self._last_mode = mode
        engine = getattr(self, "engine", None)
        if engine is not None:
            try:
                engine.set_mode(mode)
            except RuntimeError as exc:
                self.inform_user(str(exc))
                index = self.acquisition_mode_input.findData(engine.mode)
                if index >= 0:
                    self.acquisition_mode_input.blockSignals(True)
                    self.acquisition_mode_input.setCurrentIndex(index)
                    self.acquisition_mode_input.blockSignals(False)
                return

        serial_only = mode == AcquisitionMode.SERIAL_ONLY
        for widget_name in (
            "sample_rate_label",
            "sample_rate_input",
            "config_label",
            "config_file_edit",
            "config_input",
            "validate_ni_button",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setEnabled(not serial_only)

        if serial_only:
            self.labels_to_save = []
            self.acquisition_button.setEnabled(True)
            self.validate_ni_button.setText("NI Not Required")
            self.notify("Acquisition mode: SERIAL_ONLY", "info")
        else:
            validated = bool(engine and engine.ni_hardware_validated)
            self.acquisition_button.setEnabled(validated)
            self.validate_ni_button.setText(
                "NI Validated" if validated else "Validate NI Hardware"
            )
            self.notify("Acquisition mode: FULL", "info")

    def _validate_serial_only_devices(self) -> None:
        registry = getattr(self, "device_registry", None)
        if registry is None:
            raise RuntimeError("Device registry is unavailable.")
        serial_devices = [
            device
            for device in registry.devices()
            if device.device_type != "ni_daq"
            and device.state in (DeviceState.CONNECTED, DeviceState.RUNNING)
        ]
        if not serial_devices:
            raise RuntimeError(
                "Serial Only mode requires at least one connected serial device. "
                "Connect a device in Device Manager before starting acquisition."
            )

    def _all_fields_filled(self):
        required = (
            self.name_input.text().strip(),
            self.proj_input.text().strip(),
            self.test_input.text().strip(),
        )
        if not all(required):
            raise UnfilledFieldError("Operator, project, and test fields are required.")

        if not self._is_serial_only():
            if not self.config_file_edit.text().strip():
                raise UnfilledFieldError("NI configuration file is required in Full mode.")
            if not self.sample_rate_input.text().strip():
                raise UnfilledFieldError("NI sampling rate is required in Full mode.")
        return True


    def validate_df(self, letter, path):
        """Method to check if the config or formulae file path
        provided contains valid columns

        Parameters
        ----------
            letter: str
                Indicating either config ("c") or formulae ("f") file path
            path: str
                path to the indicated file
        Returns
        ------
            The return value.

            True if columns in the file match with columns for each file.
            See details in "Config File Example"
            and "Formulae File Example" for required and
            necessary columns, and how they are used.
        """
        try:
            df = pl.read_csv(path)
            cols = []
        except Exception:
            return False
        if letter == "f":
            cols = ["Label", "RHS", "Chart", "Legend",
                    "Layout", "Position", "Processed_Unit"]
        if letter == "c":
            cols = ["#", "Panel", "Device", "Channel",
                    "ScaleMax", "ScaleMin", "Label", "TCType",
                    "Type", "Chart", "AIRangeMin", "AIRangeMax",
                    "Layout", "Position", "Processed_Unit", "Legend"]
        cols.sort()
        df_cols = [i.strip() for i in df.columns]
        df_cols.sort()
        col_intersect = list(set(cols) & set(df_cols))
        # print(col_intersect, " \n", cols, "\n", df_cols)
        if letter == "f":
            if cols == df_cols:
                return True
        elif letter == "c":
            if self.dashboard:
                # todo: Add condition for when display is only tab
                # or display is dashboard, or both
                if len(col_intersect) == len(cols):
                    return True
            else:
                if all(e in col_intersect for e in ["Device", "Channel", "Type", "TCType"]):  # noqa: E501
                    # Allow running acquisition for a minimal config file
                    return True
        return False

    def set_up(self):
        """Validate current GUI settings and build the run configuration."""
        self._all_fields_filled()
        mode = self._selected_acquisition_mode()
        self.settings["Acquisition Mode"] = mode.value

        if (
            re.fullmatch(self.re_strAllowable, self.name_input.text())
            and re.fullmatch(self.re_strAllowable, self.proj_input.text())
        ):
            self.settings["Name"] = self.name_input.text()
            self.settings["Project Name"] = self.proj_input.text()
            self.settings["Series Name"] = self.series_input.text()
        else:
            raise ValueError(
                "Operator and project names may contain only letters, numbers, "
                "and underscores."
            )

        self.settings["Experiment Type"] = self.test_type_input.currentText()
        self.Create_SavePath()

        formula_path = self.formulae_file_edit.text().strip()
        if formula_path and not self.validate_df("f", formula_path):
            raise ValueError("Formulae file does not meet requirements.")
        self.settings["Formulae File"] = formula_path
        self.formulae_file = formula_path

        if mode == AcquisitionMode.SERIAL_ONLY:
            self.settings["Sampling Rate"] = 0.0
            self.settings["Config File"] = ""
            self.config_file = ""
            self.labels_to_save = []
        else:
            try:
                sampling_rate = float(self.sample_rate_input.text())
            except ValueError as exc:
                raise ValueError("Invalid NI sampling rate.") from exc
            if sampling_rate <= 0:
                raise ValueError("NI sampling rate must be positive.")

            config_path = self.config_file_edit.text().strip()
            if not self.validate_df("c", config_path):
                raise ValueError("NI configuration file does not meet requirements.")

            self.settings["Sampling Rate"] = sampling_rate
            self.settings["Config File"] = config_path
            self.config_file = config_path
            self.config_df = pl.read_csv(config_path)
            self.config_df.columns = [column.strip() for column in self.config_df.columns]
            self.labels_to_save = self.config_df.select("Label").to_series().to_list()

        devices = self.dev_arr_to_dict() if self.device_arr else {}
        serial_devices = serial_devices_to_settings(self)
        if serial_devices:
            devices["SerialDevices"] = serial_devices
        if devices:
            self.settings["Devices"] = devices
        else:
            self.settings.pop("Devices", None)


    def Create_SavePath(self):
        """Method to create paths to
        save all data, NI settings,
        depending on the test name (`inp_text`).

        - If the user has selected a file to save using
        the `test_btn` generated in `input_content(),
        the filename is checked in the path provided.
        If a filename of that name already exists,
        the filename is appended with `_XX` number,
        that increments by 1 for every repeated
        filename.
            Example: If the filename is `Exp1` in
            directory  `C:/Users/XXX/Tests/`,
            and file by that name exits, the data
            will be saved in the following format.
            `C:/Users/XXX/Tests/Exp1_01`.
            Extensions `.parquet` for NI data,
            `.json` for NI data info, `.csv`
            for Alicat devices will be added to `Exp1_01`.

        - If the user types just a text in `inp_text`,
        a directory and file path to save all data
        is generated in the directory where
        this application is compiled.
            Example: If the `inp_text` is "Test1",
            Experiment type is "Experiment",
            User name is "User",
            Project name is "Project",
            the save path for NI data will be the following.

            "./02_ExperimentData/YYYYProject/YYYYMMDD_HHMMSS_User_Project_Series_Test1.parquet".

            `02_ExperimentData` directory will be created
            in the current working directory.
            `YYYYProject` directory will be
            created inside this directory.

            If the experiment type is calibration,
            it will be saved in `01_CalibrationData` instead.

            The YYYY, MM, DD, HH, MM, SS indicate
            the year, month, date, hour, minute, and seconds
            respectively when the `save_button` is clicked.
        """
        inp_text = self.test_input.text()
        fname = inp_text.split(self.fext)[0]
        fpath = inp_text + self.fext
        if self.is_valid_path(fpath):
            # If the user selected a custom path to save the data
            if os.path.isfile(fpath):
                # If there is already a file by that name
                test_name = f"{fname}"

                files = glob.glob(f"{fname}*{self.fext}")
                if len(files) == 1:  # one previous file
                    # Checking if there is any appended number at the
                    # last 3 characters before extension
                    fnumber = re.findall(r'\d+', files[0].split(self.fext)[0][-3:])  # noqa: E501
                    if not fnumber:
                        # If no number at the end of that file, append `_01`
                        test_name = f"{fname}_01"
                    else:
                        # If there is a number, get the number, increment by 1
                        f_x = str(int(fnumber[0])+1).rjust(2, '0')
                        test_name = f"{fname.split('_'+fnumber[0])[0]}_{f_x}"
                else:
                    # If previous files exits, sort them, get the last one,
                    # and increment the number
                    files = [sorted(files)[-1]]
                    fnumber = re.findall(r'\d+', files[0].split(self.fext)[0][-3:])  # noqa: E501
                    f_x = str(int(fnumber[0])+1).rjust(2, '0')
                    test_name = f"{fname.split('_'+fnumber[0])[0]}_{f_x}"
            else:
                # no previous file
                test_name = fname
        else:
            # If the test name is only the name and the program will create
            # the file path to save
            if re.match(self.re_strAllowable, fname):
                cwd = os.getcwd()
                now = datetime.now()
                if self.settings["Experiment Type"] == 'Calibration':
                    savedir_name = ('01_' + self.settings["Experiment Type"]
                                    + 'Data')
                else:
                    savedir_name = ('02_' + self.settings["Experiment Type"]
                                    + 'Data')
                self.save_dir = (cwd + os.sep +
                                 savedir_name +
                                 os.sep)
                if not os.path.exists(self.save_dir):
                    os.mkdir(self.save_dir)
                project_dirname = (now.strftime("%Y") +
                                   self.settings["Project Name"] +
                                   os.sep +
                                   self.settings["Series Name"] +
                                   os.sep)
                self.save_dir = self.save_dir + project_dirname
                if not os.path.exists(self.save_dir):
                    os.mkdir(self.save_dir)
                self.save_dir = (self.save_dir + now.strftime("%Y%m%d_%H%M%S")
                                 + "_" + self.settings["Name"] + "_" +
                                 self.settings["Project Name"] + "_" +
                                 self.settings["Series Name"] + "_")
                test_name = fname
            else:
                raise ValueError("""Check test name. It should be either a\
                                 valid path or a test name that can only\
                                 contain alphanumeric or\
                                 contain underscores (no spaces).""")

        self.json_file = test_name + ".json"
        self.parquet_file = test_name + ".parquet"
        if self.is_valid_path(inp_text):
            self.settings["Test Name"] = self.parquet_file
        else:
            self.settings["Test Name"] = self.save_dir + self.parquet_file
        self.common_path = test_name
        self.test_input.setText(test_name)

    def settings_to_json(self):
        """Serialize GUI and device settings, including acquisition mode."""
        self.set_up()
        self.settings["Test Name"] = self.test_input.text()
        return json.dumps(self.settings, indent=4)

    def _set_texts(self):
        """Populate input controls from loaded settings."""
        self.proj_input.setText(self.settings.get("Project Name", ""))
        self.name_input.setText(self.settings.get("Name", ""))
        self.series_input.setText(self.settings.get("Series Name", "",))
        self.test_input.setText(self.settings.get("Test Name", ""))
        self.save_dir = os.path.dirname(self.settings.get("Test Name", ""))
        self.common_path = self.settings.get("Test Name", "").split(".parquet")[0]
        self.sample_rate_input.setText(str(self.settings.get("Sampling Rate", "")))
        self.formulae_file = self.settings.get("Formulae File", "")
        self.formulae_file_edit.setText(self.formulae_file)
        self.test_type_input.setCurrentText(
            self.settings.get("Experiment Type", "Experiment")
        )
        self.config_file = self.settings.get("Config File", "")
        self.config_file_edit.setText(self.config_file)

        mode_value = self.settings.get("Acquisition Mode", AcquisitionMode.FULL.value)
        try:
            mode = AcquisitionMode(mode_value)
        except ValueError:
            mode = AcquisitionMode.FULL
        index = self.acquisition_mode_input.findData(mode)
        if index >= 0:
            self.acquisition_mode_input.setCurrentIndex(index)
        firepydaq_logger.info("%s: Configuration controls updated.", __name__)

    def inform_user(self, err_txt):
        """Method to inform important
        operations, errors, and warnings
        to the user using a pop-up QMessageBox.
        """
        self.msg = QMessageBox()
        self.msg.setWindowTitle("Error Encountered")
        self.msg.setText(err_txt)
        if self.curr_mode == "Dark":
            f = open(self.popup_dark, "r")
        else:
            f = open(self.popup_light, "r")
        str = f.read()
        self.msg.setStyleSheet(str)
        f.close()
        self.msg.exec()

    def validate_fields(self):
        """Validate experiment settings and NI processing inputs when applicable."""
        self.set_up()
        if self._is_serial_only():
            self._validate_serial_only_devices()
            return

        if self.display and self.tab and hasattr(self, "data_vis_tab"):
            self.data_vis_tab.set_labels(self.config_file)

        config_df = pl.read_csv(self.settings["Config File"])
        random_input = np.array(
            [np.random.randint(0, 10) * item for item in np.ones(config_df.select("Label").shape)]
        )
        random_dict = {
            label: random_input[index]
            for index, label in enumerate(self.labels_to_save)
        }
        random_df = pl.DataFrame(data=random_dict)
        checker = PostProcessData(
            datapath=random_df,
            configpath=self.settings["Config File"],
            formulaepath=self.settings["Formulae File"],
        )
        checker.ScaleData()
        checker.UpdateData(dump_output=False)

    def initiate_dataArrays(self):
        """Initialize NI visualization buffers; serial-only runs need empty buffers."""
        ni_device = getattr(self, "NIDAQ_Device", None)
        if ni_device is None:
            self.ydata = np.empty(0)
        elif ni_device.ai_counter > 0:
            if len(ni_device.ailabel_map) == 1:
                self.ydata = np.empty(0)
            else:
                self.ydata = np.empty((len(ni_device.ailabel_map), 0))
        else:
            self.ydata = np.empty((len(self.labels_to_save), 0))

        self.xdata = np.array([0])
        self.abs_timestamp = np.array([])
        self.timing_np = np.empty((0, 3))

    def _invalidate_ni_validation(self, *_args) -> None:
        if self._is_serial_only():
            return
        engine = getattr(self, "engine", None)
        if engine is not None:
            engine.invalidate_ni_validation()
        self.validate_ni_button.setEnabled(True)
        self.validate_ni_button.setText("Validate NI Hardware")
        if not self.acquisition_button.isChecked():
            self.acquisition_button.setEnabled(False)

    def validate_ni_hardware(self) -> None:
        """Validate GUI fields and create a connected NI runtime."""
        if self._is_serial_only():
            self.inform_user("NI validation is not used in Serial Only mode.")
            return
        try:
            self.validate_fields()
            self.NIDAQ_Device = self.engine.validate_ni_hardware(
                parent=self,
                name="NI Task",
                config_path=self.settings["Config File"],
                sampling_rate_hz=float(self.settings["Sampling Rate"]),
            )
        except Exception as exc:
            self.validate_ni_button.setText("Validation Failed")
            self.acquisition_button.setEnabled(False)
            self.inform_user(str(exc))
            self.notify(f"NI validation failed: {exc}", "error")
            return

        self.validate_ni_button.setText("NI Validated")
        self.validate_ni_button.setEnabled(False)
        self.acquisition_button.setEnabled(True)

    def acquisition_begins(self):
        """Start or stop acquisition using the explicitly selected hardware mode."""
        if not self.acquisition_button.isChecked():
            if hasattr(self, "raw_publisher"):
                try:
                    self.raw_publisher.socket.close()
                except Exception:
                    pass
                del self.raw_publisher
            self.engine.stop_acquisition()
            if self.engine.publisher is not None:
                self.engine.publisher.stop()
            self.run_counter = 0
            self.save_button.setEnabled(False)
            return

        self.run_counter = 0
        self._mode = self._selected_acquisition_mode()
        try:
            self.set_up()
            if self._mode == AcquisitionMode.SERIAL_ONLY:
                self._validate_serial_only_devices()
                self.NIDAQ_Device = None
                self.engine.start_acquisition()
            else:
                if not self.engine.ni_hardware_validated:
                    raise RuntimeError("Validate NI hardware before starting acquisition.")
                self.NIDAQ_Device = self.engine.start_ni_acquisition(
                    parent=self,
                    name="NI Task",
                    config_path=self.settings["Config File"],
                    sampling_rate_hz=float(self.settings["Sampling Rate"]),
                )
                self.initiate_dataArrays()
        except Exception as exc:
            self.acquisition_button.setChecked(False)
            self.inform_user(f"Acquisition could not start:\n{exc}")
            self.notify(f"Acquisition startup failed: {exc}", "error")
            return

        self.acquisition_start_monotonic = time.monotonic()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.settings["Run ID"] = (
            f"{timestamp}_"
            f"{self.name_input.text()}_"
            f"{self.proj_input.text()}_"
            f"{self.series_input.text()}_"
            f"{self.test_input.text()}"
        )
        self.engine.publish_metadata(
                        settings=self.settings,
                        ni_labels=self.labels_to_save if self._mode == AcquisitionMode.FULL else (),
                        force=True,
                    )
        if hasattr(self, "operator_events"):
            try:
                self.operator_events.configure_for_current_test()
            except Exception as exc:
                self.notify(
                    f"Operator event file was not initialized: {exc}",
                    "warning",
                )

        self.save_button.setEnabled(True)
        self.acquisition_button.setText("Stop Acquisition")

        if self.engine.publisher is not None:
            self.engine.publisher.start()
            self.engine.publish_metadata(
                settings=self.settings,
                ni_labels=self.labels_to_save if self._mode == AcquisitionMode.FULL else (),
                force=True,
            )

        if not hasattr(self, "raw_publisher"):
            self.raw_publisher = RawDataPublisher()
        self.runpyDAQ()

    @error_logger("SaveData")
            # self.save_bool = False

    def safe_exit(self):
        """Stop acquisition through the engine and close the application."""
        if hasattr(self, "engine"):
            self.engine.stop_acquisition()
        self.close()


# ---------------------------------------------------------------------------
# Engine-integrated application adapter
# ---------------------------------------------------------------------------

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


import ctypes
from .DeviceHealth_Chunks import DeviceHealthManager, ChunkManifestManager
from .device_registry import DeviceRegistry
from .alicat_device import AlicatDevice
from .abstract_device import DeviceState
from ..utilities.firepydaq_path import (get_firepydaq_dir, get_active_run_dir, )
from ..utilities.serial_runtime import SerialDeviceManager
from ..utilities.DAQUtils import AlicatGases
from firepydaq.core.save_manager import (
    DataBlock,
    SaveManager,
)


FIREPYDAQ_DIR = get_firepydaq_dir()

DASHBOARD_BUFFER_SAMPLES = 1200
WRITER_QUEUE_BLOCKS = 32
WRITER_STOP_TIMEOUT_S = 30.0
PARQUET_COMPRESSION = "zstd"

class application(_GuiApplication):
    def _write_device_health_from_registry(self):
        """Persist every registered AbstractDevice from current snapshots."""
        manager = getattr(self, "device_health", None)
        registry = getattr(self, "device_registry", None)
        if manager is None or registry is None:
            return
        write_registry = getattr(manager, "write_registry", None)
        if callable(write_registry):
            write_registry(registry)
        else:
            manager.write()

    """Drop-in legacy GUI subclass with bounded-memory, disk-backed recording."""

    def __init__(self):
        super().__init__()

        import threading

        if not hasattr(self, "vis_lock"):
            self.vis_lock = threading.Lock()

        self._writer: object = None
        self._writer_started = False
        self.elapsed_time_offset = 0.0
        self.save_time_offset = 0.0
        self._finalize_lock = threading.Lock()
        self._writer_messages: queue.Queue = queue.Queue(maxsize=100)
        self._writer_message_timer = QTimer(self)
        self._writer_message_timer.timeout.connect(self._drain_writer_messages)
        self._writer_message_timer.start(250)
        self._dashboard_process = None
        self.queue_warning_75_sent = False
        self.queue_warning_90_sent = False
        self._latest_ni_values = None
        self._mode = AcquisitionMode.FULL

        # Unified non-blocking device registry. NI remains on its hardware-
        # timed path during this migration; Alicat and streaming serial devices
        # are consumed through immutable snapshots.
        self.device_registry = DeviceRegistry()
        self._serial_workers_started = False

        self.device_health = None
        self.manifest = None
        self.last_device_health_write = 0.0

        from firepydaq.core import (AcquisitionEngine, EngineCallbacks,)

        self.save_manager = SaveManager(
            active_run_dir_getter=get_active_run_dir,
            firepydaq_dir=FIREPYDAQ_DIR,
            notify=self.notify,
            device_registry=self.device_registry,
            )

        self.engine = AcquisitionEngine(
            device_registry=self.device_registry,
            callbacks=EngineCallbacks(
                notify=self.notify,
                state_changed=self._on_engine_state_changed,
            ),
            save_manager=self.save_manager,
            health_manager=self.device_health,
            ni_device_factory=NIDaqDevice,
                   mode=AcquisitionMode.FULL,
        )

        self.engine.configure_cycle_scheduler(
            scheduler=QTimer.singleShot,
            callback=self.runpyDAQ,
            delay_ms=1,
        )

        from firepydaq.telemetry.mqtt_publisher import (MqttPublisher,)
        publisher = MqttPublisher(notify=self.notify,)

        self.engine.publisher = publisher

    def _on_engine_state_changed(self, state,):
        pass

    def _drain_writer_messages(self) -> None:
        for level, message in self.save_manager.drain_messages():
            self.notify(message, level)

    def _start_safe_writer(self) -> None:
        """Compatibility wrapper; SaveManager owns writer startup."""
        if self.save_manager.active:
            return
        self.save_manager.start(
            self.common_path,
            manifest=self.manifest,
        )
        self._writer = self.save_manager.writer
        self._writer_started = True
        firepydaq_logger.info(
            "Disk-backed Parquet writer started: %s",
            self._writer.chunk_dir,
        )

    def _finalize_safe_writer(self):
        """Compatibility wrapper; SaveManager owns final consolidation."""
        result = self.save_manager.stop()
        self._writer = None
        self._writer_started = False
        return result

    @staticmethod
    def _append_dashboard_buffer(existing, new, max_samples=DASHBOARD_BUFFER_SAMPLES):
        new = np.asarray(new)
        existing = np.asarray(existing)
        if new.ndim == 1:
            combined = np.concatenate((existing.reshape(-1), new.reshape(-1)))

            max_samples = int(max_samples)

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
        self.engine.capture_serial_snapshots()
        self.engine.update_health()

        cycle = None
        ni_device = getattr(self, "NIDAQ_Device", None)
        if self.engine.mode == AcquisitionMode.FULL and ni_device is not None:
            cycle = self.engine.read_ni_cycle(
                ni_device,
                labels=self.labels_to_save,
                datetime_format=self.dt_format,
            )
            if cycle is not None:
                self._latest_ni_values = cycle.values

        if self.engine.publisher is not None:
            snapshots = self.engine.collect_snapshots()
            ni_labels = self.labels_to_save if self.engine.mode == AcquisitionMode.FULL else ()
            ni_values = self._latest_ni_values if self.engine.mode == AcquisitionMode.FULL else None
            values = self.engine.publisher.payload_builder.live_values(
                ni_labels=ni_labels,
                ni_values=ni_values,
                snapshots=snapshots,
            )
            self.engine.publisher.publish_live(values)
            self.engine.publisher.publish_health(
                self.engine.publisher.payload_builder.health(snapshots)
            )

        if cycle is not None:
            try:
                self.xdata_new = cycle.acquisition_time
                self.ydata_new = cycle.values
                self.abs_timestamp = list(cycle.absolute_time)

                fill = self.save_manager.queue_fill_ratio
                if self.engine.saving:
                    if fill > 0.90 and not self.queue_warning_90_sent:
                        self.notify(f"Writer queue at {fill:.0%} capacity", "warning")
                        self.queue_warning_90_sent = True
                    elif fill > 0.75 and not self.queue_warning_75_sent:
                        self.notify(f"Writer queue at {fill:.0%} capacity", "warning")
                        self.queue_warning_75_sent = True
                    elif fill < 0.50:
                        self.queue_warning_75_sent = False
                        self.queue_warning_90_sent = False

                self.xdata = self._append_dashboard_buffer(self.xdata, self.xdata_new)
                self.ydata = self._append_dashboard_buffer(self.ydata, self.ydata_new)

                if hasattr(self, "data_vis_tab"):
                    if not hasattr(self.data_vis_tab, "dev_edit"):
                        self.data_vis_tab.set_labels(self.config_file)
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
                            if self.vis_lock.locked():
                                self.vis_lock.release()
                            raise

                if cycle.processing_overrun:
                    self.notify(
                        "Data-loss warning: acquisition processing exceeded one "
                        "hardware block duration.",
                        "warning",
                    )
            except Exception as exc:
                if self.device_health is not None:
                    try:
                        self.device_health.error("NI")
                    except Exception:
                        pass
                self.notify(f"NI cycle handling failed: {exc}", "error")
                traceback.print_exc()

        if (
            self.device_health is not None
            and time.monotonic() - self.last_device_health_write > 5.0
        ):
            self.device_health.update_stale_states()
            self.engine.update_health(force=True)
            self.last_device_health_write = time.monotonic()

        if self.running and self.engine.schedule_next_cycle():
            return

        self.run_counter = 0
        self.acquisition_button.setText("Start Acquisition")
        self.save_button.setEnabled(False)
        self.elapsed_time_offset = 0
        self._latest_ni_values = None
        self._stop_dashboard()


    @error_logger("SaveData")
    def save_data(self):
        if self.save_button.isChecked():
            self.save_button.setText("Stop")
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

            with json_path.open("x", encoding="utf-8",) as outfile:
                json.dump(settings_copy, outfile, indent=4,)

            self.initiate_dataArrays()

            health_dir = (".firepydaq")
            self.device_health = DeviceHealthManager(health_dir)
            engine = getattr(self, "engine", None)
            if engine is not None:
                engine.set_health_manager(self.device_health)
            self.last_device_health_write = 0.0

            # Register only devices participating in this run.
            if self.engine.mode == AcquisitionMode.FULL:
                self.device_health.register("NI", "DAQ")

            # Alicats
            if hasattr(self, "mfcs"):
                for mfcname in self.mfcs.keys():
                    self.device_health.register(mfcname, "ALICAT")
                configured_devices.extend(list(self.mfcs.keys()))

            # User-added devices
            if hasattr(self, "devices"):
                for devicename in self.devices.keys():
                    self.device_health.register(devicename, "DEVICE")

            chunk_dir = get_active_run_dir(self.common_path)
            chunk_dir.mkdir(parents=True, exist_ok=True,)

            self.manifest = ChunkManifestManager(chunk_dir, self.settings, self.labels_to_save,)

            self.manifest.write()

            self.settings["Run ID"] = Path(self.common_path).name
            self.engine.start_save(
                self.common_path,
                manifest=getattr(self, "manifest", None),
            )
            if hasattr(self, "operator_events"):
                self.operator_events.setEnabled(True)
            self.engine.publish_metadata(
                            settings=self.settings,
                            ni_labels=self.labels_to_save if self._mode == AcquisitionMode.FULL else (),
                            force=True,
                        )
            self.save_time_offset = self.elapsed_time_offset
            self.save_begin_time = time.time()
            if hasattr(self, "operator_events"):
                try:
                    self.acquisition_start_monotonic = time.monotonic()
                    self.operator_events.configure_for_current_test()
                    if hasattr(self.panel, "recent_events"):
                        self.panel.recent_events.clear()
                except Exception as exc:
                    self.notify(f"Operator event initialization failed: {exc}", "warning",)
            firepydaq_logger.info("Safe saving initiated")

            if (
                self.engine.mode == AcquisitionMode.FULL
                and getattr(self, "NIDAQ_Device", None) is not None
            ):
                configured_devices.insert(0, "NI")

            self.notify("Devices: " + ", ".join(configured_devices), "info")
            save_file = Path(self.settings['Test Name'])
            rel_path = str(save_file.with_suffix(""))
            self.notify(f"Save file: {rel_path}", "info")

            if self.dashboard:
                self.settings["Data File"] = self.common_path + ".parquet"
                self._start_dashboard()
        else:
            self.save_button.setText("Save")
            self.panel.operator_events.path_label.clear()
            self.panel.recent_events.clear()
            self.engine.stop_save()
            if hasattr(self, "operator_events"):
                self.operator_events.setEnabled(False)

            # self._finalize_safe_writer()
            self._stop_dashboard()

    def closeEvent(self, *args, **kwargs):
        self._stop_serial_workers()
        self.running = False
        self._writer_message_timer.stop()
        if self.save_manager.active:
            self._finalize_safe_writer()
        self.engine.invalidate_ni_validation()
        if self.engine.publisher is not None:
            self.engine.publisher.stop()
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
                        poll_interval_s=2.0,
                        notify=self.notify,
                    )
                )

        for runtime in getattr(self, "generic_serial_devices", {}).values():
            if self.device_registry.get(runtime.name) is None:
                self.device_registry.register(runtime)

        # The Device Manager owns connect/disconnect. AcquisitionEngine owns
        # start/stop for all registered participating devices.
        self._serial_workers_started = True

    def _stop_serial_workers(self):
        try:
            self.device_registry.stop_all()
        except Exception as exc:
            firepydaq_logger.warning("Device stop warning: %s", exc)
        finally:
            self._serial_workers_started = False
        self.engine.update_health()
