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

from PySide6.QtWidgets import (QWidget, QGridLayout, QLabel,
                               QLineEdit, QComboBox, QHBoxLayout,
                               QPushButton, QVBoxLayout, QCheckBox)
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtCore import QRegularExpression

from ..utilities.DAQUtils import COMports, AlicatGases

# APIs
from ..api.EchoAlicat import EchoController
from ..api.EchoThorLabsCLD101X import EchoThor
from .alicat_device import AlicatDevice
from .abstract_device import DeviceState
from .device_registry import DeviceRegistry

# Communication related
import asyncio
import time
import numpy as np
import threading
import tracemalloc
tracemalloc.start()


# class thorlabs_laser(QWidget):
#     """User-added ThorlabsCLD101X Controller

#     Attributes
#     ----------
#         reg_ex_1: QRegularExpression
#             Only accept float values
#         type: str
#             Value is "laser"
#         parent: Object
#             Defines parent class
#         settings: dict
#             Stores settings for the controller

#     """
#     reg_ex_1 = QRegularExpression(r"[0-9]*\.[0-9]{0,4}")  # double

#     def __init__(self, parent, str_name):
#         super().__init__()
#         self._makelaser(parent, str_name)

#     def _makelaser(self, parent, str_name):
#         self.type = "laser"
#         self.dev_id = str_name
#         self.parent = parent
#         self.settings = {}
#         self.content = self.create_thorlabs_laser_content()
#         self.parent.device_tab_widget.addTab(self.content, self.dev_id)

#     def create_thorlabs_laser_content(self):
#         """Method that creates laser controller contents

#         Fields
#         ----------------------
#         - comport_input: QComboBox
#             Dropdown of list of available COMPorts
#         - p_input: QLineEdit
#             Value for proportional component
#             of the PID controller for ThorlabsCLD101X
#         - i_input: QLineEdit
#             Value for integral component
#             of the PID controller for ThorlabsCLD101X
#         - d_input: QLineEdit
#             Value for derivative component
#             of the PID controller for ThorlabsCLD101X
#         - osc_input: QLineEdit
#             Value for Oscillation period in seconds
#             of the PID controller for ThorlabsCLD101X
#         - tec_input: QLineEdit
#             Value in Celsius that will be used
#             to set TEC.
#         - tec_button: QPushButton
#             Calls set_tec()
#         - laser_input: QLineEdit
#             Value in mA that will be used
#             to set the laser current.
#         - laser_button: QPushButton
#             Calls set_laser()
#         - pid_btn: QPushButton
#             Calls set_pID()
#         - laser_connection_btn: QPushButton
#             Calls establish_connection()

#         """
#         # Adds Layout
#         self.main_widget = QWidget()
#         self.main_layout = QVBoxLayout()
#         self.device_layout = QGridLayout()
#         self.main_layout.addLayout(self.device_layout)
#         self.buttons_layout = QGridLayout()
#         self.main_layout.addLayout(self.buttons_layout)
#         # Adds COMPORT input field
#         self.comport_label = QLabel("Select COMPORT:")
#         self.comport_label.setMaximumWidth(200)
#         self.device_layout.addWidget(self.comport_label, 0, 0)

#         self.comport_input = QComboBox()
#         for comport in COMports:
#             self.comport_input.addItem(comport)
#         self.comport_input.setMaximumWidth(200)
#         self.comport = self.comport_input.currentText()
#         self.device_layout.addWidget(self.comport_input, 0, 1)

#         # Adds P input field
#         self.p_label = QLabel("Enter P value:")
#         self.device_layout.addWidget(self.p_label, 1, 0)
#         self.p_label.setMaximumWidth(200)

#         self.p_input = QLineEdit()
#         self.p_input.setMaximumWidth(200)
#         self.p_input.setPlaceholderText("8.0")
#         self.p_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))  # noqa: E501
#         self.device_layout.addWidget(self.p_input, 1, 1)

#         # Adds I input field
#         self.i_label = QLabel("Enter I value:")
#         self.device_layout.addWidget(self.i_label, 2, 0)
#         self.i_label.setMaximumWidth(200)

