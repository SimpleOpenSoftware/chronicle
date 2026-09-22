"""Keep gallery-derived HTTP results behind the catalog revision they used."""

import asyncio
import logging
import tempfile
from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import JSONResponse

from simple_speaker_recognition.core.gallery_catalog import (
    catalog_snapshot,
    target_owner,
)

READ_POSTS = {
    "/identify",
    "/identify/batch",
    "/diarize-and-identify",
    "/v1/diarize-identify-match",
    "/v1/reidentify-clusters",
    "/enrollment/candidates/score",
    "/enrollment/candidates/score-embeddings",
}

_protected_request = ContextVar("gallery_response_privacy", default=False)


class GalleryRequestLogFilter(logging.Filter):
    def filter(self, record):
        return not _protected_request.get()


for logger_name in (
    "speaker_service",
    "simple_speaker_recognition.core.audio_backend",
    "simple_speaker_recognition.core.unified_speaker_db",
    "uvicorn.access",
):
    logging.getLogger(logger_name).addFilter(GalleryRequestLogFilter())


async def get_gallery():
    # Defer this dependency to break the import cycle through
    # simple_speaker_recognition.api.service -> simple_speaker_recognition.api.gallery_fence.
    from . import service

    return await service.get_db()


class GalleryRevisionFence:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        protected = scope["type"] == "http" and (
            (scope["method"] == "POST" and path in READ_POSTS)
            or (
                scope["method"] == "GET"
                and (
                    path.startswith("/speakers")
                    or path == "/enrollment/health"
                    or path.startswith("/enrollment/segments/")
                )
            )
        )
        if not protected:
            return await self.app(scope, receive, send)
        token = _protected_request.set(True)
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as request_body:
                return await self._protected(scope, receive, send, request_body)
        finally:
            _protected_request.reset(token)

    async def _protected(self, scope, receive, send, request_body):
        headers = dict(scope.get("headers", []))
        original_receive = receive

        async def record_receive():
            message = await original_receive()
            if message["type"] == "http.request":
                request_body.write(message.get("body", b""))
            return message

        # Scope comes from the actual endpoint arguments, never from the caller's
        # revision header alone. Preserve exact multipart bytes for FastAPI.
        request = Request(scope, record_receive)
        owner = request.query_params.get("user_id")
        speaker_id = None
        try:
            if scope["method"] == "POST":
                if "application/json" in request.headers.get("content-type", ""):
                    fields = await request.json()
                    if not isinstance(fields, dict):
                        raise ValueError()
                elif "multipart/form-data" in request.headers.get(
                    "content-type", ""
                ) or "application/x-www-form-urlencoded" in request.headers.get(
                    "content-type", ""
                ):
                    form = await request.form()
                    try:
                        fields = {
                            key: form.get(key) for key in ("user_id", "speaker_id")
                        }
                    finally:
                        await form.close()
                else:
                    fields = {}
                    await request.body()
                actual = fields.get("user_id")
                if actual is not None:
                    if owner is not None and owner != str(actual):
                        raise ValueError()
                    owner = str(actual)
                speaker_id = fields.get("speaker_id")
                request_body.seek(0)
                ended = False

                async def replay_receive():
                    nonlocal ended
                    if not ended:
                        chunk = request_body.read(65536)
                        ended = not chunk
                        return {
                            "type": "http.request",
                            "body": chunk,
                            "more_body": not ended,
                        }
                    return await original_receive()

                receive = replay_receive
            parts = scope["path"].strip("/").split("/")
            segment_id = None
            if len(parts) >= 3 and parts[:2] == ["enrollment", "segments"]:
                segment_id = int(parts[2])
            elif len(parts) >= 3 and parts[0] == "speakers" and parts[2] == "audio":
                speaker_id = parts[1]
            if speaker_id is not None or segment_id is not None:
                target = await asyncio.to_thread(target_owner, speaker_id, segment_id)
                if target is not None:
                    if owner is not None and owner != target:
                        raise ValueError()
                    owner = target
            expected_owner = headers.get(b"x-speaker-gallery-user")
            if expected_owner is not None and expected_owner.decode() != owner:
                raise ValueError()
        except Exception:
            return await JSONResponse(
                {"detail": "Gallery request scope unavailable"}, status_code=423
            )(scope, receive, send)
        try:
            original = await asyncio.to_thread(catalog_snapshot, owner)
            expected = headers.get(b"x-speaker-catalog")
            revision = headers.get(b"x-speaker-gallery-revision")
            if (
                original["unverified_speaker_ids"]
                or (expected and expected.decode() != original["catalog_id"])
                or (revision and revision.decode() != original["revision"])
            ):
                raise RuntimeError()
        except Exception:
            return await JSONResponse(
                {"detail": "Speaker gallery held for privacy review"}, status_code=423
            )(scope, receive, send)

        # Delay headers and body until inference finishes and its gallery is
        # rechecked. Large audio bodies spill to a private, auto-deleted file.
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
            start = None

            async def collect(message):
                nonlocal start
                if message["type"] == "http.response.start":
                    start = message
                elif message["type"] == "http.response.body":
                    body.write(message.get("body", b""))

            inner_scope = dict(scope)
            inner_scope["extensions"] = {
                key: value
                for key, value in scope.get("extensions", {}).items()
                if key not in {"http.response.pathsend", "http.response.zerocopysend"}
            }
            await self.app(inner_scope, receive, collect)
            # Gallery writes use this same lock. Once the response is approved,
            # quarantine cannot race its final delivery.
            gallery = await get_gallery()
            async with gallery._lock:
                current = await asyncio.to_thread(catalog_snapshot, owner)
                if start is None or current != original:
                    return await JSONResponse(
                        {"detail": "Speaker gallery changed during processing"},
                        status_code=423,
                    )(scope, receive, send)
                start = dict(start)
                start["headers"] = list(start.get("headers", [])) + [
                    (b"x-speaker-catalog", original["catalog_id"].encode()),
                    (b"x-speaker-gallery-revision", original["revision"].encode()),
                ]
                await send(start)
                body.seek(0)
                while chunk := body.read(65536):
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )
