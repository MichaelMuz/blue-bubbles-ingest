import hashlib
import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from blue_bubbles_ingest.app import MAX_BODY_BYTES, app

client = TestClient(app)
fixture_path = Path(__file__).parent / "fixtures" / "new-message.json"


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


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
    assert summary == {
        "eventType": "new-message",
        "messageGuidHash": hashlib.sha256(b"invented-message-guid-001").hexdigest()[:12],
        "isFromMe": False,
        "hasText": True,
        "dateCreated": 1712345678000,
        "attachmentCount": 1,
        "chatCount": 1,
        "chatGuidHashes": [hashlib.sha256(b"invented-chat-guid-001").hexdigest()[:12]],
    }
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
        "eventType": "new-message",
        "hasText": False,
        "attachmentCount": 0,
        "chatCount": 2,
    }
