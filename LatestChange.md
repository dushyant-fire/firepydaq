## FIREpyDAQ v1.0
- Fixed device queue issues while saving
- Independent writers for NI, alicat, serial devices
- Adding generic serial device reader
- Device health, system log, operator events modified.
- Device health refers to the following
Healthy        Saving active with no writer or device faults
Warning        A device is stale or the queue is elevated
Device error   A registered device reports ERROR
At risk        Writer queue is critically full
Writer error   Background writer has failed
Idle           Saving is not active

## FIREpyDAQ v0.1.1
- Path for saving data changed for a text input to file name for improved clarity. Now, for a text input to file name, a directory `02_ExperimentData` or `01_CalibratoinData` will be created. Within this, a project directory of the format `YYYYProjectName` will be created in which all the data for the same project and experiment type will be saved as per the previous format.
- Added tool-tip for Dash chart export and play/pause button.

**Primary Contributors**
@dushyant-fire