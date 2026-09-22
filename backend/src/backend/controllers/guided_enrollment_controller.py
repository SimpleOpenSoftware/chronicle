"""Guided speaker enrollment — active-learning clip selection for one speaker.

The user picks an enrolled speaker; we scan the corpus for candidate segments,
score a shortlist against the speaker's gallery on the speaker service, and
serve small batches (3-5) of the most *informative* clips for a human yes/no.
Accepted clips are appended to the speaker's voiceprint; every decision is
recorded in the ``enrollment_reviews`` collection so a clip is never re-shown.

Selection follows the enrollment literature: total net speech helps up to
~30-60s then flattens; clips from *different* sessions/acoustic conditions beat
more-of-the-same; and human confirmation adds the most on clips the system is
uncertain about. Ranking therefore combines
  * novelty  — 1 - max cosine to the speaker's existing per-clip gallery,
  * uncertainty — proximity of the centroid cosine to the operating threshold
    (confirmed "hard positives" expand coverage the most),
  * duration — capped so one long clip can't dominate,
with a plausibility gate (too-low cosine, or a different speaker scoring
clearly higher, wastes the user's time) and ≤2 clips per conversation for
session diversity.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi.responses import JSONResponse
from rq.exceptions import NoSuchJobError
from rq.job import Job

from backend.config import get_diarization_settings
from backend.constants import is_non_enrollable_speaker
from backend.controllers import data_audit_controller
from backend.controllers.queue_controller import JOB_RESULT_TTL, default_queue
from backend.models.annotation import Annotation, AnnotationType
from backend.models.conversation import Conversation
from backend.services import privacy
from backend.services.speaker_enrollment import capture_evidence
from backend.speaker_recognition_client import SpeakerRecognitionClient
from backend.users import User
from backend.utils.audio_chunk_utils import reconstruct_audio_segment
from backend.workers.speaker_benchmark_jobs import run_speaker_benchmark_job
from backend.workers.speaker_discovery_jobs import discover_speaker_candidates_job
from backend.workers.unknown_speaker_jobs import discover_unknown_speakers_job

logger = logging.getLogger(__name__)

MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 30.0
MIN_PLAUSIBLE_SIM = 0.30
OTHER_SPEAKER_MARGIN = 0.07
UNCERTAINTY_BAND = 0.25
MAX_PER_CONVERSATION = 2
SCORE_CONCURRENCY = 4

W_NOVELTY, W_UNCERTAINTY, W_DURATION = 0.40, 0.35, 0.25


async def _require_recordings(rows, visibility=None):
    """Keep the original evidence owners' policies across asynchronous work."""
    visibility = visibility or privacy.ConversationPrivacyFilter()
    if any(not row.get("conversation_id") for row in rows):
        raise privacy.PrivacyHeld()
    if len(await visibility.filter(rows)) != len(rows):
        raise privacy.PrivacyHeld()
    await visibility.assert_current()
    return visibility


async def _require_cluster(cluster, visibility=None):
    """A cluster depends on the corpus used to form it, including other groups."""
    visibility = visibility or privacy.ConversationPrivacyFilter()
    identifiers = cluster.get("evidence_conversation_ids")
    members = cluster.get("members")
    if (
        not identifiers
        or not members
        or not {member.get("conversation_id") for member in members} <= set(identifiers)
    ):
        raise privacy.PrivacyHeld()
    await visibility.require_receipt(cluster.get("privacy_revisions"))
    return await _require_recordings(
        [{"conversation_id": identifier} for identifier in identifiers], visibility
    )


def _reviews_collection():
    return Conversation.get_pymongo_collection().database["enrollment_reviews"]


def _batches_collection():
    return Conversation.get_pymongo_collection().database["enrollment_batches"]


def _discovery_collection():
    return Conversation.get_pymongo_collection().database["speaker_corpus_matches"]


def _discovery_runs_collection():
    return Conversation.get_pymongo_collection().database["speaker_discovery_runs"]


def _unknown_clusters_collection():
    return Conversation.get_pymongo_collection().database["unknown_speaker_clusters"]


def _unknown_runs_collection():
    return Conversation.get_pymongo_collection().database["unknown_speaker_runs"]


