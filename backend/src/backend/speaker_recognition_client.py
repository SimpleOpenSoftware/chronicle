"""
Speaker recognition client for integrating with the speaker recognition service.

This module provides an optional integration with the speaker recognition service
to enhance transcripts with actual speaker names instead of generic labels.

Configuration is managed via config.yml (speaker_recognition section).

NOTE: user_id is currently hardcoded to "1" throughout this client because only
a single admin user is supported at this time. Update when multi-user support
is implemented.
"""

import asyncio
import io
import json
import logging
import os
import traceback
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from aiohttp import ClientConnectorError

import backend.service_deployment as service_deployment
from backend.config import get_diarization_settings
from backend.model_registry import get_models_registry
from backend.models.conversation import Conversation
from backend.services.speaker_gallery_privacy import (
    GalleryLogFilter,
    checked_response,
    guard_gallery,
    request_headers,
)
from backend.utils.audio_chunk_utils import reconstruct_audio_ranges
from backend.utils.audio_extraction import extract_audio_for_results
from backend.utils.audio_utils import pcm_to_wav_bytes
from backend.utils.segment_utils import is_non_speech

logger = logging.getLogger(__name__)
logger.addFilter(GalleryLogFilter())

SPEAKER_IDENTIFY_CONCURRENCY = int(os.getenv("SPEAKER_IDENTIFY_CONCURRENCY", "8"))
SPEAKER_IDENTIFY_BATCH_SIZE = int(os.getenv("SPEAKER_IDENTIFY_BATCH_SIZE", "32"))
SPEAKER_IDENTIFY_BATCH_MAX_SECONDS = float(
    os.getenv("SPEAKER_IDENTIFY_BATCH_MAX_SECONDS", "220")
)


def _pack_identification_batches(
    segments: List[Dict],
    indices: List[int],
    *,
    max_items: int,
    max_seconds: float,
) -> List[List[int]]:
    """Pack ordered segments under both service item and duration limits."""
    if max_items < 1 or max_seconds <= 0:
        raise ValueError("Speaker batch limits must be positive")
    batches: List[List[int]] = []
    current: List[int] = []
    current_seconds = 0.0
    for index in indices:
        duration = max(
            0.0, float(segments[index]["end"]) - float(segments[index]["start"])
        )
        if current and (
            len(current) >= max_items or current_seconds + duration > max_seconds
        ):
            batches.append(current)
            current = []
            current_seconds = 0.0
        current.append(index)
        current_seconds += duration
    if current:
        batches.append(current)
    return batches


def _require_wav_audio(wav_bytes: bytes) -> None:
    """Reject an empty/invalid local reconstruction before calling the service."""

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frames = wav_file.getnframes()
    except (EOFError, wave.Error) as error:
        raise ValueError("Reconstructed segment contains no decodable audio") from error
    if frames <= 0:
        raise ValueError("Reconstructed segment contains no audio frames")


def _select_label_mappings(
    label_votes: Dict[str, List[tuple[str, float]]],
    *,
    similarity_threshold: float,
) -> Dict[str, tuple[str, float]]:
    """Choose conservative, one-to-one names for diarized speaker labels.

    Two agreeing samples are enough. A lone sample must clear a deliberately
    stricter threshold, and an enrolled identity may name only one diarized label.
    """
    candidates: list[tuple[str, str, int, float]] = []
    single_sample_threshold = max(0.65, similarity_threshold + 0.15)
    for label, votes in label_votes.items():
        by_name: Dict[str, List[float]] = {}
        for name, confidence in votes:
            by_name.setdefault(name, []).append(float(confidence))
        if not by_name:
            continue
        best_name = max(
            by_name,
            key=lambda name: (
                len(by_name[name]),
                sum(by_name[name]) / len(by_name[name]),
            ),
        )
        scores = by_name[best_name]
        average = sum(scores) / len(scores)
        if len(scores) < 2 and average < single_sample_threshold:
            logger.info(
                "Speaker label %r left unknown: one sample confidence %.3f < %.3f",
                label,
                average,
                single_sample_threshold,
            )
            continue
        candidates.append((label, best_name, len(scores), average))

    selected: Dict[str, tuple[str, float]] = {}
    used_names: set[str] = set()
    for label, name, vote_count, average in sorted(
        candidates, key=lambda item: (item[2], item[3]), reverse=True
    ):
        identity = name.casefold()
        if identity in used_names:
            logger.info(
                "Speaker label %r left unknown: identity %r already assigned",
                label,
                name,
            )
            continue
        used_names.add(identity)
        selected[label] = (name, average)
    return selected


