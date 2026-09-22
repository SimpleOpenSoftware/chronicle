"""Assemble timestamped ScreenPipe chunks into Chronicle conversation sessions."""

import asyncio
import hashlib
import json
import logging
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from beanie import PydanticObjectId

import backend.services.privacy as privacy
import backend.services.privacy_audio_recovery as privacy_audio_recovery
from backend.config import require_speech_for_transcription
from backend.controllers.audio_controller import materialize_and_process_audio_claim
from backend.models.audio_capture import AudioRangeRef
from backend.models.device_input import DeviceInputItem, utcnow
from backend.models.timeline import AudioEvidenceSpan, EvidenceLocator
from backend.models.user import User
from backend.services.audio_claims import clip_audio_ranges
from backend.services.timeline.dirty_ranges import note_evidence_dirty
from backend.services.timeline.source_ids import format_screenpipe_segment_source_id
from backend.utils.audio_chunk_utils import convert_wav_to_chunks
from backend.utils.vad_analysis import (
    AudioEvidenceProfile,
    SpeechDetectionReason,
    SpeechDetectionResult,
    profile_pcm_audio,
)

logger = logging.getLogger(__name__)

_SESSION_GAP = timedelta(seconds=60)
_CLOSE_DELAY = timedelta(seconds=90)
# Ingest attempts allowed for one session start before its chunks are dropped.
_MAX_INGEST_ATTEMPTS = 5
# Transform identity: corrected assembly must never overwrite retained captures
# or adopt conversations produced by a different sample-time mapping.
_CAPTURE_ASSEMBLY_VERSION = "aligned-v2"
# How much contiguous capture is mixed and profiled at once. This bounds *compute*,
# not conversations: where one recording ends is decided afterwards, from the speech
# profile (see plan_session_cuts). ScreenPipe records continuously, so the 60s gap
# rule almost never fires and this window is what actually terminates a session.
_MAX_WINDOW = timedelta(hours=2)
# Preferred recording length. A cut is placed near this, but only where the audio is
# quiet — never mid-sentence.
_TARGET_SESSION = timedelta(minutes=30)
# Chunks the collector tagged with the same meeting interval belong to one
# conversation: tolerate longer silences, and never cut inside the meeting.
_MEETING_SESSION_GAP = timedelta(minutes=5)
_MAX_MEETING_SESSION = timedelta(hours=2)


