"""Disjoint allowed portions of one capture must remain distinct evidence."""

import array
import io
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from backend.models.timeline import AudioEvidenceSpan, EvidenceLocator
from backend.services import device_audio_ingest as ingest
from backend.utils.vad_analysis import AudioEvidenceProfile, SpeechDetectionReason


async def test_audio_assembly_preserves_sample_time(tmp_path):
    samples = array.array("h", [0] * 16000)
    samples[4000] = 20000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        f.writeframes(samples.tobytes())
    item = SimpleNamespace(
        captured_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
        media_data=buffer.getvalue(),
        media_filename="synthetic.wav",
    )
    output = tmp_path / "assembled.wav"
    await ingest._mix_session([item], tmp_path, output)
    with wave.open(str(output), "rb") as f:
        actual = array.array("h", f.readframes(f.getnframes()))
    assert len(actual) == len(samples)
    assert max(range(len(actual)), key=lambda i: abs(actual[i])) == 4000


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_disjoint_allowed_spans_survive_save_and_retry(monkeypatch, direction):
    db = AsyncMongoMockClient().privacy_audio_spans
    monkeypatch.setattr(
        AudioEvidenceSpan, "_document_settings", AudioEvidenceSpan._document_settings
    )
    await init_beanie(database=db, document_models=[AudioEvidenceSpan])
    monkeypatch.setattr(ingest, "_mark_span_dirty", AsyncMock())
    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    item = SimpleNamespace(
        user_id="owner",
        source_id="synthetic-source",
        source_item_id="capture-1",
        captured_at=start,
        ended_at=start + timedelta(seconds=3),
        metadata={},
        locator=EvidenceLocator(
            capture_source_id="synthetic-source", modality="audio", track_id=direction
        ),
    )
    profile = AudioEvidenceProfile(
        scored=False,
        reason=SpeechDetectionReason.PROVIDER_UNAVAILABLE,
        bucket_seconds=1,
        speech_seconds=None,
        longest_no_speech_seconds=None,
        acoustic_active_seconds=1,
        acoustic_quiet_seconds=0,
        speech_fraction=[None],
        acoustic_active_fraction=[1],
        rms_dbfs=[-20],
        peak_dbfs=[-10],
        provider=None,
        frame_hop_ms=None,
    )
    first = await ingest._save_evidence_span(
        [item],
        direction,
        profile,
        "unscored",
        "first-conversation",
        bounds=(start, start + timedelta(seconds=1)),
    )
    second = await ingest._save_evidence_span(
        [item],
        direction,
        profile,
        "unscored",
        "second-conversation",
        bounds=(start + timedelta(seconds=2), start + timedelta(seconds=3)),
    )
    replay = await ingest._save_evidence_span(
        [item],
        direction,
        profile,
        "unscored",
        "first-conversation",
        bounds=(start, start + timedelta(seconds=1)),
    )
    rows = await AudioEvidenceSpan.find_all().sort("started_at").to_list()
    assert len(rows) == 2
    assert first.id == replay.id != second.id
    assert [r.conversation_id for r in rows] == [
        "first-conversation",
        "second-conversation",
    ]
    assert [r.attempts for r in rows] == [2, 1]
    assert rows[0].source_range_hash != rows[1].source_range_hash
    assert [
        (ingest._as_utc(r.started_at), ingest._as_utc(r.ended_at)) for r in rows
    ] == [
        (start, start + timedelta(seconds=1)),
        (start + timedelta(seconds=2), start + timedelta(seconds=3)),
    ]


@pytest.mark.parametrize(
    "new_source,offset",
    [
        ("synthetic:output:system", 0),
        ("synthetic:input:mic", 30),
        ("other-device:input:mic", 0),
    ],
)
async def test_identical_pcm_keeps_native_source_and_capture_time(
    monkeypatch, new_source, offset
):
    from backend.models.audio_capture import AudioCaptureSession
    from backend.models.audio_chunk import AudioChunkDocument
    from backend.utils import audio_chunk_utils as audio

    db = AsyncMongoMockClient().privacy_capture_identity
    for model in (AudioCaptureSession, AudioChunkDocument):
        monkeypatch.setattr(model, "_document_settings", model._document_settings)
    await init_beanie(
        database=db, document_models=[AudioCaptureSession, AudioChunkDocument]
    )
    # mongomock-motor's create_indexes adapter loses partialFilterExpression.
    # Install the exact declared index definitions through its single-index API.
    for model in (AudioCaptureSession, AudioChunkDocument):
        for index in model.Settings.indexes:
            definition = getattr(index, "document", {})
            if "partialFilterExpression" in definition:
                options = {k: v for k, v in definition.items() if k != "key"}
                collection = db[model.Settings.name]
                await collection.drop_index(definition["name"])
                await collection.create_index(
                    list(definition["key"].items()), **options
                )
    monkeypatch.setattr(
        audio, "encode_pcm_to_opus", AsyncMock(return_value=b"synthetic-encoded-audio")
    )
    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    common = dict(user_id="owner", audio_data=b"\x00\x01" * 16000, origin="screenpipe")
    first = await audio.convert_audio_to_chunks(
        **common,
        capture_source_id="synthetic:input:mic",
        captured_at=start,
        capture_session_id="capture-one",
    )
    second = await audio.convert_audio_to_chunks(
        **common,
        capture_source_id=new_source,
        captured_at=start + timedelta(seconds=offset),
        capture_session_id="capture-two",
    )
    replay = await audio.convert_audio_to_chunks(
        **common,
        capture_source_id=new_source,
        captured_at=start + timedelta(seconds=offset),
        capture_session_id="capture-two",
    )
    assert second.audio_range.capture_source_id == new_source
    assert second.audio_range.started_at == start + timedelta(seconds=offset)
    assert second.audio_range.chunk_ids != first.audio_range.chunk_ids
    assert replay.audio_range.chunk_ids == second.audio_range.chunk_ids
    assert await AudioChunkDocument.count() == 2
