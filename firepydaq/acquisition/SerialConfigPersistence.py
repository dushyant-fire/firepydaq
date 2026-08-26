from __future__ import annotations

from dataclasses import asdict, is_dataclass


def serial_devices_to_settings(app) -> dict:
    """Serialize Device Manager streaming serial devices into configuration JSON."""
    saved = {}
    for name, runtime in getattr(app, "generic_serial_devices", {}).items():
        config = getattr(runtime, "config", None)
        if config is None:
            continue
        if is_dataclass(config):
            payload = asdict(config)
        else:
            payload = dict(vars(config))
        payload["name"] = str(payload.get("name", name))
        saved[name] = payload
    return saved


def restore_serial_devices(app, devices_payload: dict) -> None:
    """Recreate saved streaming serial runtimes without auto-connecting ports."""
    serial_payload = devices_payload.get("SerialDevices", {})
    if not serial_payload:
        return
    from .DeviceManager import StreamingSerialConfig, StreamingSerialRuntime

    if not hasattr(app, "generic_serial_devices"):
        app.generic_serial_devices = {}
    for name, raw in serial_payload.items():
        allowed = {
            "name", "port", "baud_rate", "delimiter", "columns",
            "read_timeout_s", "save_frequency_hz", "encoding", "enabled",
        }
        values = {key: value for key, value in raw.items() if key in allowed}
        values["name"] = str(values.get("name", name))
        values["baud_rate"] = int(values.get("baud_rate", 9600))
        values["read_timeout_s"] = float(values.get("read_timeout_s", 0.25))
        values["save_frequency_hz"] = float(values.get("save_frequency_hz", 5.0))
        config = StreamingSerialConfig(**values)
        app.generic_serial_devices[name] = StreamingSerialRuntime(config, app.notify)
        registry = getattr(app, "device_registry", None,)

        if registry is not None:
            registry.register(app.generic_serial_devices[name])
