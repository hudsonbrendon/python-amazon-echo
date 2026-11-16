"""Tests for the 0.2.0 API: endpoints, communications, Guard, alarms, pushes."""

from __future__ import annotations

from datetime import datetime
import json

import pytest

from amazon_echo import (
    AmazonEchoNotSupportedError,
    AmazonEchoRateLimitError,
    Device,
    Endpoint,
    Guard,
    Notification,
)
from amazon_echo.client import _next_ring, _parse_voice_record
from amazon_echo.const import INFO_SKILLS
from amazon_echo.endpoints import parse_features

from .conftest import FakeResponse, load_json

COMMS = "https://alexa-comms-mobile-service.amazon.com.br"
PREFERENCES = "/devicesTypes/A18O6U1UQFJ0XK/deviceId/G000TEST00000001/preferences"
FEATURE_OK = {
    "data": {
        "setEndpointFeatures": {
            "featureControlResponses": [{"endpointId": "e1", "code": "SUCCESS"}],
            "errors": None,
        }
    }
}
GUARD_ENTITY = "00000000-0000-0000-0000-00000000guard"
GUARD_APPLIANCE = "AAA_OnGuardSmartHomeBridgeService_abcdef0123456789"


def node_of(session) -> dict:
    sequence = json.loads(
        session.sent("/api/behaviors/preview")[-1]["json"]["sequenceJson"]
    )
    (node,) = sequence["startNode"]["nodesToExecute"]
    return node


def alarm(**raw) -> Notification:
    return Notification(
        id="alarm-1",
        kind=raw.pop("kind", "alarm"),
        status="OFF",
        device_serial="G000TEST00000001",
        device_type="A18O6U1UQFJ0XK",
        sound=raw.pop("sound", None),
        raw={"notificationIndex": "index-1", "originalTime": "07:00:00.000", **raw},
    )


# ------------------------------------------------------------- endpoints


async def test_alexa_device_sensors(client, session):
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_smart_home.json"))
    states = await client.get_endpoint_states()
    # Amazon hardware without a microphone has a serial too
    assert set(states) == {"G000TEST00000004", "G000TEST00000005", "G000TEST00000006"}
    show = states["G000TEST00000004"]
    assert show["illuminance"] == 120.5
    assert show["motion"] is True
    assert show["humidity"] == 55.0
    assert show["temperature"] is None  # -280 while the sensor calibrates
    assert show["reachable"] is True
    assert show["model"] == "Echo Show 5"
    assert "contact" not in show
    assert "power" not in show


async def test_smart_home_endpoints(client, session):
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_smart_home.json"))
    endpoints = {e.name: e for e in await client.get_smart_home_endpoints()}
    assert "Office Show" not in endpoints  # Alexa voice devices are not smart home
    assert len(endpoints) == 10

    monitor = endpoints["Air Monitor"]
    assert monitor.is_amazon_device
    assert (monitor.category, monitor.model) == (
        "AIR_QUALITY_MONITOR",
        "Air Quality Monitor",
    )
    assert monitor.state == {
        "reachable": True,
        "temperature": 22.5,
        "humidity": 48.0,
        "voc": 2.0,
        "pm25": 7.0,
        "pm10": 9.0,
        "co": 0.0,
        "air_quality": 87.0,
        "aqm_led": True,
    }
    assert monitor.supports("toggle", "turnOff")

    glow = endpoints["Glow"]
    assert glow.serial == "G000TEST00000006"
    assert glow.state["power"] is True
    assert glow.state["brightness"] == 80
    assert glow.state["color"] == {"hue": 240.0, "saturation": 1.0, "brightness": 0.8}
    assert glow.state["mode"] == "Rainbow.Mode"
    assert glow.state["mode_instance"] == "1"
    assert glow.state["mode_options"] == {
        "Rainbow": "Rainbow.Mode",
        "Campfire": "Campfire.Mode",
        "Plain.Mode": "Plain.Mode",
    }
    assert glow.supports("mode", "setMode")

    lamp = endpoints["Desk Lamp"]
    assert not lamp.is_amazon_device
    assert (lamp.manufacturer, lamp.model, lamp.category) == (
        "Lamp Maker",
        "LM-1",
        "LIGHT",
    )
    assert lamp.state["power"] is False
    assert lamp.state["color_temperature"] == 2700
    assert lamp.state["color"] is None  # the lamp has colour, Amazon cannot read it
    assert lamp.supports("colorTemperature", "setColorTemperature")
    assert not lamp.supports("mode", "setMode")

    assert endpoints["Coffee Plug"].state == {"reachable": True, "power": True}
    assert endpoints["Offline Plug"].state == {"reachable": False, "power": None}
    assert endpoints["Front Door"].state == {
        "reachable": True,
        "contact": True,
        "battery": 71,
    }
    assert endpoints["Hall Motion"].state == {"reachable": True, "motion": False}
    lock = endpoints["Back Door Lock"]
    assert lock.state == {"reachable": True, "lock": "LOCKED", "battery": 90}
    assert lock.supports("lock", "unlock")
    assert endpoints["Fridge Thermometer"].state["temperature"] is None
    scene = endpoints["Good Night"]
    assert isinstance(scene, Endpoint)
    assert (scene.state, scene.operations) == ({"reachable": True}, frozenset())


