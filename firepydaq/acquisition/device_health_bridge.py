from __future__ import annotations

from datetime import datetime
from typing import Any

from .abstract_device import DeviceSnapshot


def snapshot_to_health(snapshot: DeviceSnapshot) -> dict[str, Any]:
    """Convert the authoritative device snapshot to persisted health JSON."""
    return {
        "name": snapshot.name,
        "device_type": snapshot.device_type,
        "status": snapshot.state.value,
        "read_count": snapshot.sequence,
        "error_count": 1 if snapshot.error else 0,
        "last_good_read_local": snapshot.local_time,
        "last_error_local": (
            datetime.now().astimezone().isoformat()
            if snapshot.error
            else None
        ),
        "last_error": snapshot.error,
    }
