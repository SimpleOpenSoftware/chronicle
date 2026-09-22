"""Retain enrollment evidence policy across a gallery-dependent provider call."""

import inspect
import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

from backend.services import privacy

_current = ContextVar("speaker_gallery_privacy", default=None)
_processing_result = ContextVar("speaker_result_privacy", default=False)


class GalleryResult(dict):
    """Keep the checked gallery boundary with an in-process inference result.

    The scope is deliberately not a dictionary key: ordinary JSON responses must
    not expose enrollment evidence or internal catalog details.
    """

    def __init__(self, result, scope):
        super().__init__(result)
        self.gallery_scope = scope


def result_scope(result):
    if not isinstance(result, GalleryResult):
        raise privacy.PrivacyHeld()
    return result.gallery_scope


class GalleryLogFilter(logging.Filter):
    """Never retain names, provider bodies or recognition outputs in client logs."""

    def filter(self, record):
        return _current.get() is None and not _processing_result.get()


logging.getLogger("backend.workers.speaker_jobs").addFilter(GalleryLogFilter())


def protect_result_logs(function):
    """Recognition names must not outlive policy in an unfenced log sink."""

    @wraps(function)
    async def guarded(*args, **kwargs):
        token = _processing_result.set(True)
        try:
            return await function(*args, **kwargs)
        finally:
            _processing_result.reset(token)

    return guarded


