"""What an Echo exposes on the LAN: discovery and liveness, never control.

An Echo has no local control API. It does answer the Spotify Connect zeroconf
endpoint without authentication, which gives its name, model, multi-room groups
and the active Spotify user, and it accepts TCP connections on the whole-home
audio port, which is enough to tell whether it is powered and on the network.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

import aiohttp

_SPOTIFY_PORT = 4070


@dataclass(slots=True)
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
    timeout: float = 4.0,  # noqa: ASYNC109 - part of the public contract
) -> LocalInfo | None:
    """Return what the Echo at ``host`` says about itself.

    Returns None when the host is unreachable, does not speak Spotify Connect
    or is not an Amazon device.
    """
    own_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(
            f"http://{host}:{_SPOTIFY_PORT}/spotifyConnect",
            params={"action": "getInfo"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except TimeoutError, aiohttp.ClientError, ValueError:
        return None
    finally:
        if own_session:
            await session.close()
    if not isinstance(data, dict) or data.get("brandDisplayName") != "Amazon":
        return None
    aliases = [a for a in data.get("aliases") or [] if isinstance(a, dict)]
    own = [a.get("name") for a in aliases if a.get("isGroup") != "true"]
    return LocalInfo(
        host=host,
        name=data.get("remoteName") or next(iter(own), None),
        model=data.get("modelDisplayName") or None,
        brand=data.get("brandDisplayName"),
        device_id=data.get("deviceID") or None,
        spotify_user=data.get("activeUser") or None,
        groups=tuple(
            a["name"] for a in aliases if a.get("isGroup") == "true" and a.get("name")
        ),
        library_version=data.get("libraryVersion") or None,
    )


async def is_reachable(
    host: str,
    *,
    port: int = 55442,
    timeout: float = 2.0,  # noqa: ASYNC109 - part of the public contract
) -> bool:
    """Return True when ``host`` accepts a TCP connection on ``port``."""
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(host, port)
    except TimeoutError, OSError:
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True
