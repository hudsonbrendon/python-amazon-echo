"""Tests for the HTTP/2 push channel (mocked transport only)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from amazon_echo import AmazonEchoResponseError, PushClient, PushEvent
import amazon_echo.push as push_module
from amazon_echo.push import StreamParser, parse_boundary, parse_events

CONTENT_TYPE = "multipart/related; boundary=------abcde123; type=application/json"
DELIMITER = b"--------abcde123"
SERIAL = "G000TEST00000001"


def directive(command: str, payload: dict, **extra) -> dict:
    """Build a push directive the way Amazon nests it (JSON inside JSON)."""
    metadata = {"command": command, "payload": json.dumps(payload), "timeStamp": 1}
    update = {"resourceId": command, "resourceMetadata": json.dumps(metadata), **extra}
    return {
        "directive": {
            "header": {"namespace": "Alexa.ApiGateway", "name": "RenderingUpdate"},
            "payload": {"renderingUpdates": [update]},
        }
    }


def part(document: dict) -> bytes:
    body = json.dumps(document).encode()
    return b"\r\nContent-Type: application/json\r\n\r\n" + body + b"\r\n" + DELIMITER


VOLUME = directive(
    "PUSH_VOLUME_CHANGE",
    {
        "dopplerId": {"deviceSerialNumber": SERIAL, "deviceType": "A18O6U1UQFJ0XK"},
        "isMuted": False,
        "volumeSetting": 47,
    },
)


def test_parse_boundary():
    assert parse_boundary(CONTENT_TYPE) == DELIMITER
    with pytest.raises(AmazonEchoResponseError):
        parse_boundary("application/json")


def test_stream_parser_handles_split_chunks():
    parser = StreamParser(DELIMITER)
    assert parser.feed(DELIMITER + b"\r\n") == []  # preamble
    data = part(VOLUME) + part({"directive": {"x": "{}"}})
    documents = []
    for index in range(0, len(data), 7):
        documents += parser.feed(data[index : index + 7])
    assert documents == [VOLUME, {"directive": {"x": "{}"}}]


def test_stream_parser_skips_garbage():
    parser = StreamParser(DELIMITER)
    data = (
        b"\r\nContent-Type: application/json\r\n\r\n{broken\r\n}"
        + DELIMITER
        + b"\r\n\r\n[1, 2]"
        + DELIMITER
        + b"\r\nno json here\r\n"
        + DELIMITER
        + part(VOLUME)
    )
    assert parser.feed(data) == [VOLUME]


def test_stream_parser_overflow():
    parser = StreamParser(DELIMITER)
    with pytest.raises(AmazonEchoResponseError, match="overflow"):
        parser.feed(b"x" * (600 * 1024))
    assert parser.feed(part(VOLUME)) == [VOLUME]


def test_parse_events_volume():
    (event,) = parse_events(VOLUME)
    assert event == PushEvent(
        command="PUSH_VOLUME_CHANGE",
        device_serial=SERIAL,
        device_type="A18O6U1UQFJ0XK",
        payload={
            "dopplerId": {"deviceSerialNumber": SERIAL, "deviceType": "A18O6U1UQFJ0XK"},
            "isMuted": False,
            "volumeSetting": 47,
        },
    )


def test_parse_events_activity_entry_id():
    document = directive(
        "PUSH_ACTIVITY",
        {
            "key": {
                "entryId": f"1800000000000#A18O6U1UQFJ0XK#{SERIAL}",
                "registeredUserId": "u",
            }
        },
    )
    (event,) = parse_events(document)
    assert (event.command, event.device_serial, event.device_type) == (
        "PUSH_ACTIVITY",
        SERIAL,
        "A18O6U1UQFJ0XK",
    )


def test_parse_events_never_drops_unknown_shapes():
    # already-decoded metadata, unknown command, no device
    update = {"resourceId": "SOMETHING_NEW", "resourceMetadata": {"payload": {"a": 1}}}
    document = {"directive": {"payload": {"renderingUpdates": [update, "junk"]}}}
    (event,) = parse_events(document)
    assert (event.command, event.device_serial, event.payload) == (
        "SOMETHING_NEW",
        None,
        {"a": 1},
    )

    # smart-home update identified only by its route
    fdal = {"route": "EventBus:AlexaMobile::FDAL", "resourceMetadata": "not json"}
    (event,) = parse_events({"directive": {"payload": {"renderingUpdates": [fdal]}}})
    assert event.command == "EventBus:AlexaMobile::FDAL"
    assert event.payload == fdal

    (event,) = parse_events({"directive": {"payload": {"renderingUpdates": [{}]}}})
    assert event.command == "UNKNOWN"

    # a directive without rendering updates is passed through by name
    other = {
        "directive": {
            "header": {"namespace": "System", "name": "SetEndpoint"},
            "payload": {"endpoint": "https://example.com"},
        }
    }
    (event,) = parse_events(other)
    assert (event.command, event.payload) == (
        "System.SetEndpoint",
        {"endpoint": "https://example.com"},
    )
    assert parse_events({})[0].command == "UNKNOWN"


class Server:
    """A scripted AVS endpoint behind httpx.MockTransport."""

    def __init__(self, *streams: bytes | int) -> None:
        self.streams = list(streams)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(204)
        if request.url.path == "/ping":
            return httpx.Response(204)
        stream = self.streams.pop(0) if len(self.streams) > 1 else self.streams[0]
        if isinstance(stream, int):
            return httpx.Response(stream)
        return httpx.Response(
            200, headers={"content-type": CONTENT_TYPE}, content=stream
        )

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(push_module, "_BACKOFF_BASE", 0.01)


def make_push(client, session, server, events, states):
    session.add(
        "GET", "/api/endpoints", {"bobDispatchUrl": "https://push.example.com/"}
    )
    push = PushClient(client, events.append, on_state=states.append)
    push._http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return push


async def wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def test_push_delivers_events_and_reconnects(client, session, fast):
    events, states = [], []
    server = Server(
        DELIMITER + b"\r\n" + part(VOLUME), DELIMITER + part(VOLUME) + part(VOLUME)
    )
    push = make_push(client, session, server, events, states)
    await push.start()
    await push.start()  # idempotent
    await wait_for(lambda: len(events) >= 3)
    await push.stop()
    await push.stop()
    assert not push.connected
    assert all(event.command == "PUSH_VOLUME_CHANGE" for event in events)
    assert states[:4] == [True, False, True, False]
    assert states[-1] is False
    assert server.paths()[0] == "/v1/devices/@self/capabilities"  # once, first
    assert server.paths().count("/v1/devices/@self/capabilities") == 1
    request = server.requests[1]
    assert str(request.url) == "https://push.example.com/v20160207/directives"
    assert request.headers["authorization"] == "Bearer Atna|access"
    assert request.headers["accept"] == "multipart/related"


async def test_push_registers_capabilities_payload(client, session, fast):
    server = Server(DELIMITER + part(VOLUME))
    events = []
    push = make_push(client, session, server, events, [])
    await push.start()
    await wait_for(lambda: events)
    await push.stop()
    put = next(r for r in server.requests if r.method == "PUT")
    assert str(put.url) == "https://api.amazonalexa.com/v1/devices/@self/capabilities"
    body = json.loads(put.content)
    interfaces = {item["interface"] for item in body["capabilities"]}
    assert "Alexa.Mobile.Push" in interfaces
    assert body["envelopeVersion"] == "20160207"
    assert put.headers["authorization"] == "Bearer Atna|access"


async def test_push_imported_session_skips_capabilities(client, session, fast):
    client.credentials.source = "alexa_media"
    server = Server(DELIMITER + part(VOLUME))
    events = []
    push = make_push(client, session, server, events, [])
    await push.start()
    await wait_for(lambda: events)
    await push.stop()
    assert all(request.method == "GET" for request in server.requests)


async def test_push_rejected_token_is_renewed(client, session, fast):
    session.add("POST", "/auth/token", {"access_token": "Atna|new", "expires_in": 3600})
    client.credentials.source = "alexa_media"
    events, states = [], []
    server = Server(403, 500, DELIMITER + part(VOLUME))
    push = make_push(client, session, server, events, states)
    await push.start()
    await wait_for(lambda: events)
    await push.stop()
    tokens = [request.headers["authorization"] for request in server.requests[:3]]
    assert tokens == ["Bearer Atna|access", "Bearer Atna|new", "Bearer Atna|new"]
    assert states[0] is True  # never reported up for the failed attempts


async def test_push_falls_back_to_the_region_host(client, session, fast):
    session.add("GET", "/api/endpoints", {})
    events = []
    server = Server(DELIMITER + part(VOLUME))
    push = PushClient(client, events.append)
    push._http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    await push.start()
    await wait_for(lambda: events)
    await push.stop()
    stream = next(r for r in server.requests if r.url.path.endswith("/directives"))
    assert stream.url.host == "bob-dispatch-prod-na.amazon.com"


async def test_push_survives_failing_callbacks(client, session, fast):
    seen = []

    def on_event(event):
        seen.append(event)
        raise RuntimeError("boom")

    def on_state(_up):
        raise RuntimeError("boom")

    session.add("GET", "/api/endpoints", {"bobDispatchUrl": "https://push.example.com"})
    push = PushClient(client, on_event, on_state=on_state)
    push._http = httpx.AsyncClient(
        transport=httpx.MockTransport(Server(DELIMITER + part(VOLUME) * 2))
    )
    await push.start()
    await wait_for(lambda: len(seen) >= 2)
    await push.stop()


async def test_push_ping_failure_ends_the_stream(client, session, fast, monkeypatch):
    monkeypatch.setattr(push_module, "_PING_INTERVAL", 0.01)
    gate = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield DELIMITER + part(VOLUME)
            await gate.wait()

    pings = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            pings.append(request)
            return httpx.Response(204 if len(pings) == 1 else 403)
        return httpx.Response(
            200, headers={"content-type": CONTENT_TYPE}, stream=Slow()
        )

    client.credentials.source = "alexa_media"
    session.add("GET", "/api/endpoints", {"bobDispatchUrl": "https://push.example.com"})
    session.add("POST", "/auth/token", {"access_token": "Atna|new", "expires_in": 3600})
    states = []
    push = PushClient(client, lambda _e: None, on_state=states.append)
    push._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await push.start()
    await wait_for(lambda: False in states)
    await push.stop()
    gate.set()
    assert len(pings) >= 2
    assert states[:2] == [True, False]


async def test_push_creates_its_http_client(client, session, fast, monkeypatch):
    created = []
    real = httpx.AsyncClient

    def factory(**kwargs):
        created.append(kwargs)
        return real(transport=httpx.MockTransport(Server(DELIMITER + part(VOLUME))))

    monkeypatch.setattr(push_module.httpx, "AsyncClient", factory)
    session.add("GET", "/api/endpoints", {"bobDispatchUrl": "https://push.example.com"})
    events = []
    push = PushClient(client, events.append)
    await push.start()
    await wait_for(lambda: events)
    await push.stop()
    assert created[0]["http2"] is True
    assert push._http is None


async def test_push_survives_unexpected_errors(client, session, fast):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("h2 state machine blew up")
        return httpx.Response(
            200,
            headers={"content-type": CONTENT_TYPE},
            content=DELIMITER + part(VOLUME),
        )

    client.credentials.source = "alexa_media"
    session.add("GET", "/api/endpoints", {"bobDispatchUrl": "https://push.example.com"})
    events = []
    push = PushClient(client, events.append)
    push._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await push.start()
    await wait_for(lambda: events)
    await push.stop()
    assert len(calls) >= 2
