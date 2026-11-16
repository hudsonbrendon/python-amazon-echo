"""Tests for the public surface and the models."""

from __future__ import annotations

import amazon_echo
from amazon_echo import Device, Notification, PushEvent


def test_public_api():
    assert amazon_echo.__version__ == "0.2.0"
    for name in amazon_echo.__all__:
        assert hasattr(amazon_echo, name)
    assert issubclass(
        amazon_echo.AmazonEchoConnectionTimeoutError,
        amazon_echo.AmazonEchoConnectionError,
    )
    for name in (
        "Authentication",
        "Connection",
        "RateLimit",
        "Response",
        "NotSupported",
    ):
        error = getattr(amazon_echo, f"AmazonEcho{name}Error")
        assert issubclass(error, amazon_echo.AmazonEchoError)


def test_response_error():
    error = amazon_echo.AmazonEchoResponseError(400, "bad")
    assert (error.status, error.body) == (400, "bad")
    assert str(error) == "HTTP 400: bad"


def test_device_properties():
    group = Device(serial="abc", device_type="A3C9PE6TNYLTCH", name="All", family="WHA")
    app = Device(serial="def", device_type="A2IVLV5VM2W81", name="App", family="VOX")
    echo = Device(
        serial="G000TEST00000001",
        device_type="A18O6U1UQFJ0XK",
        name="Echo",
        family="ECHO",
        capabilities=frozenset({"REMINDERS"}),
    )
    assert group.is_group
    assert not group.is_app
    assert app.is_app
    assert echo.key == "G000TEST00000001"
    assert echo.supports("REMINDERS")
    assert not echo.supports("EARCONS")
    assert echo.mac is None
    assert echo.battery_level is None


def test_notification_is_active():
    def make(status):
        return Notification(
            id="n", kind="alarm", status=status, device_serial="s", device_type="t"
        )

    assert make("ON").is_active
    assert not make("OFF").is_active
    assert not make("PAUSED").is_active


def test_push_event_defaults():
    event = PushEvent("PUSH_VOLUME_CHANGE")
    assert event.device_serial is None
    assert event.payload == {}