#         self.i_input = QLineEdit()
#         self.i_input.setPlaceholderText("3.7")
#         self.i_input.setMaximumWidth(200)
#         self.i_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))
#         self.device_layout.addWidget(self.i_input, 2, 1)

#         # Adds D input field
#         self.d_label = QLabel("Enter D value:")
#         self.device_layout.addWidget(self.d_label, 3, 0)
#         self.d_label.setMaximumWidth(200)

#         self.d_input = QLineEdit()
#         self.d_input.setPlaceholderText("3.2")
#         self.d_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))
#         self.d_input.setMaximumWidth(200)
#         self.device_layout.addWidget(self.d_input, 3, 1)

#         # Adds Osc Period input field
#         self.osc_label = QLabel("Enter Osc period (s):")
#         self.device_layout.addWidget(self.osc_label, 4, 0)
#         self.osc_label.setMaximumWidth(200)

#         self.osc_input = QLineEdit()
#         self.osc_input.setPlaceholderText("2")
#         self.osc_input.setMaximumWidth(200)
#         self.osc_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))
#         self.device_layout.addWidget(self.osc_input, 4, 1)

#         # Adds TEC rate
#         self.tec_label = QLabel("Set TEC Temperature (C):")
#         self.tec_label.setMaximumWidth(200)
#         self.device_layout.addWidget(self.tec_label, 5, 0)

#         self.tec_layout = QHBoxLayout()
#         self.tec_input = QLineEdit()
#         self.tec_input.setMaximumWidth(150)
#         self.tec = self.tec_input.text()
#         self.tec_input.setPlaceholderText("25")
#         self.tec_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))
#         self.tec_button = QPushButton("Set")
#         self.tec_button.clicked.connect(self.set_tec)
#         self.tec_button.setEnabled(False)
#         self.tec_button.setMaximumWidth(50)
#         self.tec_layout.addWidget(self.tec_input)
#         self.tec_layout.addWidget(self.tec_button)
#         self.device_layout.addLayout(self.tec_layout, 5, 1)

#         # Adds Laser rate
#         self.laser_label = QLabel("Set Laser Output (mA):")
#         self.laser_label.setMaximumWidth(200)
#         self.device_layout.addWidget(self.laser_label, 6, 0)

#         self.laser_layout = QHBoxLayout()
#         self.laser_input = QLineEdit()
#         self.laser_input.setPlaceholderText("0")
#         self.laser_input.setMaximumWidth(150)
#         self.laser_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))  # noqa E501
#         self.laser_rate = self.laser_input.text()
#         self.laser_button = QPushButton("Set")
#         self.laser_button.clicked.connect(self.set_laser)
#         self.laser_button.setEnabled(False)
#         self.laser_button.setMaximumWidth(50)
#         self.laser_layout.addWidget(self.laser_input)
#         self.laser_layout.addWidget(self.laser_button)
#         self.device_layout.addLayout(self.laser_layout, 6, 1)

#         self.pid_btn = QPushButton("Set PID values")
#         self.pid_btn.setMaximumWidth(150)
#         self.pid_btn.clicked.connect(self.set_pid)
#         self.pid_btn.setEnabled(False)
#         self.buttons_layout.addWidget(self.pid_btn, 0, 2)

#         self.laser_switch = QPushButton("Start laser")
#         self.laser_switch.setMaximumWidth(150)
#         self.laser_switch.clicked.connect(self.start_laser)
#         self.laser_switch.setEnabled(False)
#         self.buttons_layout.addWidget(self.laser_switch, 0, 1)

#         self.laser_connection_btn = QPushButton("Establish Connection")
#         self.laser_connection_btn.setMaximumWidth(150)
#         self.laser_connection_btn.clicked.connect(self.establish_connection)  # noqa E501
#         self.laser_connection_btn.setCheckable(True)
#         self.buttons_layout.addWidget(self.laser_connection_btn, 0, 0)
#         self.main_widget.setLayout(self.main_layout)

#         return self.main_widget

#     def set_laser(self):
#         """Method that sets the laser output
#         for the connected ThorlabsCLD101X device
#         to `laser_input` mA.

