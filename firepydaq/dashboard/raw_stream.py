from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow.parquet as pq
import zmq

RAW_PUB_ADDRESS = "tcp://127.0.0.1:5557"
LOGGER = logging.getLogger(__name__)


class RawDataPublisher:
    def __init__(self, address=RAW_PUB_ADDRESS):
        self.context = zmq.Context.instance()
        self.socket = self.context.socket(zmq.PUB)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.bind(address)

    def publish(self, payload):
        try:
            self.socket.send_pyobj(payload, flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False

    def close(self):
        self.socket.close(linger=0)


class IncrementalProcessor:
    def __init__(self, config_path, formulae_path=None):
        self.config_path = str(config_path)
        self.formulae_path = str(formulae_path or "")
        self.config = pl.read_csv(self.config_path, ignore_errors=True)
        self.config.columns = [str(column).strip() for column in self.config.columns]
        self.labels = [str(value).strip() for value in self.config["Label"].to_list()]
        self.scale = self._build_scale_map()

    def _build_scale_map(self):
        scale = {}
        for row in self.config.iter_rows(named=True):
            label = str(row["Label"]).strip()
            chart = str(row.get("Chart", "")).strip()
            channel_type = str(row.get("Type", "")).strip()

            if chart.lower() == "none":
                scale[label] = None
                continue

            if channel_type.lower() == "thermocouple":
                scale[label] = ("passthrough", 1.0, 0.0)
                continue

            try:
                ai_min = float(row["AIRangeMin"])
                ai_max = float(row["AIRangeMax"])
                scale_min = float(row["ScaleMin"])
                scale_max = float(row["ScaleMax"])

                if np.isclose(ai_max, ai_min):
                    scale[label] = ("passthrough", 1.0, 0.0)
                    continue

                gain = (scale_max - scale_min) / (ai_max - ai_min)
                offset = scale_min - ai_min * gain
                scale[label] = ("linear", gain, offset)
            except (KeyError, TypeError, ValueError):
                scale[label] = ("passthrough", 1.0, 0.0)

        return scale

    def process_batch(self, raw_batch):
        labels = [str(value).strip() for value in raw_batch["labels"]]
        data = np.asarray(raw_batch["data"])
        if data.ndim == 1:
            data = data[np.newaxis, :]

        result = {
            "AbsoluteTime": list(raw_batch["absolute_time"]),
            "Time": np.asarray(raw_batch["time"], dtype=np.float64),
        }

        for index, label in enumerate(labels):
            if index >= data.shape[0]:
                break

            rule = self.scale.get(label, ("passthrough", 1.0, 0.0))
            if rule is None:
                continue

            mode, gain, offset = rule
            values = np.asarray(data[index], dtype=np.float64)
            result[label] = values * gain + offset if mode == "linear" else values

        return result


class RawDataSubscriber:
    """Read live blocks from ZMQ and atomic Parquet chunks for one active run."""

    def __init__(
        self,
        config_path,
        formulae_path=None,
        address=RAW_PUB_ADDRESS,
        chunk_dir=None,
        max_seconds=300,
        max_ui_hz=2,
        poll_interval_s=0.2,
    ):
        self.context = zmq.Context.instance()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(address)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, "")

        self.processor = IncrementalProcessor(config_path, formulae_path)

        # IMPORTANT: use the exact per-run directory supplied by app.py.
        # Do not replace this with the .firepydaq root directory.
        self.chunk_dir = Path(chunk_dir).expanduser().resolve() if chunk_dir else None

        self.max_seconds = float(max_seconds)
        self.min_update_period = 1.0 / max(float(max_ui_hz), 0.1)
        self.poll_interval_s = float(poll_interval_s)

        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.processed_batches = deque()
        self._seen_chunks = set()
        self.last_error = None

    def start(self):
        if self.running:
            return

        self.running = True
        self.thread = threading.Thread(
            target=self._loop,
            name="firepydaq-dashboard-stream",
            daemon=True,
        )
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.socket.close(linger=0)

    def _loop(self):
        poller = zmq.Poller()
        poller.register(self.socket, zmq.POLLIN)

        while self.running:
            try:
                events = dict(
                    poller.poll(timeout=max(1, int(self.poll_interval_s * 1000)))
                )
                if self.socket in events:
                    self._receive_zmq()
                self._read_new_chunks()
            except Exception as exc:
                self.last_error = str(exc)
                LOGGER.exception("Dashboard stream error")
                time.sleep(self.poll_interval_s)

    def _receive_zmq(self):
        while self.running:
            try:
                raw_batch = self.socket.recv_pyobj(flags=zmq.NOBLOCK)
            except zmq.Again:
                return
            self._append(self.processor.process_batch(raw_batch))

    def _read_new_chunks(self):
        if self.chunk_dir is None or not self.chunk_dir.is_dir():
            return

        # The subscriber receives the exact active-run directory. A non-recursive
        # glob prevents chunks from old or concurrent runs from entering this plot.
        for path in sorted(self.chunk_dir.glob("chunk_*.parquet")):
            identity = str(path.resolve()).casefold()
            if identity in self._seen_chunks:
                continue

            try:
                table = pq.read_table(path)
                if table.num_rows == 0:
                    self._seen_chunks.add(identity)
                    continue

                # Strip CSV/config whitespace and Parquet column whitespace before
                # matching labels. Preserve the original Arrow values.
                data = {
                    str(name).strip(): values
                    for name, values in table.to_pydict().items()
                }

                if "Time" not in data:
                    LOGGER.warning("Skipping chunk without Time column: %s", path)
                    self._seen_chunks.add(identity)
                    continue

                batch = {
                    "AbsoluteTime": data.get("AbsoluteTime", []),
                    "Time": np.asarray(data["Time"], dtype=np.float64),
                }

                for label in self.processor.labels:
                    if label in data:
                        batch[label] = np.asarray(data[label], dtype=np.float64)

                channel_keys = set(batch).difference({"Time", "AbsoluteTime"})
                if not channel_keys:
                    LOGGER.warning(
                        "Chunk has no configured channel columns. file=%s, "
                        "configured=%s, columns=%s",
                        path,
                        self.processor.labels,
                        sorted(data),
                    )

                self._append(batch)
                self._seen_chunks.add(identity)

            except (OSError, ValueError, TypeError, KeyError):
                # Atomic writes should prevent partial reads. If Windows or an AV
                # scanner briefly holds the file, retry it on the next poll.
                LOGGER.debug("Chunk not ready; will retry: %s", path, exc_info=True)
                continue

    def _append(self, processed):
        time_values = np.asarray(processed.get("Time", []), dtype=np.float64)
        if time_values.size == 0:
            return

        with self.lock:
            self.processed_batches.append(processed)
            self._trim_locked()

    def _trim_locked(self):
        if not self.processed_batches:
            return

        latest_values = np.asarray(self.processed_batches[-1].get("Time", []))
        if latest_values.size == 0:
            return

        latest = float(latest_values[-1])
        cutoff = latest - self.max_seconds

        while len(self.processed_batches) > 1:
            first = np.asarray(self.processed_batches[0].get("Time", []))
            if first.size == 0 or float(first[-1]) < cutoff:
                self.processed_batches.popleft()
                continue
            break

    def snapshot(self):
        with self.lock:
            batches = list(self.processed_batches)

        if not batches:
            return {}

        keys = set().union(*(batch.keys() for batch in batches))
        merged = {}

        for key in keys:
            values = [batch[key] for batch in batches if key in batch]
            if not values:
                continue

            if key == "AbsoluteTime":
                merged[key] = [item for value in values for item in list(value)]
            else:
                arrays = [np.asarray(value) for value in values]
                arrays = [value for value in arrays if value.size]
                if arrays:
                    merged[key] = np.concatenate(arrays)

        return merged
