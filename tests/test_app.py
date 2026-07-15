import hashlib
import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from blue_bubbles_ingest.app import (
    MAX_ARRAY_SHAPES,
    MAX_BODY_BYTES,
    MAX_SHAPE_DEPTH,
    MAX_SHAPE_KEYS,
    app,
)

client = TestClient(app)
fixture_path = Path(__file__).parent / "fixtures" / "new-message.json"


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_readyz_is_distinct_from_healthz() -> None:
    readiness = client.get("/readyz")
    liveness = client.get("/healthz")

    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready"}
    assert readiness.json() != liveness.json()


def test_valid_new_message_is_accepted() -> None:
    response = client.post(
        "/v1/webhooks/bluebubbles",
        content=fixture_path.read_bytes(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 204
    assert response.content == b""


def test_unknown_event_type_is_accepted() -> None:
    response = client.post(
        "/v1/webhooks/bluebubbles", json={"type": "future-event", "data": [1, 2, 3]}
    )
    assert response.status_code == 204


def test_malformed_json_is_rejected() -> None:
    response = client.post(
        "/v1/webhooks/bluebubbles",
        content=b'{"type":',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_wrong_content_type_is_rejected() -> None:
    response = client.post(
        "/v1/webhooks/bluebubbles", content=b"{}", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 415


def test_invalid_envelopes_are_rejected() -> None:
    invalid = [[], {}, {"type": "", "data": {}}, {"type": "new-message"}]
    for body in invalid:
        response = client.post("/v1/webhooks/bluebubbles", json=body)
        assert response.status_code == 422


def test_oversized_body_is_rejected() -> None:
    response = client.post(
        "/v1/webhooks/bluebubbles",
        content=b" " * (MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_accepted_event_log_is_privacy_safe(caplog) -> None:
    fixture = json.loads(fixture_path.read_text())
    event_logger = logging.getLogger("blue_bubbles_ingest.events")
    event_logger.addHandler(caplog.handler)
    try:
        response = client.post("/v1/webhooks/bluebubbles", json=fixture)
    finally:
        event_logger.removeHandler(caplog.handler)

    assert response.status_code == 204
    records = [r for r in caplog.records if r.name == "blue_bubbles_ingest.events"]
    assert len(records) == 1
    summary = json.loads(records[0].message)
    expected_summary = {
        "eventType": "new-message",
        "messageGuidHash": hashlib.sha256(b"invented-message-guid-001").hexdigest()[:12],
        "isFromMe": False,
        "hasText": True,
        "dateCreated": 1712345678000,
        "attachmentCount": 1,
        "chatCount": 1,
        "chatGuidHashes": [hashlib.sha256(b"invented-chat-guid-001").hexdigest()[:12]],
    }
    for key, value in expected_summary.items():
        assert summary[key] == value
    assert summary["dataShapeHash"] == hashlib.sha256(
        json.dumps(summary["dataShape"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    serialized_log = records[0].message
    for private_value in (
        fixture["data"]["text"],
        fixture["data"]["handle"]["address"],
        fixture["data"]["guid"],
        fixture["data"]["chats"][0]["guid"],
        fixture["data"]["attachments"][0]["transferName"],
        fixture["data"]["futureUnknownField"],
    ):
        assert private_value not in serialized_log


def test_wrongly_typed_nested_fields_do_not_raise(caplog) -> None:
    body = {
        "type": "new-message",
        "data": {
            "guid": 42,
            "text": ["not text"],
            "isFromMe": "false",
            "dateCreated": {"unexpected": True},
            "attachments": "not a list",
            "chats": [None, {"guid": 99}],
        },
    }
    event_logger = logging.getLogger("blue_bubbles_ingest.events")
    event_logger.addHandler(caplog.handler)
    try:
        response = client.post("/v1/webhooks/bluebubbles", json=body)
    finally:
        event_logger.removeHandler(caplog.handler)
    assert response.status_code == 204
    assert json.loads(caplog.records[-1].message) == {
        "dataShape": {
            "keys": {
                "attachments": {"type": "string"},
                "chats": {
                    "count": 2,
                    "itemShapes": [
                        {"keys": {"guid": {"type": "integer"}}, "type": "object"},
                        {"type": "null"},
                    ],
                    "type": "array",
                },
                "dateCreated": {
                    "keys": {"unexpected": {"type": "boolean"}},
                    "type": "object",
                },
                "guid": {"type": "integer"},
                "isFromMe": {"type": "string"},
                "text": {
                    "count": 1,
                    "itemShapes": [{"type": "string"}],
                    "type": "array",
                },
            },
            "type": "object",
        },
        "dataShapeHash": hashlib.sha256(
            b'{"keys":{"attachments":{"type":"string"},"chats":{"count":2,"itemShapes":[{"keys":{"guid":{"type":"integer"}},"type":"object"},{"type":"null"}],"type":"array"},"dateCreated":{"keys":{"unexpected":{"type":"boolean"}},"type":"object"},"guid":{"type":"integer"},"isFromMe":{"type":"string"},"text":{"count":1,"itemShapes":[{"type":"string"}],"type":"array"}},"type":"object"}'
        ).hexdigest(),
        "eventType": "new-message",
        "hasText": False,
        "attachmentCount": 0,
        "chatCount": 2,
    }


def test_shape_exposes_nested_structure_without_private_values(caplog) -> None:
    private_values = ["secret text", "person@example.test", "photo-secret.jpg", "group-guid"]
    body = {
        "type": "new-message",
        "data": {
            "attachments": [{"transferName": private_values[2], "totalBytes": 1234}],
            "chats": [{"guid": private_values[3], "participants": [{"address": private_values[1]}]}],
            "text": private_values[0],
            "mixed": [None, True, 7, 1.5, "private", {}, [], 9, "another"],
        },
    }
    event_logger = logging.getLogger("blue_bubbles_ingest.events")
    event_logger.addHandler(caplog.handler)
    try:
        response = client.post("/v1/webhooks/bluebubbles", json=body)
    finally:
        event_logger.removeHandler(caplog.handler)

    assert response.status_code == 204
    serialized = caplog.records[-1].message
    shape = json.loads(serialized)["dataShape"]
    assert set(shape["keys"]) == {"attachments", "chats", "mixed", "text"}
    attachment = shape["keys"]["attachments"]["itemShapes"][0]
    assert attachment["keys"] == {
        "totalBytes": {"type": "integer"},
        "transferName": {"type": "string"},
    }
    chat = shape["keys"]["chats"]["itemShapes"][0]
    assert chat["keys"]["guid"] == {"type": "string"}
    assert chat["keys"]["participants"]["itemShapes"][0]["keys"] == {
        "address": {"type": "string"}
    }
    assert {item["type"] for item in shape["keys"]["mixed"]["itemShapes"]} == {
        "array", "boolean", "float", "integer", "null", "object", "string"
    }
    assert shape["keys"]["mixed"]["count"] == 9
    assert {"type": "object", "keys": {}} in shape["keys"]["mixed"]["itemShapes"]
    assert {"type": "array", "count": 0, "itemShapes": []} in shape["keys"]["mixed"][
        "itemShapes"
    ]
    for private_value in private_values + ["private", "another", "1234"]:
        assert private_value not in serialized


def test_shape_limits_are_explicit_and_do_not_raise(caplog) -> None:
    wide = {f"key-{i:03}": i for i in range(MAX_SHAPE_KEYS + 2)}
    deep: object = "deep private value"
    for _ in range(MAX_SHAPE_DEPTH + 2):
        deep = {"next": deep}
    mixed = list(range(MAX_ARRAY_SHAPES + 2))
    # Distinct object keys make each item shape unique without logging the values.
    unique_items = [{f"variant-{i}": mixed[i]} for i in range(len(mixed))]
    body = {"type": "future-event", "data": {"wide": wide, "deep": deep, "items": unique_items}}
    event_logger = logging.getLogger("blue_bubbles_ingest.events")
    event_logger.addHandler(caplog.handler)
    try:
        response = client.post("/v1/webhooks/bluebubbles", json=body)
    finally:
        event_logger.removeHandler(caplog.handler)

    assert response.status_code == 204
    serialized = caplog.records[-1].message
    shape = json.loads(serialized)["dataShape"]["keys"]
    assert shape["wide"]["truncated"] == {"reason": "maxKeys", "omittedCount": 2}
    assert shape["items"]["truncated"] == "maxUniqueItemShapes"
    cursor = shape["deep"]
    while "keys" in cursor:
        cursor = cursor["keys"]["next"]
    assert cursor == {"type": "object", "truncated": "maxDepth"}
    assert "deep private value" not in serialized


def test_shape_hash_is_stable_across_object_key_order(caplog) -> None:
    event_logger = logging.getLogger("blue_bubbles_ingest.events")
    event_logger.addHandler(caplog.handler)
    try:
        first = client.post(
            "/v1/webhooks/bluebubbles",
            json={"type": "test", "data": {"second": [1, "private"], "first": True}},
        )
        second = client.post(
            "/v1/webhooks/bluebubbles",
            json={"type": "test", "data": {"first": False, "second": [2, "secret"]}},
        )
    finally:
        event_logger.removeHandler(caplog.handler)

    assert first.status_code == second.status_code == 204
    logs = [json.loads(record.message) for record in caplog.records[-2:]]
    assert logs[0]["dataShape"] == logs[1]["dataShape"]
    assert logs[0]["dataShapeHash"] == logs[1]["dataShapeHash"]
    assert "private" not in caplog.records[-2].message
    assert "secret" not in caplog.records[-1].message


def test_truncated_array_shape_hash_is_stable_across_item_order(caplog) -> None:
    items = [{f"variant-{i}": i} for i in range(MAX_ARRAY_SHAPES + 2)]
    event_logger = logging.getLogger("blue_bubbles_ingest.events")
    event_logger.addHandler(caplog.handler)
    try:
        first = client.post(
            "/v1/webhooks/bluebubbles", json={"type": "test", "data": items}
        )
        second = client.post(
            "/v1/webhooks/bluebubbles",
            json={"type": "test", "data": list(reversed(items))},
        )
    finally:
        event_logger.removeHandler(caplog.handler)

    assert first.status_code == second.status_code == 204
    logs = [json.loads(record.message) for record in caplog.records[-2:]]
    assert logs[0]["dataShape"] == logs[1]["dataShape"]
    assert logs[0]["dataShapeHash"] == logs[1]["dataShapeHash"]
