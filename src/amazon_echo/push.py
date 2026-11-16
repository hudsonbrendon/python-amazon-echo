"""HTTP/2 push channel: state changes arrive without polling.

The Alexa app keeps one long-lived HTTP/2 request open (the AVS "downchannel")
on which Amazon writes a multipart stream. Every part is a JSON directive; the
interesting ones carry ``renderingUpdates`` with a command such as
``PUSH_VOLUME_CHANGE`` and its payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
from email.message import EmailMessage
import json
import logging
import time
from typing import Any

import httpx

from .client import AmazonEchoClient
from .const import AVS_HOST, DEVICE_CAPABILITIES, PUSH_HOST, PUSH_VERSION
from .exceptions import (
    AmazonEchoAuthenticationError,
    AmazonEchoError,
    AmazonEchoResponseError,
)

_LOGGER = logging.getLogger(__name__)

_PING_INTERVAL = 280.0
_BACKOFF_BASE = 5.0
_BACKOFF_MAX = 600.0
_STABLE_AFTER = 60.0  # a stream that lived this long resets the back-off
_MAX_BUFFER = 512 * 1024


@dataclass(slots=True)
class PushEvent:
    """One push message."""

    command: str
    device_serial: str | None = None
    device_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def _loads(value: Any) -> dict[str, Any]:
    """Return ``value`` as a dict, decoding it when it is a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def parse_boundary(content_type: str) -> bytes:
    """Return the multipart delimiter (``--boundary``) of a Content-Type."""
    message = EmailMessage()
    message["Content-Type"] = content_type
    boundary = message.get_boundary()
    if not boundary:
        raise AmazonEchoResponseError(200, f"No multipart boundary: {content_type}")
    return f"--{boundary}".encode()


