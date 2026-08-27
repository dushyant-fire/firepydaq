from __future__ import annotations

import json
import os
import queue
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from ..utilities.serial_csv_writer import SerialCsvWriter

WRITER_QUEUE_BLOCKS = 32
WRITER_STOP_TIMEOUT_S = 30.0
PARQUET_COMPRESSION = "zstd"

Notify = Callable[[str, str], None]


@dataclass(frozen=True)
class DataBlock:
    elapsed_s: np.ndarray
    absolute_time: tuple[str, ...]
    values: np.ndarray
    labels: tuple[str, ...]


@dataclass(frozen=True)
class SaveResult:
    completed: bool
    parquet_path: Optional[Path]
    csv_path: Optional[Path]
    chunk_dir: Optional[Path]
    rows_written: int
    chunk_count: int
    writer_ok: bool
    error: Optional[str] = None


class ChunkWriter:
    """Bounded background writer for atomic Parquet chunks."""

    def __init__(self, chunk_dir: Path, messages: queue.Queue) -> None:
        self.chunk_dir = Path(chunk_dir)
        self.messages = messages
        self.items: queue.Queue = queue.Queue(maxsize=WRITER_QUEUE_BLOCKS)
        self.stop_token = object()
        self.thread: Optional[threading.Thread] = None
        self.count = 0
        self.running = False
        self.failed = False

    def start(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.chunk_dir.rglob("chunk_*.parquet"))
        if existing:
            raise FileExistsError(
                f"Chunk directory is not empty: {self.chunk_dir}. "
                "Use a new test name or recover/remove the existing chunks."
            )
        self.running = True
        self.thread = threading.Thread(
            target=self._run,
            name="firepydaq-parquet-writer",
            daemon=False,
        )
        self.thread.start()

    def put(self, block: DataBlock) -> bool:
        if not self.running or self.failed:
            return False
        try:
            self.items.put_nowait(block)
            return True
        except queue.Full:
            self._message(
                "error",
                "Writer queue is full; stopping acquisition to prevent silent data loss.",
            )
            return False

    def stop(self, timeout: float = WRITER_STOP_TIMEOUT_S) -> bool:
        if not self.running:
            return not self.failed
        try:
            self.items.put(self.stop_token, timeout=timeout)
        except queue.Full:
            self._message("error", "Writer queue did not drain before shutdown.")
            return False
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self._message(
                    "error",
                    "Writer did not stop; chunk files remain recoverable.",
                )
                return False
        self.running = False
        return not self.failed

    def _message(self, level: str, text: str) -> None:
        try:
            self.messages.put_nowait((level, text))
        except queue.Full:
            pass

    def _run(self) -> None:
        try:
            while True:
                item = self.items.get()
                try:
                    if item is self.stop_token:
                        return
                    self._write_block(item)
                finally:
                    self.items.task_done()
        except Exception as exc:
            self.failed = True
            self._message("error", f"Writer failed: {exc}")
        finally:
            self.running = False

    def _write_block(self, block: DataBlock) -> None:
        values = np.asarray(block.values)
        if values.ndim == 1:
            values = values[np.newaxis, :]
        sample_count = len(block.elapsed_s)
        if len(block.absolute_time) != sample_count or values.shape[1] != sample_count:
            raise ValueError(
                "Acquisition block dimensions disagree: "
                f"time={sample_count}, absolute_time={len(block.absolute_time)}, "
                f"values={values.shape}."
            )

        arrays = {
            "AbsoluteTime": pa.array(block.absolute_time, type=pa.string()),
            "Time": pa.array(block.elapsed_s, type=pa.float64()),
        }
        for index, label in enumerate(block.labels):
            if index < values.shape[0]:
                arrays[label] = pa.array(values[index], type=pa.float32())

        table = pa.table(arrays)
        final_path = self.chunk_dir / f"chunk_{self.count:08d}.parquet"
        temporary_path = final_path.with_suffix(".parquet.tmp")
        pq.write_table(table, temporary_path, compression=PARQUET_COMPRESSION)
        os.replace(temporary_path, final_path)
        self.count += 1


