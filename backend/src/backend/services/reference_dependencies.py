"""Immutable source/time receipts for results influenced by other recordings.

Only metadata is journaled. A later rewrite of the source Conversation cannot
retarget the original audio, and an exclusion is checked again on every read.
"""

import hashlib
import json
import re

from backend.services import privacy


def receipts(row):
    metadata = row.get("metadata") or {}
    recognition = metadata.get("speaker_recognition")
    if isinstance(recognition, dict) and recognition.get("enabled") is not False:
        yield recognition.get("privacy_reference_receipt")
    configuration = row.get("configuration") or {}
    if "privacy_gallery_receipt" in configuration:
        yield configuration.get("privacy_reference_receipt")
    for container in (row, metadata, configuration, row.get("raw_response") or {}):
        if "privacy_reference_receipt" in container:
            yield container["privacy_reference_receipt"]
    if "background_similarity" in row and "segment_start" in row:
        yield row.get("privacy_reference_receipt")
    for version in row.get("transcript_versions") or []:
        yield from receipts(
            version if isinstance(version, dict) else version.model_dump()
        )


def identity(row):
    return hashlib.sha256(
        json.dumps(
            {"user_id": row["user_id"], "evidence_records": row["evidence_records"]},
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: privacy.utc(value).isoformat(),
        ).encode()
    ).hexdigest()


async def seal(user_id, records):
    """Deduplicate each original record rather than copying the whole reference set."""
    identifiers = set()
    for record in records:
        original = {key: value for key, value in record.items() if key != "_id"}
        if not original.get("user_id") or not any(
            original.get(key)
            for key in ("conversation_id", "episode_id", "source_item_id")
        ):
            raise privacy.PrivacyHeld()
        row = {"user_id": str(user_id), "evidence_records": [original]}
        identifier = identity(row)
        await privacy.database().privacy_reference_dependencies.update_one(
            {"_id": identifier},
            {"$setOnInsert": row},
            upsert=True,
        )
        stored = await privacy.database().privacy_reference_dependencies.find_one(
            {"_id": identifier}
        )
        if stored is None or identity(stored) != identifier:
            raise privacy.PrivacyHeld()
        identifiers.add(identifier)
    return sorted(identifiers)


class ReferenceDependencies:
    def __init__(self, rows):
        self.rows = {row["_id"]: row for row in rows}
        self.used = set()
        self.visiting = set()

    def permits(self, row, owner, state):
        return all(
            self._receipt_allowed(receipt, owner, state) for receipt in receipts(row)
        )

    def _receipt_allowed(self, receipt, owner, state):
        if not isinstance(receipt, list) or not all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
            for value in receipt
        ):
            return False
        for identifier in receipt:
            self.used.add(identifier)
            row = self.rows.get(identifier)
            if not row or row.get("user_id") != owner or identifier in self.visiting:
                return False
            try:
                if identity(row) != identifier or not row.get("evidence_records"):
                    return False
            except (KeyError, TypeError, ValueError, AttributeError):
                return False
            self.visiting.add(identifier)
            try:
                for original in row["evidence_records"]:
                    evidence_owner = str(original.get("user_id") or "")
                    state._used_owners.add(evidence_owner)
                    snapshot = state.snapshots.get(evidence_owner)
                    if snapshot is None or not snapshot.permits_record(original):
                        return False
            finally:
                self.visiting.remove(identifier)
        return True

    async def assert_current(self):
        if not self.used:
            return
        current = (
            await privacy.database()
            .privacy_reference_dependencies.find({"_id": {"$in": sorted(self.used)}})
            .to_list(None)
        )
        if {row["_id"] for row in current} != self.used:
            raise privacy.PrivacyHeld()
        for row in current:
            try:
                if identity(row) != row["_id"]:
                    raise privacy.PrivacyHeld()
            except (KeyError, TypeError, ValueError, AttributeError):
                raise privacy.PrivacyHeld() from None