class SpeakerRecognitionClient:
    """Client for communicating with the speaker recognition service."""

    def __init__(self, service_url: Optional[str] = None):
        """
        Initialize the speaker recognition client.

        Configuration is read from config.yml (speaker_recognition section).
        The 'enabled' flag controls whether speaker recognition is active.

        Args:
            service_url: URL of the speaker recognition service (e.g., http://speaker-service:8085)
                        If not provided, uses config.yml service_url or SPEAKER_SERVICE_URL env var
        """
        # Check if we should use mock client (for testing)
        if os.getenv("USE_MOCK_SPEAKER_CLIENT") == "true":
            try:
                # Import mock client from testing module
                from backend.testing.mock_speaker_client import (
                    MockSpeakerRecognitionClient,
                )

                self._mock_client = MockSpeakerRecognitionClient()
                self.enabled = True
                self.service_url = "mock://speaker-service"
                logger.info("🎤 Using MOCK speaker recognition client for tests")
                return
            except ImportError as e:
                logger.error(f"Failed to import mock speaker client: {e}")
                # Fall through to normal initialization

        # Load speaker recognition config from config.yml
        registry = get_models_registry()
        if not registry or not registry.speaker_recognition:
            # No config found, default to disabled
            self.enabled = False
            self.service_url = None
            logger.info("Speaker recognition client disabled (no configuration found)")
            return

        speaker_config = registry.speaker_recognition
        if not speaker_config.get("enabled", True):
            # Disabled in config
            self.enabled = False
            self.service_url = None
            logger.info(
                "Speaker recognition client disabled (config.yml enabled=false)"
            )
            return

        self._gateway_headers = {}
        deployment = speaker_config.get("deployment")
        if deployment:
            if (
                service_url
                or speaker_config.get("service_url")
                or os.getenv("SPEAKER_SERVICE_URL")
            ):
                raise ValueError(
                    "Managed speaker recognition cannot also configure SPEAKER_SERVICE_URL or service_url"
                )

            self.service_url = service_deployment.gateway_url(deployment, "speaker")
            self._gateway_headers = {
                "X-Chronicle-Service-Token": service_deployment.gateway_token()
            }
            self.enabled = True
            return

        # Enabled - determine URL (priority: param > config > env var > minidisc)
        self.service_url = (
            service_url
            or speaker_config.get("service_url")
            or os.getenv("SPEAKER_SERVICE_URL")
        )

        if not self.service_url:
            try:
                from discovery import CHRONICLE_SPEAKER, resolve_service_url

                self.service_url = resolve_service_url(
                    None, CHRONICLE_SPEAKER, default=None
                )
            except ImportError:
                pass

        self.enabled = bool(self.service_url)

        if self.enabled:
            logger.info(
                f"Speaker recognition client initialized with URL: {self.service_url}"
            )
        else:
            logger.info(
                "Speaker recognition client disabled (no service URL configured)"
            )

    def calculate_timeout(self, audio_duration: Optional[float]) -> float:
        """
        Calculate proportional timeout based on audio duration.

        Uses the formula: timeout = min(MAX_TIMEOUT, audio_duration * MULTIPLIER + BASE_TIMEOUT)

        Args:
            audio_duration: Duration of audio in seconds

        Returns:
            Calculated timeout in seconds
        """
        BASE_TIMEOUT = 30.0  # Fallback for unknown-duration files
        MIN_KNOWN_DURATION_TIMEOUT = 900.0
        TIMEOUT_MULTIPLIER = (
            8.0  # Processing speed ratio (e.g., 1 min audio = 8 min timeout)
        )
        # Long recordings are processed as bounded 20-minute neural passes, but one
        # HTTP request remains open across every pass.  A ten-hour corpus recording
        # takes longer than the former ten-minute ceiling even though no individual
        # inference is unbounded.  Keep a finite request-level ceiling with enough
        # room for the chunk sequence to finish.
        MAX_TIMEOUT = 3600.0  # 1 hour cap for very long, chunked requests

        if audio_duration is None or audio_duration <= 0:
            logger.warning("Audio duration unknown or invalid, using base timeout")
            return BASE_TIMEOUT

        calculated_timeout = audio_duration * TIMEOUT_MULTIPLIER + BASE_TIMEOUT
        timeout = min(MAX_TIMEOUT, max(MIN_KNOWN_DURATION_TIMEOUT, calculated_timeout))

        logger.info(
            f"🕐 Calculated timeout: audio_duration={audio_duration:.1f}s → "
            f"timeout={timeout:.1f}s (base={BASE_TIMEOUT}, multiplier={TIMEOUT_MULTIPLIER}, max={MAX_TIMEOUT})"
        )
        return timeout

    @guard_gallery
    async def diarize_identify_match(
        self,
        conversation_id: str,
        backend_token: str,
        transcript_data: Dict,
        user_id: Optional[str] = None,
        audio_ranges: Optional[list[tuple[float, float]]] = None,
    ) -> Dict:
        """
        Perform diarization, speaker identification, and word-to-speaker matching.

        Speaker service fetches audio from backend and handles chunking based on its
        own memory constraints.

        Args:
            conversation_id: Conversation ID for speaker service to fetch audio
            backend_token: JWT token for speaker service to authenticate with backend
            transcript_data: Dict containing words array and text from transcription
            user_id: Optional user ID for speaker identification

        Returns:
            Dictionary containing segments with matched text and speaker identification
        """
        # Use mock client if configured
        if hasattr(self, "_mock_client"):
            return await self._mock_client.diarize_identify_match(
                conversation_id,
                backend_token,
                transcript_data,
                user_id,
                audio_ranges=audio_ranges,
            )

        if not self.enabled:
            logger.info(f"🎤 Speaker recognition disabled, returning empty result")
            return {"segments": []}

        # Fetch conversation to get audio duration for timeout calculation
        conversation = await Conversation.find_one(
            Conversation.conversation_id == conversation_id
        )
        audio_duration = conversation.audio_total_duration if conversation else None

        # Calculate proportional timeout based on audio duration
        timeout = self.calculate_timeout(audio_duration)

        try:
            logger.info(
                f"🎤 Calling speaker service with conversation_id: {conversation_id[:12]}..."
            )

            # Pyannote parameters from diarization settings. This path always
            # diarizes via the speaker service; transcripts whose provider
            # already diarized are routed to identify_provider_segments()
            # by the speaker job instead.
            config = get_diarization_settings()

            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                # Prepare form data with conversation_id + backend_token
                form_data = aiohttp.FormData()
                form_data.add_field("conversation_id", conversation_id)
                form_data.add_field("backend_token", backend_token)

                # Send existing transcript for diarization and speaker matching
                form_data.add_field("transcript_data", json.dumps(transcript_data))
                if audio_ranges:
                    form_data.add_field("audio_ranges", json.dumps(audio_ranges))
                if user_id is None:
                    raise ValueError(
                        "diarize_identify_match requires a Chronicle user_id: it "
                        "selects which speaker gallery is searched"
                    )
                form_data.add_field("user_id", user_id)
                form_data.add_field(
                    "similarity_threshold",
                    str(config.get("similarity_threshold", 0.45)),
                )

                # Add pyannote diarization parameters
                form_data.add_field(
                    "min_duration", str(config.get("min_duration", 0.0))
                )
                form_data.add_field("collar", str(config.get("collar", 2.0)))
                form_data.add_field(
                    "min_duration_off", str(config.get("min_duration_off", 0.0))
                )
                if config.get("min_speakers"):
                    form_data.add_field("min_speakers", str(config.get("min_speakers")))
                if config.get("max_speakers"):
                    form_data.add_field("max_speakers", str(config.get("max_speakers")))

                # Cross-chunk reconciliation + open-set identification knobs
                form_data.add_field(
                    "reconciliation_threshold",
                    str(config.get("reconciliation_threshold", 0.4)),
                )
                form_data.add_field(
                    "identify_margin", str(config.get("identify_margin", 0.1))
                )
                form_data.add_field(
                    "exclusive", str(config.get("exclusive", True)).lower()
                )

                # Use /v1/diarize-identify-match endpoint for backend integration
                endpoint = "/v1/diarize-identify-match"

                # Make the request to the consolidated endpoint
                request_url = f"{self.service_url}{endpoint}"
                logger.info(
                    f"🎤 DEBUG: Making request to speaker service URL: {request_url}"
                )

                async with session.post(
                    request_url,
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    logger.info(
                        f"🎤 Speaker service response status: {response.status}"
                    )

                    if response.status != 200:
                        response_text = await checked_response(response, "text")
                        logger.error(
                            f"🎤 ❌ Speaker service returned status {response.status}: {response_text}"
                        )
                        # An HTTP error is not an empty result — surface it so the
                        # job's error handling fails the job instead of reporting
                        # success with no identified speakers.
                        return {
                            "error": "server_error",
                            "message": f"HTTP {response.status}: {response_text[:500]}",
                            "segments": [],
                        }

                    result = await checked_response(response, "json")

                    # Log basic result info
                    num_segments = len(result.get("segments", []))
                    logger.info(
                        f"🎤 Speaker recognition returned {num_segments} segments"
                    )

                    return result

        except ClientConnectorError as e:
            logger.error(f"🎤 Failed to connect to speaker recognition service: {e}")
            return {"error": "connection_failed", "message": str(e), "segments": []}
        except asyncio.TimeoutError as e:
            logger.error(f"🎤 Timeout connecting to speaker recognition service: {e}")
            return {"error": "timeout", "message": str(e), "segments": []}
        except aiohttp.ClientError as e:
            logger.warning(f"🎤 Client error during speaker recognition: {e}")
            return {"error": "client_error", "message": str(e), "segments": []}
        except Exception as e:
            logger.error(f"🎤 Error during speaker recognition: {e}")
            return {"error": "unknown_error", "message": str(e), "segments": []}

    @guard_gallery
    async def identify_segment(
        self,
        audio_wav_bytes: bytes,
        user_id: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
    ) -> Dict:
        """
        Identify a single speaker from a WAV audio segment via POST /identify.

        Args:
            audio_wav_bytes: WAV audio bytes for a single segment
            user_id: Optional user ID to scope identification
            similarity_threshold: Optional similarity threshold override

        Returns:
            Dict with keys: found, speaker_id, speaker_name, confidence, status, duration
        """
        if hasattr(self, "_mock_client"):
            return await self._mock_client.identify_segment(
                audio_wav_bytes, user_id, similarity_threshold
            )

        if not self.enabled:
            return {
                "found": False,
                "speaker_name": None,
                "confidence": 0.0,
                "status": "unknown",
            }

        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                form_data = aiohttp.FormData()
                form_data.add_field(
                    "file",
                    audio_wav_bytes,
                    filename="segment.wav",
                    content_type="audio/wav",
                )
                if user_id is not None:
                    form_data.add_field("user_id", user_id)
                if similarity_threshold is not None:
                    form_data.add_field(
                        "similarity_threshold", str(similarity_threshold)
                    )

                async with session.post(
                    f"{self.service_url}/identify",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        response_text = await checked_response(response, "text")
                        logger.warning(
                            f"🎤 /identify returned status {response.status}: {response_text}"
                        )
                        return {
                            "found": False,
                            "speaker_name": None,
                            "confidence": 0.0,
                            "status": "error",
                        }

                    return await checked_response(response, "json")

        except ClientConnectorError as e:
            logger.error(f"🎤 Failed to connect to speaker service /identify: {e}")
            return {
                "found": False,
                "speaker_name": None,
                "confidence": 0.0,
                "status": "error",
            }

        except asyncio.TimeoutError:
            logger.error("🎤 Timeout calling speaker service /identify")
            return {
                "found": False,
                "speaker_name": None,
                "confidence": 0.0,
                "status": "error",
            }
        except aiohttp.ClientError as e:
            logger.warning(f"🎤 Client error during /identify: {e}")
            return {
                "found": False,
                "speaker_name": None,
                "confidence": 0.0,
                "status": "error",
            }
        except Exception as e:
            logger.error(f"🎤 Error during /identify: {e}")
            return {
                "found": False,
                "speaker_name": None,
                "confidence": 0.0,
                "status": "error",
            }

    @guard_gallery
    async def identify_batch(
        self,
        clips: List[tuple[str, bytes]],
        user_id: Optional[str] = None,
        similarity_threshold: Optional[float] = None,
        *,
        session: Optional[aiohttp.ClientSession] = None,
        include_embeddings: bool = False,
    ) -> Dict:
        """Identify an ordered clip batch through one HTTP request."""
        if not clips:
            return {
                "results": [],
                "batch": {"requested": 0, "processed": 0, "failed": 0},
            }
        owns_session = session is None
        if session is None:
            session = aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            )
        try:
            form_data = aiohttp.FormData()
            for segment_id, audio_wav_bytes in clips:
                _require_wav_audio(audio_wav_bytes)
                form_data.add_field(
                    "files",
                    audio_wav_bytes,
                    filename=f"segment-{segment_id}.wav",
                    content_type="audio/wav",
                )
                form_data.add_field("segment_ids", segment_id)
            if user_id is not None:
                form_data.add_field("user_id", user_id)
            if similarity_threshold is not None:
                form_data.add_field("similarity_threshold", str(similarity_threshold))
            form_data.add_field("include_embeddings", str(include_embeddings).lower())
            async with session.post(
                f"{self.service_url}/identify/batch",
                headers=request_headers(getattr(self, "_gateway_headers", {})),
                data=form_data,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    response_text = await checked_response(response, "text")
                    return {
                        "error": "server_error",
                        "message": f"HTTP {response.status}: {response_text[:500]}",
                        "results": [],
                    }
                return await checked_response(response, "json")
        except ClientConnectorError as error:
            return {
                "error": "connection_failed",
                "message": str(error),
                "results": [],
            }
        except asyncio.TimeoutError as error:
            return {"error": "timeout", "message": str(error), "results": []}
        except aiohttp.ClientError as error:
            return {
                "error": "client_error",
                "message": str(error),
                "results": [],
            }
        finally:
            if owns_session:
                await session.close()

    @guard_gallery
    async def identify_provider_segments(
        self,
        conversation_id: str,
        segments: List[Dict],
        user_id: Optional[str] = None,
        per_segment: bool = False,
        min_segment_duration: float = 1.5,
    ) -> Dict:
        """
        Identify speakers in provider-diarized segments.

        Default mode: majority-vote per label. Picks top 3 longest segments per label,
        identifies each, and majority-votes to map labels to names.

        Per-segment mode (per_segment=True): identifies every segment individually.
        Used during reprocessing so that fine-tuned embeddings benefit each segment.

        Args:
            conversation_id: Conversation ID for audio extraction from MongoDB
            segments: List of dicts with keys: start, end, text, speaker
            user_id: Optional user ID for speaker identification
            per_segment: If True, identify each segment individually instead of majority-vote
            min_segment_duration: Minimum segment duration in seconds for identification

        Returns:
            Dict with 'segments' list matching diarize_identify_match() format
        """
        if hasattr(self, "_mock_client"):
            return await self._mock_client.identify_provider_segments(
                conversation_id,
                segments,
                user_id,
                per_segment=per_segment,
                min_segment_duration=min_segment_duration,
            )

        if not self.enabled:
            return {"segments": []}

        config = get_diarization_settings()
        similarity_threshold = config.get("similarity_threshold", 0.45)

        MAX_SAMPLES_PER_LABEL = 3

        def _is_non_speech(seg: Dict) -> bool:
            return is_non_speech(
                seg.get("text", ""),
                str(seg.get("speaker", "")),
            )

        # Separate speech and non-speech segments
        speech_segments = []
        non_speech_indices = set()
        for i, seg in enumerate(segments):
            if _is_non_speech(seg):
                non_speech_indices.add(i)
            else:
                speech_segments.append(seg)

        # Group speech segments by speaker label
        label_groups: Dict[str, List[Dict]] = {}
        for seg in speech_segments:
            label = seg.get("speaker", "Unknown")
            label_groups.setdefault(label, []).append(seg)

        logger.info(
            f"🎤 Segment-level identification: {len(segments)} segments "
            f"({len(non_speech_indices)} non-speech filtered), "
            f"{len(label_groups)} unique labels: {list(label_groups.keys())}"
        )

        # Per-segment mode: identify every segment individually (used during reprocess)
        if per_segment:
            return await self._identify_per_segment(
                conversation_id=conversation_id,
                segments=segments,
                speech_segments=speech_segments,
                non_speech_indices=non_speech_indices,
                user_id=user_id,
                similarity_threshold=similarity_threshold,
                min_segment_duration=min_segment_duration,
            )

        # For each label, pick top N longest segments >= min_segment_duration
        label_samples: Dict[str, List[Dict]] = {}
        for label, segs in label_groups.items():
            eligible = [
                s for s in segs if (s["end"] - s["start"]) >= min_segment_duration
            ]
            eligible.sort(key=lambda s: s["end"] - s["start"], reverse=True)
            label_samples[label] = eligible[:MAX_SAMPLES_PER_LABEL]
            if not label_samples[label]:
                logger.info(
                    f"🎤 Label '{label}': no segments >= {min_segment_duration}s, skipping identification"
                )

        sample_refs: List[tuple[str, Dict]] = []
        for label, samples in label_samples.items():
            for seg in samples:
                sample_refs.append((label, seg))

        sample_results: List[Optional[Dict]] = [None] * len(sample_refs)
        async with aiohttp.ClientSession(
            headers=getattr(self, "_gateway_headers", {})
        ) as session:
            for offset in range(0, len(sample_refs), SPEAKER_IDENTIFY_BATCH_SIZE):
                batch_refs = sample_refs[offset : offset + SPEAKER_IDENTIFY_BATCH_SIZE]
                ranges = [(seg["start"], seg["end"]) for _, seg in batch_refs]
                try:
                    wav_files = await reconstruct_audio_ranges(conversation_id, ranges)
                    for wav_file in wav_files:
                        _require_wav_audio(wav_file)
                except ValueError as error:
                    logger.warning(
                        "🎤 Failed to reconstruct %d majority-vote samples: %s",
                        len(batch_refs),
                        error,
                    )
                    continue

                batch_response = await self.identify_batch(
                    [
                        (str(offset + index), wav_file)
                        for index, wav_file in enumerate(wav_files)
                    ],
                    user_id=user_id,
                    similarity_threshold=similarity_threshold,
                    session=session,
                )
                if batch_response.get("error"):
                    logger.warning(
                        "🎤 Failed to identify %d majority-vote samples: %s",
                        len(batch_refs),
                        batch_response.get("message", "Batch failed"),
                    )
                    continue
                response_by_id = {
                    str(result["segment_id"]): result
                    for result in batch_response.get("results", [])
                }
                for index in range(len(batch_refs)):
                    sample_results[offset + index] = response_by_id.get(
                        str(offset + index)
                    )

        # Majority-vote per label
        label_votes: Dict[str, List[tuple[str, float]]] = {}
        identification_evidence: Dict[str, Dict] = {}
        result_offset = 0
        for label, samples in label_samples.items():
            votes: List[tuple[str, float]] = []
            sample_evidence = []
            for sample in samples:
                result = sample_results[result_offset]
                result_offset += 1
                sample_evidence.append(
                    {
                        "start": sample["start"],
                        "end": sample["end"],
                        "duration": sample["end"] - sample["start"],
                        "found": bool(result and result.get("found")),
                        "confidence": (result or {}).get("confidence", 0.0),
                        "candidates": (result or {}).get("candidates", []),
                    }
                )
                if not result:
                    continue
                if result and result.get("found"):
                    name = result.get("speaker_name", "Unknown")
                    confidence = result.get("confidence", 0.0)
                    votes.append((name, confidence))
            label_votes[label] = votes
            identification_evidence[label] = {"samples": sample_evidence}
            if not votes:
                logger.info(
                    f"🎤 Label '{label}' -> no identification (keeping original)"
                )
        label_mapping = _select_label_mappings(
            label_votes, similarity_threshold=similarity_threshold
        )
        for label, (name, confidence) in label_mapping.items():
            logger.info("🎤 Label %r -> %r (conf=%.3f)", label, name, confidence)
        for label, evidence in identification_evidence.items():
            mapped = label_mapping.get(label)
            evidence["assigned_name"] = mapped[0] if mapped else None
            evidence["assigned_confidence"] = mapped[1] if mapped else 0.0

        # Build result segments in same format as diarize_identify_match()
        # Non-speech segments are kept but not speaker-identified
        result_segments = []
        for i, seg in enumerate(segments):
            label = seg.get("speaker", "Unknown")
            if i in non_speech_indices:
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": label,
                        "confidence": 0.0,
                        "status": "non_speech",
                    }
                )
            else:
                mapped = label_mapping.get(label)
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": mapped[0] if mapped else None,
                        "confidence": mapped[1] if mapped else 0.0,
                        "status": "identified" if mapped else "unknown",
                    }
                )

        identified_count = sum(1 for m in label_mapping.values() if m)
        logger.info(
            f"🎤 Segment identification complete: {identified_count}/{len(label_groups)} labels identified, "
            f"{len(result_segments)} total segments ({len(non_speech_indices)} non-speech kept as-is)"
        )

        return {
            "segments": result_segments,
            "identification_evidence": {
                "mode": "majority_vote",
                "similarity_threshold": similarity_threshold,
                "labels": identification_evidence,
            },
        }

    async def _identify_per_segment(
        self,
        conversation_id: str,
        segments: List[Dict],
        speech_segments: List[Dict],
        non_speech_indices: set,
        user_id: Optional[str],
        similarity_threshold: float,
        min_segment_duration: float,
    ) -> Dict:
        """
        Identify every speech segment individually (no majority vote).

        Used during reprocessing so that fine-tuned speaker embeddings
        benefit each segment directly.

        Args:
            conversation_id: Conversation ID for audio extraction
            segments: All segments (speech + non-speech) in original order
            speech_segments: Only the speech segments
            non_speech_indices: Indices of non-speech segments in the original list
            user_id: User ID for speaker identification
            similarity_threshold: Similarity threshold for identification
            min_segment_duration: Minimum duration for identification attempt

        Returns:
            Dict with 'segments' list matching diarize_identify_match() format
        """
        logger.info(
            f"🎤 Per-segment identification: {len(speech_segments)} speech segments "
            f"(min_duration={min_segment_duration}s)"
        )

        eligible_indices = []
        for i, seg in enumerate(segments):
            if i in non_speech_indices:
                continue
            duration = seg["end"] - seg["start"]
            if duration >= min_segment_duration:
                eligible_indices.append(i)

        segment_results: List[Optional[Dict]] = [None] * len(segments)
        async with aiohttp.ClientSession(
            headers=getattr(self, "_gateway_headers", {})
        ) as session:
            batches = _pack_identification_batches(
                segments,
                eligible_indices,
                max_items=SPEAKER_IDENTIFY_BATCH_SIZE,
                max_seconds=SPEAKER_IDENTIFY_BATCH_MAX_SECONDS,
            )
            for batch_indices in batches:
                batch_ranges = [
                    (segments[index]["start"], segments[index]["end"])
                    for index in batch_indices
                ]
                try:
                    wav_files = await reconstruct_audio_ranges(
                        conversation_id,
                        batch_ranges,
                    )
                    for wav_file in wav_files:
                        _require_wav_audio(wav_file)
                except ValueError as error:
                    logger.error(
                        "Transcript/audio data error for %d-segment batch: %s",
                        len(batch_indices),
                        error,
                    )
                    for index in batch_indices:
                        segment_results[index] = {
                            "_local_error": "transcript_data_error",
                            "message": str(error),
                        }
                    continue

                batch_response = await self.identify_batch(
                    [
                        (str(index), wav_file)
                        for index, wav_file in zip(batch_indices, wav_files)
                    ],
                    user_id=user_id,
                    similarity_threshold=similarity_threshold,
                    session=session,
                    include_embeddings=True,
                )
                if batch_response.get("error"):
                    for index in batch_indices:
                        segment_results[index] = {
                            "_local_error": "speaker_client_error",
                            "message": batch_response.get("message", "Batch failed"),
                        }
                    continue
                response_by_id = {
                    str(result["segment_id"]): result
                    for result in batch_response.get("results", [])
                }
                for index in batch_indices:
                    segment_results[index] = response_by_id.get(
                        str(index),
                        {
                            "_local_error": "speaker_client_error",
                            "message": "Batch response omitted this segment",
                        },
                    )

        # Build result segments
        result_segments = []
        identified_count = 0
        error_count = 0
        data_error_count = 0
        for i, seg in enumerate(segments):
            label = seg.get("speaker", "Unknown")

            if i in non_speech_indices:
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": label,
                        "confidence": 0.0,
                        "status": "non_speech",
                    }
                )
                continue

            if i not in eligible_indices:
                # Too short for identification
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": None,
                        "confidence": 0.0,
                        "status": "too_short",
                    }
                )
                continue

            result = segment_results[i]

            if result is None or result.get("_local_error") == "speaker_client_error":
                error_count += 1
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": None,
                        "confidence": 0.0,
                        "status": "error",
                    }
                )
                continue

            if result.get("_local_error") == "transcript_data_error":
                data_error_count += 1
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": None,
                        "confidence": 0.0,
                        "status": "data_error",
                        "error": result.get("message"),
                    }
                )
                continue

            if result.get("found"):
                name = result.get("speaker_name", label)
                confidence = result.get("confidence", 0.0)
                embedding_context = (
                    {
                        "_evaluation_embedding": result["embedding"],
                        "_embedding_model": result.get("embedding_model"),
                    }
                    if result.get("embedding")
                    else {}
                )
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": name,
                        "confidence": confidence,
                        "status": "identified",
                        **embedding_context,
                    }
                )
                identified_count += 1
            elif result and result.get("status") == "error":
                # Speaker service returned an error (500, timeout, etc.)
                error_count += 1
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": None,
                        "confidence": 0.0,
                        "status": "error",
                    }
                )
            else:
                embedding_context = (
                    {
                        "_evaluation_embedding": result["embedding"],
                        "_embedding_model": result.get("embedding_model"),
                    }
                    if result.get("embedding")
                    else {}
                )
                result_segments.append(
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg.get("text", ""),
                        "speaker": label,
                        "identified_as": None,
                        "confidence": 0.0,
                        "status": "unknown",
                        **embedding_context,
                    }
                )

        logger.info(
            f"🎤 Per-segment identification complete: "
            f"{identified_count}/{len(speech_segments)} segments identified, "
            f"{error_count} service/client errors, {data_error_count} data errors, "
            f"{len(result_segments)} total segments"
        )

        result = {"segments": result_segments}

        # Local transcript/audio failures never say the remote service is unhealthy.
        # Prefer the data signal for a mixed all-failed batch: at least one request was
        # never sent remotely, so reporting the service itself as wholly down is false.
        all_failed = error_count + data_error_count == len(eligible_indices)
        if data_error_count > 0 and all_failed:
            result["error"] = "transcript_data_error"
            result["message"] = (
                f"{data_error_count} segment(s) fall outside the conversation audio; "
                f"{error_count} other identification request(s) failed. "
                "The transcript timing data is invalid."
            )
        # If all remote requests errored, surface this as a service error.
        elif error_count > 0 and error_count == len(eligible_indices):
            result["error"] = "speaker_service_error"
            result["message"] = (
                f"All {error_count} identification requests failed. "
                f"Speaker service may be misconfigured or unhealthy."
            )
        elif error_count > 0:
            result["partial_errors"] = error_count
        if data_error_count > 0 and "error" not in result:
            result["data_errors"] = data_error_count

        return result

    @guard_gallery
    async def diarize_and_identify(
        self,
        audio_data: bytes,
        words: None,
        user_id: Optional[str] = None,  # NOT IMPLEMENTED
    ) -> Dict:
        """
        Perform diarization and speaker identification using the speaker recognition service.

        Args:
            audio_data: WAV audio data as bytes (in-memory)
            words: Optional word-level data from transcription provider (for hints)
            user_id: Optional user ID for speaker identification

        Returns:
            Dictionary containing segments with speaker identification results
        """
        if words:
            logger.warning("Words parameter is not implemented yet")

        if not self.enabled:
            logger.warning("🎤 [DIARIZE] Speaker recognition is disabled")
            return {"segments": []}

        try:
            logger.info(
                f"🎤 [DIARIZE] Starting diarization and identification from in-memory audio "
                f"({len(audio_data) / 1024 / 1024:.2f} MB)"
            )

            # Estimate audio duration from data size (assuming 16kHz, 16-bit PCM)
            # WAV header is typically 44 bytes
            estimated_duration = (
                len(audio_data) - 44
            ) / 32000  # 16000 Hz * 2 bytes per sample
            timeout = self.calculate_timeout(estimated_duration)

            # Call the speaker recognition service
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                # Prepare the audio data for upload (no disk I/O!)
                form_data = aiohttp.FormData()
                form_data.add_field(
                    "file", audio_data, filename="audio.wav", content_type="audio/wav"
                )

                # Get current diarization settings from config
                diarization_settings = get_diarization_settings()

                # Add all diarization parameters for the diarize-and-identify endpoint
                min_duration = diarization_settings.get("min_duration", 0.0)
                similarity_threshold = diarization_settings.get(
                    "similarity_threshold", 0.45
                )
                collar = diarization_settings.get("collar", 2.0)
                min_duration_off = diarization_settings.get("min_duration_off", 0.0)

                form_data.add_field("min_duration", str(min_duration))
                form_data.add_field("similarity_threshold", str(similarity_threshold))
                form_data.add_field("collar", str(collar))
                form_data.add_field("min_duration_off", str(min_duration_off))

                if diarization_settings.get("min_speakers"):
                    form_data.add_field(
                        "min_speakers", str(diarization_settings["min_speakers"])
                    )
                if diarization_settings.get("max_speakers"):
                    form_data.add_field(
                        "max_speakers", str(diarization_settings["max_speakers"])
                    )

                form_data.add_field("identify_only_enrolled", "false")
                if user_id is None:
                    raise ValueError(
                        "diarize_and_identify requires a Chronicle user_id: it "
                        "selects which speaker gallery is searched"
                    )
                form_data.add_field("user_id", user_id)

                endpoint_url = f"{self.service_url}/diarize-and-identify"
                logger.info(f"🎤 [DIARIZE] Calling speaker service: {endpoint_url}")
                logger.info(
                    f"🎤 [DIARIZE] Parameters: min_duration={min_duration}, "
                    f"similarity_threshold={similarity_threshold}, collar={collar}, "
                    f"min_duration_off={min_duration_off}, user_id={user_id}"
                )

                # Make the request
                async with session.post(
                    endpoint_url,
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    logger.info(f"🎤 [DIARIZE] Response status: {response.status}")

                    if response.status != 200:
                        response_text = await checked_response(response, "text")
                        logger.warning(
                            f"🎤 [DIARIZE] ❌ Speaker recognition service returned status {response.status}: {response_text}"
                        )
                        return {
                            "error": "server_error",
                            "message": f"HTTP {response.status}: {response_text[:500]}",
                            "segments": [],
                        }

                    result = await checked_response(response, "json")
                    segments_count = len(result.get("segments", []))
                    logger.info(
                        f"🎤 [DIARIZE] ✅ Speaker service returned {segments_count} segments"
                    )

                    # Log details about identified speakers
                    if segments_count > 0:
                        identified_names = set()
                        for seg in result.get("segments", []):
                            identified_as = seg.get("identified_as")
                            if identified_as and identified_as != "Unknown":
                                identified_names.add(identified_as)

                        if identified_names:
                            logger.info(
                                f"🎤 [DIARIZE] Identified speakers in segments: {identified_names}"
                            )
                        else:
                            logger.warning(
                                f"🎤 [DIARIZE] No identified speakers found in {segments_count} segments"
                            )

                    return result

        except ClientConnectorError as e:
            logger.error(
                f"🎤 [DIARIZE] ❌ Failed to connect to speaker recognition service at {self.service_url}: {e}"
            )
            return {"error": "connection_failed", "message": str(e), "segments": []}
        except asyncio.TimeoutError as e:
            logger.error(
                f"🎤 [DIARIZE] ❌ Timeout connecting to speaker recognition service: {e}"
            )
            return {"error": "timeout", "message": str(e), "segments": []}
        except aiohttp.ClientError as e:
            logger.warning(
                f"🎤 [DIARIZE] ❌ Client error during speaker recognition: {e}"
            )
            return {"error": "client_error", "message": str(e), "segments": []}
        except Exception as e:
            logger.error(
                f"🎤 [DIARIZE] ❌ Error during speaker diarization and identification: {e}"
            )
            logger.debug(traceback.format_exc())
            return {"error": "unknown_error", "message": str(e), "segments": []}

    @guard_gallery
    async def identify_speakers(
        self, audio_path: str, segments: List[Dict]
    ) -> Dict[str, str]:
        """
        Identify speakers in audio segments using the speaker recognition service.

        Args:
            audio_path: Path to the audio file
            segments: List of transcript segments with speaker labels

        Returns:
            Dictionary mapping generic speaker labels to identified names
            e.g., {"Speaker 0": "alex", "Speaker 1": "unknown_speaker_0"}
        """
        if not self.enabled:
            return {}

        try:
            # Extract unique speakers from segments
            unique_speakers = set()
            for segment in segments:
                if "speaker" in segment:
                    unique_speakers.add(segment["speaker"])

            logger.info(f"Identifying {len(unique_speakers)} speakers in {audio_path}")

            # Get audio duration for timeout calculation
            try:
                with wave.open(audio_path, "rb") as wav_file:
                    frame_count = wav_file.getnframes()
                    sample_rate = wav_file.getframerate()
                    audio_duration = (
                        frame_count / sample_rate if sample_rate > 0 else None
                    )
            except Exception as e:
                logger.warning(f"Failed to get audio duration from {audio_path}: {e}")
                audio_duration = None

            # Calculate proportional timeout based on audio duration
            timeout = self.calculate_timeout(audio_duration)

            # Call the speaker recognition service
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                # Prepare the audio file for upload
                with open(audio_path, "rb") as audio_file:
                    form_data = aiohttp.FormData()
                    form_data.add_field(
                        "file",
                        audio_file,
                        filename=Path(audio_path).name,
                        content_type="audio/wav",
                    )
                    # Get current diarization settings
                    _diarization_settings = get_diarization_settings()

                    # Add all diarization parameters for the diarize-and-identify endpoint
                    form_data.add_field(
                        "min_duration",
                        str(_diarization_settings.get("min_duration", 0.0)),
                    )
                    form_data.add_field(
                        "similarity_threshold",
                        str(_diarization_settings.get("similarity_threshold", 0.45)),
                    )
                    form_data.add_field(
                        "collar", str(_diarization_settings.get("collar", 2.0))
                    )
                    form_data.add_field(
                        "min_duration_off",
                        str(_diarization_settings.get("min_duration_off", 0.0)),
                    )
                    if _diarization_settings.get("min_speakers"):
                        form_data.add_field(
                            "min_speakers", str(_diarization_settings["min_speakers"])
                        )
                    if _diarization_settings.get("max_speakers"):
                        form_data.add_field(
                            "max_speakers", str(_diarization_settings["max_speakers"])
                        )
                    form_data.add_field("identify_only_enrolled", "false")

                    # Make the request
                    async with session.post(
                        f"{self.service_url}/diarize-and-identify",
                        headers=request_headers(getattr(self, "_gateway_headers", {})),
                        data=form_data,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as response:
                        if response.status != 200:
                            logger.warning(
                                f"Speaker recognition service returned status {response.status}: {await checked_response(response, "text")}"
                            )
                            return {}

                        result = await checked_response(response, "json")

                        # Process the response to create speaker mapping
                        speaker_mapping = self._process_diarization_result(
                            result, segments
                        )

                        if speaker_mapping:
                            logger.info(f"Speaker mapping created: {speaker_mapping}")
                        else:
                            logger.warning(
                                "No speaker mapping could be created from diarization result"
                            )

                        return speaker_mapping

        except aiohttp.ClientError as e:
            logger.warning(f"Failed to connect to speaker recognition service: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error during speaker identification: {e}")
            return {}

    def _process_diarization_result(
        self, diarization_result: Dict, original_segments: List[Dict]
    ) -> Dict[str, str]:
        """
        Process the diarization result to create a mapping from generic to identified speakers.

        Args:
            diarization_result: Response from the diarize-and-identify endpoint
            original_segments: Original transcript segments with generic speaker labels

        Returns:
            Dictionary mapping generic speaker labels to identified names
        """
        try:
            identified_segments = diarization_result.get("segments", [])

            # Create a mapping based on temporal overlap between segments
            speaker_mapping = {}
            unknown_counter = 0

            # Group diarization segments by their original speaker label
            diar_speakers = {}
            for seg in identified_segments:
                speaker_label = f"Speaker {seg.get('speaker', 0)}"
                if speaker_label not in diar_speakers:
                    diar_speakers[speaker_label] = []
                diar_speakers[speaker_label].append(seg)

            # Map each generic speaker to the most common identified speaker
            for generic_speaker in diar_speakers:
                segments_for_speaker = diar_speakers[generic_speaker]

                # Count identified names for this speaker
                name_counts = {}
                for seg in segments_for_speaker:
                    identified_name = seg.get("identified_as")
                    if identified_name and identified_name != "Unknown":
                        name_counts[identified_name] = (
                            name_counts.get(identified_name, 0) + 1
                        )

                # Assign the most common identified name, or unknown if none found
                if name_counts:
                    best_name = max(name_counts.items(), key=lambda x: x[1])[0]
                    speaker_mapping[generic_speaker] = best_name
                else:
                    speaker_mapping[generic_speaker] = (
                        f"unknown_speaker_{unknown_counter}"
                    )
                    unknown_counter += 1

            logger.info(f"🎤 Speaker mapping: {speaker_mapping}")
            return speaker_mapping

        except Exception as e:
            logger.error(f"🎤 Error processing diarization result: {e}")
            return {}

    @guard_gallery
    async def get_enrolled_speakers(self, user_id: Optional[str] = None) -> Dict:
        """
        Get enrolled speakers from the speaker recognition service.

        Args:
            user_id: Optional user ID to filter speakers

        Returns:
            Dictionary containing speakers list and metadata
        """
        if not self.enabled:
            return {"speakers": []}

        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.get(
                    f"{self.service_url}/speakers",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    params={"user_id": user_id} if user_id is not None else {},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            f"🎤 Failed to get enrolled speakers: status {response.status}"
                        )
                        return {"speakers": []}

                    result = await checked_response(response, "json")
                    speakers = result.get("speakers", [])
                    logger.info(f"🎤 Retrieved {len(speakers)} enrolled speakers")
                    return result

        except aiohttp.ClientError as e:
            logger.warning(f"🎤 Failed to connect to speaker recognition service: {e}")
            return {"speakers": []}
        except Exception as e:
            logger.error(f"🎤 Error getting enrolled speakers: {e}")
            return {"speakers": []}

    @guard_gallery
    async def get_speaker_by_name(
        self, speaker_name: str, user_id: str
    ) -> Optional[Dict]:
        """
        Look up enrolled speaker by name.

        Args:
            speaker_name: Name of the speaker to find
            user_id: User ID to filter speakers (default: 1)

        Returns:
            Speaker dict with id, name, etc. or None if not found
        """
        if not self.enabled:
            logger.warning("🎤 Speaker recognition disabled, cannot lookup speaker")
            return None

        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.get(
                    f"{self.service_url}/speakers",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    params={"user_id": user_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            f"🎤 Failed to get speakers: status {response.status}"
                        )
                        return None

                    result = await checked_response(response, "json")
                    speakers = result.get("speakers", [])

                    # Case-insensitive name match
                    for speaker in speakers:
                        if speaker["name"].lower() == speaker_name.lower():
                            logger.info(
                                f"🎤 Found speaker '{speaker_name}' with ID: {speaker['id']}"
                            )
                            return speaker

                    logger.info(
                        f"🎤 Speaker '{speaker_name}' not found in {len(speakers)} enrolled speakers"
                    )
                    return None

        except aiohttp.ClientError as e:
            logger.warning(f"🎤 Failed to lookup speaker: {e}")
            return None
        except Exception as e:
            logger.error(f"🎤 Error looking up speaker: {e}")
            return None

    async def enrollment_catalog(self, user_id=None):
        # Defer this dependency to break the import cycle through
        # backend.services.speaker_enrollment -> backend.speaker_recognition_client.
        from backend.services.speaker_enrollment import EnrollmentUnavailable

        if not self.enabled or not self.service_url:
            raise EnrollmentUnavailable()
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.get(
                    f"{self.service_url}/enrollment/operations/catalog",
                    params={"user_id": user_id} if user_id is not None else {},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        raise EnrollmentUnavailable()
                    return await response.json()
        except Exception:
            raise EnrollmentUnavailable() from None

    async def enrollment_operation(
        self,
        action,
        operation_id,
        binding,
        *,
        catalog_id,
        service_url,
        audio_data=None,
    ):
        # Defer this dependency to break the import cycle through
        # backend.services.speaker_enrollment -> backend.speaker_recognition_client.
        from backend.services.speaker_enrollment import EnrollmentUnavailable

        if (
            not self.enabled
            or self.service_url != service_url
            or action not in {"prepare", "activate", "quarantine"}
        ):
            raise EnrollmentUnavailable()
        headers = {
            **getattr(self, "_gateway_headers", {}),
            "X-Speaker-Catalog": catalog_id,
        }
        if action == "prepare":
            form = aiohttp.FormData()
            form.add_field("binding", json.dumps(binding, sort_keys=True))
            form.add_field(
                "file", audio_data, filename="segment.wav", content_type="audio/wav"
            )
            kwargs = {"data": form}
        else:
            kwargs = {
                "json": (
                    {"user_id": binding["user_id"]}
                    if action == "quarantine"
                    else binding
                )
            }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(
                    f"{service_url}/enrollment/operations/{operation_id}/{action}",
                    timeout=aiohttp.ClientTimeout(
                        total=120 if action == "prepare" else 30
                    ),
                    **kwargs,
                ) as response:
                    if response.status != 200:
                        raise EnrollmentUnavailable()
                    return await response.json()
        except Exception:
            raise EnrollmentUnavailable() from None

    async def enroll_new_speaker(
        self,
        speaker_name: str,
        audio_data: bytes,
        user_id: str,
        *,
        conversation_ids=(),
        visibility=None,
        evidence_records=None,
    ) -> Dict:
        # Defer this dependency to break the import cycle through
        # backend.services.speaker_enrollment -> backend.speaker_recognition_client.
        from backend.services.speaker_enrollment import enroll

        return await enroll(
            self,
            speaker_name=speaker_name,
            speaker_id=None,
            audio_data=audio_data,
            user_id=user_id,
            conversation_ids=conversation_ids,
            visibility=visibility,
            evidence_records=evidence_records,
        )

    async def append_to_speaker(
        self,
        speaker_id: str,
        audio_data: bytes,
        user_id: str,
        *,
        speaker_name,
        conversation_ids=(),
        visibility=None,
        evidence_records=None,
    ) -> Dict:
        # Defer this dependency to break the import cycle through
        # backend.services.speaker_enrollment -> backend.speaker_recognition_client.
        from backend.services.speaker_enrollment import enroll

        return await enroll(
            self,
            speaker_name=speaker_name,
            speaker_id=speaker_id,
            audio_data=audio_data,
            user_id=user_id,
            conversation_ids=conversation_ids,
            visibility=visibility,
            evidence_records=evidence_records,
        )

    @guard_gallery
    async def score_enrollment_candidate(
        self, audio_wav_bytes: bytes, speaker_id: str
    ) -> Dict:
        """Score a candidate clip's enrollment value for one target speaker.

        POST /enrollment/candidates/score — returns sim_centroid (cosine to the
        target's centroid), max_clip_sim (redundancy vs the target's per-clip
        gallery), n_gallery_clips, best_other ({speaker_id, name, score} of the
        closest other enrolled speaker), and duration.
        """
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}

        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                form_data = aiohttp.FormData()
                form_data.add_field(
                    "file",
                    audio_wav_bytes,
                    filename="candidate.wav",
                    content_type="audio/wav",
                )
                form_data.add_field("speaker_id", speaker_id)

                async with session.post(
                    f"{self.service_url}/enrollment/candidates/score",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        response_text = await checked_response(response, "text")
                        logger.warning(
                            f"🎤 /enrollment/candidates/score returned {response.status}: {response_text}"
                        )
                        return {"error": "score_failed", "status": response.status}
                    return await checked_response(response, "json")

        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to score enrollment candidate: {e}")
            return {"error": "connection_failed", "message": str(e)}

    @guard_gallery
    async def get_enrollment_health(
        self, user_id: str, before: Optional[datetime] = None
    ) -> Dict:
        """Return per-clip gallery cohesion and contamination metrics."""
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                params = {"user_id": user_id}
                if before is not None:
                    params["before"] = before.isoformat()
                async with session.get(
                    f"{self.service_url}/enrollment/health",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        return {
                            "error": "health_audit_failed",
                            "status": response.status,
                        }
                    return await checked_response(response, "json")
        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to audit enrollment health: {e}")
            return {"error": "connection_failed", "message": str(e)}

    @guard_gallery
    async def get_enrollment_segment_audio(self, segment_id: int) -> Optional[bytes]:
        """Fetch one enrolled clip's audio for playback (None if unavailable)."""
        if not self.enabled:
            return None
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.get(
                    f"{self.service_url}/enrollment/segments/{segment_id}/audio",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        return None
                    return await checked_response(response, "read")
        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to fetch enrollment segment audio: {e}")
            return None

    async def delete_enrollment_segment(
        self, segment_id: int, hard: bool = False
    ) -> Dict:
        """Remove one clip from a speaker's voiceprint (quarantined by default);
        the service recomputes the speaker's centroid."""
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                form_data = aiohttp.FormData()
                form_data.add_field("hard", "true" if hard else "false")
                async with session.post(
                    f"{self.service_url}/enrollment/segments/{segment_id}/delete",
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        response_text = await response.text()
                        logger.warning(
                            f"🎤 Segment delete returned {response.status}: {response_text}"
                        )
                        return {
                            "error": "segment_delete_failed",
                            "status": response.status,
                        }
                    return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to delete enrollment segment: {e}")
            return {"error": "connection_failed", "message": str(e)}

    async def delete_speaker(
        self, speaker_id: str, user_id: str, delete_audio: bool = True
    ) -> Dict:
        """Delete an enrolled speaker (and, by default, their enrollment audio)."""
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}
        if not user_id:
            raise ValueError("user_id is required to delete a speaker")
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.delete(
                    f"{self.service_url}/speakers/{speaker_id}",
                    params={
                        "user_id": user_id,
                        "delete_audio": "true" if delete_audio else "false",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        response_text = await response.text()
                        logger.warning(
                            f"🎤 Speaker delete returned {response.status}: {response_text}"
                        )
                        return {
                            "error": "speaker_delete_failed",
                            "status": response.status,
                        }
                    return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to delete speaker: {e}")
            return {"error": "connection_failed", "message": str(e)}

    async def extract_speaker_embedding(self, audio_wav_bytes: bytes) -> Dict:
        """Extract an evaluation embedding without mutating speaker enrollment."""
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                form_data = aiohttp.FormData()
                form_data.add_field(
                    "file",
                    audio_wav_bytes,
                    filename="evaluation.wav",
                    content_type="audio/wav",
                )
                async with session.post(
                    f"{self.service_url}/enrollment/candidates/embed",
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        return {"error": "embedding_failed", "status": response.status}
                    return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to extract evaluation embedding: {e}")
            return {"error": "connection_failed", "message": str(e)}

    @guard_gallery
    async def score_cached_embeddings(
        self, speaker_id: str, embeddings: list[list[float]]
    ) -> Dict:
        """Score cached corpus embeddings against the current live gallery."""
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.post(
                    f"{self.service_url}/enrollment/candidates/score-embeddings",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    json={"speaker_id": speaker_id, "embeddings": embeddings},
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as response:
                    if response.status != 200:
                        return {
                            "error": "embedding_score_failed",
                            "status": response.status,
                            "message": await checked_response(response, "text"),
                        }
                    return await checked_response(response, "json")
        except aiohttp.ClientError as e:
            logger.error(f"🎤 Failed to score cached embeddings: {e}")
            return {"error": "connection_failed", "message": str(e)}

    async def get_embedding_info(self) -> Dict:
        """Return the active speaker embedding model fingerprint."""
        if not self.enabled:
            return {"error": "speaker_recognition_disabled"}
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.get(
                    f"{self.service_url}/enrollment/candidates/embedding-info",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        return {
                            "error": "embedding_info_failed",
                            "status": response.status,
                        }
                    return await response.json()
        except aiohttp.ClientError as e:
            return {"error": "connection_failed", "message": str(e)}

    @guard_gallery
    async def reidentify_clusters(
        self,
        clusters: Dict[str, list],
        user_id: str,
        similarity_threshold: Optional[float] = None,
        identify_margin: Optional[float] = None,
        exclusive: Optional[bool] = None,
    ) -> Dict:
        """Re-identify stored per-cluster centroids against the CURRENT gallery.

        Pure vector math on the speaker service (no audio, no GPU). Used by the reprocess-
        impact finder to see what a conversation's speaker labels would be now.
        Returns ``{"assignments": {label: {name, id, confidence}}}``.
        """
        if not self.enabled:
            return {"error": "speaker_recognition_disabled", "assignments": {}}
        payload: Dict = {"clusters": clusters, "user_id": user_id}
        if similarity_threshold is not None:
            payload["similarity_threshold"] = similarity_threshold
        if identify_margin is not None:
            payload["identify_margin"] = identify_margin
        if exclusive is not None:
            payload["exclusive"] = exclusive
        try:
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.post(
                    f"{self.service_url}/v1/reidentify-clusters",
                    headers=request_headers(getattr(self, "_gateway_headers", {})),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        return {
                            "error": "reidentify_failed",
                            "status": response.status,
                            "assignments": {},
                        }
                    return await checked_response(response, "json")
        except Exception as e:
            return {"error": "connection_failed", "message": str(e), "assignments": {}}

    async def embed_clusters(self, audio_bytes: bytes, segments: List[dict]) -> Dict:
        """Pool one centroid per diarized speaker for an EXISTING segmentation.

        Sends conversation audio + its stored segment boundaries to the speaker service
        (no re-diarization). Used by the one-time backlog backfill.
        Returns ``{"clusters": {label: centroid}}``.
        """
        if not self.enabled:
            return {"error": "speaker_recognition_disabled", "clusters": {}}
        try:
            form_data = aiohttp.FormData()
            form_data.add_field(
                "file", audio_bytes, filename="conv.wav", content_type="audio/wav"
            )
            form_data.add_field("segments_data", json.dumps(segments))
            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                async with session.post(
                    f"{self.service_url}/v1/embed-clusters",
                    data=form_data,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as response:
                    if response.status != 200:
                        text = await response.text()
                        return {
                            "error": "embed_failed",
                            "status": response.status,
                            "message": text,
                            "clusters": {},
                        }
                    return await response.json()
        except Exception as e:
            return {"error": "connection_failed", "message": str(e), "clusters": {}}

    @guard_gallery
    async def check_if_enrolled_speaker_present(
        self,
        redis_client,
        client_id: str,
        session_id: str,
        user_id: str,
        transcription_results: List[dict],
    ) -> tuple[bool, dict]:
        """
        Check if any enrolled speakers are present in the transcription results.

        This extracts audio from Redis, runs speaker recognition, and checks if
        any identified speakers match the user's enrolled speakers.

        Args:
            redis_client: Redis client
            client_id: Client identifier
            session_id: Session identifier
            user_id: User ID
            transcription_results: List of transcription results from aggregator

        Returns:
            Tuple of (enrolled_present: bool, speaker_result: dict)
            - enrolled_present: True if enrolled speaker detected, False otherwise
            - speaker_result: Full speaker recognition result dict with segments
        """
        logger.info(
            f"🎤 [SPEAKER CHECK] Starting speaker check for session {session_id}"
        )
        logger.info(f"🎤 [SPEAKER CHECK] Client: {client_id}, User: {user_id}")
        logger.info(
            f"🎤 [SPEAKER CHECK] Transcription results count: {len(transcription_results)}"
        )

        # Get enrolled speakers for this user
        logger.info(
            f"🎤 [SPEAKER CHECK] Fetching enrolled speakers for user {user_id}..."
        )
        enrolled_result = await self.get_enrolled_speakers(user_id)
        enrolled_speakers = set(
            speaker["name"] for speaker in enrolled_result.get("speakers", [])
        )

        logger.info(f"🎤 [SPEAKER CHECK] Enrolled speakers: {enrolled_speakers}")

        if not enrolled_speakers:
            logger.warning(
                "🎤 [SPEAKER CHECK] No enrolled speakers found, allowing conversation"
            )
            return (True, {})  # If no enrolled speakers, allow all conversations

        # Extract audio chunks (PCM format)
        logger.info(f"🎤 [SPEAKER CHECK] Extracting audio chunks from Redis...")
        pcm_data = await extract_audio_for_results(
            redis_client=redis_client,
            client_id=client_id,
            session_id=session_id,
            transcription_results=transcription_results,
        )

        if not pcm_data:
            logger.warning(
                "🎤 [SPEAKER CHECK] No audio data extracted, skipping speaker check"
            )
            return (False, {})

        audio_size_kb = len(pcm_data) / 1024
        audio_duration_sec = len(pcm_data) / (16000 * 2)  # 16kHz, 16-bit
        logger.info(
            f"🎤 [SPEAKER CHECK] Extracted audio: {audio_size_kb:.1f} KB, ~{audio_duration_sec:.1f}s"
        )

        # Convert PCM to WAV in memory (no disk I/O!)

        logger.info(f"🎤 [SPEAKER CHECK] Converting PCM to WAV in memory...")
        wav_data = pcm_to_wav_bytes(
            pcm_data, sample_rate=16000, channels=1, sample_width=2
        )

        logger.info(
            f"🎤 [SPEAKER CHECK] WAV created in memory: {len(wav_data) / 1024 / 1024:.2f} MB"
        )

        try:
            # Run speaker recognition (diarize and identify) with in-memory audio
            logger.info(
                f"🎤 [SPEAKER CHECK] Calling diarize_and_identify with in-memory audio..."
            )
            result = await self.diarize_and_identify(
                audio_data=wav_data,  # Pass bytes directly, no temp file!
                words=None,
                user_id=user_id,
            )

            logger.info(f"🎤 [SPEAKER CHECK] Speaker recognition result: {result}")

            # Check if any identified speakers are enrolled
            identified_speakers = set()
            segments_count = len(result.get("segments", []))
            logger.info(
                f"🎤 [SPEAKER CHECK] Processing {segments_count} segments from speaker recognition"
            )

            for idx, segment in enumerate(result.get("segments", [])):
                identified_name = segment.get("identified_as")
                speaker_label = segment.get("speaker", "unknown")
                segment_start = segment.get("start", 0)
                segment_end = segment.get("end", 0)

                logger.debug(
                    f"🎤 [SPEAKER CHECK] Segment {idx+1}/{segments_count}: "
                    f"speaker={speaker_label}, identified_as={identified_name}, "
                    f"time=[{segment_start:.2f}s - {segment_end:.2f}s]"
                )

                if identified_name and identified_name != "Unknown":
                    identified_speakers.add(identified_name)
                    logger.info(
                        f"🎤 [SPEAKER CHECK] Found identified speaker: {identified_name}"
                    )

            logger.info(
                f"🎤 [SPEAKER CHECK] All identified speakers: {identified_speakers}"
            )
            logger.info(f"🎤 [SPEAKER CHECK] Enrolled speakers: {enrolled_speakers}")

            matches = enrolled_speakers & identified_speakers

            if matches:
                logger.info(
                    f"🎤 [SPEAKER CHECK] ✅ MATCH! Enrolled speaker(s) detected: {matches}"
                )
                return (
                    True,
                    result,
                )  # Return both boolean and speaker recognition results
            else:
                logger.info(
                    f"🎤 [SPEAKER CHECK] ❌ NO MATCH. "
                    f"Identified: {identified_speakers}, Enrolled: {enrolled_speakers}"
                )
                return (
                    False,
                    result,
                )  # Return both boolean and speaker recognition results

        except Exception as e:
            logger.error(
                f"🎤 [SPEAKER CHECK] ❌ Speaker recognition check failed: {e}",
                exc_info=True,
            )
            return (False, {})  # Fail closed - don't create conversation on error

    async def health_check(self) -> bool:
        """
        Check if the speaker recognition service is healthy and responding.

        Returns:
            True if service is healthy, False otherwise
        """
        if not self.enabled:
            return False

        try:
            logger.debug(
                f"Performing health check on speaker service: {self.service_url}"
            )

            async with aiohttp.ClientSession(
                headers=getattr(self, "_gateway_headers", {})
            ) as session:
                # Use the /health endpoint if available, otherwise try a simple endpoint
                health_endpoints = ["/health", "/speakers"]

                for endpoint in health_endpoints:
                    try:
                        async with session.get(
                            f"{self.service_url}{endpoint}",
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as response:
                            if response.status == 200:
                                logger.debug(
                                    f"Speaker service health check passed via {endpoint}"
                                )
                                return True
                            else:
                                logger.debug(
                                    f"Health check endpoint {endpoint} returned {response.status}"
                                )
                    except Exception as endpoint_error:
                        logger.debug(
                            f"Health check failed for {endpoint}: {endpoint_error}"
                        )
                        continue

                logger.warning("All health check endpoints failed")
                return False

        except Exception as e:
            logger.error(f"Error during speaker service health check: {e}")
            return False
