"""Immutable speaker snapshots are the unit of safe inference failover."""

import hashlib
import json

import starlette.responses as responses

READ_PATHS = {
    "/identify",
    "/identify/batch",
    "/diarize-and-identify",
    "/v1/diarize-identify-match",
    "/v1/reidentify-clusters",
    "/v1/embed-clusters",
    "/enrollment/candidates/score",
    "/enrollment/candidates/score-embeddings",
    "/enrollment/candidates/embed",
}


def fingerprint(rows, model):
    """Hash canonical enrollment records plus the embedding-space identity."""
    body = json.dumps(
        {"model": model, "speakers": sorted(rows, key=lambda r: r["id"])},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


class ReadOnlyCatalog:
    """Deny every unlisted mutation, including websocket mutations, on HA replicas."""

    def __init__(self, app, enabled):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if self.enabled and scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if self.enabled and scope["type"] == "http":
            safe = scope["method"] in ("GET", "HEAD", "OPTIONS") or (
                scope["method"] == "POST" and scope["path"] in READ_PATHS
            )
            if not safe:

                await responses.JSONResponse(
                    {"detail": "This speaker replica has a read-only catalog"},
                    status_code=409,
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)
