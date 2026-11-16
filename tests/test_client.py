"""Tests for the HTTP core and every API method (mocked HTTP only)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import aiohttp
import pytest

from amazon_echo import (
    AmazonEchoAuthenticationError,
    AmazonEchoClient,
    AmazonEchoConnectionError,
    AmazonEchoConnectionTimeoutError,
    AmazonEchoNotSupportedError,
    AmazonEchoRateLimitError,
    AmazonEchoResponseError,
    Device,
    ListItem,
    Routine,
)
from amazon_echo.client import _recurrence_rule, format_mac, parse_notification

from .conftest import FakeResponse, load_json

COOKIE_ANSWER = {
    "response": {
        "tokens": {
            "cookies": {
                ".amazon.com.br": [
                    {"Name": "session-id", "Value": "999-888"},
                    {"Name": "at-acbbr", "Value": '"Atza|fresh"'},
                ]
            }
        }
    }
}
TOKEN_ANSWER = {"access_token": "Atna|new", "expires_in": 3600, "token_type": "bearer"}


def sequence_of(session) -> dict:
    """Return the decoded sequence of the last behaviors/preview call."""
    call = session.sent("/api/behaviors/preview")[-1]
    assert call["json"]["behaviorId"] == "PREVIEW"
    assert call["json"]["status"] == "ENABLED"
    return json.loads(call["json"]["sequenceJson"])


def node_of(session) -> dict:
    nodes = sequence_of(session)["startNode"]["nodesToExecute"]
    assert len(nodes) == 1
    return nodes[0]


# ---------------------------------------------------------------- HTTP core


async def test_request_sends_cookies_and_csrf(client, session):
    session.add("GET", "/api/ping", {"ok": True})
    assert await client.request("GET", "/api/ping", params={"a": "b"}) == {"ok": True}
    call = session.calls[0]
    assert call["url"] == "https://alexa.amazon.com.br/api/ping"
    assert call["params"] == {"a": "b"}
    assert call["headers"]["Cookie"] == "session-id=111-222; csrf=424242"
    assert call["headers"]["csrf"] == "424242"
    assert "PitanguiBridge" in call["headers"]["User-Agent"]
    assert call["headers"]["Accept-Language"] == "pt-BR"
    assert "Authorization" not in call["headers"]


async def test_request_empty_body_is_none(client, session):
    session.add("PUT", "/api/x", FakeResponse(200, text="  "))
    assert await client.request("PUT", "/api/x", json={"a": 1}) is None
    assert session.calls[0]["json"] == {"a": 1}


async def test_request_html_on_json_route(client, session):
    session.add(
        "GET", "/api/namedLists", FakeResponse(200, text="<!DOCTYPE html><html>")
    )
    with pytest.raises(AmazonEchoResponseError) as err:
        await client.request("GET", "/api/namedLists")
    assert err.value.status == 200
    assert err.value.body.startswith("<!DOCTYPE")


async def test_request_http_error(client, session):
    session.add("GET", "/api/x", FakeResponse(500, text="boom"))
    with pytest.raises(AmazonEchoResponseError) as err:
        await client.request("GET", "/api/x")
    assert (err.value.status, err.value.body) == (500, "boom")
    assert "500" in str(err.value)


async def test_request_rate_limit_then_ok(client, session):
    session.add("GET", "/api/x", FakeResponse(429), FakeResponse(503), {"ok": 1})
    assert await client.request("GET", "/api/x") == {"ok": 1}
    assert len(session.calls) == 3


async def test_request_rate_limit_gives_up(client, session):
    session.add("GET", "/api/x", FakeResponse(429, text="Too many"))
    with pytest.raises(AmazonEchoRateLimitError):
        await client.request("GET", "/api/x")
    assert len(session.calls) == 3


async def test_request_rate_exceeded_body(client, session):
    session.add("GET", "/api/x", FakeResponse(400, {"message": "Rate exceeded"}))
    with pytest.raises(AmazonEchoRateLimitError):
        await client.request("GET", "/api/x")


async def test_request_timeout(client, session):
    session.add("GET", "/api/x", TimeoutError())
    with pytest.raises(AmazonEchoConnectionTimeoutError):
        await client.request("GET", "/api/x")


async def test_request_connection_error(client, session):
    session.add("GET", "/api/x", aiohttp.ClientConnectionError("down"))
    with pytest.raises(AmazonEchoConnectionError):
        await client.request("GET", "/api/x")


async def test_request_refreshes_cookies_once(client, session, updates):
    session.add("GET", "/api/x", FakeResponse(401), {"ok": 1})
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    assert await client.request("GET", "/api/x") == {"ok": 1}
    form = session.sent("/auth/token")[0]["data"]
    assert form["requested_token_type"] == "auth_cookies"
    assert form["source_token"] == "Atnr|refresh"
    assert form["domain"] == "www.amazon.com.br"
    assert client.credentials.cookies == {
        "session-id": "999-888",
        "csrf": "424242",
        "at-acbbr": "Atza|fresh",
    }
    assert updates == [client.credentials]
    assert "session-id=999-888" in session.calls[-1]["headers"]["Cookie"]


async def test_request_still_rejected_after_refresh(client, session):
    session.add("GET", "/api/x", FakeResponse(403))
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    with pytest.raises(AmazonEchoAuthenticationError):
        await client.request("GET", "/api/x")
    assert len(session.sent("/auth/token")) == 1


async def test_request_refresh_token_rejected(client, session):
    session.add("GET", "/api/x", FakeResponse(401))
    session.add(
        "POST",
        "/auth/token",
        FakeResponse(400, {"error": "invalid_grant", "error_description": "revoked"}),
    )
    with pytest.raises(AmazonEchoAuthenticationError, match="invalid_grant"):
        await client.request("GET", "/api/x")


async def test_request_no_second_exchange_within_cooldown(client, session):
    session.add("GET", "/api/a", FakeResponse(401), {"ok": 1})
    session.add("GET", "/api/b", FakeResponse(403))
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    await client.request("GET", "/api/a")
    with pytest.raises(AmazonEchoAuthenticationError, match="right after"):
        await client.request("GET", "/api/b")
    assert len(session.sent("/auth/token")) == 1


async def test_request_signin_redirect_is_auth_failure(client, session):
    session.add(
        "GET",
        "/api/x",
        FakeResponse(200, text="<html>", url="https://www.amazon.com.br/ap/signin?x=1"),
        {"ok": 1},
    )
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    assert await client.request("GET", "/api/x") == {"ok": 1}


async def test_concurrent_rejections_share_one_exchange(client, session):
    session.add("GET", "/api/a", FakeResponse(401), {"ok": "a"})
    session.add("GET", "/api/b", FakeResponse(401), {"ok": "b"})
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    results = await asyncio.gather(
        client.request("GET", "/api/a"), client.request("GET", "/api/b")
    )
    assert results == [{"ok": "a"}, {"ok": "b"}]
    assert len(session.sent("/auth/token")) == 1


async def test_response_cookies_are_kept(client, session, updates):
    session.add("GET", "/api/x", FakeResponse(200, {}, cookies={"csrf": "777"}))
    session.add("POST", "/api/y", None)
    await client.request("GET", "/api/x")
    assert client.credentials.cookies["csrf"] == "777"
    assert len(updates) == 1
    await client.request("POST", "/api/y", json={})
    assert session.calls[-1]["headers"]["csrf"] == "777"
    assert len(updates) == 1


async def test_cookies_stay_on_account_domain(client, session):
    session.add(
        "GET",
        "https://api.amazonalexa.com/v1/x",
        FakeResponse(200, {}, cookies={"evil": "1"}),
    )
    await client.request("GET", "https://api.amazonalexa.com/v1/x")
    headers = session.calls[0]["headers"]
    assert "Cookie" not in headers
    assert "csrf" not in headers
    assert "evil" not in client.credentials.cookies


async def test_host_override(client, session):
    session.add("GET", "https://www.amazon.com.br/x", {"a": 1})
    assert await client.request("GET", "/x", host="www.amazon.com.br") == {"a": 1}


async def test_bearer_uses_valid_token(client, session):
    session.add("GET", "/api/x", {})
    await client.request("GET", "/api/x", bearer=True)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer Atna|access"


async def test_bearer_refreshes_expired_token(client, session, updates):
    client.credentials.access_token_expires = 10.0
    session.add("GET", "/api/x", {})
    session.add("POST", "/auth/token", TOKEN_ANSWER)
    await client.request("GET", "/api/x", bearer=True)
    assert (
        session.sent("/auth/token")[0]["data"]["requested_token_type"] == "access_token"
    )
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer Atna|new"
    assert client.credentials.access_token_expires > 1_700_000_000
    assert len(updates) == 1
    assert await client.async_get_access_token() == "Atna|new"
    assert len(session.sent("/auth/token")) == 1


async def test_bearer_rejection_renews_token_too(client, session):
    session.add("POST", "/v1/alexa/device/reboot", FakeResponse(401), None)
    session.add("POST", "/auth/token", COOKIE_ANSWER, TOKEN_ANSWER)
    await client.request("POST", "/v1/alexa/device/reboot/x/y", bearer=True)
    kinds = [c["data"]["requested_token_type"] for c in session.sent("/auth/token")]
    assert kinds == ["auth_cookies", "access_token"]


async def test_token_without_access_token_is_auth_error(client, session):
    client.credentials.access_token = None
    session.add("POST", "/auth/token", {"unexpected": True})
    with pytest.raises(AmazonEchoAuthenticationError):
        await client.async_get_access_token()


async def test_own_session_is_created_and_closed(credentials):
    async with AmazonEchoClient(credentials) as client:
        assert client.host == "alexa.amazon.com.br"
        assert client.home_region == "NA"
        created = client.session
        assert isinstance(created, aiohttp.ClientSession)
    assert created.closed


async def test_injected_session_is_not_closed(client, session):
    await client.close()
    assert not session.closed


def test_home_region_falls_back_to_domain(client):
    client.credentials.home_region = None
    assert client.home_region == "NA"
    client.credentials.domain = "amazon.de"
    assert client.home_region == "EU"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AABBCCDDEE01", "aa:bb:cc:dd:ee:01"),
        ("AA:BB:CC:DD:EE:01", "aa:bb:cc:dd:ee:01"),
        ("aa-bb-cc-dd-ee-01", "aa:bb:cc:dd:ee:01"),
        ("", None),
        (None, None),
        ("1234", None),
    ],
)
def test_format_mac(raw, expected):
    assert format_mac(raw) == expected


# ---------------------------------------------------- account / inventory


async def test_get_account(client, session, updates):
    client.credentials.customer_id = None
    session.add(
        "GET",
        "/api/users/me",
        {
            "id": "A0TESTCUSTOMER1",
            "fullName": "Test User",
            "email": "user@example.com",
            "countryOfResidence": "BR",
            "marketPlaceId": "A2Q3Y263D00KWC",
            "marketPlaceLocale": "pt-BR",
        },
    )
    account = await client.get_account()
    assert account.customer_id == "A0TESTCUSTOMER1"
    assert account.name == "Test User"
    assert account.email == "user@example.com"
    assert account.country == "BR"
    assert account.marketplace_id == "A2Q3Y263D00KWC"
    assert client.credentials.email == "user@example.com"
    assert client.credentials.customer_name == "Test User"
    assert len(updates) == 1
    await client.get_account()
    assert len(updates) == 1


async def test_get_devices_merges_graphql(client, session):
    session.add("GET", "/api/devices-v2/device", load_json("devices.json"))
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_endpoints.json"))
    devices = {d.name: d for d in await client.get_devices()}
    assert session.sent("/api/devices-v2/device")[0]["params"] == {"cached": "false"}
    assert "listEndpoints" in session.sent("/nexus/v1/graphql")[0]["json"]["query"]
    assert len(devices) == 6

    office = devices["Office Echo"]
    assert office.serial == office.key == "G000TEST00000001"
    assert office.device_type == "A18O6U1UQFJ0XK"
    assert office.family == "ECHO"
    assert office.model == "Echo Plus"
    assert office.mac == "aa:bb:cc:dd:ee:01"
    assert office.endpoint_id.endswith("000000000001")
    assert office.account_id == "A0TESTACCOUNTID0001"
    assert office.software_version == "1234567890"
    assert office.online
    assert office.supports("EARCONS")
    assert not office.supports("NOPE")
    assert office.parent_clusters == ("00000000000000000000000000000abc",)
    assert not office.is_group
    assert not office.is_app

    kitchen = devices["Kitchen Dot"]
    assert kitchen.mac == "aa:bb:cc:dd:ee:02"  # legacy list only
    assert kitchen.model == "Echo Dot"
    assert kitchen.endpoint_id is None
    assert not kitchen.online

    group = devices["Everywhere"]
    assert group.is_group
    assert group.cluster_members == ("G000TEST00000001", "G000TEST00000002")
    assert devices["This Device"].is_app
    buds = devices["Test Buds"]
    assert (buds.battery_level, buds.charging) == (80, False)


async def test_get_devices_survives_graphql_failure(client, session):
    session.add("GET", "/api/devices-v2/device", load_json("devices.json"))
    session.add("POST", "/nexus/v1/graphql", {"errors": [{"message": "nope"}]})
    devices = await client.get_devices()
    assert len(devices) == 6
    assert all(d.endpoint_id is None for d in devices)


async def test_get_devices_propagates_auth_failure(client, session):
    session.add("GET", "/api/devices-v2/device", load_json("devices.json"))
    session.add("POST", "/nexus/v1/graphql", FakeResponse(401))
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    with pytest.raises(AmazonEchoAuthenticationError):
        await client.get_devices()


async def test_graphql_rate_limit(client, session):
    session.add(
        "POST",
        "/nexus/v1/graphql",
        {"errors": [{"extensions": {"code": "TOO_MANY_REQUESTS"}}], "data": None},
    )
    with pytest.raises(AmazonEchoRateLimitError):
        await client.get_endpoint_states()


async def test_get_endpoint_states(client, session):
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_endpoints.json"))
    states = await client.get_endpoint_states()
    assert set(states) == {"G000TEST00000001", "G000TEST00000003"}
    assert states["G000TEST00000001"] == {
        "endpoint_id": "amzn1.alexa.endpoint.00000000-0000-0000-0000-000000000001",
        "model": "Echo Plus",
        "mac": "aa:bb:cc:dd:ee:01",
        "temperature": 27.8,
        "volume": 47,
        "reachable": True,
        "remote_battery": None,
        "ambient_mode": None,
        "dnd": False,
    }
    tv = states["G000TEST00000003"]
    assert tv["reachable"] is False
    assert tv["ambient_mode"] is True
    assert tv["remote_battery"] == 64
    assert tv["temperature"] == 25.0  # converted from Fahrenheit
    assert tv["dnd"] is True
    assert tv["mac"] == "aa:bb:cc:dd:ee:03"


async def test_get_routines(client, session):
    session.add("GET", "/api/behaviors/v2/automations", load_json("automations.json"))
    routines = await client.get_routines()
    assert [(r.name, r.enabled) for r in routines] == [
        ("Good night", True),
        ("movie time", False),
        ("amzn1.alexa.automation.00000000-0000-0000-0000-000000000003", True),
    ]
    assert routines[0].raw["sequence"]["startNode"]


async def test_get_routines_empty(client, session):
    session.add("GET", "/api/behaviors/v2/automations", [])
    assert await client.get_routines() == []


async def test_get_music_providers(client, session):
    session.add(
        "GET",
        "/api/behaviors/entities",
        [
            {
                "id": "AMAZON_MUSIC",
                "displayName": "Amazon Music",
                "availability": "AVAILABLE",
            },
            {"id": "PANDORA", "displayName": "Pandora", "availability": "UNAVAILABLE"},
            {"id": "", "displayName": "x", "availability": "AVAILABLE"},
        ],
    )
    assert await client.get_music_providers() == {"AMAZON_MUSIC": "Amazon Music"}
    assert session.calls[0]["params"] == {"skillId": "amzn1.ask.1p.music"}


async def test_get_service_urls_is_cached(client, session):
    session.add(
        "GET",
        "/api/endpoints",
        {
            "alexaApiUrl": "https://api.amazonalexa.com/",
            "awsRegion": "us-east-1",
            "x": 1,
        },
    )
    urls = await client.get_service_urls()
    assert urls == {
        "alexaApiUrl": "https://api.amazonalexa.com",
        "awsRegion": "us-east-1",
    }
    await client.get_service_urls()
    assert len(session.calls) == 1


# ------------------------------------------------------------- bulk state


async def test_get_dnd(client, session):
    session.add(
        "GET",
        "/api/dnd/device-status-list",
        {
            "doNotDisturbDeviceStatusList": [
                {
                    "deviceSerialNumber": "G000TEST00000001",
                    "deviceType": "X",
                    "enabled": True,
                },
                {
                    "deviceSerialNumber": "G000TEST00000002",
                    "deviceType": "X",
                    "enabled": False,
                },
            ]
        },
    )
    assert await client.get_dnd() == {
        "G000TEST00000001": True,
        "G000TEST00000002": False,
    }


async def test_get_volumes(client, session):
    session.add(
        "GET",
        "/audio/v1/allDeviceVolumes",
        {
            "volumes": [
                {
                    "dsn": "G000TEST00000001",
                    "deviceType": "X",
                    "speakerVolume": 47,
                    "speakerMuted": False,
                },
                {
                    "dsn": "G000TEST00000002",
                    "deviceType": "X",
                    "speakerVolume": 0,
                    "speakerMuted": True,
                },
                {"dsn": "G000TEST00000003", "deviceType": "X", "speakerVolume": None},
            ]
        },
    )
    assert await client.get_volumes() == {
        "G000TEST00000001": (47, False),
        "G000TEST00000002": (0, True),
    }


async def test_get_bluetooth(client, session):
    session.add(
        "GET",
        "/api/bluetooth",
        {
            "bluetoothStates": [
                {
                    "deviceSerialNumber": "G000TEST00000001",
                    "pairedDeviceList": [
                        {
                            "address": "00:11:22:33:44:55",
                            "friendlyName": "Test Phone",
                            "connected": True,
                            "profiles": ["A2DP-SOURCE", "AVRCP"],
                        }
                    ],
                },
                {"deviceSerialNumber": "G000TEST00000002", "pairedDeviceList": None},
            ]
        },
    )
    states = await client.get_bluetooth()
    assert states["G000TEST00000002"] == []
    paired = states["G000TEST00000001"][0]
    assert paired.address == "00:11:22:33:44:55"
    assert paired.name == "Test Phone"
    assert paired.connected
    assert paired.profiles == ("A2DP-SOURCE", "AVRCP")


async def test_get_notifications(client, session):
    session.add("GET", "/api/notifications", load_json("notifications.json"))
    alarm, timer, reminder, music, task = await client.get_notifications()

    assert alarm.kind == "alarm"
    assert alarm.is_active
    assert alarm.when == datetime.fromtimestamp(1_800_000_000, UTC)
    assert alarm.recurrence == "XXXX-WD"
    assert alarm.sound == "system_alerts_melodic_01"
    assert alarm.device_serial == "G000TEST00000001"
    assert alarm.device_type == "A18O6U1UQFJ0XK"
    assert alarm.remaining is None
    assert alarm.raw["originalTime"] == "07:00:00.000"

    assert timer.kind == "timer"
    assert timer.status == "PAUSED"
    assert not timer.is_active
    assert timer.label == "pasta"
    assert timer.remaining == timedelta(minutes=5)
    assert timer.when == datetime.fromtimestamp(1_800_000_300, UTC)

    assert reminder.kind == "reminder"
    assert reminder.label == "Take out the trash"
    assert reminder.when is None
    assert reminder.recurrence == "P1D"

    assert music.kind == "music_alarm"
    assert music.recurrence is None
    assert task.kind == "task"


def test_parse_notification_defaults():
    parsed = parse_notification({"id": "x", "alarmTime": "garbage"})
    assert (parsed.kind, parsed.status, parsed.when) == ("unknown", "OFF", None)


async def test_get_alarm_volumes(client, session):
    session.add(
        "GET",
        "/api/device-notification-state",
        {
            "deviceNotificationStates": [
                {"deviceSerialNumber": "G000TEST00000001", "volumeLevel": 80},
                {"deviceSerialNumber": "G000TEST00000002", "volumeLevel": None},
            ]
        },
    )
    assert await client.get_alarm_volumes() == {"G000TEST00000001": 80}


async def test_get_wake_words(client, session):
    session.add(
        "GET",
        "/api/wake-word",
        {
            "wakeWords": [
                {"deviceSerialNumber": "G000TEST00000001", "wakeWord": "ALEXA"},
                {"deviceSerialNumber": "G000TEST00000002", "wakeWord": None},
            ]
        },
    )
    assert await client.get_wake_words() == {"G000TEST00000001": "ALEXA"}


async def test_get_ascending_alarms(client, session):
    session.add(
        "GET",
        "/api/ascending-alarm",
        {
            "ascendingAlarmModelList": [
                {
                    "deviceSerialNumber": "G000TEST00000001",
                    "ascendingAlarmEnabled": True,
                }
            ]
        },
    )
    assert await client.get_ascending_alarms() == {"G000TEST00000001": True}


async def test_get_media_playing(client, session, office):
    session.add("GET", "/api/np/player", load_json("np_player.json"))
    media = await client.get_media(office)
    assert session.calls[0]["params"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "screenWidth": "2560",
    }
    assert media.state == "playing"
    assert (media.title, media.artist, media.album) == (
        "Test Song",
        "Test Artist",
        "Test Album",
    )
    assert media.image_url == "https://example.com/art.jpg"
    assert (media.duration, media.position) == (215.0, 42.0)
    assert media.position_updated_at is not None
    assert media.provider == "Amazon Music"
    assert media.shuffle is True
    assert media.repeat is None
    assert media.can_next
    assert not media.can_previous
    assert media.can_seek
    assert (media.muted, media.volume) == (False, 30)


async def test_get_media_idle(client, session, office):
    session.add(
        "GET", "/api/np/player", {"playerInfo": {"state": None, "volume": None}}
    )
    media = await client.get_media(office)
    assert media.state == "idle"
    assert media.title is None
    assert media.volume is None
    assert media.position_updated_at is None


# ---------------------------------------------------- per-device settings


async def test_get_wifi(client, session, office):
    session.add(
        "GET",
        "/api/device-wifi-details",
        {"essid": "TestNet", "macAddress": "AABBCCDDEE01"},
    )
    assert await client.get_wifi(office) == ("TestNet", "aa:bb:cc:dd:ee:01")


async def test_equalizer(client, session, office):
    path = "/api/equalizer/G000TEST00000001/A18O6U1UQFJ0XK"
    session.add("GET", path, {"bass": 1, "mid": 0, "treble": -2})
    session.add("POST", path, None)
    assert await client.get_equalizer(office) == {"bass": 1, "mid": 0, "treble": -2}
    await client.set_equalizer(office, bass=6, mid=-6, treble=0)
    assert session.calls[-1]["json"] == {"bass": 6, "mid": -6, "treble": 0}
    with pytest.raises(ValueError, match="between"):
        await client.set_equalizer(office, bass=7, mid=0, treble=0)


async def test_earcons(client, session, office):
    path = "/api/earcon/states/A18O6U1UQFJ0XK/1234567890/G000TEST00000001"
    session.add(
        "GET", path, {"wakeWordEarconEnabled": True, "endOfSpeechEarconEnabled": False}
    )
    session.add("PUT", path, None)
    assert await client.get_earcons(office) == {
        "wake_word": True,
        "end_of_speech": False,
    }
    await client.set_earcons(office, wake_word=False, end_of_speech=True)
    assert session.calls[-1]["json"] == {
        "wakeWordEarconEnabled": False,
        "endOfSpeechEarconEnabled": True,
    }


async def test_aux(client, session, office):
    base = "/api/auxcontroller/A18O6U1UQFJ0XK/G000TEST00000001"
    session.add(
        "GET",
        f"{base}/state",
        {
            "ports": [
                {
                    "direction": "OUTPUT",
                    "id": "aux0",
                    "inputActivity": "INACTIVE",
                    "isEnabled": True,
                    "isPlugged": False,
                }
            ]
        },
    )
    session.add("POST", f"{base}/ports/aux0/setDirection", None)
    port = (await client.get_aux(office))[0]
    assert (port.id, port.direction, port.plugged, port.enabled, port.active) == (
        "aux0",
        "OUTPUT",
        False,
        True,
        False,
    )
    await client.set_aux_direction(office, "aux0", "INPUT")
    assert session.calls[-1]["json"] == {"direction": "INPUT"}
    with pytest.raises(ValueError, match="INPUT or OUTPUT"):
        await client.set_aux_direction(office, "aux0", "sideways")


async def test_settings(client, session, office):
    base = "/api/v1/devices/A0TESTACCOUNTID0001/settings"
    session.add(
        "GET", f"{base}/alexaSpeakingRate", {"key": "alexaSpeakingRate", "value": "1.0"}
    )
    session.add("GET", f"{base}/timeZone", {"value": '"America/Sao_Paulo"'})
    session.add("GET", f"{base}/plain", {"value": "not json"})
    session.add("GET", f"{base}/missing", {"value": None})
    session.add(
        "GET",
        f"{base}/bogusKey",
        FakeResponse(400, {"message": "Setting key, bogusKey does not exist."}),
    )
    session.add("GET", f"{base}/broken", FakeResponse(500, text="x"))
    session.add("PUT", f"{base}/alexaSpeakingRate", None)
    session.add(
        "PUT",
        f"{base}/bogusKey",
        FakeResponse(400, {"message": "Setting key, bogusKey does not exist."}),
    )
    session.add("PUT", f"{base}/broken", FakeResponse(500, text="x"))

    assert await client.get_setting(office, "alexaSpeakingRate") == 1.0
    assert await client.get_setting(office, "timeZone") == "America/Sao_Paulo"
    assert await client.get_setting(office, "plain") == "not json"
    assert await client.get_setting(office, "missing") is None
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.get_setting(office, "bogusKey")
    with pytest.raises(AmazonEchoResponseError):
        await client.get_setting(office, "broken")

    await client.set_setting(office, "alexaSpeakingRate", 1.0)
    assert session.calls[-1]["json"] == {"value": "1.0"}
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.set_setting(office, "bogusKey", "x")
    with pytest.raises(AmazonEchoResponseError):
        await client.set_setting(office, "broken", "x")


async def test_settings_need_an_account_id(client):
    device = Device(serial="S", device_type="T", name="No account", family="WHA")
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.get_setting(device, "timeZone")


async def test_get_available_wake_words(client, session, office):
    session.add("GET", "/api/wake-words", {"wakeWords": ["ALEXA", "ECHO"]})
    assert await client.get_available_wake_words(office) == ["ALEXA", "ECHO"]
    assert session.calls[0]["params"] == {
        "deviceType": "A18O6U1UQFJ0XK",
        "softwareVersion": "1234567890",
    }


async def test_alarm_sounds(client, session, office):
    session.add(
        "GET",
        "/api/notification/sounds",
        {
            "notificationSounds": [
                {"id": "system_alerts_melodic_01", "displayName": "Simple Alarm"},
                {"id": "system_alerts_rhythmic_02", "displayName": None},
            ]
        },
    )
    session.add(
        "GET",
        "/api/notification/default-sound",
        {"defaultNotificationSound": {"id": "system_alerts_rhythmic_02"}},
    )
    session.add("PUT", "/api/notification/default-sound", None)
    assert await client.get_alarm_sounds(office) == {
        "system_alerts_melodic_01": "Simple Alarm",
        "system_alerts_rhythmic_02": "system_alerts_rhythmic_02",
    }
    assert await client.get_default_alarm_sound(office) == "system_alerts_rhythmic_02"
    assert session.calls[-1]["params"]["notificationType"] == "ALARM"
    await client.set_default_alarm_sound(office, "system_alerts_melodic_01")
    assert session.calls[-1]["json"] == {
        "deviceType": "A18O6U1UQFJ0XK",
        "deviceSerialNumber": "G000TEST00000001",
        "notificationType": "ALARM",
        "soundId": "system_alerts_melodic_01",
    }


# ---------------------------------------------------- account preferences


async def test_whisper_and_adaptive_volume(client, session):
    session.add(
        "GET",
        "/api/response-modulation-preferences",
        {"whisperModulationEnabled": True},
    )
    session.add("POST", "/api/response-modulation-preferences", None)
    session.add("GET", "/api/adaptive-volume", {"adaptiveVolumeEnabled": False})
    session.add("POST", "/api/adaptive-volume", None)
    assert await client.get_whisper_mode() is True
    assert await client.get_adaptive_volume() is False
    await client.set_whisper_mode(False)
    assert session.calls[-1]["method"] == "POST"
    assert session.calls[-1]["json"] == {"whisperModulationEnabled": False}
    await client.set_adaptive_volume(True)
    assert session.calls[-1]["json"] == {"adaptiveVolumeEnabled": True}


# ------------------------------------------------------------------ writes


async def test_set_dnd(client, session, office):
    session.add("PUT", "/api/dnd/status", None)
    await client.set_dnd(office, True)
    assert session.calls[0]["json"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "enabled": True,
    }


async def test_set_alarm_volume(client, session, office):
    path = "/api/device-notification-state/A18O6U1UQFJ0XK/1234567890/G000TEST00000001"
    session.add("PUT", path, None)
    await client.set_alarm_volume(office, 55)
    assert session.calls[0]["json"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "softwareVersion": "1234567890",
        "volumeLevel": 55,
    }
    with pytest.raises(ValueError, match="between"):
        await client.set_alarm_volume(office, 101)


async def test_set_wake_word(client, session, office):
    model = {
        "active": True,
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "midFieldState": None,
        "wakeWord": "ALEXA",
    }
    session.add("GET", "/api/wake-word", {"wakeWords": [model]})
    session.add("PUT", "/api/wake-word/G000TEST00000001", None)
    await client.set_wake_word(office, " echo ")
    assert session.calls[-1]["json"] == {**model, "wakeWord": "ECHO"}


async def test_set_wake_word_unknown_device(client, session, office):
    session.add("GET", "/api/wake-word", {"wakeWords": []})
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.set_wake_word(office, "ECHO")


async def test_set_ascending_alarm(client, session, office):
    session.add("PUT", "/api/ascending-alarm/G000TEST00000001", None)
    await client.set_ascending_alarm(office, False)
    assert session.calls[0]["json"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "ascendingAlarmEnabled": False,
    }


async def test_bluetooth_connect_and_disconnect(client, session, office):
    session.add(
        "POST", "/api/bluetooth/pair-sink/A18O6U1UQFJ0XK/G000TEST00000001", None
    )
    session.add(
        "POST", "/api/bluetooth/disconnect-sink/A18O6U1UQFJ0XK/G000TEST00000001", None
    )
    await client.bluetooth_connect(office, "00:11:22:33:44:55")
    assert session.calls[-1]["json"] == {"bluetoothDeviceAddress": "00:11:22:33:44:55"}
    await client.bluetooth_disconnect(office)
    assert session.calls[-1]["json"] is None


async def test_reboot(client, session, office):
    session.add("POST", "/v1/alexa/device/reboot/A18O6U1UQFJ0XK/G000TEST00000001", None)
    await client.reboot(office)
    call = session.calls[0]
    assert call["json"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
    }
    assert call["headers"]["Authorization"] == "Bearer Atna|access"
    assert call["headers"]["User-Agent"].startswith("AmazonWebView/AmazonAlexa")


# ----------------------------------------------------------- media / voice


@pytest.mark.parametrize(
    ("command", "kwargs", "body"),
    [
        ("play", {}, {"type": "PlayCommand"}),
        ("pause", {}, {"type": "PauseCommand"}),
        ("next", {}, {"type": "NextCommand"}),
        ("previous", {}, {"type": "PreviousCommand"}),
        ("forward", {}, {"type": "ForwardCommand"}),
        ("rewind", {}, {"type": "RewindCommand"}),
        ("shuffle", {"enabled": True}, {"type": "ShuffleCommand", "shuffle": True}),
        ("repeat", {"enabled": False}, {"type": "RepeatCommand", "repeat": False}),
    ],
)
async def test_media_command(client, session, office, command, kwargs, body):
    session.add("POST", "/api/np/command", None)
    await client.media_command(office, command, **kwargs)
    assert session.calls[0]["json"] == body
    assert session.calls[0]["params"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
    }


async def test_media_command_unknown(client, office):
    with pytest.raises(ValueError, match="Unknown media command"):
        await client.media_command(office, "explode")


BASE_PAYLOAD = {
    "deviceSerialNumber": "G000TEST00000001",
    "deviceType": "A18O6U1UQFJ0XK",
    "locale": "pt-BR",
    "customerId": "A0TESTCUSTOMER1",
}


async def test_set_volume(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.set_volume(office, 47)
    sequence = sequence_of(session)
    assert sequence["@type"] == "com.amazon.alexa.behaviors.model.Sequence"
    assert (
        sequence["startNode"]["@type"] == "com.amazon.alexa.behaviors.model.SerialNode"
    )
    assert node_of(session) == {
        "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
        "type": "Alexa.DeviceControls.Volume",
        "operationPayload": {**BASE_PAYLOAD, "value": 47},
    }
    with pytest.raises(ValueError, match="between"):
        await client.set_volume(office, -1)


async def test_sequence_fetches_the_account_once(client, session, office):
    client.credentials.customer_id = None
    client._locale = None
    session.add("POST", "/api/behaviors/preview", None)
    session.add(
        "GET", "/api/users/me", {"id": "A0TESTCUSTOMER9", "marketPlaceLocale": "en-US"}
    )
    await client.set_volume(office, 10)
    await client.set_volume(office, 11)
    assert len(session.sent("/api/users/me")) == 1
    payload = node_of(session)["operationPayload"]
    assert (payload["customerId"], payload["locale"]) == ("A0TESTCUSTOMER9", "en-US")


async def test_stop(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.stop(office)
    node = node_of(session)
    assert node["type"] == "Alexa.DeviceControls.Stop"
    assert node["operationPayload"] == {
        **BASE_PAYLOAD,
        "devices": [
            {"deviceSerialNumber": "G000TEST00000001", "deviceType": "A18O6U1UQFJ0XK"}
        ],
        "skillId": "amzn1.ask.1p.alexadevicecontrols",
    }


async def test_speak(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.speak(office, "olá")
    node = node_of(session)
    assert node["type"] == "Alexa.Speak"
    assert node["operationPayload"] == {
        **BASE_PAYLOAD,
        "textToSpeak": "olá",
        "target": {
            "customerId": "A0TESTCUSTOMER1",
            "devices": [
                {
                    "deviceSerialNumber": "G000TEST00000001",
                    "deviceTypeId": "A18O6U1UQFJ0XK",
                }
            ],
        },
        "skillId": "amzn1.ask.1p.saysomething",
    }


async def test_announce_expands_groups(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    session.add("GET", "/api/devices-v2/device", load_json("devices.json"))
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_endpoints.json"))
    group = Device(
        serial="00000000000000000000000000000abc",
        device_type="A3C9PE6TNYLTCH",
        name="Everywhere",
        family="WHA",
        cluster_members=("G000TEST00000001", "G000TEST00000002", "GONE"),
    )
    await client.announce([group, office], "Dinner", title="Kitchen")
    node = node_of(session)
    assert node["type"] == "AlexaAnnouncement"
    payload = node["operationPayload"]
    assert payload["skillId"] == "amzn1.ask.1p.routines.messaging"
    assert payload["expireAfter"] == "PT5S"
    assert payload["content"] == [
        {
            "locale": "pt-BR",
            "display": {"title": "Kitchen", "body": "Dinner"},
            "speak": {"type": "text", "value": "Dinner"},
        }
    ]
    assert payload["target"]["devices"] == [
        {"deviceSerialNumber": "G000TEST00000001", "deviceTypeId": "A18O6U1UQFJ0XK"},
        {"deviceSerialNumber": "G000TEST00000002", "deviceTypeId": "A1RABVCI4QCIKC"},
    ]
    assert payload["deviceSerialNumber"] == "G000TEST00000001"


async def test_announce_default_title_and_no_target(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.announce([office], "Hi")
    display = node_of(session)["operationPayload"]["content"][0]["display"]
    assert display == {"title": "Home Assistant", "body": "Hi"}
    with pytest.raises(ValueError, match="No device"):
        await client.announce([], "Hi")


async def test_text_command(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.text_command(office, "what time is it")
    node = node_of(session)
    assert node["type"] == "Alexa.TextCommand"
    assert node["operationPayload"] == {
        **BASE_PAYLOAD,
        "skillId": "amzn1.ask.1p.tellalexa",
        "text": "what time is it",
    }


async def test_play_sound(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.play_sound(office, "amzn_sfx_doorbell_chime_01")
    node = node_of(session)
    assert node["type"] == "Alexa.Sound"
    assert node["operationPayload"] == {
        **BASE_PAYLOAD,
        "soundStringId": "amzn_sfx_doorbell_chime_01",
        "skillId": "amzn1.ask.1p.sound",
    }


async def test_play_music(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.play_music(office, "SPOTIFY", "jazz")
    node = node_of(session)
    assert node["type"] == "Alexa.Music.PlaySearchPhrase"
    assert node["operationPayload"] == {
        **BASE_PAYLOAD,
        "searchPhrase": "jazz",
        "sanitizedSearchPhrase": "jazz",
        "musicProviderId": "SPOTIFY",
    }


async def test_launch_skill(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.launch_skill(office, "amzn1.ask.skill.test")
    node = node_of(session)
    assert node["type"] == "Alexa.Operation.SkillConnections.Launch"
    assert node["operationPayload"] == {
        **BASE_PAYLOAD,
        "targetDevice": {
            "deviceType": "A18O6U1UQFJ0XK",
            "deviceSerialNumber": "G000TEST00000001",
        },
        "connectionRequest": {
            "uri": "connection://AMAZON.Launch/amzn1.ask.skill.test",
            "input": {},
        },
    }


async def test_run_routine_replaces_placeholders(client, session, office):
    session.add("GET", "/api/behaviors/v2/automations", load_json("automations.json"))
    session.add("POST", "/api/behaviors/preview", None)
    routine = (await client.get_routines())[0]
    await client.run_routine(routine, office)
    start = node_of(session)
    assert start["@type"] == "com.amazon.alexa.behaviors.model.SerialNode"
    payload = start["nodesToExecute"][0]["operationPayload"]
    assert payload == {**BASE_PAYLOAD, "value": 20}
    # the stored routine is untouched
    stored = routine.raw["sequence"]["startNode"]["nodesToExecute"][0][
        "operationPayload"
    ]
    assert stored["deviceSerialNumber"] == "ALEXA_CURRENT_DSN"


async def test_run_routine_without_device(client, session):
    session.add("GET", "/api/behaviors/v2/automations", load_json("automations.json"))
    session.add("POST", "/api/behaviors/preview", None)
    routines = await client.get_routines()
    await client.run_routine(routines[0])
    payload = node_of(session)["nodesToExecute"][0]["operationPayload"]
    assert payload["deviceSerialNumber"] == "ALEXA_CURRENT_DSN"
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.run_routine(routines[1])
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.run_routine(Routine(id="x", name="empty"))


# ------------------------------------------- alarms / timers / reminders


async def test_create_reminder(client, session, office):
    created = {
        "id": "A18O6U1UQFJ0XK-G000TEST00000001-new_REMINDER",
        "type": "Reminder",
        "status": "ON",
        "alarmTime": 1_830_000_000_000,
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "reminderLabel": "Water the plants",
    }
    session.add("PUT", "/api/notifications/createReminder", created)
    when = datetime(2027, 12, 28, 9, 30, tzinfo=UTC)
    reminder = await client.create_reminder(office, when, "water the plants")
    body = session.calls[0]["json"]
    local = when.astimezone()
    assert body["type"] == "Reminder"
    assert body["status"] == "ON"
    assert body["id"] == "createReminder"
    assert body["alarmTime"] == int(when.timestamp() * 1000)
    assert body["originalDate"] == local.strftime("%Y-%m-%d")
    assert body["originalTime"] == local.strftime("%H:%M:%S.000")
    assert body["reminderLabel"] == "water the plants"
    assert body["deviceSerialNumber"] == "G000TEST00000001"
    assert body["deviceType"] == "A18O6U1UQFJ0XK"
    assert body["isRecurring"] is False
    assert body["isSaveInFlight"] is True
    assert reminder.kind == "reminder"
    assert reminder.label == "Water the plants"
    assert reminder.is_active


async def test_create_reminder_naive_time_is_local(client, session, office):
    session.add(
        "PUT", "/api/notifications/createReminder", {"id": "x", "type": "Reminder"}
    )
    when = datetime(2027, 12, 28, 9, 30)
    await client.create_reminder(office, when, "x")
    body = session.calls[0]["json"]
    assert (body["originalDate"], body["originalTime"]) == (
        "2027-12-28",
        "09:30:00.000",
    )
    assert body["alarmTime"] == int(when.astimezone().timestamp() * 1000)


async def test_create_reminder_without_answer(client, session, office):
    session.add("PUT", "/api/notifications/createReminder", None)
    with pytest.raises(AmazonEchoResponseError):
        await client.create_reminder(office, datetime(2027, 1, 1, 9, 0), "x")


def _listing(*extra: dict) -> dict:
    return {
        "notifications": [*load_json("notifications.json")["notifications"], *extra]
    }


async def test_create_alarm(client, session, office):
    new = {
        "id": "A18O6U1UQFJ0XK-G000TEST00000001-new",
        "type": "Alarm",
        "status": "ON",
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "createdDate": 1_800_000_000_000,
    }
    session.add("GET", "/api/notifications", _listing(), _listing(new))
    session.add("GET", "/api/endpoints", {"alexaApiUrl": "https://api.amazonalexa.com"})
    session.add("POST", "https://api.amazonalexa.com/v1/alerts/alarms", None)
    alarm = await client.create_alarm(
        office, datetime(2027, 1, 15, 7, 0), label="ignored", recurrence="weekdays"
    )
    call = session.sent("/v1/alerts/alarms")[0]
    assert call["json"] == {
        "trigger": {
            "scheduledTime": "2027-01-15T07:00:00",
            "recurrence": {
                "freq": "WEEKLY",
                "byDay": ["MO", "TU", "WE", "TH", "FR"],
                "interval": 1,
            },
        },
        "extensions": [],
        "endpointId": "G000TEST00000001@A18O6U1UQFJ0XK",
    }
    assert call["headers"]["authorization"] == "Atna|access"
    assert "Cookie" not in call["headers"]
    assert alarm.id == new["id"]


async def test_create_alarm_that_never_shows_up(client, session, office):
    session.add("GET", "/api/notifications", _listing())
    session.add("GET", "/api/endpoints", {"alexaApiUrl": "https://api.amazonalexa.com"})
    session.add("POST", "/v1/alerts/alarms", None)
    with pytest.raises(AmazonEchoResponseError, match="did not show up"):
        await client.create_alarm(office, datetime(2027, 1, 15, 7, 0))
    assert "recurrence" not in session.sent("/v1/alerts/alarms")[0]["json"]["trigger"]


async def test_alerts_api_missing(client, session, office):
    session.add("GET", "/api/notifications", _listing())
    session.add("GET", "/api/endpoints", {})
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.create_alarm(office, datetime(2027, 1, 15, 7, 0))


async def test_create_timer(client, session, office):
    new = {
        "id": "A18O6U1UQFJ0XK-G000TEST00000001-timer",
        "type": "Timer",
        "status": "ON",
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "remainingTime": 300_000,
        "createdDate": 1_800_000_000_000,
    }
    session.add("GET", "/api/notifications", _listing(), _listing(new))
    session.add("GET", "/api/endpoints", {"alexaApiUrl": "https://api.amazonalexa.com"})
    session.add("POST", "/v1/alerts/timers", None)
    timer = await client.create_timer(office, timedelta(minutes=5), label="tea")
    body = session.sent("/v1/alerts/timers")[0]["json"]
    assert body["duration"] == "PT300S"
    assert body["timerLabel"] == "tea"
    assert body["endpointId"] == "G000TEST00000001@A18O6U1UQFJ0XK"
    assert timer.remaining == timedelta(minutes=5)
    with pytest.raises(ValueError, match="positive"):
        await client.create_timer(office, timedelta(0))


async def test_delete_notification(client, session):
    session.add("GET", "/api/notifications", load_json("notifications.json"))
    reminder = (await client.get_notifications())[2]
    session.add("DELETE", "/api/notifications/A1RABVCI4QCIKC-G000TEST00000002", None)
    await client.delete_notification(reminder)
    call = session.calls[-1]
    assert call["url"].endswith("0000000c_REMINDER")
    assert call["json"] == reminder.raw


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("daily", {"freq": "DAILY", "byDay": [], "interval": 1}),
        ("WEEKENDS", {"freq": "WEEKLY", "byDay": ["SA", "SU"], "interval": 1}),
        ("weekly", {"freq": "WEEKLY", "byDay": ["FR"], "interval": 1}),
        ("mo, wed,FR", {"freq": "WEEKLY", "byDay": ["MO", "WE", "FR"], "interval": 1}),
    ],
)
def test_recurrence_rule(text, rule):
    assert _recurrence_rule(text, datetime(2027, 1, 15, 7, 0)) == rule


def test_recurrence_rule_rejects_garbage():
    with pytest.raises(ValueError, match="Unsupported recurrence"):
        _recurrence_rule("sometimes", datetime(2027, 1, 15))


# ------------------------------------------------------------------- lists

LISTS = "https://www.amazon.com.br/alexashoppinglists/api/v2/lists"


async def test_get_lists(client, session):
    session.add(
        "POST",
        f"{LISTS}/fetch",
        {
            "listInfoList": [
                {"listId": "list-shop", "listType": "SHOP"},
                {"listId": "list-todo", "listType": "TODO"},
                {"listId": "list-custom", "listType": "CUSTOM", "listName": "Party"},
            ]
        },
    )
    lists = await client.get_lists()
    assert [(item.id, item.name, item.kind) for item in lists] == [
        ("list-shop", "Shopping", "shopping"),
        ("list-todo", "To-do", "todo"),
        ("list-custom", "Party", "custom"),
    ]
    assert session.calls[0]["json"] == {}
    assert "Cookie" in session.calls[0]["headers"]


async def test_list_items(client, session):
    session.add(
        "POST",
        f"{LISTS}/list-shop/items/fetch",
        {
            "itemInfoList": [
                {
                    "itemId": "i1",
                    "itemName": "milk",
                    "itemStatus": "ACTIVE",
                    "version": 1,
                },
                {
                    "itemId": "i2",
                    "itemName": "eggs",
                    "itemStatus": "COMPLETE",
                    "version": 3,
                },
            ]
        },
    )
    items = await client.get_list_items("list-shop")
    assert items == [
        ListItem(id="i1", name="milk", completed=False, version=1),
        ListItem(id="i2", name="eggs", completed=True, version=3),
    ]
    assert session.calls[0]["params"] == {"limit": "100"}

    session.add("POST", f"{LISTS}/list-shop/items", None)
    await client.add_list_item("list-shop", "bread")
    assert session.calls[-1]["url"] == f"{LISTS}/list-shop/items"
    assert session.calls[-1]["json"] == {
        "items": [{"itemType": "KEYWORD", "itemName": "bread"}]
    }

    session.add("PUT", f"{LISTS}/list-shop/items/", None)
    await client.update_list_item("list-shop", items[1], name="6 eggs", completed=False)
    assert session.calls[-1]["params"] == {"version": "3"}
    assert session.calls[-1]["json"] == {
        "itemAttributesToUpdate": [
            {"type": "itemName", "value": "6 eggs"},
            {"type": "itemStatus", "value": "ACTIVE"},
        ],
        "itemAttributesToRemove": [],
    }
    await client.update_list_item("list-shop", items[0], completed=True)
    assert session.calls[-1]["json"]["itemAttributesToUpdate"] == [
        {"type": "itemStatus", "value": "COMPLETE"}
    ]
    sent = len(session.calls)
    await client.update_list_item("list-shop", items[0])
    assert len(session.calls) == sent

    session.add("DELETE", f"{LISTS}/list-shop/items/i1", None)
    await client.delete_list_item("list-shop", items[0])
    assert session.calls[-1]["method"] == "DELETE"
    assert session.calls[-1]["params"] == {"version": "1"}


# ---------------------------------------------- history / escape hatch

PRIVACY_PAGE = (
    '<html><head><meta name="csrf-token" content="tok+en/123="></head></html>'
)
HISTORY = "/alexa-privacy/apd/rvh/customer-history-records-v2"


async def test_get_voice_history(client, session):
    session.add("GET", "/alexa-privacy/apd/rvh", FakeResponse(200, text=PRIVACY_PAGE))
    session.add("POST", HISTORY, load_json("history.json"))
    since = datetime(2027, 1, 1, tzinfo=UTC)
    records = await client.get_voice_history(since=since, limit=5)
    assert [r.utterance for r in records] == [
        "alexa turn off the lamp",
        "what time is it",
    ]
    first = records[0]
    assert first.id == "A0TESTCUSTOMER1#1800000002000#A18O6U1UQFJ0XK#G000TEST00000001"
    assert first.timestamp == datetime.fromtimestamp(1_800_000_002, UTC)
    assert first.device_serial == "G000TEST00000001"
    assert first.response == "OK"
    assert first.intent == "TurnOffApplianceIntent"
    assert first.person == "Tester"
    assert records[1].intent is None
    assert records[1].response is None

    call = session.sent(HISTORY)[0]
    assert call["url"].startswith("https://www.amazon.com.br/")
    assert call["params"]["startTime"] == str(int(since.timestamp() * 1000))
    assert call["params"]["recordType"] == "VOICE_HISTORY"
    assert call["headers"]["anti-csrftoken-a2z"] == "tok+en/123="
    assert call["json"] == {"previousRequestToken": None}

    await client.get_voice_history(since=datetime(2027, 1, 1))
    assert len(session.sent("/alexa-privacy/apd/rvh", "GET")) == 1  # token cached


async def test_get_voice_history_pages_by_time_window(client, session):
    page = load_json("history.json")
    session.add("GET", "/alexa-privacy/apd/rvh", FakeResponse(200, text=PRIVACY_PAGE))
    older = {
        "customerHistoryRecords": [
            {
                **page["customerHistoryRecords"][3],
                "recordKey": "A0TESTCUSTOMER1#1799999990000#A1RABVCI4QCIKC#G000TEST00000002",
                "timestamp": 1_799_999_990_000,
            },
            page["customerHistoryRecords"][3],  # Amazon repeats records
        ]
    }
    session.add("POST", HISTORY, {**page, "encodedRequestToken": "next"}, older)
    records = await client.get_voice_history(
        since=datetime.fromtimestamp(1_700_000_000, UTC), limit=3
    )
    assert [r.timestamp.timestamp() for r in records] == [
        1_800_000_002.0,
        1_800_000_000.0,
        1_799_999_990.0,
    ]
    first, second = session.sent(HISTORY)
    assert first["json"] == second["json"] == {"previousRequestToken": None}
    assert second["params"]["endTime"] == "1799999999999"  # oldest record - 1 ms
    assert second["params"]["startTime"] == first["params"]["startTime"]


async def test_get_voice_history_stops_at_the_window_start(client, session):
    page = {**load_json("history.json"), "encodedRequestToken": "next"}
    session.add("GET", "/alexa-privacy/apd/rvh", FakeResponse(200, text=PRIVACY_PAGE))
    session.add("POST", HISTORY, page)
    records = await client.get_voice_history(
        since=datetime.fromtimestamp(1_800_000_000, UTC), limit=50
    )
    assert len(records) == 2
    assert len(session.sent(HISTORY)) == 1


async def test_get_voice_history_without_token(client, session):
    session.add(
        "GET", "/alexa-privacy/apd/rvh", FakeResponse(200, text="<html></html>")
    )
    with pytest.raises(AmazonEchoResponseError, match="csrf-token"):
        await client.get_voice_history()


async def test_call_feature(client, session):
    session.add(
        "POST",
        "/nexus/v1/graphql",
        {
            "data": {
                "setEndpointFeatures": {
                    "featureControlResponses": [
                        {"endpointId": "e1", "code": "SUCCESS"}
                    ],
                    "errors": None,
                }
            }
        },
    )
    await client.call_feature(
        "e1", "brightness", "setBrightness", instance="main", payload={"brightness": 50}
    )
    sent = session.calls[0]["json"]
    assert "setEndpointFeatures" in sent["query"]
    assert sent["variables"] == {
        "input": {
            "featureControlRequests": [
                {
                    "endpointId": "e1",
                    "featureName": "brightness",
                    "featureOperationName": "setBrightness",
                    "instance": "main",
                    "payload": {"brightness": 50},
                }
            ]
        }
    }
    await client.call_feature("e1", "power", "turnOn")
    request = session.calls[-1]["json"]["variables"]["input"]["featureControlRequests"][
        0
    ]
    assert request == {
        "endpointId": "e1",
        "featureName": "power",
        "featureOperationName": "turnOn",
    }


async def test_call_feature_rejected(client, session):
    session.add(
        "POST",
        "/nexus/v1/graphql",
        {
            "data": {
                "setEndpointFeatures": {
                    "featureControlResponses": [],
                    "errors": [{"code": "ENDPOINT_UNREACHABLE", "message": "offline"}],
                }
            }
        },
        {
            "data": {
                "setEndpointFeatures": {"featureControlResponses": [], "errors": []}
            }
        },
    )
    with pytest.raises(AmazonEchoResponseError, match="ENDPOINT_UNREACHABLE: offline"):
        await client.call_feature("e1", "power", "turnOn")
    with pytest.raises(AmazonEchoResponseError, match="No response"):
        await client.call_feature("e1", "power", "turnOn")


async def test_cookie_refresh_works_right_after_boot(client, session, monkeypatch):
    """A low monotonic clock (fresh boot) must not look like a recent refresh."""
    monkeypatch.setattr("amazon_echo.client.time.monotonic", lambda: 5.0)
    session.add("GET", "/api/x", FakeResponse(401), {"ok": 1})
    session.add("POST", "/auth/token", COOKIE_ANSWER)
    assert await client.request("GET", "/api/x") == {"ok": 1}
    assert len(session.sent("/auth/token")) == 1
