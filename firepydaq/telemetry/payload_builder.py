"""Build compact, transport-neutral FirePyDAQ telemetry payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


class PayloadBuilder:
    """Build canonical metadata, live-value, and health payloads."""

    @staticmethod
    def metadata(
        *,
        settings: Mapping[str, Any],
        ni_labels: Sequence[str],
        ni_units: Mapping[str, str] | None = None,
        snapshots: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        experiment = {
            "run_id": settings.get("Run ID", ""),
            "project_name": settings.get("Project Name", "",),
            "series_name": settings.get("Series Name", "",),
            "test_name": Path(settings.get("Test Name", "",)).stem,
            "operator": settings.get("Name", "",),
            "acquisition_mode": settings.get("Acquisition Mode", "FULL",),
            "sample_rate_hz": settings.get("Sampling Rate", 0.0,),
        }
        experiment = {
            key: value for key, value in experiment.items() if value is not None
        }
        channels: dict[str, dict[str, Any]] = {}
        units = ni_units or {}

        # NI channels are defined only from ni_labels. The NI registry snapshot is
        # deliberately skipped below so each NI channel appears exactly once.
        for label in ni_labels:
            channel_name = str(label)
            channels[channel_name] = PayloadBuilder._channel_metadata(
                source="NI",
                unit=units.get(channel_name, ""),
            )

        for device_name, snapshot in snapshots.items():
            device_type = PayloadBuilder._enum_value(
                getattr(snapshot, "device_type", "unknown")
            )
            if device_type == "ni_daq":
                continue

            snapshot_values = getattr(snapshot, "values", {}) or {}
            snapshot_units = getattr(snapshot, "units", {}) or {}

            for field_name in snapshot_values:
                channel_name = PayloadBuilder.serial_channel_name(
                    str(device_name),
                    str(field_name),
                )
                channels[channel_name] = PayloadBuilder._channel_metadata(
                    source=device_type,
                    unit=str(snapshot_units.get(field_name, "") or ""),
                )

        return experiment, channels

    @staticmethod
    def live_values(
        *,
        ni_labels: Sequence[str],
        ni_values,
        snapshots: Mapping[str, Any],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}

        if ni_values is not None:
            for index, label in enumerate(ni_labels):
                try:
                    channel_values = ni_values[index]
                except (IndexError, TypeError):
                    break

                try:
                    value = channel_values[-1]
                except (IndexError, TypeError):
                    value = channel_values

                values[str(label)] = PayloadBuilder._scalar(value)

        for device_name, snapshot in snapshots.items():
            device_type = PayloadBuilder._enum_value(
                getattr(snapshot, "device_type", "unknown")
            )
            if device_type == "ni_daq":
                continue

            snapshot_values = getattr(snapshot, "values", {}) or {}
            for field_name, value in snapshot_values.items():
                values[PayloadBuilder.serial_channel_name(
                    str(device_name),
                    str(field_name),
                )] = PayloadBuilder._scalar(value)

        return values

    @staticmethod
    def health(snapshots: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Return compact dashboard health, not complete internal snapshots."""
        devices: dict[str, dict[str, Any]] = {}

        for name, snapshot in snapshots.items():
            state = PayloadBuilder._enum_value(
                getattr(snapshot, "state", "UNKNOWN")
            )
            device = {
                "state": state,
                "read_hz": PayloadBuilder._rounded_number(
                    getattr(snapshot, "read_frequency_hz", 0.0)
                ),
                "save_hz": PayloadBuilder._rounded_number(
                    getattr(snapshot, "sample_save_frequency_hz", 0.0)
                ),
                "error": getattr(snapshot, "error", None),
            }
            devices[str(name)] = {
                key: value
                for key, value in device.items()
                if value is not None
            }

        return devices

    @staticmethod
    def serial_channel_name(device_name: str, field_name: str) -> str:
        return f"{device_name}.{field_name}"

    @staticmethod
    def _channel_metadata(*, source: str, unit: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {"source": source}
        if unit:
            metadata["unit"] = unit
        return metadata

    @staticmethod
    def _enum_value(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw)

    @staticmethod
    def _scalar(value: Any) -> Any:
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return value

    @staticmethod
    def _rounded_number(value: Any) -> float:
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return 0.0


# Backward-compatible name for existing imports.
MqttPayloadBuilder = PayloadBuilder