class StreamParser:
    """Split the multipart byte stream into JSON documents."""

    def __init__(self, delimiter: bytes) -> None:
        """Initialize the parser with the ``--boundary`` delimiter."""
        self._delimiter = delimiter
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Add received bytes and return the documents that are now complete."""
        self._buffer.extend(chunk)
        if len(self._buffer) > _MAX_BUFFER:
            self._buffer.clear()
            raise AmazonEchoResponseError(200, "Push buffer overflow")
        documents = []
        while (index := self._buffer.find(self._delimiter)) != -1:
            part = bytes(self._buffer[:index])
            del self._buffer[: index + len(self._delimiter)]
            start, end = part.find(b"{"), part.rfind(b"}")
            if start == -1 or end < start:
                continue  # preamble, heartbeat or headers only
            try:
                document = json.loads(part[start : end + 1])
            except ValueError:
                _LOGGER.debug("Unparsable push part (%d bytes)", len(part))
                continue
            if isinstance(document, dict):
                documents.append(document)
        return documents


def parse_events(document: dict[str, Any]) -> list[PushEvent]:
    """Turn one directive into push events; nothing is dropped."""
    directive = _loads(document.get("directive"))
    payload = _loads(directive.get("payload"))
    updates = payload.get("renderingUpdates")
    if not isinstance(updates, list):
        header = _loads(directive.get("header"))
        name = ".".join(
            str(header[key]) for key in ("namespace", "name") if header.get(key)
        )
        return [PushEvent(command=name or "UNKNOWN", payload=payload)]
    events = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        metadata = _loads(update.get("resourceMetadata"))
        body = _loads(metadata.get("payload"))
        command = (
            metadata.get("command")
            or update.get("resourceId")
            or update.get("route")
            or "UNKNOWN"
        )
        doppler = _loads(body.get("dopplerId"))
        serial = doppler.get("deviceSerialNumber")
        device_type = doppler.get("deviceType")
        if not serial:
            # activity records: key.entryId = "<epoch ms>#<deviceType>#<serial>"
            entry = str(_loads(body.get("key")).get("entryId") or "").split("#")
            if len(entry) >= 3:
                device_type, serial = entry[-2], entry[-1]
        events.append(
            PushEvent(
                command=str(command),
                device_serial=serial or None,
                device_type=device_type or None,
                payload=body or metadata or update,
            )
        )
    return events


class PushClient:
    """Long-lived HTTP/2 connection that delivers :class:`PushEvent`.

    Args:
        client: The API client whose session is used.
        on_event: Called for every push message.
        on_state: Called with True/False when the stream opens/closes.

    """

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
        self._task: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None
        self._registered = False
        self.connected = False

    async def start(self) -> None:
        """Start the background task (it reconnects on its own)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="amazon_echo-push")

    async def stop(self) -> None:
        """Stop the background task and close the connection."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()
        self._set_connected(connected=False)

    def _set_connected(self, *, connected: bool) -> None:
        if self.connected == connected:
            return
        self.connected = connected
        if self._on_state is not None:
            try:
                self._on_state(connected)
            except Exception:
                _LOGGER.exception("Push state callback failed")

    async def _run(self) -> None:
        """Keep the stream open, backing off 5·2ⁿ s (≤ 600 s) between tries."""
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                await self._stream()
                _LOGGER.debug("Push stream closed by Amazon")
            except AmazonEchoError as err:
                _LOGGER.debug("Push stream failed: %s", err)
            except (httpx.HTTPError, OSError) as err:
                _LOGGER.debug("Push connection lost: %r", err)
            finally:
                self._set_connected(connected=False)
            if time.monotonic() - started >= _STABLE_AFTER:
                attempt = 0
            await asyncio.sleep(min(_BACKOFF_BASE * 2**attempt, _BACKOFF_MAX))
            attempt += 1

    async def _base_url(self) -> str:
        urls = await self._client.get_service_urls()
        return urls.get("bobDispatchUrl") or "https://" + PUSH_HOST.format(
            region=self._client.home_region.lower()
        )

    async def _bearer(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._client.async_get_access_token()}"
        }

    async def _register_capabilities(self, http: httpx.AsyncClient) -> None:
        """Declare the app's AVS capabilities; without them nothing is pushed.

        Sessions imported from ``alexa_media`` share that integration's
        registration, which already declared them, so they are left alone.
        """
        if self._registered or self._client.credentials.source == "alexa_media":
            return
        response = await http.put(
            f"https://{AVS_HOST}/v1/devices/@self/capabilities",
            json=DEVICE_CAPABILITIES,
            headers=await self._bearer(),
        )
        self._check(response.status_code, "capabilities")
        self._registered = True

    def _check(self, status: int, what: str) -> None:
        if status in (401, 403):
            # make the next attempt fetch a new token
            self._client.credentials.access_token_expires = 0.0
            raise AmazonEchoAuthenticationError(f"Push {what} rejected (HTTP {status})")
        if status >= 400:
            raise AmazonEchoResponseError(status, f"push {what}")

    async def _stream(self) -> None:
        """Open the downchannel once and process it until it ends."""
        if self._http is None:
            # building the TLS context reads the CA bundle from disk
            self._http = await asyncio.get_running_loop().run_in_executor(
                None, lambda: httpx.AsyncClient(http2=True, timeout=httpx.Timeout(30.0))
            )
        http = self._http
        base = await self._base_url()
        await self._register_capabilities(http)
        async with http.stream(
            "GET",
            f"{base}/{PUSH_VERSION}/directives",
            headers={**await self._bearer(), "Accept": "multipart/related"},
            timeout=httpx.Timeout(30.0, read=None),
        ) as response:
            self._check(response.status_code, "stream")
            parser = StreamParser(
                parse_boundary(response.headers.get("content-type", ""))
            )
            self._set_connected(connected=True)
            ping = asyncio.create_task(self._ping(http, base))
            read = asyncio.create_task(self._read(response, parser))
            try:
                done, _ = await asyncio.wait(
                    {ping, read}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (ping, read):
                    task.cancel()
                await asyncio.gather(ping, read, return_exceptions=True)
            for task in done:
                task.result()

    async def _read(self, response: httpx.Response, parser: StreamParser) -> None:
        async for chunk in response.aiter_bytes():
            for document in parser.feed(chunk):
                for event in parse_events(document):
                    try:
                        self._on_event(event)
                    except Exception:
                        _LOGGER.exception("Push event callback failed")

    async def _ping(self, http: httpx.AsyncClient, base: str) -> None:
        """Keep the connection alive; a failing ping ends the stream."""
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            response = await http.get(f"{base}/ping", headers=await self._bearer())
            self._check(response.status_code, "ping")
