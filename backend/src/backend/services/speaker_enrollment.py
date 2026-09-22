"""Durable enrollment ownership across Chronicle and the speaker catalog.

The journal precedes every external write. Only a privacy-fenced activation can
finish an enrollment. Any uncertain outcome remains recoverable and must not be
treated as an allowed gallery update.
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import backend.speaker_recognition_client as speaker_recognition_client
from backend.services import privacy
from backend.services.redis_lock import distributed_lock


class EnrollmentUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Speaker enrollment is held pending recovery")


def _now():
    return privacy.utc(datetime.now(timezone.utc))


def _journal():
    return privacy.database().speaker_enrollment_operations


def _evidence_hash(records):
    return hashlib.sha256(
        json.dumps(
            sorted(records, key=lambda row: row["conversation_id"]),
            sort_keys=True,
            default=lambda value: privacy.utc(value).isoformat(),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def capture_evidence(visibility, conversation_ids):
    """Pin source/time metadata before decoding a mutable conversation claim."""
    identifiers = sorted(set(conversation_ids))
    references = [{"conversation_id": i} for i in identifiers]
    if not identifiers or len(await visibility.filter(references)) != len(references):
        raise privacy.PrivacyHeld()
    records = (
        await privacy.database()
        .conversations.find(
            {"conversation_id": {"$in": identifiers}},
            {**privacy._RECORD_PROJECTION, "_id": 0},
        )
        .to_list(None)
    )
    if len(records) != len(identifiers):
        raise privacy.PrivacyHeld()
    for record in records:
        snapshot = visibility.snapshots.get(str(record.get("user_id")))
        if snapshot is None or not snapshot.permits_record(record):
            raise privacy.PrivacyHeld()
    await visibility.assert_current()
    return records


def _lock(operation_id):
    return distributed_lock(
        f"speaker:enrollment:{operation_id}",
        timeout=180,
        blocking_timeout=5,
        renew=True,
    )


def _check_reply(reply, operation_id, state):
    if (
        not isinstance(reply, dict)
        or reply.get("operation_id") != operation_id
        or reply.get("state") != state
    ):
        raise EnrollmentUnavailable()
    if state == "active" and (
        type(reply.get("segment_id")) is not int or reply["segment_id"] <= 0
    ):
        raise EnrollmentUnavailable()


async def _quarantine(row, client):
    collection = _journal()
    await collection.update_one(
        {"_id": row["_id"], "state": {"$ne": "quarantined"}},
        {"$set": {"state": "quarantine_pending", "updated_at": _now()}},
    )
    reply = await client.enrollment_operation(
        "quarantine",
        row["_id"],
        row["binding"],
        catalog_id=row["catalog_id"],
        service_url=row["service_url"],
    )
    _check_reply(reply, row["_id"], "quarantined")
    await collection.update_one(
        {"_id": row["_id"]},
        {"$set": {"state": "quarantined", "updated_at": _now(), "lease_until": None}},
    )


async def enroll(
    client,
    *,
    speaker_name,
    speaker_id,
    audio_data,
    user_id,
    conversation_ids=(),
    visibility=None,
    evidence_records=None,
):
    """Prepare and activate one exact evidence-bound clip, or retain a hold."""
    identifiers = sorted(set(conversation_ids))
    if not all(isinstance(i, str) and i for i in identifiers):
        raise privacy.PrivacyHeld()
    visibility = visibility or privacy.ConversationPrivacyFilter()
    evidence_records = evidence_records or []
    if identifiers:
        current_records = await capture_evidence(visibility, identifiers)
        if not evidence_records or _evidence_hash(current_records) != _evidence_hash(
            evidence_records
        ):
            raise privacy.PrivacyHeld()
    elif evidence_records:
        raise ValueError("Source evidence requires conversation identifiers")
    receipt = visibility.revision_receipt()
    identity = {
        "user_id": str(user_id),
        "speaker_name": speaker_name,
        "conversation_ids": identifiers,
        "privacy_revisions": receipt,
        "audio_sha256": hashlib.sha256(audio_data).hexdigest(),
        "capture_hash": _evidence_hash(evidence_records),
    }
    operation_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    # Policy is evaluated afresh; identical allowed audio need not create another
    # provider contribution merely because an unrelated capture advanced policy.
    content_key = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in identity.items()
                if key != "privacy_revisions"
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    async with _lock(content_key):
        collection = _journal()
        row = await collection.find_one({"content_key": content_key, "state": "active"})
        if row:
            operation_id = row["_id"]
        else:
            row = await collection.find_one({"_id": operation_id})
        if row and row["state"] in {"quarantined", "quarantine_pending"}:
            raise EnrollmentUnavailable()
        already_active = bool(row and row["state"] == "active")
        if row is None:
            catalog = await client.enrollment_catalog()
            catalog_id = (
                catalog.get("catalog_id") if isinstance(catalog, dict) else None
            )
            if not isinstance(catalog_id, str) or not re.fullmatch(
                r"[a-f0-9]{32}", catalog_id
            ):
                raise EnrollmentUnavailable()
            bound = {
                "user_id": str(user_id),
                "speaker_name": speaker_name,
                "speaker_id": speaker_id or f"speaker_{operation_id}",
                "mode": "append" if speaker_id else "create",
                "evidence": (
                    {
                        "conversation_ids": identifiers,
                        "privacy_revisions": receipt,
                        "capture_hash": identity["capture_hash"],
                    }
                    if identifiers
                    else None
                ),
            }
            row = {
                "_id": operation_id,
                "content_key": content_key,
                "user_id": str(user_id),
                "evidence_owner_ids": sorted(receipt),
                "conversation_ids": identifiers,
                "evidence_records": evidence_records,
                "binding": bound,
                "audio_sha256": identity["audio_sha256"],
                "catalog_id": catalog_id,
                "service_url": client.service_url,
                "state": "preparing",
                "created_at": _now(),
                "updated_at": _now(),
                "lease_until": _now() + timedelta(minutes=5),
            }
            await collection.insert_one(row)
        try:
            await visibility.assert_current()
            if not already_active:
                prepared = await client.enrollment_operation(
                    "prepare",
                    operation_id,
                    row["binding"],
                    audio_data=audio_data,
                    catalog_id=row["catalog_id"],
                    service_url=row["service_url"],
                )
                # A lost activation response can leave the provider already active.
                # The same evidence and catalog remain bound; replay activation below
                # confirms the exact segment under the original publication fence.
                if prepared.get("state") not in {"prepared", "active"}:
                    raise EnrollmentUnavailable()
                _check_reply(prepared, operation_id, prepared["state"])
                await collection.update_one(
                    {"_id": operation_id},
                    {"$set": {"state": "activating", "updated_at": _now()}},
                )
            async with visibility.publication():
                reply = await client.enrollment_operation(
                    "activate",
                    operation_id,
                    row["binding"],
                    catalog_id=row["catalog_id"],
                    service_url=row["service_url"],
                )
                _check_reply(reply, operation_id, "active")
                if reply.get("speaker_id") != row["binding"]["speaker_id"]:
                    raise EnrollmentUnavailable()
                await visibility.assert_current()
                await collection.update_one(
                    {"_id": operation_id},
                    {
                        "$set": {
                            "state": "active",
                            "segment_id": reply["segment_id"],
                            "updated_at": _now(),
                            "lease_until": None,
                        }
                    },
                )
            await visibility.assert_current()
            return {
                "status": "already_enrolled" if already_active else "enrolled",
                "speaker_id": reply["speaker_id"],
                "operation_id": operation_id,
            }
        except BaseException:
            # A second cancellation or an unavailable dependency can interrupt
            # compensation. The pre-existing nonterminal row still records the
            # uncertain write, and recovery will retry the original catalog.
            try:
                await _quarantine(row, client)
            except BaseException:
                pass
            raise


async def recover_speaker_enrollments():
    """Registered recovery entry point; no audio reads or implicit activations."""

    collection = _journal()
    await collection.create_index([("state", 1), ("lease_until", 1)])
    await collection.create_index([("user_id", 1), ("state", 1)])
    await collection.create_index([("content_key", 1), ("state", 1)])
    stats = {"checked": 0, "quarantined": 0, "pending": 0}
    client = speaker_recognition_client.SpeakerRecognitionClient()
    cursor = collection.find({"state": {"$ne": "quarantined"}}).sort("_id", 1)
    async for candidate in cursor:
        if (
            candidate["state"] not in {"active", "quarantine_pending"}
            and candidate.get("lease_until")
            and privacy.utc(candidate["lease_until"]) > _now()
        ):
            continue
        try:
            async with _lock(candidate["content_key"]):
                row = await collection.find_one({"_id": candidate["_id"]})
                if row is None or row["state"] == "quarantined":
                    continue
                stats["checked"] += 1
                if row["state"] == "active":
                    continue
                await _quarantine(row, client)
                stats["quarantined"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Content and provider exception text never enter cron logs/results.
            stats["pending"] += 1
    return stats
