from __future__ import annotations

from typing import Any, Mapping

from .abstract_device import DeviceSnapshot


def snapshot_to_health(snapshot: DeviceSnapshot) -> dict[str, Any]:
    """Convert one authoritative snapshot to persisted device health."""
    return {
        "name": snapshot.name,
        "device_type": snapshot.device_type,
        "status": snapshot.state.value,
        "read_count": snapshot.sequence,
        "reads_this_run": snapshot.run_sequence,
        "device_read_frequency_hz": round(snapshot.read_frequency_hz, 3),
        "saved_samples_this_run": snapshot.saved_samples_this_run,
        "sample_save_frequency_hz": round(
            snapshot.sample_save_frequency_hz,
            3,
        ),
        "last_good_read_local": snapshot.local_time,
        "last_save_local": snapshot.last_save_local,
        "error_count": 1 if snapshot.error else 0,
        "last_error_local": (
            snapshot.local_time if snapshot.error else None
        ),
        "last_error": snapshot.error,
    }


def snapshots_to_health(
    snapshots: Mapping[str, DeviceSnapshot],
) -> dict[str, dict[str, Any]]:
    return {
        name: snapshot_to_health(snapshot)
        for name, snapshot in sorted(snapshots.items())
    }
