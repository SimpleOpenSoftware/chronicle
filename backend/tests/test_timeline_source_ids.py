import pytest

from backend.services.timeline.source_ids import (
    format_screenpipe_segment_source_id,
    parse_screenpipe_segment_source_id,
    transcript_evidence_locator,
)


def test_current_segment_id_round_trips_track_separately_from_source_range():
    external = format_screenpipe_segment_source_id(
        "capture-node", "input", "audio:desk-mic", "71360", "71369"
    )

    assert external == "screenpipe:capture-node:input:audio:desk-mic:71360-71369"
    parsed = parse_screenpipe_segment_source_id(external)
    assert parsed is not None
    assert parsed.capture_source_id == "capture-node"
    assert parsed.direction == "input"
    assert parsed.track_id == "audio:desk-mic"
    assert parsed.source_range == "71360-71369"


def test_current_segments_share_transcript_track_when_only_range_changes():
    first = transcript_evidence_locator(
        "screenpipe:capture-node:input:desk-mic:71360-71369",
        "client",
        "conversation-1",
        "input",
    )
    second = transcript_evidence_locator(
        "screenpipe:capture-node:input:desk-mic:71370-71379",
        "client",
        "conversation-2",
        "input",
    )

    assert first == second
    assert first.track_id == "desk-mic"


def test_legacy_segment_range_is_not_guessed_to_be_a_track():
    first = transcript_evidence_locator(
        "screenpipe:capture-node:input:71360-71369",
        "client",
        "conversation-1",
        "input",
    )
    second = transcript_evidence_locator(
        "screenpipe:capture-node:input:71370-71379",
        "client",
        "conversation-2",
        "input",
    )

    assert first == second
    assert first.capture_source_id == "capture-node"
    assert first.track_id is None


def test_oldest_legacy_segment_omits_direction_and_track_honestly():
    parsed = parse_screenpipe_segment_source_id(
        "screenpipe:screenpipe-7423630a12f57567:1939-1959"
    )

    assert parsed is not None
    assert parsed.capture_source_id == "screenpipe-7423630a12f57567"
    assert parsed.direction == "unknown"
    assert parsed.track_id is None
    assert parsed.source_range == "1939-1959"


def test_malformed_screenpipe_segment_id_fails_instead_of_guessing():
    with pytest.raises(ValueError, match="malformed ScreenPipe"):
        parse_screenpipe_segment_source_id("screenpipe:capture-node:not-a-range")
