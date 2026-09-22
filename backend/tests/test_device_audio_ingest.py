import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import backend.services.device_audio_ingest as device_audio_ingest
import backend.utils.vad_analysis as vad_analysis
from backend.models.audio_capture import AudioRangeRef
from backend.models.timeline import EvidenceLocator
from backend.services.device_audio_ingest import (
    _audio_capture_source_id,
    _ingest_segment,
    _profile_wav,
    _Segment,
    audio_stream_key,
    group_audio_sessions,
)
from backend.services.timeline.evidence import _audio_item
from backend.utils.vad_analysis import AudioEvidenceProfile, SpeechDetectionReason


def item(
    identifier: str,
    start: datetime,
    duration: float = 30,
    meeting_id: str | None = None,
):
    return SimpleNamespace(
        source_item_id=identifier,
        captured_at=start,
        ended_at=start + timedelta(seconds=duration),
        metadata={"meeting_id": meeting_id} if meeting_id else {},
    )


def test_audio_stream_key_separates_microphone_from_system_output():
    microphone = SimpleNamespace(
        user_id="user",
        source_id="rainbow",
        locator=EvidenceLocator(
            capture_source_id="rainbow", modality="audio", track_id="desk-mic"
        ),
        metadata={"direction": "input"},
    )
    system = SimpleNamespace(
        user_id="user",
        source_id="rainbow",
        locator=EvidenceLocator(
            capture_source_id="rainbow", modality="audio", track_id="speakers"
        ),
        metadata={"direction": "output"},
    )

    assert audio_stream_key(microphone) != audio_stream_key(system)


def test_audio_stream_key_separates_same_direction_provider_tracks():
    def microphone(track_id: str):
        return SimpleNamespace(
            user_id="user",
            source_id="rainbow",
            locator=EvidenceLocator(
                capture_source_id="rainbow", modality="audio", track_id=track_id
            ),
            metadata={"direction": "input"},
        )

    assert audio_stream_key(microphone("desk-mic")) != audio_stream_key(
        microphone("headset-mic")
    )


def test_capture_source_identity_retains_same_direction_provider_track():
    def microphone(track_id: str):
        return SimpleNamespace(
            user_id="user",
            source_id="rainbow",
            source_item_id="chunk-1",
            locator=EvidenceLocator(
                capture_source_id="rainbow", modality="audio", track_id=track_id
            ),
            metadata={"direction": "input"},
        )

    assert _audio_capture_source_id("rainbow", "input", [microphone("desk-mic")]) == (
        "rainbow:input:desk-mic"
    )
    assert (
        _audio_capture_source_id("rainbow", "input", [microphone("headset-mic")])
        == "rainbow:input:headset-mic"
    )


def test_audio_evidence_item_retains_provider_local_track_locator():
    started_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
    locator = EvidenceLocator(
        capture_source_id="rainbow", modality="audio", track_id="desk-mic"
    )
    span = SimpleNamespace(
        id="span-1",
        user_id="user",
        source_id="rainbow",
        locator=locator,
        source_item_ids=["chunk-1"],
        first_source_item_id="chunk-1",
        last_source_item_id="chunk-1",
        source_range_hash="hash",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10),
        direction="input",
        state="transcribed",
        covered_seconds=10,
        missing_seconds=0,
        coverage_fraction=[1.0],
        speech_fraction=[1.0],
        acoustic_active_fraction=[1.0],
        rms_dbfs=[-20.0],
        peak_dbfs=[-5.0],
        speech_seconds=10.0,
        acoustic_active_seconds=10.0,
        acoustic_quiet_seconds=0.0,
        meeting_id=None,
        bucket_seconds=10.0,
        longest_no_speech_seconds=0.0,
        conversation_id="conversation-1",
    )

    assert _audio_item(span).locator == locator


def test_audio_chunks_group_across_input_and_output_devices():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("input-1", start),
            item("output-1", start),
            item("input-2", start + timedelta(seconds=30)),
        ]
    )
    assert len(sessions) == 1
    assert len(sessions[0]) == 3


