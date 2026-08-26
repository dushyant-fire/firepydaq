from __future__ import annotations

import csv
import json
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any, Optional
import time


class SerialCsvWriter:
    FIELDNAMES = ("LocalTime", "ElapsedTime", "Device", "Field", "Value")

    def __init__(self, output_prefix: Path, messages: queue.Queue,
                 max_items: int = 512):
        self.output_prefix = Path(output_prefix)
        self.messages = messages
        self.items: queue.Queue = queue.Queue(maxsize=max_items)
        self.stop_token = object()
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.failed = False
        self._files = {}
        self._writers = {}
        self._paths = {}
        self._last_fsync = {}

    def start(self) -> None:
        if self.running:
            return
        self.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.thread = threading.Thread(target=self._run,
                                        name="firepydaq-serial-csv-writer",
                                        daemon=False)
        self.thread.start()

    def put(self, device: str, snapshot: dict, elapsed_s: float) -> bool:
        if not self.running or self.failed:
            return False
        try:
            self.items.put_nowait((device, snapshot, float(elapsed_s)))
            return True
        except queue.Full:
            self._message("error", "Serial writer queue is full; serial data was not saved.")
            return False

    def stop(self, timeout: float = 10.0) -> bool:
        if not self.running:
            return not self.failed
        try:
            self.items.put(self.stop_token, timeout=timeout)
        except queue.Full:
            self._message("error", "Serial writer queue did not drain before shutdown.")
            return False
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self._message("error", "Serial writer did not stop cleanly.")
                return False
        return not self.failed

    def output_paths(self) -> tuple[Path, ...]:
        return tuple(self._paths[name] for name in sorted(self._paths))

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("._")
        return cleaned or "serial_device"

    @staticmethod
    def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
        if isinstance(value, dict):
            rows = []
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                rows.extend(SerialCsvWriter._flatten(child, child_prefix))
            return rows
        if isinstance(value, (list, tuple)):
            rows = []
            for index, child in enumerate(value):
                child_prefix = f"{prefix}.{index}" if prefix else str(index)
                rows.extend(SerialCsvWriter._flatten(child, child_prefix))
            return rows
        if hasattr(value, "tolist"):
            return SerialCsvWriter._flatten(value.tolist(), prefix)
        if value is None or isinstance(value, (str, int, float, bool)):
            return [(prefix or "value", value)]
        return [(prefix or "value", json.dumps(value, default=str))]

    def _get_writer(self, device: str, snapshot: dict):

        if device in self._writers:
            return self._writers[device]

        path = self.output_prefix.with_name(
            f"{self.output_prefix.name}_{self._safe_name(device)}_alicat.csv"
        )

        if path.exists():
            raise FileExistsError(
                f"Alicat output already exists: {path}"
            )

        fields = list(snapshot["value"].keys())

        fieldnames = [
            "LocalTime",
            "ElapsedTime",
        ] + fields

        fp = path.open(
            "x",
            newline="",
            encoding="utf-8",
        )

        writer = csv.DictWriter(
            fp,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        fp.flush()
        os.fsync(fp.fileno())

        self._files[device] = fp
        self._writers[device] = writer
        self._paths[device] = path

        return writer

    def _run(self) -> None:
        try:
            while True:

                item = self.items.get()

                try:

                    if item is self.stop_token:
                        return

                    device, snapshot, elapsed_s = item

                    writer = self._get_writer(
                        device,
                        snapshot,
                    )

                    row = {
                        "LocalTime": snapshot["local_time"],
                        "ElapsedTime": f"{elapsed_s:.6f}",
                    }

                    row.update(snapshot["value"])

                    writer.writerow(row)

                    fp = self._files[device]

                    fp.flush()
                    now = time.monotonic()
                    last_fsync = self._last_fsync.get(device, 0.0)

                    if now - last_fsync > 5.0:
                        os.fsync(fp.fileno())
                        self._last_fsync[device] = now

                finally:
                    self.items.task_done()

        except Exception as exc:

            self.failed = True

            self._message(
                "error",
                f"Serial CSV writer failed: {exc}"
            )

        finally:

            for fp in self._files.values():

                try:
                    fp.flush()
                    os.fsync(fp.fileno())
                    fp.close()

                except Exception:
                    pass

            self.running = False

    def _message(self, level: str, text: str) -> None:
        try:
            self.messages.put_nowait((level, text))
        except queue.Full:
            pass