def _as_utc(value: datetime) -> datetime:
    """Normalize Mongo's naïve UTC datetimes before ordering or arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _meeting_id(item: DeviceInputItem) -> str | None:
    value = item.metadata.get("meeting_id")
    return str(value) if value else None


def group_audio_sessions(items: list[DeviceInputItem]) -> list[list[DeviceInputItem]]:
    sessions: list[list[DeviceInputItem]] = []
    for item in sorted(items, key=lambda row: _as_utc(row.captured_at)):
        if not sessions:
            sessions.append([item])
            continue
        previous = sessions[-1][-1]
        previous_end = _as_utc(previous.ended_at or previous.captured_at)
        session_start = _as_utc(sessions[-1][0].captured_at)
        captured_at = _as_utc(item.captured_at)
        # A meeting boundary always starts a new session; within one meeting
        # the collector's interval is trusted over the blind gap/duration
        # rules, up to a safety cap.
        same_meeting = _meeting_id(item) is not None and _meeting_id(
            item
        ) == _meeting_id(previous)
        gap_limit = _MEETING_SESSION_GAP if same_meeting else _SESSION_GAP
        max_session = _MAX_MEETING_SESSION if same_meeting else _MAX_WINDOW
        if (
            _meeting_id(item) != _meeting_id(previous)
            or captured_at - previous_end > gap_limit
            or captured_at - session_start >= max_session
        ):
            sessions.append([item])
        else:
            sessions[-1].append(item)
    return sessions


def plan_session_cuts(
    speech_fraction: Sequence[float | None],
    bucket_seconds: float,
    *,
    target_seconds: float = _TARGET_SESSION.total_seconds(),
    max_seconds: float = _MAX_WINDOW.total_seconds(),
    min_quiet_seconds: float = 30.0,
) -> list[float]:
    """Where a capture window should be cut into recordings, in seconds from its start.

    The old rule cut at exactly 30 minutes regardless of what was being said. Measured
    over this deployment's ScreenPipe corpus, 176 of 237 recordings hit that cap and 94
    of them had speech running to within 15 seconds of the cut — more than half were
    severed mid-conversation, with the rest of the conversation filed as a separate
    recording.

    So the target is only honoured where the audio agrees. A cut needs a quiet run of
    at least ``min_quiet_seconds`` reasonably near the target; failing that the window
    stays whole, because a dense 63-minute call IS one recording and splitting it is
    the bug being fixed. The only unconditional cut is the safety cap, and even there
    the quietest available point within the cap is preferred.

    Args:
        speech_fraction: per-bucket speech share; None where the VAD had no verdict.
        bucket_seconds: seconds of audio per bucket.
        target_seconds: preferred recording length.
        max_seconds: hard cap; a cut happens here even with nowhere good to put it.
        min_quiet_seconds: shortest silence that may carry a cut.
    """
    if bucket_seconds <= 0 or not speech_fraction:
        return []
    total = len(speech_fraction) * bucket_seconds
    # A window nothing was measured in has no honest seam. ``None`` means "the VAD
    # returned no verdict" -- profile_pcm_audio emits an all-``None`` series when
    # scoring fails outright -- so an unscored window is *uniformly* unknown, the
    # longest "quiet" run is the whole thing, and its midpoint is the blind target cut
    # this function exists to remove. It hides itself too: such a window also reports
    # as carrying no speech at all, so the "no cut landed in speech" check passes
    # vacuously. Measured during the corpus re-bound: 18 windows, 17.5 hours, every
    # cut at exactly 30:00. Leaving it whole hands the decision to the caller, which
    # is the only party that can tell "silent" from "never analysed".
    if not any(fraction is not None for fraction in speech_fraction):
        return []
    # Measured silence is a real seam. An unscored bucket is a *weaker* one: given the
    # choice, cut where nothing is known rather than through speech we can see.
    measured_quiet = [
        fraction is not None and not fraction for fraction in speech_fraction
    ]
    quiet = [fraction is None or not fraction for fraction in speech_fraction]
    min_buckets = max(1, int(min_quiet_seconds / bucket_seconds))

    def quiet_cut(low: float, high: float) -> float | None:
        first, last = int(low // bucket_seconds), int(high // bucket_seconds)
        return _longest_quiet_run(
            measured_quiet, bucket_seconds, first, last, min_buckets
        ) or _longest_quiet_run(quiet, bucket_seconds, first, last, min_buckets)

    cuts: list[float] = []
    window_start = 0.0
    # Half a target's overshoot is tolerated rather than cut: a 40-minute recording is
    # better than a 30 and a 10.
    while total - window_start > target_seconds * 1.5:
        remaining = total - window_start
        forced = remaining > max_seconds
        # Never leave a stub: the search starts half a target in, and stops half a
        # target short of the end so the tail is a recording rather than a fragment.
        # That tail bound is measured from the end of the whole window, not from
        # ``low`` — using ``low`` subtracts ``window_start`` a second time, so every
        # iteration after the first searches a band that has silently collapsed
        # towards its own start. Invisible at a 30-minute target inside a 2-hour cap,
        # where the loop never reaches a third pass, but it costs a longer window
        # every seam after the first.
        low = window_start + target_seconds * 0.5
        high = min(window_start + target_seconds * 1.5, total - target_seconds * 0.5)
        cut = quiet_cut(low, high)
        if cut is None and forced:
            # Past the safety cap something has to give — take the quietest point
            # anywhere inside the cap before resorting to the cap itself.
            cut = quiet_cut(low, window_start + max_seconds)
        if cut is None:
            if not forced:
                # No good seam and no obligation to cut: one longer recording is the
                # honest answer.
                break
            cut = window_start + max_seconds
        if cut <= window_start:
            break
        cuts.append(round(cut, 3))
        window_start = cut
    return cuts


def _longest_quiet_run(
    quiet: list[bool],
    bucket_seconds: float,
    first: int,
    last: int,
    min_buckets: int = 1,
) -> float | None:
    """Midpoint of the longest quiet run of at least ``min_buckets`` in [first, last)."""
    best_length = 0
    best_middle: float | None = None
    index = max(0, first)
    last = min(last, len(quiet))
    while index < last:
        if not quiet[index]:
            index += 1
            continue
        run_start = index
        while index < last and quiet[index]:
            index += 1
        length = index - run_start
        if length >= min_buckets and length > best_length:
            best_length = length
            best_middle = (run_start + index) / 2 * bucket_seconds
    return best_middle


def _audio_locator(item: DeviceInputItem) -> EvidenceLocator:
    """Read and validate the typed provider-local lane carried by an audio item."""

    locator = EvidenceLocator.model_validate(item.locator)
    if locator.capture_source_id != item.source_id or locator.modality != "audio":
        raise ValueError("audio item locator must match its source and modality")
    if not locator.track_id:
        raise ValueError("audio item locator requires a provider-local track id")
    return locator


def audio_stream_key(item: DeviceInputItem) -> tuple[str, str, str, str]:
    """Keep every provider-local audio lane in an independent processing stream."""
    locator = _audio_locator(item)
    return (
        item.user_id,
        locator.capture_source_id,
        str(item.metadata.get("direction", "unknown")),
        locator.track_id,
    )


def _capture_external_source_id(
    source_id: str, direction: str, session: Sequence[DeviceInputItem]
) -> str:
    """Stable identity for one closed ScreenPipe compute window."""
    capture_source_id = _audio_capture_source_id(source_id, direction, session)
    return (
        f"screenpipe-capture:{_CAPTURE_ASSEMBLY_VERSION}:{capture_source_id}:"
        f"{session[0].source_item_id}-{session[-1].source_item_id}"
    )


def _audio_capture_source_id(
    source_id: str, direction: str, session: Sequence[DeviceInputItem]
) -> str:
    """Chronicle capture identity for one provider-local audio lane."""

    locator = _audio_locator(session[0])
    if locator.capture_source_id != source_id:
        raise ValueError("audio capture source does not match its locator")
    return f"{source_id}:{direction}:{locator.track_id}"


def _capture_session_id(user_id: str, external_source_id: str) -> str:
    """Make retries converge on one finite capture instead of copying its audio."""
    digest = hashlib.sha256(f"{user_id}:{external_source_id}".encode()).hexdigest()
    return f"screenpipe-{digest[:40]}"


async def _persist_capture_window(
    user: User,
    source_id: str,
    direction: str,
    session: list[DeviceInputItem],
    wav_path: Path,
):
    """Persist the complete mixed window before VAD or semantic segmentation."""
    external_source_id = _capture_external_source_id(source_id, direction, session)
    return await convert_wav_to_chunks(
        user_id=user.user_id,
        capture_source_id=_audio_capture_source_id(source_id, direction, session),
        wav_file_path=wav_path,
        captured_at=min(_as_utc(item.captured_at) for item in session),
        capture_session_id=_capture_session_id(user.user_id, external_source_id),
        origin="screenpipe",
        external_source_id=external_source_id,
        data_purpose="capture_evidence",
    )


async def _segment_audio_range(
    capture_range: AudioRangeRef, segment: "_Segment"
) -> AudioRangeRef:
    """Claim only the segment's wall-clock interval from the persisted capture."""
    capture_start = _as_utc(capture_range.started_at)
    start = max(0.0, (_as_utc(segment.started_at) - capture_start).total_seconds())
    end = min(
        capture_range.duration_seconds,
        (_as_utc(segment.ended_at) - capture_start).total_seconds(),
    )
    ranges = await clip_audio_ranges([capture_range], start, end)
    if len(ranges) != 1:
        raise RuntimeError(
            "ScreenPipe segment did not resolve to exactly one capture range"
        )
    return ranges[0]