def test_audio_session_closes_after_meaningful_gap():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("one", start),
            item("two", start + timedelta(minutes=3)),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["one"],
        ["two"],
    ]


def test_a_processing_window_no_longer_decides_where_a_recording_ends():
    """Grouping bounds compute; the boundary is chosen later from the speech profile.

    This used to cut at exactly 30 minutes, which severed conversations that were
    still going. A 32-minute run of continuous capture is now one window, and where
    it becomes one or two recordings is ``plan_session_cuts``' decision.
    """
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [item(str(index), start + timedelta(minutes=index)) for index in range(32)]

    sessions = group_audio_sessions(rows)

    assert [len(session) for session in sessions] == [32]


def test_a_processing_window_is_still_bounded_for_compute():
    """Mixing and profiling have to be bounded even when capture never stops."""
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [item(str(index), start + timedelta(minutes=index)) for index in range(150)]

    sessions = group_audio_sessions(rows)

    assert [len(session) for session in sessions] == [120, 30]


def test_meeting_chunks_are_not_split_by_the_thirty_minute_window():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [
        item(str(index), start + timedelta(minutes=index), meeting_id="meeting:1")
        for index in range(45)
    ]
    sessions = group_audio_sessions(rows)
    assert [len(session) for session in sessions] == [45]


def test_meeting_boundary_starts_a_new_session():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("ambient", start),
            item("call-1", start + timedelta(seconds=30), meeting_id="meeting:1"),
            item("call-2", start + timedelta(seconds=60), meeting_id="meeting:1"),
            item("after", start + timedelta(seconds=90)),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["ambient"],
        ["call-1", "call-2"],
        ["after"],
    ]


def test_back_to_back_meetings_stay_separate_conversations():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("first", start, meeting_id="meeting:1"),
            item("second", start + timedelta(seconds=30), meeting_id="meeting:2"),
        ]
    )
    assert len(sessions) == 2


def test_meeting_tolerates_longer_silences_than_ambient_capture():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("one", start, meeting_id="meeting:1"),
            item("two", start + timedelta(minutes=4), meeting_id="meeting:1"),
            item("three", start + timedelta(minutes=11), meeting_id="meeting:1"),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["one", "two"],
        ["three"],
    ]


def test_meetings_still_split_at_the_safety_cap():
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [
        item(str(index), start + timedelta(minutes=4 * index), meeting_id="meeting:1")
        for index in range(32)
    ]
    sessions = group_audio_sessions(rows)
    assert [len(session) for session in sessions] == [30, 2]


def test_mongo_naive_and_aware_timestamps_group_together():
    naive = datetime(2026, 7, 22, 10, 0)
    aware = datetime(2026, 7, 22, 10, 0, 30, tzinfo=timezone.utc)
    sessions = group_audio_sessions(
        [
            item("mongo-naive", naive),
            item("api-aware", aware),
        ]
    )
    assert [[row.source_item_id for row in session] for session in sessions] == [
        ["mongo-naive", "api-aware"]
    ]


def test_profile_wav_reads_wav_and_reports_vad_verdict(tmp_path, monkeypatch):
    path = tmp_path / "session.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)

    class FakeProvider:
        frame_hop_ms = 16
        name = "fixture-vad"

        def __init__(self, scores):
            self._scores = scores

        def score(self, mono, sample_rate):
            return self._scores

    monkeypatch.setattr(
        vad_analysis,
        "get_vad_provider",
        lambda: FakeProvider(np.array([0.9] * 40)),
    )
    speech = _profile_wav(path)
    assert speech.scored is True
    assert speech.reason is SpeechDetectionReason.SPEECH_DETECTED

    monkeypatch.setattr(
        vad_analysis,
        "get_vad_provider",
        lambda: FakeProvider(np.array([0.1] * 40)),
    )
    silence = _profile_wav(path)
    assert silence.reason is SpeechDetectionReason.NO_SPEECH


