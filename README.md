# blue-bubbles-ingest

A small HTTP receiver for proving BlueBubbles Server to Kubernetes delivery while observing event shape without exposing message data.

## Endpoints

- `GET /healthz` returns `{"status":"healthy"}`.
- `POST /v1/webhooks/bluebubbles` accepts BlueBubbles JSON envelopes up to 1 MiB and returns `204` after validation and safe logging.

Configure BlueBubbles Server 1.9.9 with the private HTTPRoute URL ending in `/v1/webhooks/bluebubbles`. Keep this single generic endpoint configured while exercising direct messages, group chats, attachments, reactions, and other event variants, then compare the shape hashes and inspect pod logs to learn their schemas. BlueBubbles cannot add a custom authentication header to webhook requests, so the endpoint is intended to remain private.

The accepted contract is pinned to BlueBubbles Server commit [`ba31cd1cf6c03e154c18ffa2cd6da47a934c3be1`](https://github.com/BlueBubblesApp/bluebubbles-server/tree/ba31cd1cf6c03e154c18ffa2cd6da47a934c3be1): Axios sends an `application/json` envelope with `type` and `data`. Only that envelope is validated, message fields remain forward-compatible.

## Privacy and v0 limits

Accepted requests produce one JSON log summary. Alongside the existing allowlisted fields and hashes, `dataShape` recursively records object keys and value types; arrays record their count and deduplicated item shapes. `dataShapeHash` provides a stable comparison key. Depth, object width, and unique array shapes are bounded with explicit truncation markers.

Logs expose structure, never primitive data values: no message text, addresses, raw GUIDs, attachment names, arbitrary strings or numbers, or request bodies. Use live pod logs only to compare event structures before designing event-specific handling.

This version has no persistence, queue, authentication, or retry. BlueBubbles delivery itself is fire-and-forget with no retry, so it is suitable for delivery validation, not guaranteed ingestion.

## Develop

Python 3.13 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --frozen
uv run pytest
uv run uvicorn blue_bubbles_ingest.app:app --host 127.0.0.1 --port 8080
```

Build and run the production image:

```sh
docker build -t blue-bubbles-ingest .
docker run --rm --read-only -p 8080:8080 blue-bubbles-ingest
```