async def _mix_session(
    items: list[DeviceInputItem], workspace: Path, output: Path
) -> None:
    start = min(_as_utc(item.captured_at) for item in items)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    valid = [item for item in items if item.media_data]
    if not valid:
        raise ValueError("session has no available audio data")
    for index, item in enumerate(valid):
        suffix = Path(item.media_filename or "chunk.wav").suffix or ".wav"
        input_path = workspace / f"input-{index}{suffix}"
        input_path.write_bytes(item.media_data or b"")
        command.extend(["-i", str(input_path)])
    chains = []
    labels = []
    for index, item in enumerate(valid):
        delay_ms = max(
            0, int((_as_utc(item.captured_at) - start).total_seconds() * 1000)
        )
        label = f"a{index}"
        chains.append(
            f"[{index}:a]aresample=16000,aformat=channel_layouts=mono,adelay={delay_ms}[{label}]"
        )
        labels.append(f"[{label}]")
    chains.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=1,alimiter=limit=0.95:latency=1[out]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[out]",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio assembly failed: {stderr.decode(errors='replace')[-1000:]}"
        )


@dataclass
class _Segment:
    """One recording carved out of a capture window."""

    items: list[DeviceInputItem]
    path: Path
    started_at: datetime
    ended_at: datetime
    profile: AudioEvidenceProfile | None
    privacy_partition: bool = False


def _unprocessed_spans(spans, processed_spans):
    for done_start, done_end in processed_spans:
        remaining = []
        for low, high in spans:
            if done_end <= low or done_start >= high:
                remaining.append((low, high))
            else:
                if low < done_start:
                    remaining.append((low, done_start))
                if done_end < high:
                    remaining.append((done_end, high))
        spans = remaining
    return spans