async def test_endpoint_answer_is_shared_until_a_write(client, session):
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_smart_home.json"))
    await client.get_endpoint_states()
    await client.get_smart_home_endpoints()
    assert len(session.sent("/nexus/v1/graphql")) == 1
    session.routes.clear()
    session.add(
        "POST",
        "/nexus/v1/graphql",
        FEATURE_OK,
        load_json("graphql_smart_home.json"),
    )
    await client.call_feature("e1", "power", "turnOn")
    await client.get_smart_home_endpoints()
    assert len(session.sent("/nexus/v1/graphql")) == 3


async def test_endpoint_cache_expires(client, session, monkeypatch):
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_smart_home.json"))
    clock = [100.0]
    monkeypatch.setattr("amazon_echo.client.time.monotonic", lambda: clock[0])
    await client.get_endpoint_states()
    clock[0] += 6
    await client.get_endpoint_states()
    assert len(session.sent("/nexus/v1/graphql")) == 2


async def test_parse_endpoint_update(client, session):
    """A push carries the changed features without the query aliases."""
    session.add("POST", "/nexus/v1/graphql", load_json("graphql_smart_home.json"))
    monitor = next(
        e for e in await client.get_smart_home_endpoints() if e.name == "Air Monitor"
    )
    payload = {
        "entity": {"__typename": "Endpoint", "id": monitor.id},
        "data": {
            "__typename": "Endpoint",
            "features": [
                {
                    "name": "temperatureSensor",
                    "instance": None,
                    "properties": [
                        {
                            "name": "temperature",
                            "value": {"value": 77, "scale": "FAHRENHEIT"},
                        }
                    ],
                },
                {
                    "name": "range",
                    "instance": "6",
                    "properties": [{"name": "rangeValue", "rangeValue": {"value": 12}}],
                },
                {
                    "name": "speaker",
                    "instance": None,
                    "properties": [{"name": "volume", "value": {"value": 30}}],
                },
                {"name": "bluetooth", "instance": None, "properties": None},
            ],
        },
    }
    assert client.parse_endpoint_update(payload) == (
        monitor.id,
        {"temperature": 25.0, "pm25": 12, "volume": 30},
    )
    # the same range instance means nothing on an endpoint that is not the monitor
    payload["entity"]["id"] = "unknown"
    assert client.parse_endpoint_update(payload) == (
        "unknown",
        {"temperature": 25.0, "volume": 30},
    )
    assert client.parse_endpoint_update({"dopplerId": {}}) is None
    assert client.parse_endpoint_update({"entity": {"id": "x"}, "data": {}}) is None


def test_parse_features_keeps_the_battery_that_reads():
    battery = {"name": "battery", "batteryValue": {"levelPercentage": 64}}
    unread = {"name": "level", "inventoryLevelValue": None}
    health = {"name": "endpointHealth", "properties": [battery, None]}
    inventory = {
        "name": "inventory",
        "instance": "Battery.Level",
        "properties": [unread],
    }
    assert parse_features([health, inventory]) == {"battery": 64}
    assert parse_features([inventory, health]) == {"battery": 64}
    assert parse_features([inventory]) == {"battery": None}
    assert parse_features(None) == {}
    # a remote's battery is not the device's, other inventories are not batteries
    ink = {"name": "inventory", "instance": "black ink", "properties": [unread]}
    assert parse_features([ink]) == {}
    # a detection state only means something on a motion or contact sensor
    other = {
        "name": "doorbell",
        "properties": [{"name": "detectionState", "detectionStateValue": "DETECTED"}],
    }
    assert parse_features([other]) == {}
    unreadable = {
        "name": "motionSensor",
        "properties": [{"name": "detectionState", "detectionStateValue": None}],
    }
    assert parse_features([unreadable]) == {"motion": None}
    toggle = {
        "name": "toggle",
        "instance": "switched",
        "properties": [{"name": "toggleState", "toggleStateValue": "ON"}],
    }
    assert parse_features([toggle]) == {}