class GalleryRead:
    def __init__(self, client, user_id=None, speaker_id=None, segment_id=None):
        self.client = client
        self.user_id = str(user_id) if user_id is not None else None
        self.speaker_id = speaker_id
        self.segment_id = segment_id
        self.visibility = privacy.ConversationPrivacyFilter()
        self.service_url = client.service_url
        self.catalog_id = None
        self.revision = None
        self.query = None
        self.identity = None
        self.failed = False
        self.operation_ids = ()

    async def _catalog(self, user_id=None):
        try:
            value = await self.client.enrollment_catalog(user_id=user_id)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Exception:
            raise privacy.PrivacyHeld() from None

    @staticmethod
    def _identity(rows):
        # Membership/state changes matter even if quarantine removed the last
        # problematic entry and the new catalog is now otherwise allowed.
        return sorted(
            (
                str(row["_id"]),
                row.get("state"),
                json.dumps(
                    {
                        key: row.get(key)
                        for key in (
                            "binding",
                            "audio_sha256",
                            "evidence_records",
                            "segment_id",
                        )
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
            for row in rows
        )

    async def start(self):
        collection = privacy.database().speaker_enrollment_operations
        catalog = await self._catalog()
        self.catalog_id = (
            catalog.get("catalog_id") if isinstance(catalog, dict) else None
        )
        if not isinstance(self.catalog_id, str) or len(self.catalog_id) != 32:
            raise privacy.PrivacyHeld()
        self.query = {"catalog_id": self.catalog_id, "state": {"$ne": "quarantined"}}
        if self.user_id is None and (
            self.speaker_id is not None or self.segment_id is not None
        ):
            target = {"catalog_id": self.catalog_id}
            if self.speaker_id is not None:
                target["binding.speaker_id"] = self.speaker_id
            if self.segment_id is not None:
                target["segment_id"] = self.segment_id
            owners = await collection.distinct("user_id", target)
            if len(owners) == 1:
                self.user_id = str(owners[0])
        if self.user_id is not None:
            self.query["user_id"] = self.user_id
            catalog = await self._catalog(user_id=self.user_id)
        if (
            catalog.get("catalog_id") != self.catalog_id
            or not isinstance(catalog.get("revision"), str)
            or len(catalog["revision"]) != 64
            or catalog.get("unverified_speaker_ids") != []
            or not isinstance(catalog.get("active_operation_ids"), list)
        ):
            raise privacy.PrivacyHeld()
        self.revision = catalog["revision"]
        rows = await collection.find(self.query).to_list(None)
        held = set(catalog.get("held_speaker_ids", []))
        if self.speaker_id in held or any(
            row.get("segment_id") == self.segment_id
            and self.segment_id is not None
            and row["binding"]["speaker_id"] in held
            for row in rows
        ):
            raise privacy.PrivacyHeld()
        available = [
            row for row in rows if row.get("binding", {}).get("speaker_id") not in held
        ]
        if set(catalog["active_operation_ids"]) != {
            row["_id"] for row in available if row.get("state") == "active"
        }:
            raise privacy.PrivacyHeld()
        self.identity = self._identity(rows)
        self.operation_ids = tuple(sorted(row["_id"] for row in available))
        # Completed clips are independent assets. Source records are provenance,
        # not ongoing admission dependencies; incomplete writes still fail closed.
        if any(row.get("state") != "active" for row in available):
            raise privacy.PrivacyHeld()
        await self.assert_current()

    async def assert_current(self):
        if self.failed or self.client.service_url != self.service_url:
            raise privacy.PrivacyHeld()
        catalog = await self._catalog(user_id=self.user_id)
        if (
            catalog.get("catalog_id") != self.catalog_id
            or catalog.get("revision") != self.revision
        ):
            raise privacy.PrivacyHeld()
        await self.visibility.assert_current()
        rows = (
            await privacy.database()
            .speaker_enrollment_operations.find(self.query)
            .to_list(None)
        )
        if self._identity(rows) != self.identity:
            raise privacy.PrivacyHeld()

    def receipt(self):
        """Durable dependencies for the immutable result and its projections."""
        if self.identity is None or self.failed:
            raise privacy.PrivacyHeld()
        return {
            "catalog_id": self.catalog_id,
            "gallery_revision": self.revision,
            "operation_ids": list(self.operation_ids),
            "user_id": self.user_id,
        }

    @asynccontextmanager
    async def publication(self, target_visibility):
        # One sorted set of locks avoids acquiring the same owner's lock twice.
        combined = privacy.ConversationPrivacyFilter()
        for visibility in (self.visibility, target_visibility):
            for owner, snapshot in visibility.snapshots.items():
                prior = combined.snapshots.get(owner)
                if prior is not None and prior.revisions != snapshot.revisions:
                    raise privacy.PrivacyHeld()
                combined.snapshots[owner] = snapshot
        async with combined.publication():
            await self.assert_current()
            yield
            await self.assert_current()


def guard_gallery(function):
    """Explicitly applied to gallery consumers; preparation/inference-only APIs differ."""
    signature = inspect.signature(function)

    @wraps(function)
    async def guarded(self, *args, **kwargs):
        if not self.enabled:
            return await function(self, *args, **kwargs)
        values = signature.bind(self, *args, **kwargs).arguments
        scope = GalleryRead(
            self,
            values.get("user_id"),
            values.get("speaker_id"),
            values.get("segment_id"),
        )
        token = _current.set(scope)
        try:
            await scope.start()
            result = await function(self, *args, **kwargs)
            await scope.assert_current()
            return GalleryResult(result, scope) if isinstance(result, dict) else result
        finally:
            _current.reset(token)

    return guarded


async def checked_response(response, method):
    """Check before body access and again before any content can be used/logged."""
    scope = _current.get()
    try:
        if scope is not None:
            await scope.assert_current()
            # Every read is pinned too; a gateway cannot switch catalogs mid-call.
            if (
                response.headers.get("X-Speaker-Catalog") != scope.catalog_id
                or response.headers.get("X-Speaker-Gallery-Revision") != scope.revision
            ):
                raise privacy.PrivacyHeld()
        result = await getattr(response, method)()
        if scope is not None:
            await scope.assert_current()
        return result
    except privacy.PrivacyHeld:
        if scope is not None:
            scope.failed = True
        raise


def request_headers(base):
    result = dict(base)
    scope = _current.get()
    if scope is not None:
        result.update(
            {
                "X-Speaker-Catalog": scope.catalog_id,
                "X-Speaker-Gallery-Revision": scope.revision,
            }
        )
        if scope.user_id is not None:
            result["X-Speaker-Gallery-User"] = scope.user_id
    return result
