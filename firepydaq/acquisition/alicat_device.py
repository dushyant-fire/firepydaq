from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

from .abstract_device import AbstractDevice, DeviceState
from ..api.EchoAlicat import EchoController


class AlicatDevice(AbstractDevice):
    """Sole owner of one Alicat connection and polling worker."""

    def __init__(
        self,
        *,
        name: str,
        port: str,
        gas: str,
        poll_interval_s: float = 0.5,
        notify: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        super().__init__(name=name, device_type="alicat")
        self.port = port
        self.gas = gas
        self.poll_interval_s = max(float(poll_interval_s), 0.1)
        self.notify = notify

        self._io_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._controller: Optional[EchoController] = None

        print(
            "NEW ALICAT DEVICE:",
            name,
            "poll_interval_s =",
            poll_interval_s,
        )

    @property
    def connected(self) -> bool:
        return self.state in (DeviceState.CONNECTED, DeviceState.RUNNING)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def configure(self, *, port: str, gas: str) -> None:
        if self.connected or self.running:
            raise RuntimeError("Disconnect the Alicat before changing its configuration.")
        self.port = port
        self.gas = gas

    def connect(self) -> None:
        if self.connected:
            return

        self._stop_worker()
        with self._io_lock:
            self._close_transport_locked(suppress_errors=True)
            loop = asyncio.new_event_loop()
            controller = EchoController()
            try:
                loop.run_until_complete(
                    controller.set_params(self.port, gas=self.gas)
                )
            except Exception as exc:
                loop.close()
                self._set_error(exc)
                raise

            self._loop = loop
            self._controller = controller
            self._set_state(DeviceState.CONNECTED)

        self._notify(f"{self.name} connected successfully", "success")

    def disconnect(self) -> None:
        self._stop_worker()
        close_error: Optional[BaseException] = None

        with self._io_lock:
            try:
                self._close_transport_locked(suppress_errors=False)
            except Exception as exc:
                close_error = exc
            finally:
                self._controller = None
                self._loop = None
                self._clear_snapshot(DeviceState.DISCONNECTED)

        if close_error is None:
            self._notify(
                f"{self.name} disconnected as intended",
                "warning",
            )
        else:
            self._notify(
                f"{self.name} disconnected with warning: {close_error}",
                "warning",
            )

    def start(self) -> None:
        if not self.connected or self.running:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"firepydaq-alicat-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_worker()
        if self.connected:
            self._set_state(DeviceState.CONNECTED)

    def set_flow(self, flow_rate: float) -> None:
        print(
            "SET:",
            self.name,
            flow_rate,
            time.time(),
        )
        with self._io_lock:
            loop, controller = self._require_transport_locked()
            loop.run_until_complete(
                controller.set_MFC_val(flow_rate=float(flow_rate))
            )

    def stop_flow(self) -> None:
        self.set_flow(0.0)

    def settings_to_dict(self) -> dict[str, Any]:
        return {
            "Type": "alicat",
            "COMPORT": self.port,
            "Gas": self.gas,
            "PollingIntervalSeconds": self.poll_interval_s,
        }

    def _poll_loop(self) -> None:
        next_read = time.monotonic()
        while not self._stop_event.is_set():
            delay = next_read - time.monotonic()
            if delay > 0 and self._stop_event.wait(delay):
                return

            try:
                with self._io_lock:
                    loop, controller = self._require_transport_locked()
                    print(
                        "ALICAT POLL:",
                        self.name,
                        time.time(),
                    )
                    values = loop.run_until_complete(controller.get_MFC_val())
                self._publish(dict(values))
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                # self._set_error(exc)
                print(
                    "ALICAT EXCEPTION:",
                    repr(exc)
                )
                self._notify(f"{self.name} read error: {exc}", "warning")
                continue

            print(
                "ALICAT INTERVAL:",
                self.poll_interval_s
            )
            next_read = max(
                next_read + self.poll_interval_s,
                time.monotonic(),
            )

    def _stop_worker(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.poll_interval_s * 6.0))
        self._thread = None

    def _close_transport_locked(self, *, suppress_errors: bool) -> None:
        controller = self._controller
        loop = self._loop
        if loop is None:
            return

        try:
            if controller is not None and not loop.is_closed():
                loop.run_until_complete(controller.end_connection())
        except Exception:
            if not suppress_errors:
                raise
        finally:
            if not loop.is_closed():
                loop.close()

    def _require_transport_locked(self):
        if (
            self._controller is None
            or self._loop is None
            or self._loop.is_closed()
        ):
            raise RuntimeError(f"{self.name} is not connected.")
        return self._loop, self._controller

    def _notify(self, text: str, level: str) -> None:
        if self.notify is not None:
            self.notify(text, level)
