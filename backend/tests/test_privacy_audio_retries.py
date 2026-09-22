"""Retained private audio retries must not repeatedly decode an unchanged window."""

import array
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_privacy_entrypoints import START, db  # noqa: F401

from backend.models.audio_capture import AudioRangeRef
from backend.models.timeline import EvidenceLocator
from backend.services import device_audio_ingest as ingest
from backend.services import privacy


@pytest.fixture
async def window(db, monkeypatch):
    item = SimpleNamespace(
        user_id="owner",
        source_id="screenpipe-test",
        source_item_id="synthetic-audio",
        captured_at=START,
        ended_at=START + timedelta(seconds=3),
        metadata={"direction": "input"},
        locator=EvidenceLocator(
            capture_source_id="screenpipe-test", modality="audio", track_id="microphone"
        ),
        media_data=b"synthetic retained input",
        media_filename="synthetic.wav",
        delete=AsyncMock(),
    )

    class Query:
        def sort(self, *args):
            return self

        async def to_list(self):
            return [item]

    monkeypatch.setattr(
        ingest,
        "DeviceInputItem",
        SimpleNamespace(kind="kind", state="state", find=lambda *a: Query()),
    )
    monkeypatch.setattr(ingest, "PydanticObjectId", lambda x: x)
    monkeypatch.setattr(
        ingest,
        "User",
        SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(id="owner", user_id="owner"))
        ),
    )
    monkeypatch.setattr(ingest, "require_speech_for_transcription", lambda: False)
    monkeypatch.setattr(ingest, "_profile_wav", lambda *a: SimpleNamespace(scored=True))
    reference = AudioRangeRef(
        capture_source_id="screenpipe-test:input:microphone",
        time_basis="recorded",
        chunk_ids=["a" * 24],
        capture_session_ids=["synthetic-capture"],
        started_at=START,
        ended_at=item.ended_at,
    )

    async def mix(_items, _directory, path):
        ingest._write_wav(path, array.array("h", [10] * 48000).tobytes(), 16000, 1, 2)

    mixer = AsyncMock(side_effect=mix)
    persist = AsyncMock(return_value=SimpleNamespace(audio_range=reference))
    submitted = AsyncMock(return_value="synthetic-recording")
    monkeypatch.setattr(ingest, "_mix_session", mixer)
    monkeypatch.setattr(ingest, "_persist_capture_window", persist)
    monkeypatch.setattr(ingest, "_ingest_segment", submitted)

    async def segment_range(capture_range, segment):
        return capture_range.model_copy(
            update={
                "started_at": segment.started_at,
                "ended_at": segment.ended_at,
            }
        )

    monkeypatch.setattr(ingest, "_segment_audio_range", segment_range)
    return SimpleNamespace(item=item, mix=mixer, persist=persist, submitted=submitted)


async def allow(db, item, seconds=None):
    await db.privacy_overrides.insert_one(
        dict(
            user_id="owner",
            source_id=item.source_id,
            started_at=item.captured_at,
            ended_at=(
                item.ended_at
                if seconds is None
                else item.captured_at + timedelta(seconds=seconds)
            ),
            override="allowed",
            revision=2,
        )
    )
    await db.capture_sources.update_one(
        {"source_id": item.source_id}, {"$inc": {"privacy_revision": 1}}
    )


async def test_fully_held_retry_keeps_original_capture_and_avoids_repeat_decode(
    db, window
):
    first = await ingest.process_device_audio()
    assert first["held_windows_before_decode"] == 1
    assert await db.privacy_audio_inputs.count_documents({}) == 0
    assert not await db.privacy_audio_progress.count_documents({})
    result = await ingest.process_device_audio()
    assert result["held_windows_before_decode"] == 1
    assert window.mix.await_count == window.persist.await_count == 0
    window.submitted.assert_not_awaited()
    window.item.delete.assert_not_awaited()
    assert window.item.media_data == b"synthetic retained input"
    await allow(db, window.item)
    result = await ingest.process_device_audio()
    assert result["processed_sessions"] == 1
    assert window.mix.await_count == window.persist.await_count == 1
    reference = window.submitted.call_args.args[-1]
    assert (
        reference.started_at == window.item.captured_at
        and reference.ended_at == window.item.ended_at
    )
    window.item.delete.assert_awaited_once()


