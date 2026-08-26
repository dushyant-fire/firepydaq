from __future__ import annotations

import time
from pathlib import Path

import nidaqmx
import nidaqmx.constants
import pandas as pd


class CreateDAQTask:
    """Analog-input-only NI-DAQmx task driver.

    Analog output is intentionally unsupported in this version. Configuration rows
    must use AI channels such as ai0, ai1, and so on.
    """

    def __init__(self, parent, name: str) -> None:
        self.parent = parent
        self.name = name
        self.aitask = None
        self.ChanConfig = pd.DataFrame()
        self.Fig_titles = pd.Series(dtype=str)
        self.ailabel_map: dict[str, int] = {}
        self.aolabel_map: dict[str, int] = {}
        self.ao_inputs = False
        self.ai_counter = 0
        self.ao_counter = 0
        self.sampleRate = 0.0
        self.numberOfSamples = 0
        self.ActualSamplingRate = 0.0
        self._is_running = False

    def CreateFromConfig(self, config_path: str | Path) -> None:
        if self._is_running:
            raise RuntimeError(
                "Cannot reconfigure the NI analog-input task while it is running."
            )
        if self.aitask is not None:
            self.close()

        self.initialize_config(config_path)
        if not self.ailabel_map:
            raise ValueError("NI configuration does not contain analog-input channels.")

        self.aitask = nidaqmx.Task(new_task_name=f"{self.name}_AI")
        self.ChanConfig = self.ChanConfig.astype(str)

        for row_index in self.ChanConfig.index:
            channel = self.ChanConfig.loc[row_index, "Channel"].strip()
            if not channel.lower().startswith("ai"):
                continue

            self.addAITask(
                daqname=self.ChanConfig.loc[row_index, "Device"].strip(),
                aichan=channel,
                measurement=self.ChanConfig.loc[row_index, "Type"].strip(),
                TCtype=self.ChanConfig.loc[row_index, "TCType"].strip(),
            )

    def initialize_config(self, config_path: str | Path) -> None:
        config = pd.read_csv(config_path)
        config.columns = [column.strip() for column in config.columns]

        required = {"Device", "Channel", "Label", "Type", "TCType"}
        missing = sorted(required.difference(config.columns))
        if missing:
            raise ValueError(
                "NI configuration is missing required columns: "
                + ", ".join(missing)
            )

        config["Channel"] = config["Channel"].astype(str).str.strip()
        ao_rows = config[config["Channel"].str.lower().str.startswith("ao")]
        if not ao_rows.empty:
            channels = ", ".join(ao_rows["Channel"].tolist())
            raise ValueError(
                "Analog output is disabled in this FirePyDAQ version. "
                f"Remove AO rows from the NI configuration: {channels}"
            )

        ai_config = config[
            config["Channel"].str.lower().str.startswith("ai")
        ].copy()
        self.ChanConfig = ai_config.reset_index(drop=True)
        self.Fig_titles = self.ChanConfig["Label"]
        self.ailabel_map = {
            str(label).strip(): index
            for index, label in enumerate(self.ChanConfig["Label"])
        }
        self.aolabel_map = {}
        self.ao_inputs = False
        self.ai_counter = len(self.ailabel_map)
        self.ao_counter = 0

    def addAITask(
        self,
        daqname: str,
        aichan: str,
        measurement: str,
        TCtype: str,
    ) -> None:
        if self.aitask is None:
            raise RuntimeError("NI analog-input task has not been created.")

        physical_channel = f"{daqname}/{aichan}"
        measurement = measurement.strip()

        if measurement == "Thermocouple":
            tc_code = TCtype.strip()
            valid_types = {"B", "E", "J", "K", "N", "R", "S", "T"}
            if tc_code not in valid_types:
                raise ValueError(
                    "TCType must be one of B, E, J, K, N, R, S, or T."
                )
            self.aitask.ai_channels.add_ai_thrmcpl_chan(
                physical_channel,
                units=nidaqmx.constants.TemperatureUnits.DEG_C,
                thermocouple_type=nidaqmx.constants.ThermocoupleType[tc_code],
            )
        elif measurement == "Voltage":
            self.aitask.ai_channels.add_ai_voltage_chan(
                physical_channel,
                units=nidaqmx.constants.VoltageUnits.VOLTS,
            )
        elif measurement == "Current":
            self.aitask.ai_channels.add_ai_current_chan(
                physical_channel,
                units=nidaqmx.constants.CurrentUnits.AMPS,
            )
        else:
            raise ValueError(
                f"Unsupported NI analog-input type {measurement!r}. "
                "Use Thermocouple, Voltage, or Current."
            )

    def StartAIContinuousTask(
        self,
        SamplingRate,
        HowManySample,
        save_tdms: bool = False,
        save_tdms_path: str = "PreSavedData_AI.tdms",
    ) -> None:
        if self.aitask is None:
            raise RuntimeError("CreateFromConfig() must run before starting NI AI.")
        if self._is_running:
            return

        self.save_tdms = bool(save_tdms)
        self.sampleRate = float(SamplingRate)
        self.numberOfSamples = int(HowManySample)
        self.aitask.timing.cfg_samp_clk_timing(
            rate=self.sampleRate,
            sample_mode=nidaqmx.constants.AcquisitionType.CONTINUOUS,
            samps_per_chan=self.numberOfSamples,
        )

        if self.save_tdms:
            log_and_read = nidaqmx.constants.LoggingMode(15842)
            self.aitask.in_stream.configure_logging(
                save_tdms_path,
                logging_mode=log_and_read,
            )

        self.aitask.start()
        self._is_running = True
        self.ActualSamplingRate = self.aitask.timing.samp_clk_rate

    def GetActualSamplingRate(self):
        if self.aitask is None:
            return 0.0
        return self.aitask.timing.samp_clk_rate

    def _GetContinousAIData(self):
        if self.aitask is None:
            raise RuntimeError("NI AI task is not configured.")

        while self.aitask.in_stream.avail_samp_per_chan < self.numberOfSamples:
            time.sleep(0.01)

        self.ActualSamplingRate = self.aitask.timing.samp_clk_rate
        return self.aitask.read(
            number_of_samples_per_channel=self.numberOfSamples
        )

    def threadaitask(self):
        if self.aitask is None:
            raise RuntimeError("NI AI task is not configured.")
        return self.aitask.read(
            number_of_samples_per_channel=self.numberOfSamples
        )

    def stop_ai(self) -> None:
        if self.aitask is None:
            self._is_running = False
            return
        try:
            if self._is_running:
                self.aitask.stop()
        finally:
            self._is_running = False

    def close(self) -> None:
        task = self.aitask
        self.aitask = None
        self._is_running = False
        if task is None:
            return
        try:
            task.stop()
        except Exception:
            pass
        task.close()