# -------------------------------------------------------- communications


async def test_get_communication_preferences(client, session, office):
    session.add(
        "GET",
        PREFERENCES,
        {
            "devicePermissionsPreferences": [
                {"devicePreference": "communications", "state": "ON", "allowed": True},
                {"devicePreference": "announcements", "state": "OFF", "allowed": True},
                {"devicePreference": "dropin", "state": "Home", "allowed": True},
                {"devicePreference": "other", "state": "ON", "allowed": True},
            ]
        },
    )
    assert await client.get_communication_preferences(office) == {
        "communications": True,
        "announcements": False,
        "drop_in": "home",
    }
    call = session.calls[0]
    assert call["url"] == (
        f"{COMMS}{PREFERENCES}?devicePreferences=communications"
        "&devicePreferences=dropin&devicePreferences=announcements"
    )
    # the marketplace alias of the service takes the account's cookies
    assert call["headers"]["Cookie"].startswith("session-id=")
    assert "AmazonWebView" in call["headers"]["User-Agent"]


async def test_communication_preferences_not_allowed(client, session, office):
    session.add(
        "GET",
        PREFERENCES,
        {
            "devicePermissionsPreferences": [
                {"devicePreference": "dropin", "state": "All", "allowed": False},
                {"devicePreference": "announcements", "state": None, "allowed": True},
            ]
        },
        None,
    )
    assert await client.get_communication_preferences(office) == {}
    assert await client.get_communication_preferences(office) == {}


async def test_communication_preferences_are_not_retried(client, session, office):
    """One call per device: a failing service must not be asked three times."""
    session.add("GET", PREFERENCES, FakeResponse(503, text="<html>"))
    with pytest.raises(AmazonEchoRateLimitError):
        await client.get_communication_preferences(office)
    assert len(session.calls) == 1


async def test_set_communication_preferences(client, session, office):
    session.add("PATCH", PREFERENCES, None)
    await client.set_communications(office, False)
    await client.set_announcements(office, True)
    await client.set_drop_in(office, "home")
    assert [
        (call["url"].removeprefix(COMMS + PREFERENCES), call["json"])
        for call in session.calls
    ] == [
        ("/communications", {"state": "OFF"}),
        ("/announcements", {"state": "ON"}),
        ("/dropin", {"state": "Home"}),
    ]
    with pytest.raises(ValueError, match="all, home or off"):
        await client.set_drop_in(office, "everyone")


async def test_kids_mode_and_device_preferences(client, session, office):
    session.add("GET", "/api/device/op-mode", {"isChildDirectedDevice": True}, None)
    assert await client.get_kids_mode(office) is True
    assert session.calls[0]["params"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
    }
    assert await client.get_kids_mode(office) is False
    session.add(
        "GET",
        "/api/device-preferences",
        {
            "devicePreferences": [
                {
                    "deviceSerialNumber": "G000TEST00000001",
                    "locale": "pt-BR",
                    "timeZoneId": "America/Sao_Paulo",
                    "temperatureScale": "CELSIUS",
                    "postalCode": "00000-000",
                },
                {"locale": "en-US"},
            ]
        },
    )
    assert await client.get_device_preferences() == {
        "G000TEST00000001": {
            "locale": "pt-BR",
            "time_zone": "America/Sao_Paulo",
            "temperature_scale": "CELSIUS",
        }
    }


