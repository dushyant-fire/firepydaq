"""
Transport-agnostic telemetry publisher interface.

AcquisitionEngine should depend only on Publisher,
never on MQTT directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class Publisher(ABC):

    @abstractmethod
    def start(self) -> None:
        """Start publisher resources."""
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """Stop publisher resources."""
        raise NotImplementedError

    @abstractmethod
    def publish_metadata(
        self,
        *,
        experiment: Mapping[str, Any],
        channels: Mapping[str, Any],
        force: bool = False,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def publish_live(
        self,
        values: Mapping[str, Any],
        *,
        timestamp: float | None = None,
        force: bool = False,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def publish_health(
        self,
        devices: Mapping[str, Any],
        *,
        timestamp: float | None = None,
        force: bool = False,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def publish_event(
        self,
        *,
        text: str,
        event_type: str = "operator",
        timestamp: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        raise NotImplementedError