async def test_failed_capture_persistence_never_creates_skip_receipt(db, window):
    await allow(db, window.item)
    window.persist.side_effect = RuntimeError("synthetic capture failure")
    await ingest.process_device_audio()
    await ingest.process_device_audio()
    assert window.mix.await_count == window.persist.await_count == 2
    assert await db.privacy_audio_inputs.count_documents({}) == 0
    window.item.delete.assert_not_awaited()
    window.submitted.assert_not_awaited()


async def test_held_raw_window_does_not_load_saved_derivative_dependencies(
    db, window, monkeypatch
):
    from backend.services import gallery_dependencies

    dependencies = AsyncMock(wraps=gallery_dependencies.load_dependencies)
    monkeypatch.setattr(gallery_dependencies, "load_dependencies", dependencies)
    result = await ingest.process_device_audio()
    assert result["held_windows_before_decode"] == 1
    dependencies.assert_not_awaited()
    window.mix.assert_not_awaited()
    window.submitted.assert_not_awaited()


async def test_completed_raw_portions_skip_dependencies_until_new_time_is_allowed(
    db, window, monkeypatch
):
    from backend.services import gallery_dependencies

    await allow(db, window.item, seconds=1)
    await ingest.process_device_audio()
    dependencies = AsyncMock(wraps=gallery_dependencies.load_dependencies)
    monkeypatch.setattr(gallery_dependencies, "load_dependencies", dependencies)
    result = await ingest.process_device_audio()
    assert result["retained_windows_without_new_work"] == 1
    dependencies.assert_not_awaited()
    assert window.mix.await_count == window.submitted.await_count == 1
    window.item.delete.assert_not_awaited()

    await allow(db, window.item)
    result = await ingest.process_device_audio()
    assert result["processed_sessions"] == 1
    assert dependencies.await_count > 0
    claims = [call.args[-1] for call in window.submitted.call_args_list]
    assert [(claim.started_at, claim.ended_at) for claim in claims] == [
        (START, START + timedelta(seconds=1)),
        (START + timedelta(seconds=1), START + timedelta(seconds=3)),
    ]


@pytest.mark.parametrize("change", ["revision", "updating", "removed", "activated"])
async def test_raw_screening_revision_race_holds_before_decode(
    db, window, monkeypatch, change
):
    await allow(db, window.item)
    query = {"source_id": window.item.source_id}
    original_source = await db.capture_sources.find_one(query)
    if change == "activated":
        await db.capture_sources.delete_one(query)
    original = privacy._load_capture_snapshot

    async def crossed(owner, *args, **kwargs):
        snapshot = await original(owner, *args, **kwargs)
        if change == "removed":
            await db.capture_sources.delete_one(query)
        elif change == "activated":
            await db.capture_sources.insert_one(original_source)
        else:
            update = (
                {"$inc": {"privacy_revision": 1}}
                if change == "revision"
                else {"$set": {"privacy_updating": True}}
            )
            await db.capture_sources.update_one(query, update)
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", crossed)
    result = await ingest.process_device_audio()
    assert result["held_windows_before_decode"] == 1
    window.mix.assert_not_awaited()
    window.submitted.assert_not_awaited()
    window.item.delete.assert_not_awaited()


async def test_raw_screening_holds_an_already_updating_source(db, window):
    await allow(db, window.item)
    await db.capture_sources.update_one(
        {"source_id": window.item.source_id}, {"$set": {"privacy_updating": True}}
    )
    result = await ingest.process_device_audio()
    assert result["held_windows_before_decode"] == 1
    window.mix.assert_not_awaited()
    window.submitted.assert_not_awaited()


