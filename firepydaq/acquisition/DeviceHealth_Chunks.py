from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path


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
        os.replace(
            temporary,
            path,
        )
    except PermissionError:
        return


class DeviceHealthManager:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.health_file = self.output_dir / "device_health.json"
        self.devices = {}
        self._lock = threading.Lock()

    def register(self, name, device_type):
        with self._lock:
            self.devices.setdefault(name, {
                "name": name,
                "device_type": device_type,
                "status": "INITIALIZING",
                "read_count": 0,
                "error_count": 0,
                "last_good_read_local": None,
                "last_error_local": None,
            })

    def _ensure_device_locked(self, name):
        if name not in self.devices:
            self.devices[name] = {
                "name": name,
                "device_type": "UNKNOWN",
                "status": "INITIALIZING",
                "read_count": 0,
                "error_count": 0,
                "last_good_read_local": None,
                "last_error_local": None,
                "stale_timeout_s": 10.0,
            }
            # self.devices[name] = {
            #     "name": name,
            #     "device_type": "UNKNOWN",
            #     "status": "INITIALIZING",
            #     "read_count": 0,
            #     "error_count": 0,
            #     "last_good_read_local": None,
            #     "last_error_local": None,
            # }
        return self.devices[name]

    def good_read(self, name):
        with self._lock:
            device = self._ensure_device_locked(name)
            device["status"] = "ONLINE"
            device["read_count"] += 1
            device["last_good_read_local"] = _local_now_iso()

    def error(self, name):
        with self._lock:
            device = self._ensure_device_locked(name)
            device["status"] = "ERROR"
            device["error_count"] += 1
            device["last_error_local"] = _local_now_iso()

    def write(self):
        with self._lock:
            payload = {name: dict(value) for name, value in self.devices.items()}
        _atomic_json_write(self.health_file, payload)

    def update_stale_states(self):
        now = datetime.now().astimezone()

        for device in self.devices.values():

            timestamp = device["last_good_read_local"]

            if not timestamp:
                continue

            try:
                last_good = datetime.fromisoformat(
                    timestamp
                )

                age = (
                    now - last_good
                ).total_seconds()

                if (
                    age >
                    device["stale_timeout_s"]
                    and device["status"] != "ERROR"
                ):
                    device["status"] = "STALE"

            except Exception:
                pass


class ChunkManifestManager:
    def __init__(self, chunk_dir, settings, labels, current_manifest_path=None):
        self.chunk_dir = Path(chunk_dir)
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.chunk_dir / "chunk_manifest.json"
        self.current_manifest_path = (
            Path(current_manifest_path) if current_manifest_path else None
        )
        self._lock = threading.Lock()
        self.manifest = {
            "status": "RECORDING",
            "started_local": _local_now_iso(),
            "experiment_name": settings.get("Experiment Name"),
            "test_name": settings.get("Test Name"),
            "sampling_rate_hz": settings.get("Sampling Rate"),
            "channels": list(labels),
            "chunk_count": 0,
            "rows_written": 0,
            "final_parquet": None,
            "final_csv": None,
            "finished_local": None,
        }

    def update_chunk(self, rows):
        with self._lock:
            self.manifest["chunk_count"] += 1
            self.manifest["rows_written"] += int(rows)
        self.write()

    def finalize(self, parquet_path, csv_path, verified_rows):
        with self._lock:
            self.manifest["status"] = "COMPLETE"
            self.manifest["finished_local"] = _local_now_iso()
            self.manifest["final_parquet"] = str(parquet_path)
            self.manifest["final_csv"] = str(csv_path) if csv_path else None
            self.manifest["verified_rows"] = int(verified_rows)
        self.write()

    def write(self):
        with self._lock:
            payload = dict(self.manifest)
        _atomic_json_write(self.manifest_path, payload)
        if self.current_manifest_path is not None:
            _atomic_json_write(self.current_manifest_path, payload)
