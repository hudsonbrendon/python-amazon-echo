"""Parsers for the GraphQL endpoints (Alexa devices and smart-home devices).

A GraphQL endpoint lists *features* (``power``, ``temperatureSensor``…), each
with typed *properties*. They are flattened here into plain values, keyed by
what the property carries rather than by device model, so any device that
reports a property gets its value. The same shape arrives over push
(``EventBus:AlexaMobile::FDAL``), without the query aliases.
"""

from __future__ import annotations

from collections.abc import Iterator
import re
from typing import Any

from .const import (
    AMBIENT_MODE_INSTANCE,
    AQM_DEVICE_TYPE,
    AQM_LED_INSTANCE,
    AQM_RANGES,
    REMOTE_BATTERY_INSTANCE,
)
from .models import Endpoint

# What Amazon reports while a temperature sensor calibrates (after a reboot).
_CALIBRATING = -280
_DETECTION = {"motionSensor": "motion", "contactSensor": "contact"}


def format_mac(value: str | None) -> str | None:
    """Return ``aa:bb:cc:dd:ee:ff`` for any 12-hex-digit MAC spelling."""
    digits = re.sub(r"[^0-9a-fA-F]", "", value or "").lower()
    if len(digits) != 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def dig(data: Any, *path: str) -> Any:
    """Walk nested dicts, returning None as soon as a level is missing."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _celsius(reading: Any) -> float | None:
    value = dig(reading, "value")
    if not isinstance(value, (int, float)) or value == _CALIBRATING:
        return None
    if dig(reading, "scale") == "FAHRENHEIT":
        return round((value - 32) / 1.8, 1)
    return value


def _switched(value: Any) -> bool | None:
    return None if value is None else value == "ON"


def _values(
    feature: str, instance: str | None, prop: dict[str, Any], device_type: str | None
) -> Iterator[tuple[str, Any]]:
    """Yield the (key, value) pairs one property stands for."""
    name = prop.get("name")
    if name == "temperature":
        # `temp` is the query alias, `value` what a push update carries
        yield "temperature", _celsius(prop.get("temp") or prop.get("value"))
    elif name == "volume":
        yield "volume", dig(prop.get("vol") or prop.get("value"), "value")
    elif "reachabilityStatusValue" in prop:
        status = prop["reachabilityStatusValue"]
        yield "reachable", None if status is None else status == "OK"
    elif "illuminanceValue" in prop:
        yield "illuminance", dig(prop, "illuminanceValue", "value")
    elif "detectionStateValue" in prop and feature in _DETECTION:
        state = prop["detectionStateValue"]
        yield _DETECTION[feature], None if state is None else state == "DETECTED"
    elif "relativeHumidityValue" in prop:
        yield "humidity", dig(prop, "relativeHumidityValue", "value")
    elif "rangeValue" in prop:
        if device_type == AQM_DEVICE_TYPE and instance in AQM_RANGES:
            yield AQM_RANGES[instance], dig(prop, "rangeValue", "value")
    elif "powerStateValue" in prop:
        yield "power", _switched(prop["powerStateValue"])
    elif "brightnessStateValue" in prop:
        yield "brightness", prop["brightnessStateValue"]
    elif "colorStateValue" in prop:
        yield "color", prop["colorStateValue"]
    elif "colorTemperatureInKelvinStateValue" in prop:
        yield "color_temperature", prop["colorTemperatureInKelvinStateValue"]
    elif "lockState" in prop:
        yield "lock", prop["lockState"]
    elif "modeValue" in prop:
        yield "mode", dig(prop, "modeValue", "value")
    elif "toggleStateValue" in prop:
        value = _switched(prop["toggleStateValue"])
        if instance == AMBIENT_MODE_INSTANCE:
            yield "ambient_mode", value
        elif device_type == AQM_DEVICE_TYPE and instance == AQM_LED_INSTANCE:
            yield "aqm_led", value
    elif "batteryValue" in prop:
        yield "battery", dig(prop, "batteryValue", "levelPercentage")
    elif "inventoryLevelValue" in prop:
        level = dig(prop, "inventoryLevelValue", "value")
        if instance == REMOTE_BATTERY_INSTANCE:
            yield "remote_battery", level
        elif "battery" in (instance or "").casefold():
            yield "battery", level


def parse_features(
    features: list[dict[str, Any]] | None, *, device_type: str | None = None
) -> dict[str, Any]:
    """Flatten GraphQL features into values.

    A key is present when the endpoint has the feature; its value is None
    while Amazon cannot read it (device unreachable, sensor calibrating).

    Keys: ``reachable``, ``temperature`` (°C), ``humidity`` (%),
    ``illuminance`` (lx), ``motion``, ``contact`` (True = open), ``battery``
    (%), ``remote_battery`` (%), ``volume``, ``ambient_mode`` (with
    ``ambient_mode_supported``, set even when Amazon gives no state), ``power``,
    ``brightness`` (0-100), ``color`` (``hue`` 0-360, ``saturation`` and
    ``brightness`` 0-1), ``color_temperature`` (K), ``lock`` (``LOCKED`` /
    ``UNLOCKED`` / ``JAMMED``), ``mode`` with ``mode_instance`` and
    ``mode_options`` (name → value) and, for the Amazon Air Quality Monitor,
    ``pm25`` / ``pm10`` (µg/m³), ``voc`` (index), ``co`` (ppm),
    ``air_quality`` (index) and ``aqm_led``.
    """
    state: dict[str, Any] = {}
    for feature in features or []:
        name, instance = feature.get("name") or "", feature.get("instance")
        for prop in feature.get("properties") or []:
            for key, value in _values(name, instance, prop or {}, device_type):
                # two features can carry a battery: keep the one that reads
                if value is not None or state.get(key) is None:
                    state[key] = value
        if name == "toggle" and instance == AMBIENT_MODE_INSTANCE:
            # Fire TV lists the feature but never reports its state (observed)
            state["ambient_mode_supported"] = True
        options = dig(feature, "configuration", "modeOptions")
        if name == "mode" and options:
            state["mode_instance"] = instance
            state["mode_options"] = {
                dig(option, "modeResources", "friendlyName", "value", "text")
                or option["value"]: option["value"]
                for option in options
                if option.get("value")
            }
    return state


def parse_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    """Flatten one GraphQL endpoint of an Alexa device (see ``parse_features``)."""
    mac = None
    for connection in endpoint.get("connections") or []:
        if connection.get("type") == "TCP_IP" and connection.get("macAddress"):
            mac = format_mac(connection["macAddress"])
    return {
        "endpoint_id": endpoint.get("endpointId"),
        "model": dig(endpoint, "model", "value", "text"),
        "mac": mac,
        "temperature": None,
        "volume": None,
        "reachable": None,
        "remote_battery": None,
        "ambient_mode": None,
        "dnd": dig(endpoint, "settings", "doNotDisturb", "toggleValue"),
        **parse_features(
            endpoint.get("features"),
            device_type=dig(endpoint, "dmsIdentifier", "deviceType"),
        ),
    }


def build_endpoint(endpoint: dict[str, Any]) -> Endpoint:
    """Build an :class:`Endpoint` from one GraphQL endpoint."""
    features = endpoint.get("features") or []
    device_type = dig(endpoint, "dmsIdentifier", "deviceType")
    return Endpoint(
        id=endpoint["endpointId"],
        name=dig(endpoint, "friendlyNameObject", "value", "text")
        or endpoint["endpointId"],
        category=dig(endpoint, "displayCategories", "primary", "value") or "OTHER",
        manufacturer=dig(endpoint, "manufacturer", "value", "text"),
        model=dig(endpoint, "model", "value", "text"),
        serial=dig(endpoint, "dmsIdentifier", "dsn"),
        device_type=device_type,
        operations=frozenset(
            f"{feature.get('name')}.{operation.get('name')}"
            for feature in features
            for operation in feature.get("operations") or []
        ),
        state=parse_features(features, device_type=device_type),
    )