#         """
#         self.thor.UpdateLaserCurrent(float(self.laser_input.text()))
#         notif_txt = self.dev_id + " laser set to " + self.laser_input.text() + " mA"  # noqa E501
#         self.parent.notify(notif_txt)
#         return

#     def set_tec(self):
#         """Method that sets TEC temperature
#         for the connected ThorlabsCLD101X device
#         to `tec_input` C.
#         """
#         self.thor.SetTECTemp(self.tec_input.text())
#         self.laser_switch.setEnabled(True)
#         self.parent.notify(self.dev_id + " TEC set to " + self.tec_input.text() +" C")  # noqa E501
#         return

#     def start_laser(self):
#         """Method to start the laser
#         connected to the Thoelabs CLD101X device
#         """
#         self.thor.SwitchLaser(Switch=True)
#         self.laser_button.setEnabled(True)
#         self.thor.StartTEC(Switch=True)
#         self.parent.notify(self.dev_id + " laser on")
#         return

#     def set_pid(self):
#         """Method that sets the P, I, and D
#         component of the connected ThorlabCLD101X.
#         """
#         Prop = float(self.p_input.text())
#         Intgrl = float(self.i_input.text())
#         Derivative = float(self.d_input.text())
#         Osc = float(self.osc_input.text())
#         self.thor.set_TECPID(Prop, Intgrl, Derivative, Osc)
#         self.parent.notify("P,I,D, Osc period for " + self.dev_id + "set to " + str(Prop) + str(Intgrl) + str(Derivative) + str(Osc) + " s, respectively")  # noqa #E501

#     def establish_connection(self):
#         """Method that connects to the
#         ThorlabsCLD101X connected at selected
#         `comport_input`.
#         The P, I, D, and osc input labels will be
#         updated with the present values on the controller.
#         """
#         if self.laser_connection_btn.isChecked():
#             self.thor = EchoThor()
#             self.thor.set_connection(self.comport_input.currentText())
#             PID_set = self.thor.read_TECPID()
#             self.p_input.setText(PID_set["P"])
#             self.i_input.setText(PID_set["I"])
#             self.d_input.setText(PID_set["D"])
#             self.osc_input.setText(PID_set["O"])
#             self.parent.notify(self.dev_id + " succesfully connected. Present PID values updatesd.")  # noqa #E501
#             self.laser_connection_btn.setText("Stop Connection")
#             self.tec_button.setEnabled(True)
#             self.pid_btn.setEnabled(True)
#         else:
#             self.laser_connection_btn.setText("Establish Connection")
#             self.thor.close()
#             self.tec_button.setEnabled(False)
#             self.laser_button.setEnabled(False)
#             self.laser_switch.setEnabled(False)
#             self.pid_btn.setEnabled(False)

#     def load_device_data(self, p, i, d, o, comport, tec, laser_rate):
#         """Method to load a previously saved laser device data
#         """
#         self.comport = comport
#         self.comport_input.setCurrentText(self.comport)
#         self.p = p
#         self.p_input.setText(self.p)
#         self.i = i
#         self.i_input.setText(self.i)
#         self.tec = tec
#         self.tec_input.setText(self.tec)
#         self.d = d
#         self.d_input.setText(self.d)
#         self.o = o
#         self.osc_input.setText(self.o)
#         self.laser_rate = laser_rate
#         self.laser_input.setText(self.laser_rate)

#     def get_type(self):
#         """Method that returns the type of the device
#         """
#         return self.type

#     def settings_to_dict(self):
#         """Method that stores laser device
#         information to `settings` dictionary.
#         """
#         try:
#             self.settings["COMPORT"] = self.comport_input.currentText()
#             self.settings["P"] = float(self.p_input.text())
#             self.settings["I"] = float(self.i_input.text())
#             self.settings["D"] = float(self.d_input.text())
#             self.settings["O"] = float(self.osc_input.text())
#             self.settings["Laser Rate"] = float(self.laser_input.text())
#             self.settings["Tec Rate"] = float(self.tec_input.text())
#             self.settings["Type"] = self.type
#         except ValueError as e:
#             self.settings.clear()
#             raise ValueError("Data Invalid for " + self.dev_id) from e
#         return self.settings


