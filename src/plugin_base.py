"""Plugin base classes for the monitor bot."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Alert:
    """A single alert to be dispatched via Telegram."""

    monitor: str  # e.g. "POLYMARKET"
    title: str  # e.g. "Informed Flow Detected"
    body: str  # formatted alert message body
    link: str  # URL to the relevant market
    data: dict = field(default_factory=dict)  # raw data for SQLite storage


class MonitorPlugin(ABC):
    """Abstract base class for all monitor plugins.

    Subclasses must set ``name`` and ``interval`` as class attributes and
    implement the ``check`` coroutine.
    """

    name: str  # tag used in alerts (e.g. "POLYMARKET")
    interval: int  # seconds between check() calls

    @abstractmethod
    async def check(self) -> list[Alert]:
        """Run one monitoring cycle. Return Alert objects to send."""
        ...

    async def setup(self) -> None:
        """Called once at startup. Override for initialisation."""

    async def teardown(self) -> None:
        """Called on shutdown. Override for cleanup."""