def _audio_input_identity(session):
    """Exact retained bytes and assembly inputs; never an acoustic similarity key."""
    inputs = []
    for item in session:
        data = getattr(item, "media_data", None)
        if not isinstance(data, bytes) or not data:
            return None
        inputs.append(
            {
                "stream": audio_stream_key(item),
                "source_item_id": item.source_item_id,
                "started_at": _as_utc(item.captured_at).isoformat(),
                "ended_at": _as_utc(item.ended_at or item.captured_at).isoformat(),
                "suffix": Path(item.media_filename or "chunk.wav").suffix or ".wav",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return hashlib.sha256(
        json.dumps(
            [_CAPTURE_ASSEMBLY_VERSION, inputs], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


async def _screened_audio_has_no_new_work(user_id, source_id, input_identity):
    """Skip repeat assembly only after successful persistence of these exact inputs.

    This receipt never establishes permission. Recompute allowed spans from current
    policy on every attempt so an override can make previously held audio eligible.
    """
    if input_identity is None:
        return False

    scope = {"user_id": str(user_id), "source_id": source_id}
    receipt = await privacy.database().privacy_audio_inputs.find_one(
        {"_id": input_identity, **scope}
    )
    if receipt is None:
        return False
    start, end = privacy.utc(receipt["started_at"]), privacy.utc(receipt["ended_at"])
    progress = (
        await privacy.database().privacy_audio_progress.find_one(
            {"_id": receipt["progress_id"], **scope}
        )
        or {}
    )
    done = [
        (privacy.utc(s["started_at"]), privacy.utc(s["ended_at"]))
        for s in progress.get("completed", [])
    ]
    if privacy.merged(done) == [(start, end)]:
        # A crash after completion but before ingress cleanup must resume the
        # existing capture validation and deletion path, retaining raw evidence.
        return False
    spans = await privacy.capture_screening_spans(user_id, source_id, start, end)
    if spans is None or _unprocessed_spans(spans, done):
        return False
    return True


def _privacy_segments(segments, snapshot, source_id, processed_spans=()):
    """Slice transient processing WAVs; canonical capture and clocks never move."""
    safe = []
    for segment in segments:
        spans = snapshot.allowed_spans(source_id, segment.started_at, segment.ended_at)
        spans = _unprocessed_spans(spans, processed_spans)
        if spans == [(segment.started_at, segment.ended_at)]:
            safe.append(segment)
            continue
        if not spans:
            continue
        with wave.open(str(segment.path), "rb") as audio:
            rate, channels, width = (
                audio.getframerate(),
                audio.getnchannels(),
                audio.getsampwidth(),
            )
            pcm = audio.readframes(audio.getnframes())
        stride = channels * width
        for index, (start, end) in enumerate(spans):
            low = round((start - _as_utc(segment.started_at)).total_seconds() * rate)
            high = round((end - _as_utc(segment.started_at)).total_seconds() * rate)
            piece = pcm[low * stride : high * stride]
            if not piece:
                continue
            path = segment.path.with_name(
                f"{segment.path.stem}-private-cut-{index}.wav"
            )
            _write_wav(path, piece, rate, channels, width)
            safe.append(_Segment(segment.items, path, start, end, None, True))
    return safe


async def _process_screened_window(
    user, source_id, direction, session, output, capture, snapshot, input_identity=None
):
    """Retain staged private portions so a later override can process only new time.

    The progress ledger records completed absolute spans independently of the
    detector revision. Replays cannot transcribe the already admitted portions a
    second time or join private gaps into the temporary processing audio.
    """

    started = min(_as_utc(item.captured_at) for item in session)
    ended = max(_as_utc(item.ended_at or item.captured_at) for item in session)
    # Range IDs identify individual claims and are regenerated on reconstruction.
    # Capture identity, absolute bounds and chunk IDs remain stable across retries.
    identity = hashlib.sha256(
        (
            str(user.user_id)
            + capture.audio_range.model_dump_json(exclude={"range_id"})
        ).encode()
    ).hexdigest()
    ledger = privacy.database().privacy_audio_progress
    if input_identity is not None:
        # Written only after _persist_capture_window succeeds. A model failure,
        # missing input, or interrupted capture write cannot create a receipt.
        await privacy.database().privacy_audio_inputs.update_one(
            {"_id": input_identity},
            {
                "$setOnInsert": {
                    "user_id": str(user.user_id),
                    "source_id": source_id,
                    "progress_id": identity,
                    "started_at": started,
                    "ended_at": ended,
                },
            },
            upsert=True,
        )
    progress = await ledger.find_one({"_id": identity}) or {}
    done = [
        (privacy.utc(row["started_at"]), privacy.utc(row["ended_at"]))
        for row in progress.get("completed", [])
    ]
    whole = _Segment(session, output, started, ended, None, True)
    segments = await asyncio.to_thread(
        _privacy_segments, [whole], snapshot, source_id, done
    )
    processed = 0
    for segment in segments:
        if segment.profile is None:
            segment.profile = await asyncio.to_thread(_profile_wav, segment.path)
        claim = await _segment_audio_range(capture.audio_range, segment)
        # Local acoustic profiling produces no published or external result. Its
        # scheduling snapshot may age while other captures are screened. Admit
        # this exact claim against current policy before any semantic processing,
        # then retain that revision fence until its result has been recorded.
        admission = await privacy.load_snapshot(
            user.user_id, claim.started_at, claim.ended_at
        )
        if not admission.permits_record({"audio_ranges": [claim]}):
            raise privacy.PrivacyHeld()
        await privacy.assert_current(user.user_id, admission)
        detection = (
            _speech_detection(segment.profile)
            if require_speech_for_transcription()
            else None
        )
        if detection is not None and detection.should_reject:
            await _save_evidence_span(
                session,
                direction,
                segment.profile,
                state="no_speech",
                bounds=(segment.started_at, segment.ended_at),
                audio_ranges=[claim],
            )
        elif await _ingest_segment(user, source_id, direction, segment, claim) is None:
            continue
        else:
            processed += 1
        await privacy.assert_current(user.user_id, admission)
        row = {"started_at": segment.started_at, "ended_at": segment.ended_at}
        await ledger.update_one(
            {"_id": identity},
            {
                "$setOnInsert": {"user_id": str(user.user_id), "source_id": source_id},
                "$addToSet": {"completed": row},
            },
            upsert=True,
        )
        done.append((segment.started_at, segment.ended_at))

    if privacy.merged(done) == [(started, ended)]:
        completion = await privacy.load_snapshot(user.user_id, started, ended)
        if not completion.permits_record({"audio_ranges": [capture.audio_range]}):
            raise privacy.PrivacyHeld()
        await privacy.assert_current(user.user_id, completion)
        for item in session:
            await item.delete()
    return processed


def _write_wav(
    path: Path, pcm: bytes, sample_rate: int, channels: int, sample_width: int
) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def split_window(
    session: list[DeviceInputItem],
    window_start: datetime,
    cuts: list[float],
) -> list[tuple[list[DeviceInputItem], float, float]]:
    """Assign each source item to exactly one segment of the cut window.

    An item is placed by its midpoint, so a cut falling inside a 30-second source file
    does not duplicate it into both neighbours — which would double-count the audio and
    collide on the evidence span's (first item, last item) uniqueness key.

    Segments with no items are dropped: a cut can only ever land in quiet audio, but a
    window that begins or ends with a gap could still produce an empty edge.
    """
    bounds = [0.0, *cuts]
    ends = [*cuts, float("inf")]
    buckets: list[list[DeviceInputItem]] = [[] for _ in bounds]
    for item in session:
        start = (_as_utc(item.captured_at) - window_start).total_seconds()
        end = (
            _as_utc(item.ended_at or item.captured_at) - window_start
        ).total_seconds()
        middle = (start + max(start, end)) / 2
        for index, (low, high) in enumerate(zip(bounds, ends)):
            if low <= middle < high:
                buckets[index].append(item)
                break
    return [
        (items, low, high) for items, low, high in zip(buckets, bounds, ends) if items
    ]


def _profile_wav(path: Path) -> AudioEvidenceProfile:
    """Decode and profile an assembled session once."""
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            pcm = handle.readframes(handle.getnframes())
    except Exception as error:
        detail = str(error).strip()
        detail = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
        logger.warning(
            "speech_gate_unscored reason=%s detail=%s path=%s",
            SpeechDetectionReason.WAV_DECODE_FAILED.value,
            detail,
            path,
        )
        return AudioEvidenceProfile(
            scored=False,
            reason=SpeechDetectionReason.WAV_DECODE_FAILED,
            bucket_seconds=10.0,
            speech_seconds=None,
            longest_no_speech_seconds=None,
            acoustic_active_seconds=0,
            acoustic_quiet_seconds=0,
            speech_fraction=[],
            acoustic_active_fraction=[],
            rms_dbfs=[],
            peak_dbfs=[],
            provider=None,
            frame_hop_ms=None,
        )
    return profile_pcm_audio(pcm, sample_rate, channels, sample_width)


def _speech_detection(profile: AudioEvidenceProfile) -> SpeechDetectionResult:
    if not profile.scored:
        return SpeechDetectionResult.unscored(profile.reason, profile.reason.value)
    if profile.reason == SpeechDetectionReason.NO_SPEECH:
        return SpeechDetectionResult.no_speech()
    return SpeechDetectionResult.speech()


def _coverage_profile(
    items: list[DeviceInputItem],
    started_at: datetime,
    ended_at: datetime,
    bucket_seconds: float,
) -> tuple[float, float, list[float]]:
    intervals = sorted(
        (
            max(started_at, _as_utc(item.captured_at)),
            min(ended_at, _as_utc(item.ended_at or item.captured_at)),
        )
        for item in items
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    covered = sum((end - start).total_seconds() for start, end in merged)
    duration = (ended_at - started_at).total_seconds()
    bucket_count = max(1, int((duration + bucket_seconds - 0.000001) // bucket_seconds))
    fractions: list[float] = []
    for index in range(bucket_count):
        bucket_start = started_at + timedelta(seconds=index * bucket_seconds)
        bucket_end = min(ended_at, bucket_start + timedelta(seconds=bucket_seconds))
        bucket_duration = (bucket_end - bucket_start).total_seconds()
        overlap = sum(
            max(0.0, (min(end, bucket_end) - max(start, bucket_start)).total_seconds())
            for start, end in merged
        )
        fractions.append(
            min(1.0, overlap / bucket_duration) if bucket_duration else 0.0
        )
    return covered, max(0.0, duration - covered), fractions


async def _session_ingest_attempts(
    session: list[DeviceInputItem], direction: str
) -> int:
    """Ingest attempts recorded for this session start.

    A retry re-mixes a longer range, so it writes a *different* span row. Counting
    per (first, last) would therefore always read 1. Sum across every span sharing
    the session's first source item, which is stable while the session grows.
    """
    locator = _audio_locator(session[0])
    spans = await AudioEvidenceSpan.find(
        AudioEvidenceSpan.user_id == session[0].user_id,
        AudioEvidenceSpan.source_id == session[0].source_id,
        AudioEvidenceSpan.direction == direction,
        AudioEvidenceSpan.locator.track_id == locator.track_id,
        AudioEvidenceSpan.first_source_item_id == session[0].source_item_id,
        AudioEvidenceSpan.state == "failed",
    ).to_list()
    return sum(max(1, span.attempts) for span in spans)


async def _save_evidence_span(
    session: list[DeviceInputItem],
    direction: str,
    profile: AudioEvidenceProfile,
    state: str,
    conversation_id: str | None = None,
    bounds: tuple[datetime, datetime] | None = None,
    audio_ranges: Sequence[AudioRangeRef] = (),
) -> AudioEvidenceSpan:

    locator = _audio_locator(session[0])
    if any(_audio_locator(item) != locator for item in session[1:]):
        raise ValueError("audio evidence span cannot combine provider-local tracks")
    # A segment's bounds are the cut, not its items: a cut lands inside a source file,
    # and the span must describe the audio that was actually ingested.
    started_at = (
        bounds[0] if bounds else min(_as_utc(item.captured_at) for item in session)
    )
    ended_at = (
        bounds[1]
        if bounds
        else max(_as_utc(item.ended_at or item.captured_at) for item in session)
    )
    # A single source file can contribute multiple disjoint allowed portions.
    # BSON precision is also the replay identity used by persisted span bounds.
    started_at, ended_at = privacy.utc(started_at), privacy.utc(ended_at)
    source_item_ids = [item.source_item_id for item in session]
    covered, missing, coverage = _coverage_profile(
        session, started_at, ended_at, profile.bucket_seconds
    )
    series_length = len(profile.acoustic_active_fraction)
    if len(coverage) < series_length:
        coverage.extend([0.0] * (series_length - len(coverage)))
    elif len(coverage) > series_length:
        coverage = coverage[:series_length]
    range_hash = hashlib.sha256(
        json.dumps(
            [source_item_ids, started_at.isoformat(), ended_at.isoformat()],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    values = {
        "locator": locator,
        "source_item_ids": source_item_ids,
        "source_range_hash": range_hash,
        "started_at": started_at,
        "ended_at": ended_at,
        "meeting_id": _meeting_id(session[0]),
        "conversation_id": conversation_id,
        "audio_ranges": list(audio_ranges),
        "state": state,
        "covered_seconds": covered,
        "missing_seconds": missing,
        "bucket_seconds": profile.bucket_seconds,
        "coverage_fraction": coverage,
        "speech_fraction": profile.speech_fraction,
        "acoustic_active_fraction": profile.acoustic_active_fraction,
        "rms_dbfs": profile.rms_dbfs,
        "peak_dbfs": profile.peak_dbfs,
        "speech_seconds": profile.speech_seconds,
        "longest_no_speech_seconds": profile.longest_no_speech_seconds,
        "acoustic_active_seconds": profile.acoustic_active_seconds,
        "acoustic_quiet_seconds": profile.acoustic_quiet_seconds,
        "analysis": {
            "profile_version": "audio-evidence-v1",
            "vad_provider": profile.provider,
            "vad_reason": profile.reason.value,
            "vad_frame_hop_ms": profile.frame_hop_ms,
        },
    }
    existing = await AudioEvidenceSpan.find_one(
        AudioEvidenceSpan.user_id == session[0].user_id,
        AudioEvidenceSpan.source_id == session[0].source_id,
        AudioEvidenceSpan.direction == direction,
        AudioEvidenceSpan.locator.track_id == locator.track_id,
        AudioEvidenceSpan.first_source_item_id == source_item_ids[0],
        AudioEvidenceSpan.last_source_item_id == source_item_ids[-1],
        AudioEvidenceSpan.started_at == started_at,
        AudioEvidenceSpan.ended_at == ended_at,
    )
    if existing is not None:
        for field, value in values.items():
            setattr(existing, field, value)
        existing.attempts += 1
        await existing.save()
        await _mark_span_dirty(existing)
        return existing
    span = AudioEvidenceSpan(
        user_id=session[0].user_id,
        source_id=session[0].source_id,
        first_source_item_id=source_item_ids[0],
        last_source_item_id=source_item_ids[-1],
        direction=direction if direction in {"input", "output"} else "unknown",
        attempts=1,
        **values,
    )
    await span.insert()
    await _mark_span_dirty(span)
    return span


async def _mark_span_dirty(span: AudioEvidenceSpan) -> None:
    """Continuous capture is evidence too — its span bounds are the dirty range.

    A definitively silent span has no conversation to hang off, so this is the only
    trigger that reports it. Reconciliation still needs it: silence is what tells the
    reconciler an episode ended.
    """

    await note_evidence_dirty(
        span.user_id,
        span.started_at,
        span.ended_at,
        span.source_range_hash,
        "evidence_span",
        source_kind="evidence_span",
    )


def _carve_window(
    session: list[DeviceInputItem],
    path: Path,
    profile: AudioEvidenceProfile,
) -> list[_Segment]:
    """Cut one mixed capture window into the recordings it should have been.

    The mix and the VAD both already ran over the whole window, so the cut costs one
    PCM slice per segment and a re-profile of each — no second ffmpeg pass. A window
    the collector tagged as a meeting is never cut: its bounds came from a real signal
    and beat anything inferred from silence.
    """
    window_start = min(_as_utc(item.captured_at) for item in session)
    window_end = max(_as_utc(item.ended_at or item.captured_at) for item in session)
    whole = [_Segment(session, path, window_start, window_end, profile)]

    if _meeting_id(session[0]) is not None:
        return whole
    cuts = plan_session_cuts(profile.speech_fraction, profile.bucket_seconds)
    if not cuts:
        return whole

    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            pcm = handle.readframes(handle.getnframes())
    except Exception:
        logger.exception("Cutting window %s failed to decode; ingesting whole", path)
        return whole

    frame_bytes = sample_width * channels
    bytes_per_second = sample_rate * frame_bytes
    segments: list[_Segment] = []
    for index, (items, low, high) in enumerate(
        split_window(session, window_start, cuts)
    ):
        start_byte = int(low * sample_rate) * frame_bytes
        end_byte = (
            len(pcm) if high == float("inf") else int(high * sample_rate) * frame_bytes
        )
        piece = pcm[start_byte : min(end_byte, len(pcm))]
        if not piece:
            continue
        segment_path = path.with_name(f"{path.stem}-part{index}.wav")
        _write_wav(segment_path, piece, sample_rate, channels, sample_width)
        segments.append(
            _Segment(
                items=items,
                path=segment_path,
                started_at=window_start + timedelta(seconds=low),
                ended_at=window_start
                + timedelta(seconds=low + len(piece) / bytes_per_second),
                profile=profile_pcm_audio(piece, sample_rate, channels, sample_width),
            )
        )
    return segments or whole


async def _ingest_segment(
    user: User,
    source_id: str,
    direction: str,
    segment: _Segment,
    audio_range: AudioRangeRef,
) -> str | None:
    """Materialize one detected Conversation without copying capture audio."""

    await privacy.require_audio_ranges([audio_range])
    external_source_id = format_screenpipe_segment_source_id(
        source_id,
        direction,
        _audio_locator(segment.items[0]).track_id,
        segment.items[0].source_item_id,
        segment.items[-1].source_item_id,
    )
    if segment.privacy_partition:
        external_source_id += f":privacy:{int(segment.started_at.timestamp()*1000)}:{int(segment.ended_at.timestamp()*1000)}"
    try:
        conversation = await materialize_and_process_audio_claim(
            user,
            audio_range,
            device_name=f"{source_id}-{_audio_locator(segment.items[0]).track_id}",
            title="Detected conversation",
            segmentation_key=f"detected:{external_source_id}:{_CAPTURE_ASSEMBLY_VERSION}",
            external_source_id=external_source_id,
            external_source_type="screenpipe",
            # This is the semantic layer the user asked to see on Recordings. The
            # underlying full window remains capture_evidence on AudioCaptureSession;
            # only the speech-derived range is a visible Conversation.
            data_purpose="conversation",
            # This source record is eligible for durable memory, but the write is
            # deferred below until rolling Timeline classifies a conversational Episode.
            # ``memory_excluded`` is a permanent safety fence, not a scheduling flag:
            # leaving it set would make the later Timeline-triggered job no-op.
            memory_excluded=False,
            memory_exclusion_reason=None,
            skip_memory_extraction=True,
            # A VAD fragment is immediately playable/transcribed, but it is not a
            # complete semantic event: one recorder meeting can span adjacent files
            # and complementary input/output channels. Timeline owns the title and
            # summaries after it assembles those fragments into one episode.
            skip_title_summary=True,
        )
    except Exception:
        logger.exception(
            "Failed to materialize ScreenPipe claim %s", external_source_id
        )
        await _save_evidence_span(
            segment.items,
            direction,
            segment.profile,
            state="failed",
            bounds=(segment.started_at, segment.ended_at),
            audio_ranges=[audio_range],
        )
        return None

    conversation_id = conversation.conversation_id
    await _save_evidence_span(
        segment.items,
        direction,
        segment.profile,
        state="transcribed" if segment.profile.scored else "unscored",
        conversation_id=conversation_id,
        bounds=(segment.started_at, segment.ended_at),
        audio_ranges=[audio_range],
    )

    observations = await DeviceInputItem.find(
        DeviceInputItem.user_id == str(user.id),
        DeviceInputItem.source_id == source_id,
        DeviceInputItem.kind == "observation",
        DeviceInputItem.captured_at <= segment.ended_at,
        {
            "$or": [
                {"ended_at": None},
                {"ended_at": {"$gte": segment.started_at}},
            ]
        },
    ).to_list()
    for observation in observations:
        if conversation_id not in observation.related_conversation_ids:
            observation.related_conversation_ids.append(conversation_id)
            observation.related_conversation_ids.sort()
            observation.curation = "pending"
            await observation.save()
    return conversation_id


async def process_device_audio() -> dict[str, Any]:

    transcription_recovery = (
        await privacy_audio_recovery.resume_privacy_held_transcriptions()
    )
    pending = (
        await DeviceInputItem.find(
            DeviceInputItem.kind == "audio",
            DeviceInputItem.state == "received",
        )
        .sort([("source_id", 1), ("captured_at", 1)])
        .to_list()
    )
    by_source: dict[tuple[str, str, str, str], list[DeviceInputItem]] = {}
    for item in pending:
        by_source.setdefault(audio_stream_key(item), []).append(item)
    processed = 0
    rejected_no_speech = 0
    held_windows_before_decode = 0
    retained_windows_without_new_work = 0
    unscored_sessions = 0
    unscored_reasons: dict[str, int] = {}
    require_speech = require_speech_for_transcription()
    for (user_id, source_id, direction, _track_id), source_items in by_source.items():
        try:
            user = await User.get(PydanticObjectId(user_id))
        except Exception:
            user = None
        if user is None:
            continue
        for session in group_audio_sessions(source_items):
            try:
                session_end = max(
                    _as_utc(item.ended_at or item.captured_at) for item in session
                )
                if session_end > utcnow() - _CLOSE_DELAY:
                    continue

                session_start = min(_as_utc(item.captured_at) for item in session)
                try:
                    screening_spans = await privacy.capture_screening_spans(
                        user_id, source_id, session_start, session_end
                    )
                except privacy.PrivacyHeld:
                    held_windows_before_decode += 1
                    continue
                if screening_spans == []:
                    # Original uploaded bytes remain in DeviceInputItem. Wait for
                    # screening or review before decoding an entirely held window.
                    # This is only a deferral: admission below always reloads policy.
                    held_windows_before_decode += 1
                    continue
                input_identity = await asyncio.to_thread(_audio_input_identity, session)
                if await _screened_audio_has_no_new_work(
                    user_id, source_id, input_identity
                ):
                    retained_windows_without_new_work += 1
                    continue
                with tempfile.TemporaryDirectory(
                    prefix="chronicle-screenpipe-"
                ) as temp_dir:
                    output = (
                        Path(temp_dir)
                        / f"screenpipe-{source_id}-{session[0].source_item_id}.wav"
                    )
                    await _mix_session(session, Path(temp_dir), output)
                    # Capture is evidence, not a Conversation. Persist the entire
                    # window first—even silence—then let VAD decide whether any
                    # semantic Conversation should claim part of it. The deterministic
                    # session id makes a retry resume the same chunks.
                    capture = await _persist_capture_window(
                        user, source_id, direction, session, output
                    )
                    privacy_snapshot = await privacy.load_snapshot(
                        user_id, session_start, session_end
                    )
                    if privacy_snapshot.source(source_id):
                        processed += await _process_screened_window(
                            user,
                            source_id,
                            direction,
                            session,
                            output,
                            capture,
                            privacy_snapshot,
                            input_identity,
                        )
                        continue
                    # Decode + energy loop + one ctypes VAD call per 256-sample hop:
                    # ~112k foreign calls for a 30-minute window. Cron jobs run on the
                    # API's own loop, so doing that inline stops the whole process.
                    profile = await asyncio.to_thread(_profile_wav, output)
                    speech_detection = (
                        _speech_detection(profile) if require_speech else None
                    )
                    if (
                        speech_detection is not None
                        and speech_detection.has_speech is None
                    ):
                        reason = speech_detection.reason.value
                        unscored_sessions += 1
                        unscored_reasons[reason] = unscored_reasons.get(reason, 0) + 1
                    if speech_detection is not None and speech_detection.should_reject:
                        await _save_evidence_span(
                            session,
                            direction,
                            profile,
                            state="no_speech",
                            audio_ranges=[capture.audio_range],
                        )
                        # Silent capture remains durable evidence but never enters the
                        # semantic Conversation/transcription pipeline.
                        logger.info(
                            "🔇 ScreenPipe session %s-%s (%d chunks) has no speech "
                            "— raw capture retained without a Conversation "
                            "(reason=%s, scored=%s)",
                            source_id,
                            direction,
                            len(session),
                            speech_detection.reason.value,
                            speech_detection.scored,
                        )
                        for item in session:
                            await item.delete()
                        rejected_no_speech += 1
                        continue
                    # Re-decodes the window and re-profiles every piece it cuts, so it
                    # costs another full VAD pass per segment.
                    segments = await asyncio.to_thread(
                        _carve_window, session, output, profile
                    )
                    if len(segments) > 1:
                        logger.info(
                            "✂️ Capture window %s-%s (%.0f min) cut into %d recordings "
                            "at quiet points instead of at a fixed %.0f-minute mark",
                            source_id,
                            direction,
                            (
                                session_end - _as_utc(session[0].captured_at)
                            ).total_seconds()
                            / 60,
                            len(segments),
                            _TARGET_SESSION.total_seconds() / 60,
                        )
                    for segment in segments:
                        segment_range = await _segment_audio_range(
                            capture.audio_range, segment
                        )
                        ingested = await _ingest_segment(
                            user, source_id, direction, segment, segment_range
                        )
                        if ingested is None:
                            # Raw capture is already durable. Leave the transport items
                            # staged for bounded semantic-materialization retries; the
                            # deterministic capture id prevents audio duplication.
                            attempts = await _session_ingest_attempts(
                                segment.items, direction
                            )
                            if attempts >= _MAX_INGEST_ATTEMPTS:
                                logger.error(
                                    "🛑 ScreenPipe window %s-%s (start=%s) failed to "
                                    "materialize %d times — dropping %d staged input "
                                    "items; raw capture remains durable",
                                    source_id,
                                    direction,
                                    segment.items[0].source_item_id,
                                    attempts,
                                    len(segment.items),
                                )
                                await _save_evidence_span(
                                    segment.items,
                                    direction,
                                    segment.profile,
                                    state="abandoned",
                                    bounds=(segment.started_at, segment.ended_at),
                                    audio_ranges=[segment_range],
                                )
                                for item in segment.items:
                                    await item.delete()
                            continue
                        for item in segment.items:
                            await item.delete()
                        processed += 1
            except Exception:
                # One unlucky session used to abort the whole run, leaving every
                # later session's chunks staged behind it indefinitely.
                logger.exception(
                    "Failed to ingest ScreenPipe session %s-%s (start=%s)",
                    source_id,
                    direction,
                    session[0].source_item_id,
                )
                continue
    return {
        "privacy_transcription_recovery": transcription_recovery,
        "pending_chunks": len(pending),
        "processed_sessions": processed,
        "rejected_no_speech": rejected_no_speech,
        "held_windows_before_decode": held_windows_before_decode,
        "retained_windows_without_new_work": retained_windows_without_new_work,
        "vad_unscored_sessions": unscored_sessions,
        "vad_unscored_reasons": unscored_reasons,
    }
