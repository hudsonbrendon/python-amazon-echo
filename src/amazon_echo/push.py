"""HTTP/2 push channel (state changes without polling)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .client import AmazonEchoClient


@dataclass(slots=True, kw_only=True)
class PushEvent:
    """One push message."""

    command: str
    device_serial: str | None = None
    device_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class PushClient:
    """Long-lived HTTP/2 connection that delivers :class:`PushEvent`."""

    def __init__(
        self,
        client: AmazonEchoClient,
        on_event: Callable[[PushEvent], None],
        *,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        """Initialize the push client."""
        self._client = client
        self._on_event = on_event
        self._on_state = on_state
        self.connected = False

    async def start(self) -> None:
        """Start the background task."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Stop the background task."""
        raise NotImplementedError