async def test_amazon_kids(client, session, office):
    parents = "https://parents.amazon.com.br"
    session.add(
        "GET",
        "/ajax/get-household-with-age",
        {
            "members": [
                {
                    "role": "ADULT",
                    "directedId": "amzn1.account.ADULT",
                    "firstName": "A",
                },
                {
                    "role": "CHILD",
                    "directedId": "amzn1.account.KID1",
                    "firstName": "Kid",
                },
                {"role": "CHILD", "directedId": "amzn1.account.KID2"},
                {"role": "CHILD"},
            ]
        },
        None,
    )
    assert await client.get_children() == {
        "amzn1.account.KID1": "Kid",
        "amzn1.account.KID2": "amzn1.account.KID2",
    }
    assert session.calls[0]["url"] == f"{parents}/ajax/get-household-with-age"
    assert await client.get_children() == {}

    session.add(
        "GET",
        "/ajax/get-oobe-device-data",
        {"childDirectedId": "amzn1.account.KID1"},
        {"childDirectedId": ""},
    )
    assert await client.get_kids_profile(office) == "amzn1.account.KID1"
    assert session.calls[-1]["params"] == {
        "deviceId": "G000TEST00000001",
        "deviceTypeId": "A18O6U1UQFJ0XK",
    }
    assert await client.get_kids_profile(office) is None

    # Assigning needs the dashboard's own CSRF token, set by its setup page.
    start = "/oobe/brownie/start/A18O6U1UQFJ0XK/G000TEST00000001"
    session.add(
        "GET",
        start,
        FakeResponse(200, text="<html>", cookies={"ft-panda-csrf-token": "panda"}),
    )
    session.add("POST", "/ajax/assign-device-to-child", None)
    await client.set_kids_mode(office, "amzn1.account.KID1")
    call = session.calls[-1]
    assert call["url"] == f"{parents}/ajax/assign-device-to-child"
    assert call["json"] == {
        "deviceId": "G000TEST00000001",
        "deviceTypeId": "A18O6U1UQFJ0XK",
        "childDirectedId": "amzn1.account.KID1",
    }
    assert call["headers"]["x-amzn-csrf"] == "panda"
    assert call["headers"]["Referer"] == parents + start
    assert call["headers"]["Origin"] == parents

    session.add("POST", "/api/unassign-device-from-child", None)
    await client.set_kids_mode(office, None)
    assert session.calls[-1]["url"] == (
        "https://alexa.amazon.com.br/api/unassign-device-from-child"
        "/G000TEST00000001/A18O6U1UQFJ0XK"
    )


async def test_kids_dashboard_host_is_translated_in_germany(client, session, office):
    client.credentials.domain = "amazon.de"
    session.add("GET", "/ajax/get-household-with-age", {"members": []})
    await client.get_children()
    assert session.calls[0]["url"].startswith("https://eltern.amazon.de/")


async def test_set_background_image(client, session, office):
    session.add("POST", "/api/background-image", None)
    await client.set_background_image(office, "https://example.com/photo.jpg")
    assert session.calls[0]["json"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "backgroundImageID": "JqIFZhtBTx25wLGTJGdNGQ",
        "backgroundImageType": "PERSONAL_PHOTOS",
        "backgroundImageURL": "https://example.com/photo.jpg",
    }


# ------------------------------------------------------------- sequences