def test_profile_wav_reports_decode_failure(tmp_path):
    missing = tmp_path / "missing.wav"
    result = _profile_wav(missing)

    assert result.scored is False
    assert result.reason is SpeechDetectionReason.WAV_DECODE_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_enabled", [False, True])
async def test_no_speech_keeps_raw_capture_without_materializing_conversation(
    monkeypatch,
    privacy_enabled,
    isolated_privacy_database,
):
    """VAD filters semantic Conversations, never the underlying capture evidence."""

    class PendingQuery:
        def __init__(self, rows):
            self.rows = rows

        def sort(self, *_args):
            return self

        async def to_list(self):
            return self.rows

    class PendingItem:
        user_id = "507f1f77bcf86cd799439011"
        source_id = "rainbow"
        source_item_id = "audio-1"
        kind = "audio"
        state = "received"
        captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ended_at = captured_at + timedelta(seconds=30)
        metadata = {"direction": "input"}
        locator = EvidenceLocator(
            capture_source_id="rainbow", modality="audio", track_id="desk-mic"
        )
        deleted = False

        async def delete(self):
            self.deleted = True

    pending = PendingItem()
    if privacy_enabled:
        await isolated_privacy_database.capture_sources.insert_one(
            {
                "user_id": pending.user_id,
                "source_id": pending.source_id,
                "privacy_enabled_from": pending.captured_at,
                "privacy_revision": 1,
                "privacy_tracks": ["display"],
            }
        )
    events = []
    captured_range = AudioRangeRef(
        capture_source_id="rainbow:input",
        time_basis="recorded",
        chunk_ids=["507f1f77bcf86cd799439012"],
        capture_session_ids=["screenpipe-capture"],
        started_at=pending.captured_at,
        ended_at=pending.ended_at,
    )
    profile = AudioEvidenceProfile(
        scored=True,
        reason=SpeechDetectionReason.NO_SPEECH,
        bucket_seconds=10.0,
        speech_seconds=0.0,
        longest_no_speech_seconds=30.0,
        acoustic_active_seconds=0.0,
        acoustic_quiet_seconds=30.0,
        speech_fraction=[0.0, 0.0, 0.0],
        acoustic_active_fraction=[0.0, 0.0, 0.0],
        rms_dbfs=[-90.0, -90.0, -90.0],
        peak_dbfs=[-90.0, -90.0, -90.0],
        provider="fixture",
        frame_hop_ms=16.0,
    )

    class DeviceInputFixture:
        kind = "kind"
        state = "state"

        @staticmethod
        def find(*_args, **_kwargs):
            return PendingQuery([pending])

    class UserFixture:
        @staticmethod
        async def get(_user_id):
            return SimpleNamespace(user_id=pending.user_id, id=pending.user_id)

    monkeypatch.setattr(device_audio_ingest, "DeviceInputItem", DeviceInputFixture)
    monkeypatch.setattr(device_audio_ingest, "User", UserFixture)

    async def mix(*_args):
        events.append("mix")

    async def persist(*_args):
        events.append("capture")
        return SimpleNamespace(audio_range=captured_range)

    def profile_wav(_path):
        events.append("vad")
        return profile

    spans = []

    async def save_span(*_args, **kwargs):
        spans.append(kwargs)
        return SimpleNamespace()

    async def unexpected_ingest(*_args, **_kwargs):
        raise AssertionError("silence must not materialize a Conversation")

    monkeypatch.setattr(device_audio_ingest, "_mix_session", mix)
    monkeypatch.setattr(device_audio_ingest, "_persist_capture_window", persist)
    monkeypatch.setattr(device_audio_ingest, "_profile_wav", profile_wav)
    monkeypatch.setattr(device_audio_ingest, "_save_evidence_span", save_span)
    monkeypatch.setattr(device_audio_ingest, "_ingest_segment", unexpected_ingest)
    monkeypatch.setattr(
        device_audio_ingest, "require_speech_for_transcription", lambda: True
    )

    result = await device_audio_ingest.process_device_audio()

    assert events == ([] if privacy_enabled else ["mix", "capture", "vad"])
    assert spans == (
        []
        if privacy_enabled
        else [{"state": "no_speech", "audio_ranges": [captured_range]}]
    )
    assert pending.deleted is not privacy_enabled
    assert result["processed_sessions"] == 0
    assert result["rejected_no_speech"] == (0 if privacy_enabled else 1)