class alicat_mfc(QWidget):
    """User-added Alicat Mass Flow Controller

    Attributes
    ----------
        type: str
            Value is "mfc"
        parent: Object
            Defines parent class
        dev_id: str
            Device ID
        settings: dict
            Stores settings for the controller
    """
    reg_ex_1 = QRegularExpression(r"[0-9]*\.[0-9]{0,4}")  # double

    def __init__(self, parent, tabs, str_name):
        super().__init__()
        self._makemfc(parent, tabs, str_name)

    def _makemfc(self, parent, tabs, str_name):
        self.type = "mfc"
        self.dev_id = str_name
        self.parent = parent
        self.settings = {}
        self.content = self.create_alicat_mfc_content()
        # GUI placement is owned by MainWorkspace; no legacy tab.

    def create_alicat_mfc_content(self):
        """Method that creates Alicat MFC contents

        Fields
        ----------------------
        - comport_input: QComboBox
            Dropdown of list of available COMPorts
        - gas_input: QComboBox
            Dropdown of list of available COMPorts.
            Currently available gases are listed
            in utilities.DAQUtils submodule.
        - dil_rate_input: QLineEdit
            Value in default Alicat flow-rate unit.
            Usually slpm/sccm.
            This input will be used
            to set the MFC flow-rate.
        - set_flow_btn: QPushButton
            Calls set_flow_rate()
        - stop_flow_btn: QPushButton
            Calls stop_flow_rate()
        - mfc_connection_btn: QPushButton
            Calls establish_connection()
        """
        # Adds Layout
        self.device_widget = QWidget()
        self.device_layout = QGridLayout()

        # Adds COMPORT input field
        self.comport_label = QLabel("Select COMPORT:")
        self.comport_label.setMaximumWidth(200)
        self.device_layout.addWidget(self.comport_label, 0, 0)
        self.comport_input = QComboBox()
        for comport in COMports:
            self.comport_input.addItem(comport)
        self.comport_input.setMaximumWidth(200)
        self.comport = self.comport_input.currentText()
        self.device_layout.addWidget(self.comport_input, 0, 1)

        # Adds gas input field
        self.gas_label = QLabel("Select gas:")
        self.device_layout.addWidget(self.gas_label, 1, 0)
        self.gas_label.setMaximumWidth(200)
        self.gas_input = QComboBox()
        for _, gas in AlicatGases.items():
            self.gas_input.addItem(gas)
        self.gas = self.gas_input.currentText()
        self.gas_input.setMaximumWidth(200)
        self.device_layout.addWidget(self.gas_input, 1, 1)

        # Adds dilution rate
        self.dil_rate_label = QLabel("Enter gas flow rate \n(default Alicat, slpm/sccm):")  # noqa E501
        self.dil_rate_label.setMaximumWidth(200)
        self.device_layout.addWidget(self.dil_rate_label, 2, 0)
        self.dil_rate_input = QLineEdit()
        self.dil_rate_input.setText("0")
        self.dil_rate_input.setValidator(QRegularExpressionValidator(self.reg_ex_1))  # noqa E501
        self.dil_rate_input.setMaximumWidth(200)
        self.dil_rate = self.dil_rate_input.text()
        self.device_layout.addWidget(self.dil_rate_input, 2, 1)
        self.device_widget.setLayout(self.device_layout)

        self.flow_layout = QHBoxLayout()
        self.set_flow_btn = QPushButton("Set Flow Rate")
        self.set_flow_btn.setEnabled(False)
        self.stop_flow_btn = QPushButton("Stop Flow Rate")
        self.stop_flow_btn.setEnabled(False)
        self.set_flow_btn.clicked.connect(self.set_flow_rate)
        self.stop_flow_btn.clicked.connect(self.stop_flow_rate)
        self.stop_flow_btn.setMaximumWidth(100)
        self.set_flow_btn.setMaximumWidth(100)
        self.flow_layout.addWidget(self.set_flow_btn)
        self.flow_layout.addWidget(self.stop_flow_btn)
        self.device_layout.addLayout(self.flow_layout, 3, 0)

        self.mfc_connection_btn = QPushButton("Establish Connection")
        self.set_flow_btn.setMaximumWidth(200)
        self.mfc_connection_btn.clicked.connect(self.establish_connection)
        self.mfc_connection_btn.setCheckable(True)
        self.device_layout.addWidget(self.mfc_connection_btn, 3, 1)

        # Pulse generation is intentionally removed from the active Alicat GUI.
        # Hidden compatibility controls keep the existing connection methods safe.
        self.checkpulses = QCheckBox()
        self.checkpulses.setChecked(False)
        self.checkpulses.hide()
        self.pulse_btn = QPushButton("Start Pulses")
        self.pulse_btn.setEnabled(False)
        self.pulse_btn.hide()

        # Keep this tab compact so the main acquisition region receives space.
        self.device_widget.setMaximumHeight(210)
        self.device_layout.setContentsMargins(10, 8, 10, 8)
        self.device_layout.setVerticalSpacing(6)
        return self.device_widget

    def toggle_pulses(self):
        """Method to toggle sending pulses during acquisition.
        The process is as follows:
            - Opens a drop down for sensing square of traingular puleses.
            - The square pulses start with a specific period and amplitude.
            - Pulse amplitude is the amplitude above the set gas flow rate.
            - The number of intervals for each period is
                defined by the user (default is 3).
            - The program then calculates the number of pulses to be sent,
                such that the time duration for each interval reduces
                from initial to final pulse period
            - Subsequent pulse period are halved after the specified
                number of intervals, until the final pule period is reached.
        """
        if self.checkpulses.isChecked():
            if self.checkpulses.isChecked():
                self.pulse_btn.setEnabled(True)

            self.parent.notify("Pulses will be sent during acquisition", "info")  # noqa E501
            self.device_layout.addWidget(QLabel("Pulse Type:"), 1, 2)
            self.pulse_type = QComboBox()
            self.pulse_type.addItem("Square")
            self.pulse_type.addItem("Triangular")
            self.pulse_type.setMaximumWidth(100)
            self.device_layout.addWidget(self.pulse_type, 1, 3)
            self.device_layout.addWidget(QLabel("Initial Pulse Period (s):"), 2, 2)  # noqa E501
            self.pulse_period = QLineEdit()
            self.pulse_period.setValidator(QRegularExpressionValidator(self.reg_ex_1))  # noqa E501
            self.pulse_period.setText("60.0")
            self.pulse_period.setMaximumWidth(100)
            self.device_layout.addWidget(self.pulse_period, 2, 3)
            self.device_layout.addWidget(QLabel("Final Pulse Period (s):"), 3, 2)  # noqa E501
            self.final_pulse_period = QLineEdit()
            self.final_pulse_period.setValidator(QRegularExpressionValidator(self.reg_ex_1)) # noqa E501
            self.final_pulse_period.setText("4.0")
            self.final_pulse_period.setMaximumWidth(100)
            self.device_layout.addWidget(self.final_pulse_period, 3, 3)
            self.device_layout.addWidget(QLabel("Baseline flow rate"), 4, 2)  # noqa E501
            self.baseline_flow = QLineEdit()
            self.baseline_flow.setValidator(QRegularExpressionValidator(self.reg_ex_1))  # noqa E501
            self.baseline_flow.setText("0.0")
            self.baseline_flow.setMaximumWidth(100)
            self.device_layout.addWidget(self.baseline_flow, 4, 3)
            self.device_layout.addWidget(QLabel("Pulse Amplitude (Above Setpoint):"), 5, 2)  # noqa E501
            self.pulse_amplitude = QLineEdit()
            self.pulse_amplitude.setValidator(QRegularExpressionValidator(self.reg_ex_1))  # noqa E501
            self.pulse_amplitude.setText("10.0")
            self.pulse_amplitude.setMaximumWidth(100)
            self.device_layout.addWidget(self.pulse_amplitude, 5, 3)
            self.device_layout.addWidget(QLabel("Total intervals between max and min periods:"), 6, 2)  # noqa E501
            self.num_intervals = QLineEdit()
            self.num_intervals.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]*")))  # noqa E501 int only
            self.num_intervals.setText("4")
            self.num_intervals.setMaximumWidth(100)
            self.device_layout.addWidget(self.num_intervals, 6, 3)
        else:
            if not self.checkpulses.isChecked():
                self.pulse_btn.setEnabled(False)

            self.parent.notify("Pulses will NOT be sent during acquisition", "info")  # noqa E501
            self.device_layout.itemAtPosition(1, 2).widget().deleteLater()
            self.device_layout.itemAtPosition(1, 3).widget().deleteLater()
            self.device_layout.itemAtPosition(2, 2).widget().deleteLater()
            self.device_layout.itemAtPosition(2, 3).widget().deleteLater()
            self.device_layout.itemAtPosition(3, 2).widget().deleteLater()
            self.device_layout.itemAtPosition(3, 3).widget().deleteLater()
            self.device_layout.itemAtPosition(4, 2).widget().deleteLater()
            self.device_layout.itemAtPosition(4, 3).widget().deleteLater()
            self.device_layout.itemAtPosition(5, 2).widget().deleteLater()
            self.device_layout.itemAtPosition(5, 3).widget().deleteLater()
            self.device_layout.itemAtPosition(6, 2).widget().deleteLater()
            self.device_layout.itemAtPosition(6, 3).widget().deleteLater()
        return

    def sendPulses(self):
        """Method to calculate square or triangular pulses 
            and send them to the MFC.
        """
        if self.pulse_btn.isChecked():
            self.pulse_btn.setText("Stop Pulses")
            self.send_pulses = True
            self.parent.notify("Pulses started", "info")

            pulse_thread = threading.Thread(target=self.PulseThreader)  # noqa E501
            pulse_thread.start()

            # if self.pulse_type.currentText() == "Square":
            #     # Calculate square pulses
        else:
            self.pulse_btn.setText("Start Pulses")
            self.send_pulses = True
            self.parent.notify("Pulses stopped", "info")

        return

    def PulseThreader(self):
        self.pulse_btn.setText("Sending Pulses")
        self.pulse_type_text = self.pulse_type.currentText()
        self.initial_pp_value = float(self.pulse_period.text())
        self.final_pp_value = float(self.final_pulse_period.text())
        self.ampli = float(self.pulse_amplitude.text())
        self.num_int_value = int(self.num_intervals.text())

        All_periods = np.linspace(self.final_pp_value, self.initial_pp_value, self.num_int_value)  # noqa E501
        All_periods = [int(a) if ((int(a) % 2) == 0) else int(a) + 1 for a in All_periods]  # noqa E501
        All_periods.sort(reverse=True)
        All_periods_list = All_periods*3  # repeating each period thrice
        All_periods_list.sort(reverse=True)
        print(All_periods, All_periods_list)
        Total_time = sum([i*3 for i in All_periods])
        print(f'Sending {self.pulse_type_text} pulses for a total of {Total_time} s')  # noqa E501

        time.sleep(2)
        time_start = time.time()
        t = 0
        p_counter = 0
        while t < Total_time or p_counter > (len(All_periods_list)-1):

            if self.pulse_type_text == 'Square':
                boolean_t = sum(All_periods_list[:p_counter]) + All_periods_list[p_counter]/2  # noqa #501
                flow_value = float(self.baseline_flow.text()) + self.ampli*int(t > boolean_t)  # noqa E501
                self.dil_rate_input.setText(str(flow_value))
                print(f'Setting {flow_value} for the next {All_periods_list[p_counter]/2} s')  # noqa E501
                # self.set_flow_rate()
                new_flow = float(self.dil_rate_input.text())

                # Creating a different asyncio event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.MFC.flow_controller.set_flow_rate(new_flow))  # noqa E501

                time.sleep(All_periods_list[p_counter]/2)

                tnew = time.time()
                t = tnew - time_start
                print('Time elapsed', round(t, 2), All_periods_list[p_counter], p_counter)
                if t > sum(All_periods_list[:p_counter]) + All_periods_list[p_counter]:  # noqa E501
                    p_counter += 1

            elif self.pulse_type_text == 'Triangular':
                print('Triangular pulse setup')

            else:
                print('Error in pulse type.')

        self.pulse_btn.setText("Start Pulses")

        return

    def set_flow_rate(self):
        runtime = self._abstract_alicat()
        new_flow = float(self.dil_rate_input.text())
        runtime.set_flow(new_flow)
        self.parent.notify(
            f"{self.dev_id} flow set to {new_flow}",
            "success",
        )

    def stop_flow_rate(self):
        runtime = self._abstract_alicat()
        runtime.stop_flow()
        self.dil_rate_input.setText("0.0")
        self.parent.notify(
            f"{self.dev_id} flow set to zero",
            "success",
        )

    def GetMFCFlow(self):
        print(
            "LEGACY DEVICE POLL",
            time.time(),
        )
        MFC_Vals = self.loop.run_until_complete(self.MFC.get_MFC_val())
        return MFC_Vals

    def establish_connection(self):
        runtime = self._abstract_alicat()
        try:
            if self.mfc_connection_btn.isChecked():
                if runtime.state == DeviceState.DISCONNECTED:
                    runtime.configure(
                        port=self.comport_input.currentText(),
                        gas=self._alicat_gas_code(),
                    )
                    runtime.connect()
                runtime.start()
                self.mfc_connection_btn.setText("Stop Connection")
                self.set_flow_btn.setEnabled(True)
                self.stop_flow_btn.setEnabled(True)
            else:
                runtime.disconnect()
                self.mfc_connection_btn.setText("Establish Connection")
                self.set_flow_btn.setEnabled(False)
                self.stop_flow_btn.setEnabled(False)
        except Exception as exc:
            self.mfc_connection_btn.setChecked(False)
            self.mfc_connection_btn.setText("Establish Connection")
            self.set_flow_btn.setEnabled(False)
            self.stop_flow_btn.setEnabled(False)
            self.parent.notify(
                f"{self.dev_id} connection error: {exc}",
                "error",
            )

    def get_name(self):
        """Method to get the id of the MFC.
        """
        return self.dev_id

    def get_dil_rate(self):
        """Method to get flow-rate input defined byt he user.
        """
        self.dil_rate = self.dil_rate_input.text()
        return self.dil_rate

    def get_gas(self):
        """Method to read the gas type selected by the user.
        """
        self.gas = self.gas_input.currentText()
        gas_unicode = self.gas.encode('ascii', 'ignore')
        return gas_unicode

    def load_device_data(self, gas_val, rate_val, comp_val):
        """Method to load the pre-saved Alicat MFC device data
        """
        self.gas = gas_val
        self.gas_input.setCurrentText(gas_val)
        self.dil_rate = rate_val
        self.dil_rate_input.setText(rate_val)
        self.comport = comp_val
        self.comport_input.setCurrentText(comp_val)

    def get_type(self):
        """Method to get the type of the device
        """
        return self.type

    def settings_to_dict(self):
        """Method that saves the MFC device data to `settings` dictionary.
        """
        self.settings["COMPORT"] = self.comport_input.currentText()
        self.settings["Gas"] = self.gas_input.currentText()
        self.settings["Type"] = self.type
        try:
            self.settings["Rate"] = float(self.dil_rate_input.text())
        except ValueError as v:
            self.settings.clear()
            raise ValueError("Data Invalid for " + self.dev_id) from v
        return self.settings

    def _alicat_gas_code(self):
        selected = self.gas_input.currentText()
        return next(
            (code for code, label in AlicatGases.items() if label == selected),
            selected,
        )

    def _abstract_alicat(self):
        registry = getattr(self.parent, "device_registry", None)
        if registry is None:
            registry = DeviceRegistry()
            self.parent.device_registry = registry

        runtime = registry.get(self.dev_id)
        if runtime is None:
            runtime = AlicatDevice(
                name=self.dev_id,
                port=self.comport_input.currentText(),
                gas=self._alicat_gas_code(),
                poll_interval_s=0.2,
                notify=self.parent.notify,
            )
            registry.register(runtime)
        return runtime

    def GetFlows(self):
        """Return the latest AbstractDevice snapshot without device I/O."""
        return dict(self._abstract_alicat().snapshot().values)


