"""Evaluate saved recognition results against independent enrollment assets.

The durable enrollment journal is the authority after a provider response has
returned. No model, provider HTTP call, audio, or transcript text is needed here.
"""

import hashlib
import json
import re
import weakref

from backend.services import privacy
from backend.services.reference_dependencies import ReferenceDependencies
from backend.services.reference_dependencies import receipts as reference_receipts


def _fingerprint(rows):
    fields = (
        "_id",
        "state",
        "user_id",
        "catalog_id",
        "binding",
        "evidence_records",
        "conversation_ids",
    )
    body = [
        {key: row.get(key) for key in fields}
        for row in sorted(rows, key=lambda r: r["_id"])
    ]
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()


def _receipts(row):
    """Yield missing proof too: an old mixed projection must stay held."""
    metadata = row.get("metadata") or {}
    recognition = metadata.get("speaker_recognition")
    if "speaker_recognition" in metadata:
        if not isinstance(recognition, dict):
            yield None
        elif recognition.get("enabled") is not False:
            yield recognition.get("privacy_gallery_receipt")
    for container in (row, row.get("configuration") or {}):
        if "privacy_gallery_receipt" in container:
            yield container["privacy_gallery_receipt"]
    for version in row.get("transcript_versions") or []:
        if not isinstance(version, dict):
            version = version.model_dump()
        yield from _receipts(version)


class GalleryDependencies:
    def __init__(self, rows, snapshots, reference_rows=()):
        self.references = ReferenceDependencies(reference_rows)
        self.rows = {row["_id"]: row for row in rows}
        self.snapshots = snapshots
        self._visiting = set()
        self._used = set()
        self._used_owners = set()

    @property
    def publication_owners(self):
        return frozenset(self._used_owners)

    def admission_checkpoint(self):
        return (
            self._used.copy(),
            self._used_owners.copy(),
            self.references.used.copy(),
            {
                owner: snapshot.admission_checkpoint()
                for owner, snapshot in self.snapshots.items()
            },
        )

    def restore_admission(self, checkpoint):
        self._used, self._used_owners, self.references.used, snapshots = checkpoint
        for owner, state in snapshots.items():
            self.snapshots[owner].restore_admission(state)

    def permits(self, row, owner):
        return self.references.permits(row, owner, self) and all(
            self._receipt_allowed(receipt, owner) for receipt in _receipts(row)
        )

    def _receipt_allowed(self, receipt, owner):
        if not isinstance(receipt, dict) or set(receipt) != {
            "catalog_id",
            "gallery_revision",
            "operation_ids",
            "user_id",
        }:
            return False
        if receipt["user_id"] != owner:
            return False
        for key, size in (("catalog_id", 32), ("gallery_revision", 64)):
            if not isinstance(receipt[key], str) or not re.fullmatch(
                r"[a-f0-9]{%d}" % size, receipt[key]
            ):
                return False
        identifiers = receipt["operation_ids"]
        if not isinstance(identifiers, list) or not all(
            isinstance(value, str) for value in identifiers
        ):
            return False
        return all(
            self._operation_allowed(identifier, receipt) for identifier in identifiers
        )

    def _operation_allowed(self, identifier, receipt):
        self._used.add(identifier)
        row = self.rows.get(identifier)
        # Enrollment provenance is descriptive after activation. Explicit
        # enrollment quarantine still invalidates results that used the asset.
        return bool(
            row
            and row.get("state") == "active"
            and row.get("catalog_id") == receipt["catalog_id"]
            and row.get("user_id") == receipt["user_id"]
        )

    async def assert_current(self):
        await self.references.assert_current()
        for owner, snapshot in self.snapshots.items():
            if owner in self._used_owners:
                await privacy._assert_capture_current(owner, snapshot)
        if not self._used:
            return
        query = {"_id": {"$in": sorted(self._used)}}
        rows = (
            await privacy.database()
            .speaker_enrollment_operations.find(query)
            .to_list(None)
        )
        expected = _fingerprint(
            [row for identifier, row in self.rows.items() if identifier in self._used]
        )
        if _fingerprint(rows) != expected:
            raise privacy.PrivacyHeld()


async def load_dependencies(owner, full_policies):
    """Follow original evidence owners without recursion through snapshot loading."""
    owners, pending, rows, reference_rows = set(), {str(owner)}, [], []
    while pending:
        batch = pending - owners
        if not batch:
            break
        owners.update(batch)
        found = (
            await privacy.database()
            .speaker_enrollment_operations.find({"user_id": {"$in": sorted(batch)}})
            .to_list(None)
        )
        rows.extend(found)
        references = (
            await privacy.database()
            .privacy_reference_dependencies.find({"user_id": {"$in": sorted(batch)}})
            .to_list(None)
        )
        reference_rows.extend(references)
        pending = {
            str(record["user_id"])
            for row in references
            for record in row.get("evidence_records") or []
            if record.get("user_id")
        }
    # Enrollment audio can predate the displayed window. Reuse only a full
    # policy from this same request, with independent admission bookkeeping.
    # Bounded reads and other owners still require their full policy loads.
    evidence_owners = {
        str(record["user_id"])
        for row in reference_rows
        for record in row.get("evidence_records") or []
        if record.get("user_id")
    }
    snapshots = {
        value: (
            full_policies[value].fresh_capture_admission()
            if value in full_policies
            else await privacy._load_capture_snapshot(value)
        )
        for value in sorted(evidence_owners)
    }
    state = GalleryDependencies(rows, snapshots, reference_rows)
    for value, snapshot in snapshots.items():
        snapshot.owner = value
        # The request owns state, which owns these dependency snapshots. Their
        # back-reference must not keep entire historical policies alive until a
        # generation-two garbage collection pauses concurrent capture work.
        snapshot.gallery_dependencies = weakref.proxy(state)
    # Loading the dependency inventory is not admission. Validate only receipts
    # actually followed by permits(), before the caller consumes or publishes
    # their result. Unused references must not make another device unavailable.
    return state


def permits_without_dependency_state(row):
    # Directly constructed snapshots have no journal proof available.
    return not any(True for _ in _receipts(row)) and not any(
        True for _ in reference_receipts(row)
    )