class SaveManager:
    """Own NI block persistence and final Parquet/CSV consolidation."""

    def __init__(
        self,
        *,
        active_run_dir_getter: Callable[[Path], Path],
        firepydaq_dir: Path,
        notify: Notify,
        device_registry=None,
    ) -> None:
        self._active_run_dir_getter = active_run_dir_getter
        self._firepydaq_dir = Path(firepydaq_dir)
        self._notify = notify
        self._device_registry = device_registry
        self._messages: queue.Queue = queue.Queue(maxsize=100)
        self._finalize_lock = threading.Lock()
        self._writer: Optional[ChunkWriter] = None
        self._active = False
        self._output_prefix: Optional[Path] = None
        self._manifest = None
        self.last_result: Optional[SaveResult] = None
        self._serial_writer: Optional[SerialCsvWriter] = None
        self._serial_last_saved_sequence: dict[str, int] = {}
        self._serial_elapsed_origin: Optional[float] = None

    @property
    def writer(self) -> Optional[ChunkWriter]:
        return self._writer

    @property
    def active(self) -> bool:
        return self._active

    @property
    def queue_fill_ratio(self) -> float:
        writer = self._writer
        if writer is None:
            return 0.0
        return writer.items.qsize() / max(1, writer.items.maxsize)

    def start(self, output_prefix: str | Path, *, manifest=None) -> None:
        if self._active:
            return
        output_prefix = Path(output_prefix)
        chunk_dir = Path(self._active_run_dir_getter(output_prefix))
        chunk_dir.mkdir(parents=True, exist_ok=True)
        writer = ChunkWriter(chunk_dir, self._messages)
        writer.start()
        self._writer = writer
        self._output_prefix = output_prefix
        self._manifest = manifest
        self._active = True
        self.last_result = None
        if self._manifest is not None:
            self._manifest.write()
        self._serial_writer = SerialCsvWriter(
            output_prefix,
            self._messages,
        )
        self._serial_writer.start()
        self._serial_last_saved_sequence.clear()
        self._serial_elapsed_origin = __import__("time").monotonic()

    def submit_block(self, block: DataBlock) -> bool:
        writer = self._writer
        if not self._active or writer is None:
            return False
        accepted = writer.put(block)
        if accepted and self._manifest is not None:
            self._manifest.update_chunk(len(block.elapsed_s))
        return accepted

    def submit_serial_snapshots(
        self,
        snapshots,
        *,
        device_types: tuple[str, ...] = ("alicat",),
        maximum_age_s: float = 2.0,
    ) -> int:
        """Persist each new eligible serial snapshot exactly once."""
        writer = self._serial_writer
        if not self._active or writer is None:
            return 0

        saved = 0
        for name, snapshot in snapshots.items():
            if (
                snapshot.device_type not in device_types
                or not snapshot.has_value
                or snapshot.monotonic_time is None
                or snapshot.age_seconds() > maximum_age_s
            ):
                continue

            last_sequence = self._serial_last_saved_sequence.get(name, 0)
            if snapshot.sequence <= last_sequence:
                continue

            origin = self._serial_elapsed_origin
            elapsed_s = (
                0.0
                if origin is None
                else max(0.0, snapshot.monotonic_time - origin)
            )
            payload = {
                "value": dict(snapshot.values),
                "local_time": snapshot.local_time,
                "monotonic_time": snapshot.monotonic_time,
                "sequence": snapshot.sequence,
                "error": snapshot.error,
            }
            if not writer.put(name, payload, elapsed_s):
                continue

            self._serial_last_saved_sequence[name] = snapshot.sequence
            device = (
                self._device_registry.get(name)
                if self._device_registry is not None
                else None
            )
            if device is not None:
                device.register_saved_samples(1)
            saved += 1
        return saved

    def drain_messages(self) -> list[tuple[str, str]]:
        messages: list[tuple[str, str]] = []
        while True:
            try:
                messages.append(self._messages.get_nowait())
            except queue.Empty:
                return messages

    def stop(self) -> SaveResult:
        with self._finalize_lock:
            if not self._active or self._writer is None or self._output_prefix is None:
                result = SaveResult(True, None, None, None, 0, 0, True)
                self.last_result = result
                return result

            writer = self._writer
            output_prefix = self._output_prefix
            manifest = self._manifest
            writer_ok = writer.stop()
            serial_writer = self._serial_writer
            serial_ok = True
            serial_paths: tuple[Path, ...] = ()
            if serial_writer is not None:
                serial_ok = serial_writer.stop()
                serial_paths = serial_writer.output_paths()
            self._active = False
            self._forward_messages()
            for serial_path in serial_paths:
                self._notify(
                    f"Alicat CSV: {serial_path}",
                    "success" if serial_ok else "warning",
                )

            chunks = sorted(writer.chunk_dir.glob("chunk_*.parquet"))
            if not chunks:
                self._notify("No data chunks were written.", "warning")
                result = SaveResult(
                    False, None, None, writer.chunk_dir, 0, 0, writer_ok,
                    "No data chunks were written.",
                )
                self.last_result = result
                return result

            final_path = Path(str(output_prefix) + ".parquet")
            temporary_path = final_path.with_suffix(final_path.suffix + ".tmp")
            if final_path.exists() or temporary_path.exists():
                error = (
                    "Final output already exists; raw chunks remain in "
                    f"{writer.chunk_dir}"
                )
                self._notify(error, "error")
                result = SaveResult(
                    False, final_path, None, writer.chunk_dir, 0,
                    len(chunks), writer_ok, error,
                )
                self.last_result = result
                return result

            parquet_writer = None
            rows = 0
            csv_path: Optional[Path] = None
            try:
                for chunk in chunks:
                    table = pq.read_table(chunk)
                    if parquet_writer is None:
                        parquet_writer = pq.ParquetWriter(
                            temporary_path,
                            table.schema,
                            compression=PARQUET_COMPRESSION,
                        )
                    parquet_writer.write_table(table)
                    rows += table.num_rows

                if parquet_writer is not None:
                    parquet_writer.close()
                    parquet_writer = None
                os.replace(temporary_path, final_path)

                if self._device_registry is not None:
                    for device in self._device_registry.devices():
                        device.stop_run()

                csv_path = final_path.with_suffix(".csv")
                try:
                    pq.read_table(final_path).to_pandas().to_csv(csv_path, index=False)
                    final_rows = pq.ParquetFile(final_path).metadata.num_rows
                    if final_rows != rows:
                        raise RuntimeError(
                            f"Row mismatch. chunks={rows}, final={final_rows}"
                        )
                    if manifest is not None:
                        manifest.finalize(final_path, csv_path, final_rows)
                        final_manifest_path = (
                            self._firepydaq_dir / f"{final_path.stem}_manifest.json"
                        )
                        with final_manifest_path.open("w", encoding="utf-8") as stream:
                            json.dump(manifest.manifest, stream, indent=2)
                except Exception as exc:
                    self._notify(f"CSV export failed: {exc}", "warning")

                status = "success" if writer_ok else "warning"
                if writer_ok:
                    shutil.rmtree(writer.chunk_dir, ignore_errors=True)
                    self._notify(
                        f"Save completed: {rows:,} samples; file: {final_path}",
                        status,
                    )
                else:
                    self._notify(
                        f"Save completed with writer warning: {rows:,} samples; "
                        f"file: {final_path}; raw chunks retained",
                        status,
                    )

                result = SaveResult(
                    True, final_path, csv_path, writer.chunk_dir, rows,
                    len(chunks), writer_ok,
                )
                self.last_result = result
                return result
            except Exception as exc:
                if parquet_writer is not None:
                    parquet_writer.close()
                temporary_path.unlink(missing_ok=True)
                error = (
                    f"Final consolidation failed: {exc}. Raw chunks remain in "
                    f"{writer.chunk_dir}"
                )
                self._notify(error, "error")
                result = SaveResult(
                    False, None, None, writer.chunk_dir, rows,
                    len(chunks), writer_ok, error,
                )
                self.last_result = result
                return result
            finally:
                self._writer = None
                self._output_prefix = None
                self._manifest = None
                self._serial_writer = None
                self._serial_last_saved_sequence.clear()
                self._serial_elapsed_origin = None

    def _forward_messages(self) -> None:
        for level, text in self.drain_messages():
            self._notify(text, level)