class mfm(QWidget):
    """User-added Alicat Mass Flow Meter device.

    Attributes
    ----------
        type: str
            Value is "mfm"
        parent: Object
            Defines parent class
        dev_id: str
            Device ID
        settings: dict
            Stores settings for the meter
    """
    def __init__(self, parent, str_name):
        super().__init__()
        self._makemfm(parent, str_name)

    def _makemfm(self, parent, str_name):
        self.type = "mfm"
        self.dev_id = str_name
        self.parent = parent
        self.settings = {}
        self.content = self.create_mfm_content()
        # GUI placement is owned by MainWorkspace; no legacy tab.

    def create_mfm_content(self):
        """Method to add Mass Flow Meter content.

        Fields
        ----------------------
        - comport_input: QComboBox
            Dropdown of list of available COMPorts
        - gas_input: QComboBox
            Dropdown of list of available COMPorts.
            Currently available gases are listed
            in utilities.DAQUtils submodule.
        - mfm_connection_btn: QPushButton
            Calls establish_connection()
        """
        # Adds Layout
        self.device_widget = QWidget()
        self.device_layout = QGridLayout()

        # Adds COMPORT input field
        self.comport_label = QLabel("Select COMPORT:")
        self.comport_label.setMaximumWidth(200)
        self.device_layout.addWidget(self.comport_label, 0, 0)
        self.comport_input = QComboBox()
        for comport in COMports:
            self.comport_input.addItem(comport)
        self.comport_input.setMaximumWidth(200)
        self.comport = self.comport_input.currentText()
        self.device_layout.addWidget(self.comport_input, 0, 1)

        # Adds gas input field
        self.gas_label = QLabel("Enter gas name:")
        self.device_layout.addWidget(self.gas_label, 1, 0)
        self.gas_label.setMaximumWidth(200)
        self.gas_input = QComboBox()
        for _, gas_text in AlicatGases.items():
            self.gas_input.addItem(gas_text)
        self.gas = self.gas_input.currentText()
        self.gas_input.setMaximumWidth(200)
        self.device_layout.addWidget(self.gas_input, 1, 1)

        # Adds dilution rate
        self.rate_updtLabel = QLabel("Flow Rate")
        self.rate_updtLabel.setMaximumWidth(200)
        self.rate_updtLabel.setMaximumHeight(20)
        self.device_layout.addWidget(self.rate_updtLabel, 2, 0)
        self.flow_rateVal = QLabel("Flow rate will be updated here.")
        self.flow_rateVal.setMaximumHeight(30)
        self.device_layout.addWidget(self.flow_rateVal, 2, 1)

        self.mfm_connection_btn = QPushButton("Establish Connection")
        self.mfm_connection_btn.setMaximumWidth(200)
        self.mfm_connection_btn.setCheckable(True)
        self.mfm_connection_btn.clicked.connect(self.establish_connection)  # noqa E501
        self.device_layout.addWidget(self.mfm_connection_btn, 3, 1)
        self.device_widget.setLayout(self.device_layout)

        return self.device_widget

    def establish_connection(self):
        """Method to establish connection with an Alicat
        MFM device connected at `comport_input` with
        `gas_input` selected gas type.
        """
        if self.mfm_connection_btn.isChecked():
            self.mfm_connection_btn.setText("Establish Connection")
            self.parent.notify(self.dev_id + " connected successfully", "success")  # noqa E501
        else:
            self.mfm_connection_btn.setText("Stop Connection")
            self.parent.notify("Connection to " + self.dev_id + " ended successfully", "success")  # noqa E501

    def get_name(self):
        """Method to get device id for the MFM device
        """
        return self.dev_id

    def get_gas(self):
        """Method to get the user-selected gas type for the MFM device
        """
        self.gas = self.gas_input.currentText()
        return self.gas

    def load_device_data(self, gas_val, comport):
        """Method to load pre-saved MFM device data
        """
        self.gas = gas_val
        self.gas_input.setCurrentText(gas_val)
        self.comport = comport
        self.comport_input.setCurrentText(comport)

    def get_type(self):
        """Method to get the type of the device
        """
        return self.type

    def settings_to_dict(self):
        """Method that saves the MFM device data to `settings` dictionary.
        """
        self.settings["COMPORT"] = self.comport_input.currentText()
        self.settings["Gas"] = self.gas_input.currentText()
        return self.settings
