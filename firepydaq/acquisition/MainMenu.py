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

from PySide6.QtWidgets import QTabWidget, QMenuBar, QDialog
from PySide6.QtGui import QAction, QActionGroup

import webbrowser
from jsonschema import validate
import json
import os
from pathlib import Path

from .DeviceNameDialog import DeviceNameDialog
from .RemoveDeviceDialog import RemoveDeviceDialog
from .SaveSettingsDialog import SaveSettingsDialog
from .LoadSettingsDialog import LoadSettingsDialog
from .schema import schema
from .display_data_tab import data_vis
from .SerialConfigPersistence import restore_serial_devices
from .DeviceManager import install_device_manager
from .DeviceMonitor import show_device_monitor

from .device import alicat_mfc
from .device import mfm

from ..utilities.ErrorUtils import error_logger
from ..utilities.DAQUtils import AlicatGases
from .alicat_device import AlicatDevice


class MainMenu(QMenuBar):
    """Creates menu bar for the application

    Attributes
    ----------
        parent: object
            Parent class
    """
    def __init__(self, parent):
        super(MainMenu, self).__init__()
        self._makemenu(parent)

    def _makemenu(self, parent):
        self.parent = parent
        # File Menu Button
        self.file_menu = self.addMenu("File")
        # Loading Action
        self.load_daq_action = QAction("Load DAQ Configuration", self)
        self.load_daq_action.setObjectName("LoadJson")
        self.load_daq_action.triggered.connect(self.load_json_settings)
        self.file_menu.addAction(self.load_daq_action)
        self.load_daq_action.setShortcut("Ctrl+L")

        self.save_daq_action = QAction("Save DAQ Configuration", self)
        self.save_daq_action.triggered.connect(self.save_settings_to_json)
        self.file_menu.addAction(self.save_daq_action)
        self.save_daq_action.setShortcut("Ctrl+S")

        self.exit_action = QAction("Exit Application", self)
        self.exit_action.triggered.connect(self.parent.safe_exit)
        self.file_menu.addAction(self.exit_action)
        self.exit_action.setShortcut("Alt+X")

        # Central device manager. Legacy add/remove menus remain available
        # during migration so existing configuration loading is not broken.
        self.devices_menu = self.addMenu("Devices")
        self.manage_devices_action = QAction("Open Device Manager", self)
        self.manage_devices_action.setShortcut("Ctrl+D")
        self.manage_devices_action.triggered.connect(
            lambda: install_device_manager(self.parent)
        )
        self.devices_menu.addAction(self.manage_devices_action)
        self.monitor_devices_action = QAction("Monitor", self)
        self.monitor_devices_action.setShortcut("Ctrl+Shift+D")
        self.monitor_devices_action.triggered.connect(
            lambda: show_device_monitor(self.parent)
        )
        self.devices_menu.addAction(self.monitor_devices_action)

        # Display Data Menu Button
        self.display_data_menu = self.addMenu("Display Data")
        self.display_data_type_menu = self.display_data_menu.addMenu("Display")
        self.display_type = QActionGroup(self, exclusive=True)

        self.no_display = QAction("No Display (Default)", self, checkable=True)
        self.no_display.setObjectName("NoDisp")
        self.no_display.triggered.connect(self._do_not_display)
        self.no_display.setChecked(True)
        self.display_data_menu.addAction(self.no_display)

        self.dash_display = QAction("Display in a Dashboard", self, checkable=True)  # noqa E501
        self.dash_display.setObjectName("DispDash")
        self.dash_display.triggered.connect(self._display_dashboard)
        self.display_data_type_menu.addAction(self.dash_display)

        self.tab_display = QAction("Display in a Tab", self, checkable=True)
        self.tab_display.setObjectName("DispTab")
        self.tab_display.triggered.connect(self._display_tab)
        self.display_data_type_menu.addAction(self.tab_display)

        self.all_display = QAction("Display All", self, checkable=True)
        self.all_display.setObjectName("DispAll")
        self.all_display.triggered.connect(self._display_all)
        self.display_data_type_menu.addAction(self.all_display)

        self.display_type.addAction(self.tab_display)
        self.display_type.addAction(self.all_display)
        self.display_type.addAction(self.dash_display)
        self.display_type.addAction(self.no_display)

        # Mode
        self.mode_menu = self.addMenu("&Mode")

        # Design Mode Switch
        self.dark_mode = QAction("Dark Mode", self)
        self.dark_mode.triggered.connect(lambda: self._switch_mode("Dark"))
        self.mode_menu.addAction(self.dark_mode)
        self.dark_mode.setObjectName("Dark Mode")

        self.light_mode = QAction("Light Mode", self)
        self.light_mode.triggered.connect(lambda: self._switch_mode("Light"))
        self.mode_menu.addAction(self.light_mode)
        self.light_mode.setObjectName("Light Mode")

        # Help
        self.help_menu = self.addMenu("&Help")

        # Add Documentation and Reporting Features
        self.lookup_docs = QAction("Open Documentation", self)
        self.lookup_docs.triggered.connect(self._take_to_docs)
        self.help_menu.addAction(self.lookup_docs)

        self.report_issues = QAction("Report Issue on Github", self)
        self.report_issues.triggered.connect(self._report_issue)
        self.help_menu.addAction(self.report_issues)

    def _switch_mode(self, str):
        if str == "Light":
            f = open(self.parent.style_light, "r")
            self.parent.curr_mode = "Light"
        else:
            f = open(self.parent.style_dark, "r")
            self.parent.curr_mode = "Dark"

        style_str = f.read()
        self.parent.setStyleSheet(style_str)
        f.close()

    def _display_all(self):
        self.parent.display = True
        self.parent.tab = True
        self.parent.dashboard = True
        if not hasattr(self.parent, "data_vis_tab"):
            self.parent.data_vis_tab = data_vis(self.parent)

    def _do_not_display(self):
        if hasattr(self.parent, "data_vis_tab"):
            # todo: If AO tab is available, this will not always
            # be the at index '1'. Check for bugs.
            self.parent.input_tab_widget.removeTab(1)
            del self.parent.data_vis_tab
        self.parent.display = False
        self.parent.tab = False
        self.parent.dashboard = False

    def _display_dashboard(self):
        self.parent.display = True
        self.parent.tab = False
        self.parent.dashboard = True
        if hasattr(self.parent, "data_vis_tab"):
            self.parent.input_tab_widget.removeTab(1)
            del self.parent.data_vis_tab

    def _display_tab(self):
        self.parent.display = True
        self.parent.tab = True
        self.parent.dashboard = False
        if not hasattr(self.parent, "data_vis_tab"):
            self.parent.data_vis_tab = data_vis(self.parent)

    def _take_to_docs(self):
        webbrowser.open("https://ulfsri.github.io/firepydaq")  # todo replace

    def _report_issue(self):
        webbrowser.open("https://github.com/ulfsri/firepydaq/issues/")

    def _style_popup(self, dlg):
        if self.parent.curr_mode == "Light":
            f = open(self.parent.popup_light)
            str = f.read()
            dlg.setStyleSheet(str)
            f.close()
        else:
            f = open(self.parent.popup_dark)
            str = f.read()
            dlg.setStyleSheet(str)
            f.close()
        return

    @error_logger("SaveSettings")
    def save_settings_to_json(self):
        """Method that opens a 'SaveSettingsDialog' that prompts user
        to enter a file name and folder path.
        to save settings entered for NI Configuration
        and all devices into a .json file.

        Folder path must exist and entered filename must be unique.
        """
        try:
            json_setting = self.parent.settings_to_json()
        except Exception as e:
            self.parent.inform_user(str(e))
            return

        dlg_save_json = SaveSettingsDialog("Save settings to .json")
        self._style_popup(dlg_save_json)

        if dlg_save_json.exec() == QDialog.Accepted:
            file_json = dlg_save_json.file_path + ".json"
            folder_json = dlg_save_json.folder_path
            if os.path.exists(folder_json):
                if os.path.exists(file_json):
                    self.parent.inform_user("File with this name exists.")
                    return
                else:
                    with open(file_json, "w") as outfile:
                        outfile.write(json_setting)
                        outfile.close()
            else:
                self.parent.inform_user("Folder path not found.")
                return
            self.parent.notify("Saved to " + str(file_json) + " file successfully.") # noqa E501

    def load_json_settings(self):
        """Opens a 'LoadSettingsDialog' that prompts
        user to select a .json file
        to populate settings with information of NI Configuration
        and added devices.
        """
        dlg_load = LoadSettingsDialog()
        self._style_popup(dlg_load)
        if dlg_load.exec() == QDialog.Accepted:
            try:
                settings_file = open(dlg_load.file_name)
                data = json.load(settings_file)
                settings_file.close()
            except Exception as e:
                self.parent.inform_user(str(e))
            my_schema = schema
            # Keep compatibility with the existing schema until it explicitly
            # includes the optional Acquisition Mode property.
            validation_data = dict(data)
            validation_data.pop("Acquisition Mode", None)
            try:
                validate(instance=validation_data, schema=my_schema)
            except Exception as e:
                self.parent.inform_user("Unable to resolve json file.\n"+str(e))  # noqa E501
                return
            if self.parent.device_arr:
                self.parent.device_arr.clear()
                self.parent.lasers.clear()
                self.parent.mfcs.clear()
            if hasattr(self.parent, "generic_serial_devices"):
                for runtime in self.parent.generic_serial_devices.values():
                    try:
                        runtime.disconnect()
                    except Exception:
                        pass
                self.parent.generic_serial_devices.clear()
            if hasattr(self.parent, "device_registry"):
                self.parent.device_registry.disconnect_all()
            self.parent.settings.clear()
            self._repopulate_settings(data)
            self._load_devices(data)
            workspace = getattr(
                self.parent,
                "devices_workspace",
                None,
            )

            if workspace is not None:

                workspace.rebuild()
                workspace._refresh_all_interfaces()
            self.parent.notify("Configuration loaded: " + Path(dlg_load.file_name).name, "success") # noqa E501

    def _load_devices(self, data):
        if "Devices" in data:
            dev_dict = data["Devices"]
            if "Lasers" in dev_dict:
                laser_dict = dev_dict["Lasers"]
                for laser in laser_dict.keys():
                    my_dict = dev_dict["Lasers"][laser]
                    self.parent.device_arr[laser] = thorlabs_laser(self.parent, laser)  # noqa E501
                    self.parent.device_arr[laser].load_device_data(str(my_dict["P"]), str(my_dict["I"]),  # noqa E501
                                                                    str(my_dict["D"]), str(my_dict["O"]), str(my_dict["COMPORT"]),  # noqa E501
                                                                    str(my_dict["Tec Rate"]), str(my_dict["Laser Rate"]))  # noqa E501
                    self.parent.lasers[laser] = self.parent.device_arr[laser]
            if "MFCs" in dev_dict:
                mfc_dict = dev_dict["MFCs"]
                for mfc in mfc_dict.keys():
                    my_dict = mfc_dict[mfc]
                    self.parent.device_arr[mfc] = alicat_mfc(self.parent, None, mfc)  # noqa E501
                    self.parent.device_arr[mfc].load_device_data(my_dict["Gas"], str(my_dict["Rate"]), my_dict["COMPORT"])  # noqa E501
                    self.parent.mfcs[mfc] = self.parent.device_arr[mfc]
                    if hasattr(self.parent, "device_registry",):
                        gas = my_dict["Gas"]
                        gas_code = next(
                            (
                                code
                                for code, label
                                in AlicatGases.items()
                                if label == gas
                            ),
                            gas,
                        )

                        runtime = AlicatDevice(
                            name=mfc,
                            port=my_dict["COMPORT"],
                            gas=gas_code,
                            poll_interval_s=0.5,
                            notify=self.parent.notify,
                        )

                        self.parent.device_registry.register(runtime, replace=True,)
            restore_serial_devices(self.parent, dev_dict)

    def _repopulate_settings(self, data):
        self.parent.settings["Name"] = data["Name"]
        self.parent.settings["Project Name"] = data["Project Name"]
        self.parent.settings["Series Name"] = data.get("Series Name", "",)
        self.parent.settings["Test Name"] = data["Test Name"]
        self.parent.settings["Sampling Rate"] = data.get("Sampling Rate", 0.0)
        self.parent.settings["Formulae File"] = data.get("Formulae File", "")
        self.parent.settings["Experiment Type"] = data["Experiment Type"]
        self.parent.settings["Config File"] = data.get("Config File", "")
        self.parent.settings["Acquisition Mode"] = data.get(
            "Acquisition Mode",
            "FULL",
        )
        self.parent._set_texts()


    def remove_all(self):
        """Method to remove all added devices
        from the application including Mass Flow Meters,
        Mass Flow Controllers and ThorlabsCLD101X.
        """
        if self.parent.device_arr:
            self.parent.device_arr.clear()
            if self.parent.mfms:
                self.parent.mfms.clear()
            if self.parent.mfcs:
                self.parent.mfcs.clear()
            if self.parent.lasers:
                self.parent.lasers.clear()
        else:
            self.parent.inform_user("No devices added yet.")
            return
