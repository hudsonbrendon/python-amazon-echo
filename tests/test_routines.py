"""Routine editing: request bodies match what the Alexa app sends."""

from __future__ import annotations

import json

import pytest

from amazon_echo import AmazonEchoNotSupportedError
from amazon_echo.routines import (
    endpoint_target,
    replace_targets,
    routine_targets,
    smart_home_action,
    speak_action,
)

LAMP = "amzn1.alexa.endpoint.11111111-2222-3333-4444-555555555555"
OLD = "aaaaaaaa-0000-0000-0000-000000000000"
NEW = "bbbbbbbb-0000-0000-0000-000000000000"
ROUTINE_ID = "amzn1.alexa.automation.test"
EXISTING = {
    "automationId": ROUTINE_ID,
    "name": "desligar quarto",
    "status": "ENABLED",
    "triggers": [
        {
            "type": "CustomUtterance",
            "skillId": "amzn1.ask.1p.customutterance",
            "id": "amzn1.alexa.trigger.x",
            "payload": {
                "utterance": "desligar quarto",
                "utterances": ["desligar quarto"],
            },
        }
    ],
    "sequence": {
        "@type": "com.amazon.alexa.behaviors.model.Sequence",
        "startNode": {
            "@type": "com.amazon.alexa.behaviors.model.ParallelNode",
            "nodesToExecute": [smart_home_action(OLD, {"type": "turnOff"})],
        },
    },
}


def test_targets_helpers():
    assert endpoint_target(LAMP) == "11111111-2222-3333-4444-555555555555"
    seq = EXISTING["sequence"]
    assert routine_targets(seq) == [OLD]
    swapped = replace_targets(seq, {OLD: "amzn1.alexa.endpoint." + NEW})
    assert routine_targets(swapped) == [NEW]
    assert routine_targets(seq) == [OLD]  # input untouched


async def test_create_routine(client, session):
    session.add(
        "GET", "/api/users/me", {"id": "A0TESTCUSTOMER1", "marketPlaceId": "MKT"}
    )
    session.add("POST", "/api/behaviors/automations", {**EXISTING, "name": "Nova"})
    routine = await client.create_routine(
        "Nova",
        [smart_home_action(LAMP, {"type": "turnOn"}), speak_action("pronto")],
        phrases=["modo leitura"],
    )
    assert routine.id == ROUTINE_ID
    body = session.calls[-1]["json"]
    assert body["name"] == "Nova" and body["status"] == "ENABLED"
    trigger = json.loads(body["triggerJson"])
    assert body["triggerJsonList"] == [body["triggerJson"]]
    assert trigger["@type"].endswith("Trigger")
    payload = json.loads(trigger["payload"])  # a JSON string inside JSON
    assert payload["utterances"] == ["modo leitura"]
    assert payload["marketplaceId"] == "MKT"
    sequence = json.loads(body["sequenceJson"])
    nodes = sequence["startNode"]["nodesToExecute"]
    assert nodes[0]["operationPayload"]["target"] == endpoint_target(LAMP)
    assert nodes[0]["operationPayload"]["customerId"] == "A0TESTCUSTOMER1"
    assert nodes[1]["operationPayload"]["locale"] == "pt-BR"


async def test_create_routine_needs_a_trigger(client, session):
    session.add("GET", "/api/users/me", {"id": "A0TESTCUSTOMER1"})
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.create_routine("x", [speak_action("oi")])


async def test_update_routine_repairs_a_device(client, session):
    session.add("GET", f"/api/behaviors/automations/{ROUTINE_ID}", EXISTING)
    session.add("GET", "/api/users/me", {"id": "A0TESTCUSTOMER1"})
    session.add("PUT", f"/api/behaviors/automations/{ROUTINE_ID}", EXISTING)
    await client.update_routine(ROUTINE_ID, replace_targets={OLD: NEW})
    body = session.calls[-1]["json"]
    assert session.calls[-1]["method"] == "PUT"
    assert body["behaviorId"] == ROUTINE_ID
    assert body["name"] == "desligar quarto" and body["status"] == "ENABLED"
    assert routine_targets(json.loads(body["sequenceJson"])) == [NEW]
    trigger = json.loads(body["triggerJson"])
    assert trigger["id"] == "amzn1.alexa.trigger.x"  # existing trigger kept


async def test_update_routine_phrases_and_status(client, session):
    session.add("GET", f"/api/behaviors/automations/{ROUTINE_ID}", EXISTING)
    session.add("GET", "/api/users/me", {"id": "A0TESTCUSTOMER1"})
    session.add("PUT", f"/api/behaviors/automations/{ROUTINE_ID}", EXISTING)
    await client.update_routine(
        ROUTINE_ID, name="apagar quarto", phrases=["apagar quarto"], enabled=False
    )
    body = session.calls[-1]["json"]
    assert body["name"] == "apagar quarto" and body["status"] == "DISABLED"
    payload = json.loads(json.loads(body["triggerJson"])["payload"])
    assert payload["utterances"] == ["apagar quarto"]
    assert EXISTING["triggers"][0]["payload"]["utterance"] == "desligar quarto"


async def test_update_routine_without_voice_trigger(client, session):
    session.add(
        "GET", f"/api/behaviors/automations/{ROUTINE_ID}", {**EXISTING, "triggers": []}
    )
    session.add("GET", "/api/users/me", {"id": "A0TESTCUSTOMER1"})
    with pytest.raises(AmazonEchoNotSupportedError):
        await client.update_routine(ROUTINE_ID, phrases=["x"])


async def test_delete_routine(client, session):
    session.add("DELETE", f"/api/behaviors/automations/{ROUTINE_ID}", None)
    await client.delete_routine(ROUTINE_ID)
    assert session.calls[-1]["method"] == "DELETE"
