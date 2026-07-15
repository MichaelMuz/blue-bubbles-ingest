import hashlib
import json
import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

MAX_BODY_BYTES = 1024 * 1024
MAX_SHAPE_DEPTH = 12
MAX_SHAPE_KEYS = 100
MAX_ARRAY_SHAPES = 20
logger = logging.getLogger("blue_bubbles_ingest.events")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

app = FastAPI(title="BlueBubbles ingest", docs_url=None, redoc_url=None)


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _hash_prefix(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _shape(value: Any, depth: int = 0) -> dict[str, Any]:
    """Describe JSON structure without retaining primitive values."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "float"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, dict):
        if depth >= MAX_SHAPE_DEPTH:
            return {"type": "object", "truncated": "maxDepth"}

        string_keys = sorted(key for key in value if isinstance(key, str))
        visible_keys = string_keys[:MAX_SHAPE_KEYS]
        shape: dict[str, Any] = {
            "type": "object",
            "keys": {key: _shape(value[key], depth + 1) for key in visible_keys},
        }
        omitted = len(value) - len(visible_keys)
        if omitted:
            shape["truncated"] = {"reason": "maxKeys", "omittedCount": omitted}
        return shape
    if isinstance(value, (list, tuple)):
        if depth >= MAX_SHAPE_DEPTH:
            return {
                "type": "array",
                "count": len(value),
                "truncated": "maxDepth",
            }

        unique: dict[str, dict[str, Any]] = {}
        truncated = False
        for item in value:
            item_shape = _shape(item, depth + 1)
            canonical = json.dumps(item_shape, sort_keys=True, separators=(",", ":"))
            if canonical in unique:
                continue
            if len(unique) < MAX_ARRAY_SHAPES:
                unique[canonical] = item_shape
            else:
                truncated = True
                largest = max(unique)
                if canonical < largest:
                    del unique[largest]
                    unique[canonical] = item_shape
        shape = {
            "type": "array",
            "count": len(value),
            "itemShapes": [unique[key] for key in sorted(unique)],
        }
        if truncated:
            shape["truncated"] = "maxUniqueItemShapes"
        return shape
    return {"type": "unknown"}


def _shape_log_fields(data: Any) -> dict[str, Any]:
    shape = _shape(data)
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return {
        "dataShape": shape,
        "dataShapeHash": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _safe_summary(event_type: str, data: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"eventType": event_type}
    if not isinstance(data, dict):
        return summary

    message_hash = _hash_prefix(data.get("guid"))
    if message_hash is not None:
        summary["messageGuidHash"] = message_hash

    if isinstance(data.get("isFromMe"), bool):
        summary["isFromMe"] = data["isFromMe"]
    summary["hasText"] = isinstance(data.get("text"), str) and bool(data["text"])

    date_created = data.get("dateCreated")
    if isinstance(date_created, (str, int, float)) and not isinstance(date_created, bool):
        summary["dateCreated"] = date_created

    attachments = data.get("attachments")
    summary["attachmentCount"] = len(attachments) if isinstance(attachments, list) else 0

    chats = data.get("chats")
    if isinstance(chats, list):
        summary["chatCount"] = len(chats)
        chat_hashes = [
            digest
            for chat in chats
            if isinstance(chat, dict)
            and (digest := _hash_prefix(chat.get("guid"))) is not None
        ]
        if chat_hashes:
            summary["chatGuidHashes"] = chat_hashes
    else:
        summary["chatCount"] = 0

    return summary


async def _bounded_body(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/v1/webhooks/bluebubbles")
async def receive_webhook(request: Request) -> Response:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _error(415, "content type must be application/json")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return _error(413, "request body exceeds 1 MiB")
        except ValueError:
            return _error(400, "invalid Content-Length header")

    body = await _bounded_body(request)
    if body is None:
        return _error(413, "request body exceeds 1 MiB")
    try:
        envelope = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(400, "malformed JSON")

    if not isinstance(envelope, dict):
        return _error(422, "body must be a JSON object")
    event_type = envelope.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        return _error(422, "type must be a non-empty string")
    if "data" not in envelope:
        return _error(422, "data field is required")

    log_entry = _safe_summary(event_type, envelope["data"])
    log_entry.update(_shape_log_fields(envelope["data"]))
    logger.info(json.dumps(log_entry, sort_keys=True, separators=(",", ":")))
    return Response(status_code=204)