async def test_info_skill(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.info_skill(office, "weather")
    node = node_of(session)
    assert node["type"] == "Alexa.Weather.Play"
    assert node["operationPayload"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "locale": "pt-BR",
        "customerId": "A0TESTCUSTOMER1",
    }
    with pytest.raises(ValueError, match="Unknown info skill"):
        await client.info_skill(office, "Alexa.Weather.Play")
    assert len(INFO_SKILLS) == 17
    assert all(kind.startswith("Alexa.") for kind in INFO_SKILLS.values())


async def test_send_sequence(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.send_sequence(
        office, "Alexa.CannedTts.Speak", {"cannedTtsStringId": "x"}
    )
    node = node_of(session)
    assert node["type"] == "Alexa.CannedTts.Speak"
    assert node["operationPayload"]["cannedTtsStringId"] == "x"


async def test_send_mobile_push(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.send_mobile_push("The washer is done", title="Laundry")
    node = node_of(session)
    assert node["type"] == "Alexa.Notifications.SendMobilePush"
    assert node["operationPayload"] == {
        "customerId": "A0TESTCUSTOMER1",
        "notificationMessage": "The washer is done",
        "alexaUrl": "#v2/behaviors",
        "title": "Laundry",
        "skillId": "amzn1.ask.1p.routines.messaging",
    }
    await client.send_mobile_push("Hi", device=office)
    payload = node_of(session)["operationPayload"]
    assert payload["title"] == "Home Assistant"
    assert payload["deviceSerialNumber"] == "G000TEST00000001"
    assert payload["locale"] == "pt-BR"


async def test_send_drop_in_notification(client, session):
    session.add("POST", "/api/behaviors/preview", None)
    await client.send_drop_in_notification("Someone is at the door")
    node = node_of(session)
    assert node["type"] == "Alexa.Notifications.DropIn"
    assert node["skillId"] == "amzn1.ask.1p.action.dropin"
    assert node["operationPayload"] == {
        "customerId": "A0TESTCUSTOMER1",
        "notificationMessage": "Someone is at the door",
        "alexaUrl": "#v2/comms/conversation-list?showDropInDialog=true",
        "title": "Home Assistant",
    }


async def test_announce_methods(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    await client.announce([office], "Dinner", title="Kitchen", method="speak")
    content = node_of(session)["operationPayload"]["content"][0]
    assert content["display"] == {"title": "", "body": ""}
    assert content["speak"] == {"type": "text", "value": "Dinner"}
    await client.announce([office], "Dinner", title="Kitchen", method="show")
    content = node_of(session)["operationPayload"]["content"][0]
    assert content["display"] == {"title": "Kitchen", "body": "Dinner"}
    assert content["speak"] == {"type": "text", "value": ""}
    with pytest.raises(ValueError, match="all, speak or show"):
        await client.announce([office], "Dinner", method="shout")


# ---------------------------------------------------------------- alarms


async def test_switch_alarms(client, session, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 1, 15, 8, 0)  # a Friday, after the 07:00 ring

    monkeypatch.setattr("amazon_echo.client.datetime", Clock)
    session.add("GET", "/api/endpoints", {"alexaApiUrl": "https://api.amazonalexa.com"})
    session.add("PUT", "/v1/alerts/alarms/index-1", None)
    await client.enable_alarm(alarm(recurringPattern="XXXX-WD", sound="tone-1"))
    call = session.calls[-1]
    assert call["url"].endswith("/v1/alerts/alarms/index-1/activate")
    assert call["json"] == {
        "trigger": {"scheduledTime": "2027-01-18T07:00:00"},  # the next Monday
        "extensions": [],
        "assets": [{"type": "TONE", "assetId": "tone-1"}],
    }
    assert call["headers"]["authorization"] == "Atna|access"

    await client.enable_alarm(alarm(kind="music_alarm", musicAlarmId="station-1"))
    assert session.calls[-1]["json"]["assets"] == [
        {"type": "TONE", "assetId": "MUSIC-station-1"}
    ]
    await client.disable_alarm(alarm())
    assert session.calls[-1]["url"].endswith("/alarms/index-1/cancel")
    assert session.calls[-1]["json"] is None
    await client.skip_next_alarm(alarm())
    assert session.calls[-1]["url"].endswith("/alarms/index-1/nextOccurrence/cancel")

    with pytest.raises(AmazonEchoNotSupportedError):
        await client.disable_alarm(alarm(kind="timer"))
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.disable_alarm(alarm(notificationIndex=None))


def test_next_ring():
    friday = datetime(2027, 1, 15, 6, 0)
    assert _next_ring({"originalTime": "07:00:00.000"}, friday) == "2027-01-15T07:00:00"
    late = friday.replace(hour=9)
    assert _next_ring({"originalTime": "07:00:00.000"}, late) == "2027-01-16T07:00:00"
    weekend = {"originalTime": "07:30:00.000", "recurringPattern": "XXXX-WE"}
    assert _next_ring(weekend, friday) == "2027-01-16T07:30:00"
    daily = {"originalTime": "07:30:00.000", "recurringPattern": "P1D"}
    assert _next_ring(daily, friday) == "2027-01-15T07:30:00"
    # several days: the rule data wins over the pattern, which names only one
    some = {
        "originalTime": "07:00:00.000",
        "recurringPattern": "XXXX-WXX-1",
        "rRuleData": {"byWeekDays": ["MO", "TH", "??"]},
    }
    assert _next_ring(some, late) == "2027-01-18T07:00:00"
    with pytest.raises(ValueError, match="no time"):
        _next_ring({}, friday)
    with pytest.raises(ValueError, match="Unsupported repeat rule"):
        _next_ring({"originalTime": "07:00:00.000", "recurringPattern": "P1M"}, friday)


# ----------------------------------------------------------------- Guard


def appliances(*models: str) -> dict:
    return {
        "data": {
            "endpoints": {
                "items": [
                    {"legacyAppliance": None},
                    *(
                        {
                            "legacyAppliance": {
                                "applianceId": GUARD_APPLIANCE,
                                "modelName": model,
                                "entityId": GUARD_ENTITY,
                            }
                        }
                        for model in models
                    ),
                ]
            }
        }
    }


def guard_state(value: str) -> dict:
    return {
        "deviceStates": [
            {
                "entity": {"entityId": GUARD_ENTITY},
                "capabilityStates": [
                    "not json",
                    json.dumps({"namespace": "Alexa.EndpointHealth", "name": "x"}),
                    json.dumps(
                        {
                            "namespace": "Alexa.SecurityPanelController",
                            "name": "armState",
                            "value": value,
                        }
                    ),
                ],
            }
        ]
    }


async def test_account_without_guard(client, session):
    session.add("POST", "/nexus/v1/graphql", appliances("SOME_PLUG"))
    assert await client.get_guard() is None
    assert await client.get_guard() is None
    # looked up once, and the state API is never asked
    assert len(session.calls) == 1
    assert "legacyAppliance" in session.calls[0]["json"]["query"]


async def test_get_guard(client, session):
    session.add("POST", "/nexus/v1/graphql", appliances("REDROCK_GUARD_PANEL"))
    session.add(
        "POST", "/api/phoenix/state", guard_state("ARMED_AWAY"), {"deviceStates": []}
    )
    guard = await client.get_guard()
    assert guard == Guard(
        entity_id=GUARD_ENTITY, appliance_id=GUARD_APPLIANCE, state="ARMED_AWAY"
    )
    assert guard.armed
    assert session.sent("/api/phoenix/state")[0]["json"] == {
        "stateRequests": [{"entityId": GUARD_ENTITY, "entityType": "ENTITY"}]
    }
    guard = await client.get_guard()
    assert (guard.state, guard.armed) == (None, False)
    assert len(session.sent("/nexus/v1/graphql")) == 1


async def test_set_guard_through_an_echo(client, session, office):
    session.add("POST", "/api/behaviors/preview", None)
    guard = Guard(entity_id=GUARD_ENTITY, appliance_id=GUARD_APPLIANCE)
    echo = Device(
        serial=office.serial,
        device_type=office.device_type,
        name=office.name,
        family="ECHO",
        capabilities=frozenset({"GUARD_EARCON"}),
    )
    await client.set_guard(guard, True, echo)
    node = node_of(session)
    assert node["type"] == "controlGuardState"
    assert node["operationPayload"] == {
        "deviceSerialNumber": "G000TEST00000001",
        "deviceType": "A18O6U1UQFJ0XK",
        "locale": "pt-BR",
        "customerId": "A0TESTCUSTOMER1",
        "target": "abcdef0123456789",
        "operationId": "controlGuardState",
        "state": "AWAY",
        "skillId": "amzn1.ask.skill.f71a9b50-e99a-4669-a226-d50ebb5e0830",
    }
    await client.set_guard(
        Guard(entity_id=GUARD_ENTITY, appliance_id="odd"), False, echo
    )
    payload = node_of(session)["operationPayload"]
    assert (payload["target"], payload["state"]) == ("odd", "HOME")


async def test_set_guard_without_an_echo(client, session, office):
    """No Echo with Guard at hand: the smart-home state API takes the command."""
    session.add("PUT", "/api/phoenix/state", None)
    guard = Guard(entity_id=GUARD_ENTITY, appliance_id=GUARD_APPLIANCE)
    await client.set_guard(guard, True)
    await client.set_guard(guard, False, office)  # the office Echo has no Guard
    assert [
        call["json"]["controlRequests"][0] for call in session.sent("/api/phoenix")
    ] == [
        {
            "entityId": GUARD_ENTITY,
            "entityType": "APPLIANCE",
            "parameters": {"action": "controlSecurityPanel", "armState": state},
        }
        for state in ("ARMED_AWAY", "ARMED_STAY")
    ]


# ----------------------------------------------------------------- voice


def test_voice_record_person_type():
    record = _parse_voice_record(
        {
            "recordKey": "A0TESTCUSTOMER1#1800000000000#A18O6U1UQFJ0XK#G000TEST00000001",
            "timestamp": 1800000000000,
            "utteranceType": "GENERAL",
            "voiceHistoryRecordItems": [],
            "personsInfo": [{"personFirstName": "Tester", "personType": "ADULT"}],
        }
    )
    assert (record.person, record.person_type) == ("Tester", "ADULT")
