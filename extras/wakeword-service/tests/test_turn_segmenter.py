import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turn_segmenter import TurnFrame, TurnPolicy, TurnSegmenter


def _frame(sequence: int, *, speech: bool, duration_ms: int = 40) -> TurnFrame:
    return TurnFrame(
        voice_session_id="voice-1",
        audio_session_id="audio-1",
        capture_epoch=3,
        sequence=sequence,
        monotonic_offset_ms=sequence * duration_ms,
        duration_ms=duration_ms,
        pcm=b"\x01\x00" * int(16 * duration_ms),
        speech=speech,
    )


@pytest.mark.asyncio
async def test_speech_opens_soft_ends_and_commits_one_turn_after_grace():
    segmenter = TurnSegmenter(TurnPolicy.conversational())
    events = []
    for sequence in range(8):
        events.extend(
            await segmenter.push(
                _frame(sequence, speech=sequence < 3),
                semantic_complete=sequence >= 5,
            )
        )

    soft = next(event for event in events if event.kind == "soft_ended")
    committed = await segmenter.advance(soft.deadline_ms)

    assert [event.kind for event in events].count("opened") == 1
    assert [event.kind for event in committed] == ["committed"]
    assert committed[0].turn_id == soft.turn_id
    assert committed[0].revision == 0


@pytest.mark.asyncio
async def test_speech_during_grace_reopens_same_turn_and_invalidates_revision():
    segmenter = TurnSegmenter(TurnPolicy.conversational())
    events = []
    for sequence in range(6):
        events.extend(
            await segmenter.push(
                _frame(sequence, speech=sequence < 2),
                semantic_complete=sequence >= 4,
            )
        )
    first_soft = next(event for event in events if event.kind == "soft_ended")

    reopened = await segmenter.push(_frame(6, speech=True))

    assert [event.kind for event in reopened] == ["reopened"]
    assert reopened[0].turn_id == first_soft.turn_id
    assert reopened[0].revision == 1
    assert await segmenter.advance(first_soft.deadline_ms) == []


@pytest.mark.asyncio
async def test_sequence_gap_cancels_open_turn_instead_of_dispatching_partial_audio():
    segmenter = TurnSegmenter(TurnPolicy.conversational())
    opened = await segmenter.push(_frame(1, speech=True))

    cancelled = await segmenter.push(_frame(3, speech=True))

    assert opened[0].kind == "opened"
    assert cancelled[0].kind == "cancelled"
    assert cancelled[0].reason == "frame_gap"
    assert all(event.kind != "committed" for event in cancelled)


@pytest.mark.asyncio
async def test_epoch_change_cancels_old_turn_and_new_speech_opens_new_identity():
    segmenter = TurnSegmenter(TurnPolicy.conversational())
    first = await segmenter.push(_frame(0, speech=True))
    changed = _frame(0, speech=True)
    changed = TurnFrame(**{**changed.__dict__, "capture_epoch": 4})

    events = await segmenter.push(changed)

    assert [event.kind for event in events] == ["cancelled", "opened"]
    assert events[0].reason == "epoch_changed"
    assert events[1].turn_id != first[0].turn_id


def test_dictation_policy_has_longer_complete_and_incomplete_grace():
    conversational = TurnPolicy.conversational()
    dictation = TurnPolicy.dictation()

    assert dictation.complete_grace_ms > conversational.complete_grace_ms
    assert dictation.incomplete_grace_ms > conversational.incomplete_grace_ms


@pytest.mark.asyncio
async def test_speech_offsets_exclude_preroll_and_endpoint_silence_on_device_clock():
    segmenter = TurnSegmenter(TurnPolicy.conversational())
    events = []
    for sequence in range(12):
        frame = _frame(sequence, speech=2 <= sequence < 5)
        frame = TurnFrame(
            **{
                **frame.__dict__,
                "device_monotonic_ms": 900_000 + sequence * 40,
                "captured_at_ms": 1_000_000 + sequence * 40,
            }
        )
        events += await segmenter.push(frame, semantic_complete=sequence >= 7)
    soft = next(e for e in events if e.kind == "soft_ended")
    committed = (await segmenter.advance(soft.deadline_ms))[0]
    assert committed.speech_started_device_ms == 900_080
    assert committed.speech_ended_device_ms == 900_200
    assert committed.speech_started_at_ms == 1_000_080
    assert committed.speech_ended_at_ms == 1_000_200
    assert committed.ended_at_ms > 200