async def enqueue_unknown_discovery(user: User):
    """Start or reattach to corpus-wide unknown-identity clustering."""
    user_id = str(user.user_id)
    existing = await _unknown_runs_collection().find_one({"requested_by": user_id})
    status = _job_status(existing.get("job_id") if existing else None)
    if status in {"queued", "started", "deferred", "scheduled"}:
        return {"job_id": existing["job_id"], "status": status, "reused": True}
    job = default_queue.enqueue(
        discover_unknown_speakers_job,
        requested_by=user_id,
        job_timeout=14400,
        result_ttl=JOB_RESULT_TTL,
        description="Discover unknown speakers across corpus",
    )
    await _unknown_runs_collection().update_one(
        {"requested_by": user_id},
        {"$set": {"job_id": job.id, "queued_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return {"job_id": job.id, "status": "queued", "reused": False}


async def list_unknown_clusters(user: User, limit: int = 50):
    cursor = (
        _unknown_clusters_collection()
        .find({"requested_by": str(user.user_id), "status": "pending"}, {"_id": 0})
        .sort("segment_count", -1)
        .limit(limit)
    )
    rows = await cursor.to_list(length=limit)
    visible, checks = [], []
    for row in rows:
        visibility = privacy.ConversationPrivacyFilter()
        checks.append(visibility)
        try:
            await _require_cluster(row, visibility)
        except privacy.PrivacyHeld:
            continue
        visible.append(
            {
                key: value
                for key, value in row.items()
                if key not in {"privacy_revisions", "evidence_conversation_ids"}
            }
        )
    for visibility in checks:
        await visibility.assert_current()
    return {"clusters": visible}


async def decide_unknown_cluster(
    user: User,
    cluster_id: str,
    run_fingerprint: str,
    action: str,
    speaker_name: Optional[str],
    accepted_identity_keys: list[str],
    enrollment_clips: list[dict],
):
    """Relabel accepted members and enroll only explicitly selected clips."""
    collection = _unknown_clusters_collection()
    cluster = await collection.find_one(
        {
            "requested_by": str(user.user_id),
            "cluster_id": cluster_id,
            "run_fingerprint": run_fingerprint,
            "status": "pending",
        }
    )
    if not cluster:
        return JSONResponse(status_code=409, content={"error": "Cluster is stale"})
    if action == "dismiss":
        await collection.update_one(
            {"_id": cluster["_id"]},
            {"$set": {"status": "dismissed", "decided_at": datetime.now(timezone.utc)}},
        )
        return {"status": "dismissed"}
    visibility = await _require_cluster(cluster)
    if not speaker_name or is_non_enrollable_speaker(speaker_name):
        return JSONResponse(
            status_code=422, content={"error": "A real speaker name is required"}
        )

    accepted = {
        member["identity_key"]: member
        for member in cluster["members"]
        if member["identity_key"] in set(accepted_identity_keys)
    }
    if not accepted:
        return JSONResponse(
            status_code=422, content={"error": "Select at least one identity"}
        )
    if not enrollment_clips:
        return JSONResponse(
            status_code=422, content={"error": "Select at least one enrollment clip"}
        )

    validated_segments = []
    for member in accepted.values():
        conversation = await Conversation.find_one(
            Conversation.conversation_id == member["conversation_id"]
        )
        if not conversation or conversation.user_id != str(user.user_id):
            return JSONResponse(
                status_code=409,
                content={"error": "Conversation is no longer available"},
            )
        segments = _active_segments(conversation.model_dump())
        for ref in member["segments"]:
            index = ref["segment_index"]
            if (
                index >= len(segments)
                or abs(float(segments[index]["start"]) - ref["start"]) > 0.01
                or not is_non_enrollable_speaker(
                    segments[index].get("identified_as")
                    or segments[index].get("speaker")
                )
                or (
                    segments[index].get("identified_as")
                    or segments[index].get("speaker")
                )
                != member["local_label"]
            ):
                return JSONResponse(
                    status_code=409, content={"error": "Transcript changed since scan"}
                )
            validated_segments.append((member, ref, index, segments[index]))

    annotations = 0
    async with visibility.publication():
        for member, ref, index, segment in validated_segments:
            await Annotation(
                annotation_type=AnnotationType.DIARIZATION,
                user_id=str(user.user_id),
                conversation_id=member["conversation_id"],
                segment_index=index,
                original_speaker=segment.get("speaker") or "",
                corrected_speaker=speaker_name,
                segment_start_time=ref["start"],
                processed=False,
            ).insert()
            annotations += 1

    client = SpeakerRecognitionClient()
    existing_speaker = await client.get_speaker_by_name(
        speaker_name, user_id=str(user.user_id)
    )
    enrolled = appended = 0
    for clip in enrollment_clips:
        member = accepted.get(clip.get("identity_key"))
        if not member:
            continue
        ref = next(
            (
                segment
                for segment in member["segments"]
                if segment["segment_index"] == clip.get("segment_index")
            ),
            None,
        )
        if not ref or ref["duration"] < MIN_CLIP_SECONDS:
            continue
        await visibility.assert_current()
        evidence_records = await capture_evidence(
            visibility, [member["conversation_id"]]
        )
        wav = await reconstruct_audio_segment(
            member["conversation_id"], ref["start"], ref["end"]
        )
        await visibility.assert_current()
        if existing_speaker:
            result = await client.append_to_speaker(
                existing_speaker["id"],
                wav,
                user_id=str(user.user_id),
                speaker_name=speaker_name,
                conversation_ids=[member["conversation_id"]],
                visibility=visibility,
                evidence_records=evidence_records,
            )
            if not result.get("error"):
                appended += 1
        else:
            result = await client.enroll_new_speaker(
                speaker_name,
                wav,
                user_id=str(user.user_id),
                conversation_ids=[member["conversation_id"]],
                visibility=visibility,
                evidence_records=evidence_records,
            )
            if not result.get("error"):
                enrolled += 1
                existing_speaker = await client.get_speaker_by_name(
                    speaker_name, user_id=str(user.user_id)
                )
        await visibility.assert_current()

    async with visibility.publication():
        await collection.update_one(
            {"_id": cluster["_id"]},
            {
                "$set": {
                    "status": "confirmed",
                    "speaker_name": speaker_name,
                    "accepted_identity_keys": list(accepted),
                    "decided_at": datetime.now(timezone.utc),
                }
            },
        )
    apply_result = await data_audit_controller.apply_triage(user)
    await visibility.assert_current()
    return {
        "status": "confirmed",
        "annotations_created": annotations,
        "enrolled_new": enrolled,
        "appended": appended,
        "corrections_applied": getattr(apply_result, "body", apply_result),
    }


def _job_status(job_id: Optional[str]) -> Optional[str]:
    if not job_id:
        return None
    try:
        status = Job.fetch(job_id, connection=default_queue.connection).get_status(
            refresh=True
        )
        return status.value if hasattr(status, "value") else str(status)
    except NoSuchJobError:
        return "expired"


def _clip_key(conversation_id: str, start: float) -> str:
    return f"{conversation_id}:{round(start, 2)}"


def _active_segments(doc: dict) -> list:
    versions = doc.get("transcript_versions") or []
    active_id = doc.get("active_transcript_version")
    active = next((v for v in versions if v.get("version_id") == active_id), None)
    if active is None and versions:
        active = versions[-1]
    return (active or {}).get("segments") or []


def _effective_label(seg: dict) -> Optional[str]:
    """The segment's speaker attribution, from either labelling path.

    The pipeline writes ``identified_as``; manual annotation apply writes only
    ``segment.speaker`` (a real name, vs the "Unknown Speaker N" placeholders).
    Both count as attribution — without this, hand-labelled clips are invisible
    to the candidate pool.
    """
    identified = seg.get("identified_as")
    if identified is not None:
        return identified
    speaker = seg.get("speaker")
    if speaker and not is_non_enrollable_speaker(speaker):
        return speaker
    return None


def _is_manual_label(seg: dict) -> bool:
    """True when the attribution came from a human annotation, not the pipeline."""
    return seg.get("identified_as") is None and _effective_label(seg) is not None


def _overlaps_other_speaker(seg: dict, segments: list) -> bool:
    for other in segments:
        if other is seg or other.get("segment_type") not in (None, "speech"):
            continue
        if other.get("speaker") == seg.get("speaker"):
            continue
        if other.get("start", 0) < seg.get("end", 0) and seg.get(
            "start", 0
        ) < other.get("end", 0):
            return True
    return False


async def _gallery_stats(
    speaker_client: SpeakerRecognitionClient, speaker_name: str, user_id: str
) -> Optional[dict]:
    speaker = await speaker_client.get_speaker_by_name(speaker_name, user_id=user_id)
    if not speaker:
        return None
    return {
        "speaker_id": speaker["id"],
        "speaker_name": speaker["name"],
        "n_clips": speaker.get("audio_sample_count"),
        "total_duration_s": speaker.get("total_audio_duration"),
    }


async def _gallery_health(
    speaker_client: SpeakerRecognitionClient, speaker_id: str, user_id: str
) -> Optional[dict]:
    report = await speaker_client.get_enrollment_health(user_id=user_id)
    if report.get("error"):
        logger.warning("Guided enrollment health audit failed: %s", report)
        return None
    speaker = next(
        (
            item
            for item in report.get("speakers", [])
            if item["speaker_id"] == speaker_id
        ),
        None,
    )
    if not speaker:
        return None
    n_clips = speaker["n_clips"]
    return {
        "n_clips": n_clips,
        "median_self": speaker.get("median_self"),
        "n_flagged": speaker["n_flagged"],
        "flagged_rate": round(speaker["n_flagged"] / n_clips, 4) if n_clips else 0.0,
        "verdict": speaker["verdict"],
    }


async def _candidate_pool(
    user: User, speaker_name: str, reviewed: set, *, visibility=None
) -> list:
    """All unreviewed candidate clips for a speaker, with cheap priors only."""
    visibility = visibility or privacy.ConversationPrivacyFilter()
    query = {
        "deleted": {"$ne": True},
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    if not user.is_superuser:
        query["user_id"] = str(user.user_id)

    collection = Conversation.get_pymongo_collection()
    pool = []
    async for doc in collection.find(
        query,
        {
            "conversation_id": 1,
            "title": 1,
            "created_at": 1,
            "audio_total_duration": 1,
            "active_transcript_version": 1,
            "transcript_versions.version_id": 1,
            "transcript_versions.segments.start": 1,
            "transcript_versions.segments.end": 1,
            "transcript_versions.segments.text": 1,
            "transcript_versions.segments.speaker": 1,
            "transcript_versions.segments.identified_as": 1,
            "transcript_versions.segments.confidence": 1,
            "transcript_versions.segments.segment_type": 1,
        },
    ):
        if not await visibility.filter([doc]):
            continue
        segments = _active_segments(doc)
        speaker_present = any(_effective_label(s) == speaker_name for s in segments)
        if not speaker_present:
            continue
        audio_duration = doc.get("audio_total_duration") or 0.0
        for index, seg in enumerate(segments):
            if seg.get("segment_type") not in (None, "speech"):
                continue
            identified = _effective_label(seg)
            # Attributed segments are candidates at any confidence; unknown
            # segments only in conversations where the speaker appears (they
            # are the likeliest missed hard positives).
            if identified is not None and identified != speaker_name:
                continue
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or 0.0)
            if audio_duration:
                end = min(end, audio_duration)
            duration = end - start
            if duration < MIN_CLIP_SECONDS:
                continue
            end = min(end, start + MAX_CLIP_SECONDS)
            if _clip_key(doc["conversation_id"], start) in reviewed:
                continue
            if _overlaps_other_speaker(seg, segments):
                continue
            pool.append(
                {
                    "conversation_id": doc["conversation_id"],
                    "conversation_title": doc.get("title"),
                    "conversation_date": str(doc.get("created_at") or ""),
                    "conversation_duration": round(float(audio_duration), 3),
                    "segment_index": index,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(end - start, 3),
                    "text": (seg.get("text") or "")[:300],
                    "current_label": identified,
                    "manually_labeled": _is_manual_label(seg),
                    "stored_confidence": seg.get("confidence"),
                }
            )
    await visibility.assert_current()
    return pool


def _prior(clip: dict, threshold: float) -> float:
    """Cheap pre-score ordering before the expensive embedding pass."""
    dur = min(clip["duration"], 10.0) / 10.0
    conf = clip["stored_confidence"]
    if conf is None or clip["current_label"] is None:
        band = 0.6  # unknown segments in the speaker's conversations: promising
    else:
        band = 1.0 - min(1.0, abs(conf - threshold) / UNCERTAINTY_BAND)
    return 0.5 * dur + 0.5 * band


def _shortlist(pool: list, threshold: float, max_scan: int) -> list:
    ranked = sorted(pool, key=lambda c: _prior(c, threshold), reverse=True)
    picked: list = []
    per_conv: dict = {}
    for clip in ranked:
        if len(picked) >= max_scan:
            break
        cid = clip["conversation_id"]
        if per_conv.get(cid, 0) >= 3:
            continue
        per_conv[cid] = per_conv.get(cid, 0) + 1
        picked.append(clip)
    return picked


async def _score_clip(
    speaker_client: SpeakerRecognitionClient,
    sem: asyncio.Semaphore,
    clip: dict,
    speaker_id: str,
    *,
    visibility=None,
) -> Optional[dict]:
    async with sem:
        visibility = await _require_recordings([clip], visibility)
        try:
            wav = await reconstruct_audio_segment(
                clip["conversation_id"], clip["start"], clip["end"]
            )
        except privacy.PrivacyHeld:
            raise
        except Exception:
            logger.warning("Guided enrollment: audio reconstruction failed")
            return None
        await visibility.assert_current()
        scores = await speaker_client.score_enrollment_candidate(wav, speaker_id)
        await visibility.assert_current()
    if scores.get("error") or scores.get("sim_centroid") is None:
        return None
    return {**clip, "scores": scores}


def _information_score(clip: dict, threshold: float) -> Optional[dict]:
    """Gate on plausibility, then rank by marginal information."""
    s = clip["scores"]
    sim = s["sim_centroid"]
    best_other = s.get("best_other") or {}
    # Human-annotated clips skip the plausibility gates: the label is attested,
    # and a LOW similarity is the point — far-field/hard-condition positives are
    # exactly what the gallery is missing. The review step still guards quality.
    if not clip.get("manually_labeled"):
        if sim < MIN_PLAUSIBLE_SIM:
            return None
        if best_other.get("score", 0.0) >= sim + OTHER_SPEAKER_MARGIN:
            return None  # probably the other speaker — not worth the user's time

    novelty = 1.0 - (
        s.get("max_clip_sim") if s.get("max_clip_sim") is not None else 0.0
    )
    uncertainty = 1.0 - min(1.0, abs(sim - threshold) / UNCERTAINTY_BAND)
    dur = min(clip["duration"], 10.0) / 10.0
    score = W_NOVELTY * novelty + W_UNCERTAINTY * uncertainty + W_DURATION * dur

    reasons = []
    if clip.get("manually_labeled"):
        reasons.append("manually annotated — confirms a condition the gallery may lack")
    if novelty >= 0.5:
        reasons.append("new acoustic condition for the gallery")
    if abs(sim - threshold) <= 0.1:
        reasons.append("near the decision boundary — confirmation helps most")
    if sim >= threshold + 0.1:
        reasons.append("confident match")
    if clip["current_label"] is None:
        reasons.append("currently unlabeled in the transcript")
    elif clip.get("speaker_name") and clip["current_label"] != clip["speaker_name"]:
        reasons.append(f'currently labeled {clip["current_label"]} — possible mismatch')
    if clip["duration"] >= 8:
        reasons.append("long clip")

    return {
        **clip,
        "info_score": round(score, 4),
        "novelty": round(novelty, 3),
        "uncertainty": round(uncertainty, 3),
        "reasons": reasons,
    }


async def suggest_clips(
    user: User,
    speaker_name: str,
    batch_size: int = 4,
    max_scan: int = 24,
    order: str = "informative",
):
    """Next batch of candidate clips for one speaker.

    ``order="informative"`` ranks by marginal information (novelty + boundary
    uncertainty + duration) — best for teaching the model. ``order="confidence"``
    ranks by raw similarity to the gallery — best for finding the speaker fast
    when the gallery is small and most candidates are low-similarity noise.
    """
    batch_size = max(1, min(batch_size, 8))
    max_scan = max(batch_size, min(max_scan, 48))
    visibility = privacy.ConversationPrivacyFilter()

    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )
    gallery = await _gallery_stats(speaker_client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    review_rows = (
        await _reviews_collection()
        .find(
            {"speaker_name": speaker_name},
            {"conversation_id": 1, "segment_start": 1},
        )
        .to_list()
    )
    reviewed = {
        _clip_key(r["conversation_id"], r["segment_start"])
        for r in await visibility.filter(review_rows)
    }

    threshold = get_diarization_settings().get("similarity_threshold", 0.5)
    pool = await _candidate_pool(user, speaker_name, reviewed, visibility=visibility)
    shortlist = _shortlist(pool, threshold, max_scan)

    sem = asyncio.Semaphore(SCORE_CONCURRENCY)
    scored = await asyncio.gather(
        *(
            _score_clip(
                speaker_client, sem, clip, gallery["speaker_id"], visibility=visibility
            )
            for clip in shortlist
        ),
        return_exceptions=True,
    )
    # Drain siblings before propagating a hold; no scoring may outlive the request.
    for result in scored:
        if isinstance(result, BaseException):
            raise result
    await visibility.assert_current()
    ranked = sorted(
        filter(
            None,
            (_information_score(c, threshold) for c in scored if c is not None),
        ),
        key=lambda c: c["info_score"],
        reverse=True,
    )

    discovery_query = {
        "requested_by": str(user.user_id),
        "speaker_id": gallery["speaker_id"],
        "review_key": {"$nin": list(reviewed)},
        "$or": [
            {"human_label": None},
            {"human_label": gallery["speaker_name"]},
        ],
    }
    discovery_rows = (
        await _discovery_collection().find(discovery_query, {"_id": 0}).to_list()
    )
    discovery_rows = await visibility.filter(discovery_rows)
    discovery_count = len(discovery_rows)
    combined = {
        _clip_key(candidate["conversation_id"], candidate["start"]): candidate
        for candidate in ranked
    }
    for candidate in filter(
        None, (_information_score(row, threshold) for row in discovery_rows)
    ):
        key = _clip_key(candidate["conversation_id"], candidate["start"])
        if key not in combined or candidate["info_score"] > combined[key]["info_score"]:
            combined[key] = candidate
    rank_key = (
        (lambda candidate: candidate["scores"]["sim_centroid"])
        if order == "confidence"
        else (lambda candidate: candidate["info_score"])
    )
    ranked = sorted(combined.values(), key=rank_key, reverse=True)

    batch: list = []
    per_conv: dict = {}
    for clip in ranked:
        if len(batch) >= batch_size:
            break
        cid = clip["conversation_id"]
        if per_conv.get(cid, 0) >= MAX_PER_CONVERSATION:
            continue
        per_conv[cid] = per_conv.get(cid, 0) + 1
        batch.append(clip)

    await visibility.assert_current()
    return {
        "speaker": gallery,
        "threshold": threshold,
        "batch": batch,
        "scanned": len(shortlist),
        "gated_out": sum(1 for c in scored if c is not None) - len(ranked),
        "pool_remaining": max(0, len(pool) - len(shortlist)),
        "reviewed_total": len(reviewed),
        "discovery_indexed": discovery_count > 0,
        "discovery_candidates": discovery_count,
    }


async def enqueue_corpus_discovery(
    user: User, speaker_name: str, include_deleted: bool = False
):
    """Index all corpus speech once, then score it against one live gallery."""
    client = SpeakerRecognitionClient()
    gallery = await _gallery_stats(client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    run_key = {
        "requested_by": str(user.user_id),
        "speaker_id": gallery["speaker_id"],
    }
    existing = await _discovery_runs_collection().find_one(run_key)
    existing_status = _job_status(existing.get("job_id") if existing else None)
    if existing_status in {"queued", "started", "deferred", "scheduled"}:
        return {
            "job_id": existing["job_id"],
            "status": existing_status,
            "reused": True,
        }
    job = default_queue.enqueue(
        discover_speaker_candidates_job,
        requested_by=str(user.user_id),
        speaker_id=gallery["speaker_id"],
        speaker_name=gallery["speaker_name"],
        include_all_users=bool(user.is_superuser),
        include_deleted=include_deleted,
        job_timeout=14400,
        result_ttl=JOB_RESULT_TTL,
        description=f"Speaker corpus discovery: {gallery['speaker_name']}",
    )
    await _discovery_runs_collection().update_one(
        run_key,
        {
            "$set": {
                "speaker_name": gallery["speaker_name"],
                "job_id": job.id,
                "queued_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return {"job_id": job.id, "status": "queued", "reused": False}


async def mine_uploaded_files(user: User, speaker_name: str, files: list):
    """Ingest uploaded audio files as a mining corpus for one speaker.

    Files become annotation-only conversations (audio chunks + batch
    transcription + speaker identification, no memory extraction); a
    corpus-discovery job is chained behind the transcription jobs so mined
    speech is scored against the speaker's gallery as soon as it has segments.
    """
    # Lazy import: audio_controller pulls in the transcription stack.
    from backend.controllers.audio_controller import upload_and_process_audio_files
    from backend.workers.speaker_mining_jobs import (
        MINING_DEVICE_NAME,
        _parse_upload_response,
        enqueue_discovery_after,
    )

    client = SpeakerRecognitionClient()
    if not client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )
    gallery = await _gallery_stats(client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )

    body = _parse_upload_response(
        await upload_and_process_audio_files(
            user, files, device_name=MINING_DEVICE_NAME, annotation_only=True
        )
    )
    started = [f for f in body.get("files", []) if f.get("status") == "started"]
    transcript_job_ids = [
        f["transcript_job_id"] for f in started if f.get("transcript_job_id")
    ]

    discovery_job_id = None
    if started:
        discovery_job_id = await enqueue_discovery_after(
            str(user.user_id),
            gallery["speaker_id"],
            gallery["speaker_name"],
            transcript_job_ids,
            bool(user.is_superuser),
        )

    return {
        "speaker_name": gallery["speaker_name"],
        "ingested": len(started),
        "failed": [
            {"filename": f.get("filename"), "error": f.get("error")}
            for f in body.get("files", [])
            if f.get("status") == "error"
        ],
        "transcription_jobs": len(transcript_job_ids),
        "transcription_available": bool(transcript_job_ids) or not started,
        "discovery_job_id": discovery_job_id,
    }


async def enqueue_local_mining(user: User, speaker_name: str, paths: List[str]):
    """Queue server-side corpus mining (admin): ingest files already on the
    backend's data volume — e.g. backup WAVs of purged conversations — and
    chain discovery for the speaker."""
    # Lazy import: the workers package imports back from the controllers.
    from backend.workers.speaker_mining_jobs import mine_local_corpus_job

    client = SpeakerRecognitionClient()
    gallery = await _gallery_stats(client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    if not paths:
        return JSONResponse(status_code=400, content={"error": "No paths provided"})
    if len(paths) > 1000:
        return JSONResponse(
            status_code=400, content={"error": "Too many paths (max 1000)"}
        )
    job = default_queue.enqueue(
        mine_local_corpus_job,
        requested_by=str(user.user_id),
        speaker_id=gallery["speaker_id"],
        speaker_name=gallery["speaker_name"],
        paths=paths,
        include_all_users=bool(user.is_superuser),
        job_timeout=14400,
        result_ttl=JOB_RESULT_TTL,
        description=f"Speaker mining ingest: {len(paths)} files for {gallery['speaker_name']}",
    )
    return {"job_id": job.id, "status": "queued", "files": len(paths)}


async def corpus_discovery_state(user: User, speaker_name: str):
    visibility = privacy.ConversationPrivacyFilter()
    client = SpeakerRecognitionClient()
    gallery = await _gallery_stats(client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    key = {
        "requested_by": str(user.user_id),
        "speaker_id": gallery["speaker_id"],
    }
    run = await _discovery_runs_collection().find_one(key, {"_id": 0})
    job_id = run.get("job_id") if run else None
    candidates = (
        await _discovery_collection().find(key, {"conversation_id": 1}).to_list()
    )
    candidates = await visibility.filter(candidates)
    await visibility.assert_current()
    return {
        "speaker_name": gallery["speaker_name"],
        "job_id": job_id,
        "status": _job_status(job_id),
        "matched_segments": len(candidates),
    }


async def decide_clips(user: User, speaker_name: str, decisions: List[dict]):
    """Record review decisions and enroll clips with a confirmed identity."""
    visibility = await _require_recordings(decisions)
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )
    gallery = await _gallery_stats(speaker_client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    health_before = await _gallery_health(
        speaker_client, gallery["speaker_id"], str(user.user_id)
    )

    reviews = _reviews_collection()
    enrolled, reassigned, rejected, skipped, bad_clips, multiple_speakers, errors = (
        0,
        0,
        0,
        0,
        0,
        0,
        [],
    )
    for decision in decisions:
        await visibility.assert_current()
        conversation_id = decision.get("conversation_id")
        start = decision.get("start")
        end = decision.get("end")
        original_start = decision.get("original_start")
        original_end = decision.get("original_end")
        review_decision = decision.get("decision")
        actual_speaker = decision.get("actual_speaker")
        if (
            not conversation_id
            or start is None
            or end is None
            or end <= start
            or original_start is None
            or original_end is None
            or original_end <= original_start
        ):
            errors.append({"clip": decision, "error": "invalid clip bounds"})
            continue

        if review_decision == "another_speaker" and not actual_speaker:
            errors.append({"clip": decision, "error": "actual_speaker is required"})
            continue

        enroll_error = None
        enrollment_target = (
            speaker_name if review_decision == "accept" else actual_speaker
        )
        if enrollment_target:
            try:
                target_gallery = await _gallery_stats(
                    speaker_client, enrollment_target, str(user.user_id)
                )
                if not target_gallery:
                    raise ValueError(f"No enrolled speaker named '{enrollment_target}'")
                await visibility.assert_current()
                evidence_records = await capture_evidence(visibility, [conversation_id])
                wav = await reconstruct_audio_segment(conversation_id, start, end)
                await visibility.assert_current()
                result = await speaker_client.append_to_speaker(
                    target_gallery["speaker_id"],
                    wav,
                    user_id=str(user.user_id),
                    speaker_name=enrollment_target,
                    conversation_ids=[conversation_id],
                    visibility=visibility,
                    evidence_records=evidence_records,
                )
                await visibility.assert_current()
                if result.get("error"):
                    enroll_error = "Speaker enrollment failed"
                elif result.get("status") == "already_enrolled":
                    skipped += 1
                else:
                    enrolled += 1
                    if enrollment_target != speaker_name:
                        reassigned += 1
            except privacy.PrivacyHeld:
                raise
            except Exception:
                enroll_error = "Speaker enrollment failed"
            if enroll_error:
                errors.append({"clip": decision, "error": enroll_error})
        elif review_decision == "reject":
            rejected += 1
        elif review_decision == "skip":
            skipped += 1
        elif review_decision == "multiple_speakers":
            multiple_speakers += 1
        elif review_decision == "bad_clip":
            bad_clips += 1

        async with visibility.publication():
            await reviews.update_one(
                {
                    "speaker_name": speaker_name,
                    "conversation_id": conversation_id,
                    "segment_start": round(float(original_start), 3),
                },
                {
                    "$set": {
                        "speaker_id": gallery["speaker_id"],
                        "segment_end": round(float(original_end), 3),
                        "selected_start": round(float(start), 3),
                        "selected_end": round(float(end), 3),
                        "decision": review_decision,
                        "actual_speaker": enrollment_target,
                        "enrolled": enrollment_target is not None
                        and enroll_error is None,
                        "enroll_error": enroll_error,
                        "scores": decision.get("scores"),
                        "reviewed_by": str(user.user_id),
                        "reviewed_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )

    speaker_after = await _gallery_stats(
        speaker_client, speaker_name, str(user.user_id)
    )
    health_after = await _gallery_health(
        speaker_client, gallery["speaker_id"], str(user.user_id)
    )
    accepted_novelties = [
        1.0 - decision["scores"]["max_clip_sim"]
        for decision in decisions
        if decision.get("decision") == "accept"
        and decision.get("scores", {}).get("max_clip_sim") is not None
    ]
    coverage = {
        "accepted_novelty_mean": (
            round(sum(accepted_novelties) / len(accepted_novelties), 3)
            if accepted_novelties
            else None
        )
    }
    snapshot = {
        "evidence_conversation_ids": sorted({d["conversation_id"] for d in decisions}),
        "privacy_revisions": visibility.revision_receipt(),
        "speaker_id": gallery["speaker_id"],
        "speaker_name": speaker_name,
        "reviewed_by": str(user.user_id),
        "created_at": datetime.now(timezone.utc),
        "health_before": health_before,
        "health_after": health_after,
        "coverage": coverage,
        "decisions": {
            "enrolled": enrolled,
            "reassigned": reassigned,
            "rejected": rejected,
            "skipped": skipped,
            "multiple_speakers": multiple_speakers,
            "bad_clips": bad_clips,
        },
    }
    async with visibility.publication():
        await _batches_collection().insert_one(snapshot)
    benchmark_job_id = None
    discovery_job_id = None
    if enrolled > 0:
        await visibility.assert_current()
        benchmark_job = default_queue.enqueue(
            run_speaker_benchmark_job,
            user_id=str(user.user_id),
            job_timeout=7200,
            result_ttl=JOB_RESULT_TTL,
            description="Speaker enhancement: post-enrollment cross-validation",
        )
        benchmark_job_id = benchmark_job.id
        discovery_response = await enqueue_corpus_discovery(user, speaker_name)
        if isinstance(discovery_response, dict):
            discovery_job_id = discovery_response.get("job_id")

    await visibility.assert_current()
    return {
        "speaker": speaker_after,
        "health_before": health_before,
        "health_after": health_after,
        "coverage": coverage,
        "benchmark_job_id": benchmark_job_id,
        "discovery_job_id": discovery_job_id,
        "enrolled": enrolled,
        "reassigned": reassigned,
        "rejected": rejected,
        "skipped": skipped,
        "multiple_speakers": multiple_speakers,
        "bad_clips": bad_clips,
        "errors": errors,
        "status": "ok" if not errors else "partial",
    }


async def gallery_clips(user: User, speaker_name: str):
    """List a speaker's enrolled clips with per-clip contamination flags.

    Powers the gallery-management panel: each clip carries the audit's
    self-similarity, closest-other-speaker score, and mislabel/junk/weak flags
    so the user can spot and remove bad enrollments.
    """
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )
    gallery = await _gallery_stats(speaker_client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    report = await speaker_client.get_enrollment_health(user_id=str(user.user_id))
    if report.get("error"):
        return JSONResponse(
            status_code=503,
            content={"error": "Unable to audit the speaker's enrolled clips"},
        )
    speaker = next(
        (
            item
            for item in report.get("speakers", [])
            if item["speaker_id"] == gallery["speaker_id"]
        ),
        None,
    )
    return {
        "speaker": gallery,
        "verdict": speaker["verdict"] if speaker else None,
        "median_self": speaker.get("median_self") if speaker else None,
        "clips": speaker["clips"] if speaker else [],
        "thresholds": report.get("thresholds"),
    }


async def delete_gallery_clip(
    user: User, speaker_name: str, segment_id: int, hard: bool = False
):
    """Remove one enrolled clip from a speaker's voiceprint.

    The clip must belong to the named speaker (guards against stale UI state
    deleting another speaker's clip). Quarantined by default so it's
    recoverable; the speaker service recomputes the centroid either way.
    """
    speaker_client = SpeakerRecognitionClient()
    if not speaker_client.enabled:
        return JSONResponse(
            status_code=503, content={"error": "Speaker recognition is not enabled"}
        )
    gallery = await _gallery_stats(speaker_client, speaker_name, str(user.user_id))
    if not gallery:
        return JSONResponse(
            status_code=404,
            content={"error": f"No enrolled speaker named '{speaker_name}'"},
        )
    report = await speaker_client.get_enrollment_health(user_id=str(user.user_id))
    speaker = next(
        (
            item
            for item in report.get("speakers", [])
            if item["speaker_id"] == gallery["speaker_id"]
        ),
        None,
    )
    if not speaker or not any(c["segment_id"] == segment_id for c in speaker["clips"]):
        return JSONResponse(
            status_code=404,
            content={
                "error": f"Clip {segment_id} is not enrolled for '{speaker_name}'"
            },
        )
    result = await speaker_client.delete_enrollment_segment(segment_id, hard=hard)
    if result.get("error"):
        return JSONResponse(status_code=502, content=result)
    logger.info(
        "Guided enrollment: removed clip %s from %s (%s)",
        segment_id,
        speaker_name,
        "hard" if hard else "quarantined",
    )
    return {
        **result,
        "speaker": await _gallery_stats(
            speaker_client, speaker_name, str(user.user_id)
        ),
        "health": await _gallery_health(
            speaker_client, gallery["speaker_id"], str(user.user_id)
        ),
    }


async def reset_speaker_state(
    user: User, speaker_name: str, purge_gallery: bool = False
):
    """Forget all guided-enrollment state for a speaker name.

    Deleting a speaker on the speaker service does not touch the backend's
    review ledger, so a re-enrolled speaker with the same name inherits stale
    'already reviewed' exclusions and old session history. This clears the
    review decisions, session snapshots, and corpus-discovery matches recorded
    under the name (across old speaker ids), so every clip becomes suggestible
    again. With ``purge_gallery`` the speaker's voiceprint and enrollment audio
    are also deleted from the speaker service, leaving a truly blank slate.
    """
    scope = {"speaker_name": speaker_name}
    if not user.is_superuser:
        reviews_scope = {**scope, "reviewed_by": str(user.user_id)}
        discovery_scope = {**scope, "requested_by": str(user.user_id)}
    else:
        reviews_scope = scope
        discovery_scope = scope

    deleted = {
        "reviews": (
            await _reviews_collection().delete_many(reviews_scope)
        ).deleted_count,
        "sessions": (
            await _batches_collection().delete_many(reviews_scope)
        ).deleted_count,
        "discovery_matches": (
            await _discovery_collection().delete_many(discovery_scope)
        ).deleted_count,
        "discovery_runs": (
            await _discovery_runs_collection().delete_many(discovery_scope)
        ).deleted_count,
    }

    gallery_deleted = False
    if purge_gallery:
        speaker_client = SpeakerRecognitionClient()
        if not speaker_client.enabled:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Speaker recognition is not enabled",
                    "deleted": deleted,
                },
            )
        gallery = await _gallery_stats(speaker_client, speaker_name, str(user.user_id))
        if gallery:
            result = await speaker_client.delete_speaker(
                gallery["speaker_id"], user_id=str(user.user_id), delete_audio=True
            )
            if result.get("error"):
                return JSONResponse(
                    status_code=502, content={**result, "deleted": deleted}
                )
            gallery_deleted = True

    logger.info(
        "Guided enrollment reset for '%s' by %s: %s%s",
        speaker_name,
        user.user_id,
        deleted,
        " + gallery purged" if gallery_deleted else "",
    )
    return {
        "speaker_name": speaker_name,
        "deleted": deleted,
        "gallery_deleted": gallery_deleted,
        "status": "ok",
    }


async def enrollment_history(user: User, speaker_name: str, limit: int = 50):
    """Return dated gallery-quality snapshots for one speaker, newest first."""
    query = {"speaker_name": speaker_name}
    if not user.is_superuser:
        query["reviewed_by"] = str(user.user_id)
    rows = []
    async for row in (
        _batches_collection()
        .find(query, {"_id": 0})
        .sort("created_at", -1)
        .limit(max(1, min(limit, 200)))
    ):
        visibility = privacy.ConversationPrivacyFilter()
        identifiers = row.get("evidence_conversation_ids")
        if not identifiers:
            continue
        try:
            await visibility.require_receipt(row.get("privacy_revisions"))
            await _require_recordings(
                [{"conversation_id": identifier} for identifier in identifiers],
                visibility,
            )
        except privacy.PrivacyHeld:
            continue
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            row["created_at"] = created_at.isoformat()
        rows.append((row, visibility))
    for _, visibility in rows:
        await visibility.assert_current()
    return {
        "speaker_name": speaker_name,
        "sessions": [
            {
                key: value
                for key, value in row.items()
                if key not in {"evidence_conversation_ids", "privacy_revisions"}
            }
            for row, _ in rows
        ],
    }


async def enqueue_benchmark(user: User):
    job = default_queue.enqueue(
        run_speaker_benchmark_job,
        user_id=str(user.user_id),
        job_timeout=7200,
        result_ttl=JOB_RESULT_TTL,
        description="Speaker enhancement: grouped cross-validation",
    )
    return {"job_id": job.id, "status": "queued"}


async def latest_benchmark(user: User):
    row = (
        await _batches_collection()
        .database["speaker_benchmark_runs"]
        .find_one({"user_id": str(user.user_id)}, {"_id": 0}, sort=[("created_at", -1)])
    )
    if row:
        visibility = privacy.ConversationPrivacyFilter()
        await visibility.require_receipt(row.get("privacy_revisions"))
        identifiers = row.get("evidence_conversation_ids")
        if not isinstance(identifiers, list):
            raise privacy.PrivacyHeld()
        await _require_recordings(
            [{"conversation_id": identifier} for identifier in identifiers], visibility
        )
        row.pop("evidence_conversation_ids")
        row.pop("privacy_revisions")
    if row and isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat()
    return {"report": row}


async def reconstructed_baseline(user: User):
    first_review = await _reviews_collection().find_one(
        {"reviewed_by": str(user.user_id)},
        {"reviewed_at": 1},
        sort=[("reviewed_at", 1)],
    )
    cutoff = first_review.get("reviewed_at") if first_review else None
    if cutoff is None:
        return {"cutoff": None, "speakers": [], "status": "no_guided_reviews"}

    client = SpeakerRecognitionClient()
    baseline = await client.get_enrollment_health(
        user_id=str(user.user_id), before=cutoff
    )
    current = await client.get_enrollment_health(user_id=str(user.user_id))
    if baseline.get("error") or current.get("error"):
        return JSONResponse(
            status_code=503,
            content={"error": "Unable to compute speaker gallery baseline"},
        )
    current_by_id = {speaker["speaker_id"]: speaker for speaker in current["speakers"]}
    speakers = []
    for before in baseline["speakers"]:
        after = current_by_id.get(before["speaker_id"])
        speakers.append(
            {
                "speaker_id": before["speaker_id"],
                "name": before["name"],
                "baseline": {
                    "n_clips": before["n_clips"],
                    "median_self": before["median_self"],
                    "n_flagged": before["n_flagged"],
                    "verdict": before["verdict"],
                },
                "current": (
                    {
                        "n_clips": after["n_clips"],
                        "median_self": after["median_self"],
                        "n_flagged": after["n_flagged"],
                        "verdict": after["verdict"],
                    }
                    if after
                    else None
                ),
            }
        )
    return {
        "cutoff": cutoff.isoformat(),
        "speakers": speakers,
        "status": "reconstructed",
        "limitations": (
            "Uses surviving clip rows and their current speaker assignment. Clips deleted "
            "or relabeled before reconstruction cannot be restored to their prior state."
        ),
    }
