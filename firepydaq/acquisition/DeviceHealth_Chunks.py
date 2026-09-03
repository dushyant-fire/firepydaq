from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .abstract_device import DeviceSnapshot
from .device_health_bridge import snapshots_to_health


def _local_now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _atomic_json_write(path: Path, payload: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())

    try:
        os.replace(temporary, path)
    except PermissionError:
        # A dashboard reader may briefly hold the destination on Windows. The
        # prior health file remains valid and the next refresh will retry.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class DeviceHealthManager:
    """Persist AbstractDevice snapshots to device_health.json.

    DeviceRegistry snapshots are authoritative. Legacy register/good_read/error
    methods remain as no-op-compatible shims during migration and never overwrite
    snapshot-derived state.
    """

    def __init__(self, output_dir) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.health_file = self.output_dir / "device_health.json"
        self.devices: dict[str, dict] = {}
        self._lock = threading.RLock()

    def sync_snapshots(
        self,
        snapshots: Mapping[str, DeviceSnapshot],
    ) -> dict[str, dict]:
        payload = snapshots_to_health(snapshots)
        with self._lock:
            # Replace, rather than update, so removed devices do not remain in
            # the final file and every registered NI/Alicat/serial device appears.
            self.devices = payload
            return {
                name: dict(value)
                for name, value in self.devices.items()
            }

    def sync_registry(self, registry) -> dict[str, dict]:
        return self.sync_snapshots(registry.snapshots())

    def write_snapshots(
        self,
        snapshots: Mapping[str, DeviceSnapshot],
    ) -> None:
        payload = self.sync_snapshots(snapshots)
        _atomic_json_write(self.health_file, payload)

    def write_registry(self, registry) -> None:
        self.write_snapshots(registry.snapshots())

    def write(self) -> None:
        with self._lock:
            payload = {
                name: dict(value)
                for name, value in self.devices.items()
            }
        _atomic_json_write(self.health_file, payload)

    # Compatibility shims. These only create metadata placeholders before the
    # first registry sync. They do not own device state.
    def register(self, name, device_type) -> None:
        with self._lock:
            self.devices.setdefault(
                name,
                {
                    "name": name,
                    "device_type": device_type,
                    "status": "DISCONNECTED",
                    "read_count": 0,
                    "error_count": 0,
                    "last_good_read_local": None,
                    "last_error_local": None,
                    "last_error": None,
                },
            )

    def good_read(self, name) -> None:
        return

    def error(self, name) -> None:
        return

    def update_stale_states(self) -> None:
        return


class ChunkManifestManager:
    def __init__(
        self,
        chunk_dir,
        settings,
        labels,
        current_manifest_path=None,
    ) -> None:
        self.chunk_dir = Path(chunk_dir)
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.chunk_dir / "chunk_manifest.json"
        self.current_manifest_path = (
            Path(current_manifest_path)
            if current_manifest_path
            else None
        )
        self._lock = threading.Lock()
        self.manifest = {
            "status": "RECORDING",
            "started_local": _local_now_iso(),
            "project_name": settings.get("Project Name"),
            "series_name": settings.get("Series Name"),
            "test_name": settings.get("Test Name"),
            "sampling_rate_hz": settings.get("Sampling Rate"),
            "channels": list(labels),
            "chunk_count": 0,
            "rows_written": 0,
            "final_parquet": None,
            "final_csv": None,
            "finished_local": None,
            "acquisition_mode":
                settings.get(
                    "Acquisition Mode",
                    "FULL",
                ),
        }

    def update_chunk(self, rows) -> None:
        with self._lock:
            self.manifest["chunk_count"] += 1
            self.manifest["rows_written"] += int(rows)
        self.write()

    def finalize(self, parquet_path, csv_path, verified_rows) -> None:
        with self._lock:
            self.manifest["status"] = "COMPLETE"
            self.manifest["finished_local"] = _local_now_iso()
            self.manifest["final_parquet"] = str(parquet_path)
            self.manifest["final_csv"] = (
                str(csv_path) if csv_path else None
            )
            self.manifest["verified_rows"] = int(verified_rows)
        self.write()

    def write(self) -> None:
        with self._lock:
            payload = dict(self.manifest)
        _atomic_json_write(self.manifest_path, payload)
        if self.current_manifest_path is not None:
            _atomic_json_write(self.current_manifest_path, payload)