async def test_changed_bytes_reenter_canonical_capture_validation(db, window):
    await allow(db, window.item, seconds=1)
    await ingest.process_device_audio()
    window.item.media_data = b"changed bytes with same source item identity"
    window.persist.side_effect = ValueError("synthetic immutable capture conflict")
    await ingest.process_device_audio()
    assert window.mix.await_count == window.persist.await_count == 2
    assert await db.privacy_audio_inputs.count_documents({}) == 1
    window.item.delete.assert_not_awaited()
    window.submitted.assert_awaited_once()


async def test_completed_before_cleanup_retries_existing_capture_validation(db, window):
    await allow(db, window.item)
    window.item.delete.side_effect = RuntimeError("synthetic cleanup interruption")
    await ingest.process_device_audio()
    window.item.delete.side_effect = None
    result = await ingest.process_device_audio()
    assert result["retained_windows_without_new_work"] == 0
    assert window.mix.await_count == window.persist.await_count == 2
    assert window.submitted.await_count == 1
    assert window.item.delete.await_count == 2


async def test_receipt_is_owner_and_source_scoped(db, window):
    await allow(db, window.item, seconds=1)
    await ingest.process_device_audio()
    await db.privacy_audio_inputs.update_many({}, {"$set": {"user_id": "other-owner"}})
    result = await ingest.process_device_audio()
    assert result["retained_windows_without_new_work"] == 0
    assert window.mix.await_count == window.persist.await_count == 2
    window.submitted.assert_awaited_once()


async def test_revision_change_during_skip_defers_then_rechecks_override(
    db, window, monkeypatch
):
    await allow(db, window.item, seconds=1)
    await ingest.process_device_audio()
    original = privacy._load_capture_snapshot
    loads = 0

    async def crossed(owner, *args, **kwargs):
        nonlocal loads
        loads += 1
        snapshot = await original(owner, *args, **kwargs)
        if loads == 2:
            await allow(db, window.item)
        return snapshot

    monkeypatch.setattr(privacy, "_load_capture_snapshot", crossed)
    result = await ingest.process_device_audio()
    assert result["retained_windows_without_new_work"] == 0
    assert window.mix.await_count == 1
    window.submitted.assert_awaited_once()
    window.item.delete.assert_not_awaited()
    monkeypatch.setattr(privacy, "_load_capture_snapshot", original)
    await ingest.process_device_audio()
    assert window.submitted.await_count == 2
    claims = [call.args[-1] for call in window.submitted.call_args_list]
    assert [(claim.started_at, claim.ended_at) for claim in claims] == [
        (START, START + timedelta(seconds=1)),
        (START + timedelta(seconds=1), START + timedelta(seconds=3)),
    ]


@pytest.mark.parametrize(
    "field", ["bytes", "time", "end", "track", "owner", "item", "suffix", "assembly"]
)
async def test_input_identity_invalidates_for_every_assembly_input(
    window, monkeypatch, field
):
    item = window.item
    before = ingest._audio_input_identity([item])
    if field == "bytes":
        item.media_data = b"new bytes"
    elif field == "time":
        item.captured_at += timedelta(milliseconds=1)
    elif field == "end":
        item.ended_at += timedelta(milliseconds=1)
    elif field == "track":
        item.locator = item.locator.model_copy(update={"track_id": "other-track"})
    elif field == "owner":
        item.user_id = "other-owner"
    elif field == "item":
        item.source_item_id = "other-item"
    elif field == "suffix":
        item.media_filename = "synthetic.mp4"
    else:
        monkeypatch.setattr(
            ingest, "_CAPTURE_ASSEMBLY_VERSION", "synthetic-next-assembly"
        )
    assert ingest._audio_input_identity([item]) != before
