"""Building blocks for creating and editing Alexa routines.

The routine editor of the Alexa app talks to ``/api/behaviors/automations``
(POST create, PUT ``/{id}`` update, DELETE ``/{id}`` delete). Its body is not
the object ``GET`` returns: the trigger and the sequence travel as JSON
*strings* (``triggerJson``, ``triggerJsonList``, ``sequenceJson``), and inside
each trigger the ``payload`` is a JSON string again. Sending the object as
read gives ``400 Input failed to validate`` (PUT) or ``500`` (POST).

Smart-home actions address a device by the UUID at the end of its endpoint
id (``amzn1.alexa.endpoint.<uuid>``); :func:`endpoint_target` turns one into
the other.
"""

from __future__ import annotations

from collections.abc import Iterator
import copy
import json
from typing import Any

_MODEL = "com.amazon.alexa.behaviors.model."
_ENDPOINT_PREFIX = "amzn1.alexa.endpoint."


def endpoint_target(endpoint_id: str) -> str:
    """Return the routine target of a smart-home endpoint id."""
    return endpoint_id.removeprefix(_ENDPOINT_PREFIX)


def smart_home_action(target: str, *operations: dict[str, Any]) -> dict[str, Any]:
    """Return an action that runs ``operations`` on one smart-home device.

    ``operations`` are the editor's, e.g. ``{"type": "turnOn"}``,
    ``{"type": "setBrightness", "brightness": 40}`` or
    ``{"type": "setColor", "colorName": "blue"}``. ``target`` may be an
    endpoint id or its UUID.
    """
    return {
        "@type": _MODEL + "OpaquePayloadOperationNode",
        "type": "Alexa.SmartHome.Batch",
        "skillId": "amzn1.ask.1p.smarthome",
        "operationPayload": {
            "operations": list(operations) or [{"type": "turnOn"}],
            "target": endpoint_target(target),
        },
    }


def speak_action(text: str) -> dict[str, Any]:
    """Return an action where the device that heard the routine says ``text``."""
    return {
        "@type": _MODEL + "OpaquePayloadOperationNode",
        "type": "Alexa.Speak",
        "skillId": "amzn1.ask.1p.saysomething",
        "operationPayload": {
            "textToSpeak": text,
            "deviceSerialNumber": "ALEXA_CURRENT_DSN",
            "deviceType": "ALEXA_CURRENT_DEVICE_TYPE",
        },
    }


def build_sequence(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a sequence that runs ``actions`` one after the other.

    A dict that already is a sequence (``startNode``) is returned unchanged.
    """
    if len(actions) == 1 and "startNode" in actions[0]:
        return actions[0]
    return {
        "@type": _MODEL + "Sequence",
        "startNode": {"@type": _MODEL + "SerialNode", "nodesToExecute": actions},
    }


def utterance_trigger(
    phrases: list[str], *, customer_id: str, locale: str, marketplace_id: str | None
) -> dict[str, Any]:
    """Return a "when you say" trigger for ``phrases``."""
    return {
        "type": "CustomUtterance",
        "skillId": "amzn1.ask.1p.customutterance",
        "payload": {
            "customerId": customer_id,
            "utterance": phrases[0],
            "utterances": phrases,
            "locale": locale,
            "marketplaceId": marketplace_id,
        },
    }


def _nodes(item: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict inside ``item``, depth first."""
    if isinstance(item, dict):
        yield item
        for value in item.values():
            yield from _nodes(value)
    elif isinstance(item, list):
        for value in item:
            yield from _nodes(value)


def _smart_home_payloads(sequence: Any) -> Iterator[dict[str, Any]]:
    for node in _nodes(sequence):
        payload = node.get("operationPayload")
        if node.get("type") == "Alexa.SmartHome.Batch" and isinstance(payload, dict):
            yield payload


def fill_customer(node: Any, customer_id: str, locale: str) -> Any:
    """Return ``node`` with customerId/locale added to every operation payload."""
    node = copy.deepcopy(node)
    for item in _nodes(node):
        payload = item.get("operationPayload")
        if isinstance(payload, dict):
            payload.setdefault("customerId", customer_id)
            if item.get("type") == "Alexa.Speak":
                payload.setdefault("locale", locale)
    return node


def routine_targets(sequence: Any) -> list[str]:
    """Return the smart-home targets a sequence acts on, in order."""
    return [
        str(payload["target"])
        for payload in _smart_home_payloads(sequence)
        if payload.get("target")
    ]


def replace_targets(sequence: Any, mapping: dict[str, str]) -> Any:
    """Return ``sequence`` with its smart-home targets swapped by ``mapping``.

    Keys and values may be endpoint ids or their UUIDs.
    """
    swap = {endpoint_target(k): endpoint_target(v) for k, v in mapping.items()}
    sequence = copy.deepcopy(sequence)
    for payload in _smart_home_payloads(sequence):
        if payload.get("target") in swap:
            payload["target"] = swap[payload["target"]]
    return sequence


def _trigger_json(trigger: dict[str, Any]) -> str:
    """Encode one trigger the way the editor sends it."""
    payload = trigger.get("payload")
    return json.dumps(
        {
            **trigger,
            "@type": trigger.get("@type") or _MODEL + "Trigger",
            "payload": payload if isinstance(payload, str) else json.dumps(payload),
        }
    )


def request_body(
    *,
    name: str,
    triggers: list[dict[str, Any]],
    sequence: dict[str, Any],
    enabled: bool,
    routine_id: str | None = None,
) -> dict[str, Any]:
    """Return the create/update body of the routine editor."""
    encoded = [_trigger_json(trigger) for trigger in triggers]
    body: dict[str, Any] = {
        "name": name,
        "triggerJson": encoded[0] if encoded else None,
        "triggerJsonList": encoded,
        "sequenceJson": json.dumps(sequence),
        "status": "ENABLED" if enabled else "DISABLED",
    }
    if routine_id is not None:
        body["behaviorId"] = routine_id
    return body
