"""What an Echo exposes on the LAN (discovery and liveness only)."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp


@dataclass(slots=True, kw_only=True)
class LocalInfo:
    """Answer of the Spotify Connect zeroconf endpoint of an Echo."""

    host: str
    name: str | None = None
    model: str | None = None
    brand: str | None = None
    device_id: str | None = None
    spotify_user: str | None = None
    groups: tuple[str, ...] = ()
    library_version: str | None = None


async def probe_local(
    host: str,
    *,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 4.0,  # noqa: ASYNC109
) -> LocalInfo | None:
    """Return what the Echo at ``host`` says about itself, or None."""
    raise NotImplementedError


async def is_reachable(
    host: str,
    *,
    port: int = 55442,
    timeout: float = 2.0,  # noqa: ASYNC109
) -> bool:
    """Return True when ``host`` accepts a TCP connection on ``port``."""
    raise NotImplementedError