@pytest.mark.asyncio
async def test_speech_materializes_a_visible_conversation_claim(monkeypatch, tmp_path):
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source_item = SimpleNamespace(
        source_item_id="audio-1",
        user_id="user-1",
        source_id="rainbow",
        locator=EvidenceLocator(
            capture_source_id="rainbow", modality="audio", track_id="desk-mic"
        ),
        captured_at=started_at,
        ended_at=started_at + timedelta(seconds=30),
        metadata={"direction": "input"},
    )
    profile = AudioEvidenceProfile(
        scored=True,
        reason=SpeechDetectionReason.SPEECH_DETECTED,
        bucket_seconds=10.0,
        speech_seconds=30.0,
        longest_no_speech_seconds=0.0,
        acoustic_active_seconds=30.0,
        acoustic_quiet_seconds=0.0,
        speech_fraction=[1.0, 1.0, 1.0],
        acoustic_active_fraction=[1.0, 1.0, 1.0],
        rms_dbfs=[-20.0, -20.0, -20.0],
        peak_dbfs=[-5.0, -5.0, -5.0],
        provider="fixture",
        frame_hop_ms=16.0,
    )
    segment = _Segment(
        items=[source_item],
        path=tmp_path / "segment.wav",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=30),
        profile=profile,
    )
    audio_range = AudioRangeRef(
        capture_source_id="rainbow:input",
        time_basis="recorded",
        chunk_ids=["507f1f77bcf86cd799439012"],
        started_at=segment.started_at,
        ended_at=segment.ended_at,
    )
    received = {}

    async def materialize(_user, _audio_range, **kwargs):
        received.update(kwargs)
        return SimpleNamespace(conversation_id="conversation-1")

    async def save_span(*_args, **_kwargs):
        return SimpleNamespace()

    class EmptyQuery:
        async def to_list(self):
            return []

    class Expression:
        def __eq__(self, _other):
            return self

        def __le__(self, _other):
            return self

    class DeviceInputFixture:
        user_id = Expression()
        source_id = Expression()
        kind = Expression()
        captured_at = Expression()

        @staticmethod
        def find(*_args, **_kwargs):
            return EmptyQuery()

    monkeypatch.setattr(
        device_audio_ingest, "materialize_and_process_audio_claim", materialize
    )
    monkeypatch.setattr(device_audio_ingest, "_save_evidence_span", save_span)
    monkeypatch.setattr(device_audio_ingest, "DeviceInputItem", DeviceInputFixture)

    conversation_id = await _ingest_segment(
        SimpleNamespace(id="user-1"),
        "rainbow",
        "input",
        segment,
        audio_range,
    )

    assert conversation_id == "conversation-1"
    assert received["data_purpose"] == "conversation"
    assert received["external_source_id"] == (
        "screenpipe:rainbow:input:desk-mic:audio-1-audio-1"
    )
    # VAD creates a provisional source record. It must remain memory-eligible so a
    # later settled conversational Timeline episode can dispatch the memory write;
    # ``skip_memory_extraction`` below prevents the provisional close path from
    # writing too early.
    assert received["memory_excluded"] is False
    assert received["skip_memory_extraction"] is True
    # A VAD fragment is evidence for Timeline, not a complete semantic event. Its
    # transcript remains immediately available, while Timeline owns the title and
    # summaries after it groups adjacent input/output fragments into an episode.
    assert received["skip_title_summary"] is True
