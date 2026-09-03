"""FirePyDAQ MQTT publishing pipeline.

This module has no dependency on the internal FirePyDAQ dashboard. It accepts
plain dictionaries from AcquisitionEngine and owns MQTT publishing, rate limiting,
retained metadata, QoS selection, local mirrors, and connection state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .publisher_base import Publisher
from .payload_builder import MqttPayloadBuilder
from ..utilities.firepydaq_path import (get_telemetry_dir,)

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "MQTT support requires paho-mqtt. Install with: poetry add paho-mqtt"
    ) from exc

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    topic_root: str = "firepydaq"
    client_id: str = "firepydaq-acquisition"
    username: Optional[str] = None
    password: Optional[str] = None
    keepalive_s: int = 30
    live_interval_s: float = 1.0
    health_interval_s: float = 5.0
    qos_metadata: int = 1
    qos_live: int = 0
    qos_health: int = 1
    qos_events: int = 1
    tls_enabled: bool = False
    tls_ca_file: Optional[str] = None

    @classmethod
    def from_json(cls, path: str | Path) -> "MqttConfig":
        with Path(path).open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        return cls(**data)

    @classmethod
    def load_default(cls) -> "MqttConfig":
        """
        Load mqtt_config.default.json.
        """
        telemetry_dir = Path(__file__).parent
        default_config = (telemetry_dir / "mqtt_config.default.json")

        return cls.from_json(default_config)


@dataclass(frozen=True)
class _PublishItem:
    topic_suffix: str
    payload: dict[str, Any]
    qos: int
    retain: bool
    mirror_name: str


class MqttPublisher(Publisher):
    """Nonblocking publisher for metadata, live values, health, and events."""

    def __init__(
        self,
        *,
        config: MqttConfig | None = None,
        payload_builder=None,
        notify: Optional[Callable[[str, str], None]] = None,
        queue_size: int = 256,
    ) -> None:

        if config is None:
            config = MqttConfig.load_default()

        self.config = config
        self.telemetry_save_dir = Path(get_telemetry_dir())

        self.payload_builder = MqttPayloadBuilder()
        self._notify = notify or (lambda _text, _level: None)
        self._queue: queue.Queue[_PublishItem | object] = queue.Queue(
            maxsize=queue_size
        )
        self._stop_token = object()
        self._worker: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._running = False
        self._last_live_publish = 0.0
        self._last_health_publish = 0.0
        self._metadata_fingerprint: Optional[str] = None
        self._dropped_messages = 0
        self._lock = threading.Lock()

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.enable_logger(LOGGER)
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        if config.tls_enabled:
            self._client.tls_set(ca_certs=config.tls_ca_file)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._write_status("CONNECTING")
        self._client.connect_async(
            self.config.host,
            int(self.config.port),
            int(self.config.keepalive_s),
        )
        self._client.loop_start()
        self._worker = threading.Thread(
            target=self._publish_worker,
            name="firepydaq-mqtt-publisher",
            daemon=False,
        )
        self._worker.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._queue.put(self._stop_token, timeout=1.0)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout_s)
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._connected.clear()
            self._write_status("STOPPED")

    def publish_metadata(
        self,
        *,
        experiment: Mapping[str, Any],
        channels: Mapping[str, Mapping[str, Any]],
        force: bool = False,
    ) -> bool:
        """Publish retained metadata when first seen or when it changes."""
        project_name = experiment.get("project_name", "",)
        series_name = experiment.get("series_name", "",)
        test_name = Path(experiment.get("test_name", "")).stem

        run_id = experiment.get("run_id", test_name,)
        payload = {
            "schema": "firepydaq.metadata.v3",
            "timestamp": time.time(),

            "project_name": project_name,
            "test_series": series_name,
            "test_name": test_name,
            "run_id": run_id,
            "operator": experiment.get("operator", ""),
            "acquisition_mode": experiment.get("acquisition_mode", "FULL"),

            "channels": {
                name: dict(info)
                for name, info in channels.items()
            },
        }

        fingerprint = self._fingerprint(
            {
                "run_id": payload["run_id"],
                "project_name": payload["project_name"],
                "test_series": payload["test_series"],
                "test_name": payload["test_name"],
                "operator": payload["operator"],
                "acquisition_mode": payload["acquisition_mode"],
                "channels": payload["channels"],
            }
        )
        if not force and fingerprint == self._metadata_fingerprint:
            return False
        self._metadata_fingerprint = fingerprint
        return self._enqueue(
            "metadata",
            payload,
            qos=self.config.qos_metadata,
            retain=True,
            mirror_name="mqtt_metadata.json",
        )

    def publish_live(
        self,
        values: Mapping[str, Any],
        *,
        timestamp: Optional[float] = None,
        force: bool = False,
    ) -> bool:
        """Publish a flattened channel-value snapshot at the configured interval."""
        now_mono = time.monotonic()
        if (
            not force
            and now_mono - self._last_live_publish
            < self.config.live_interval_s
        ):
            return False
        self._last_live_publish = now_mono
        payload = {
            "schema": "firepydaq.live.v1",
            "timestamp": float(timestamp if timestamp is not None else time.time()),
            "values": self._json_safe_values(values),
        }

        return self._enqueue(
            "live",
            payload,
            qos=self.config.qos_live,
            retain=False,
            mirror_name="mqtt_live.json",
        )

    def publish_health(
        self,
        devices: Mapping[str, Any],
        *,
        timestamp: Optional[float] = None,
        force: bool = False,
    ) -> bool:
        now_mono = time.monotonic()
        if (
            not force
            and now_mono - self._last_health_publish
            < self.config.health_interval_s
        ):
            return False
        self._last_health_publish = now_mono
        payload = {
            "schema": "firepydaq.health.v1",
            "timestamp": float(timestamp if timestamp is not None else time.time()),
            "devices": self._json_safe(devices),
        }
        return self._enqueue(
            "device_health",
            payload,
            qos=self.config.qos_health,
            retain=True,
            mirror_name="mqtt_device_health.json",
        )

    def publish_event(
        self,
        *,
        text: str,
        event_type: str = "operator",
        timestamp: Optional[float] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        payload = {
            "schema": "firepydaq.event.v1",
            "timestamp": float(timestamp if timestamp is not None else time.time()),
            "type": event_type,
            "text": str(text),
            "details": self._json_safe(details or {}),
        }
        return self._enqueue(
            "events",
            payload,
            qos=self.config.qos_events,
            retain=False,
            mirror_name="mqtt_last_event.json",
        )

    def _enqueue(
        self,
        topic_suffix: str,
        payload: dict[str, Any],
        *,
        qos: int,
        retain: bool,
        mirror_name: str,
    ) -> bool:
        if not self._running:
            return False
        item = _PublishItem(topic_suffix, payload, qos, retain, mirror_name)
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            with self._lock:
                self._dropped_messages += 1
            self._write_status("QUEUE_FULL")
            self._notify("MQTT queue full; message dropped", "warning")
            return False

    def _publish_worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._stop_token:
                    return
                assert isinstance(item, _PublishItem)
                self._write_json_atomic(
                    self.telemetry_save_dir / item.mirror_name,
                    item.payload,
                )
                self._append_jsonl(
                    self.telemetry_save_dir / "mqtt_publish_log.jsonl",
                    {
                        "topic": self._topic(item.topic_suffix),
                        "qos": item.qos,
                        "retain": item.retain,
                        "payload": item.payload,
                    },
                )
                if not self._connected.wait(timeout=2.0):
                    self._write_status("DISCONNECTED")
                    continue
                info = self._client.publish(
                    self._topic(item.topic_suffix),
                    json.dumps(item.payload, separators=(",", ":")),
                    qos=item.qos,
                    retain=item.retain,
                )
                if item.qos > 0:
                    info.wait_for_publish(timeout=2.0)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    self._write_status("PUBLISH_ERROR", error=str(info.rc))
            except Exception as exc:
                LOGGER.exception("MQTT publish failed")
                self._write_status("PUBLISH_ERROR", error=str(exc))
                self._notify(f"MQTT publish failed: {exc}", "warning")
            finally:
                self._queue.task_done()

    def _topic(self, suffix: str) -> str:
        return f"{self.config.topic_root.strip('/')}/{suffix}"

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if not reason_code.is_failure:
            self._connected.set()
            self._write_status("CONNECTED")
            self._notify("MQTT connected", "success")
        else:
            self._connected.clear()
            self._write_status("CONNECT_ERROR", error=str(reason_code))

    def _on_disconnect(
        self,
        _client,
        _userdata,
        _disconnect_flags,
        reason_code,
        _properties,
    ) -> None:
        self._connected.clear()
        self._write_status("DISCONNECTED", error=str(reason_code))

    def _write_status(self, state: str, *, error: Optional[str] = None) -> None:
        with self._lock:
            dropped = self._dropped_messages
        self._write_json_atomic(
            self.telemetry_save_dir / "mqtt_status.json",
            {
                "state": state,
                "timestamp": time.time(),
                "broker": self.config.host,
                "port": self.config.port,
                "topic_root": self.config.topic_root,
                "queue_size": self._queue.qsize(),
                "dropped_messages": dropped,
                "error": error,
            },
        )

    @staticmethod
    def _fingerprint(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _json_safe_values(cls, values: Mapping[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for name, value in values.items():
            try:
                safe[str(name)] = float(value)
            except (TypeError, ValueError):
                safe[str(name)] = cls._json_safe(value)
        return safe

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        if hasattr(value, "value") and not isinstance(value, Mapping):
            return cls._json_safe(value.value)
        if isinstance(value, Mapping):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(v) for v in value]
        if hasattr(value, "__dataclass_fields__"):
            return cls._json_safe(asdict(value))
        return str(value)

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, path,)
        except PermissionError:
            return

    @staticmethod
    def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
