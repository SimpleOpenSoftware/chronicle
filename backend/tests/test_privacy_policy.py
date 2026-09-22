from datetime import datetime, timedelta, timezone

from backend.services.privacy import PrivacySnapshot


def inventory(source):
    return [
        {
            "source_id": source["source_id"],
            "observed_at": source["privacy_enabled_from"],
            "transition_started_at": source["privacy_enabled_from"],
            "track_ids": source["privacy_tracks"],
        }
    ]


def test_display_disconnect_changes_future_requirements_and_holds_uncertain_transition():
    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    at = lambda seconds: start + timedelta(seconds=seconds)
    source = {
        "source_id": "screenpipe-a",
        "privacy_enabled_from": start,
        "privacy_tracks": ["one", "two"],
    }
    displays = inventory(source) + [
        {
            "source_id": "screenpipe-a",
            "observed_at": at(20),
            "transition_started_at": at(15),
            "track_ids": ["one"],
        }
    ]
    intervals = [
        {
            "source_id": "screenpipe-a",
            "track_id": "one",
            "segments": [dict(started_at=start, ended_at=at(60), state="allowed")],
        },
        {
            "source_id": "screenpipe-a",
            "track_id": "two",
            "segments": [dict(started_at=start, ended_at=at(10), state="allowed")],
        },
    ]
    policy = PrivacySnapshot([source], intervals, display_sets=displays)
    assert policy.allowed_spans("screenpipe-a:input:microphone", start, at(60)) == [
        (start, at(10)),
        (at(20), at(60)),
    ]
    assert not policy.permits("screenpipe-a", at(12), at(14))
    assert not policy.permits("screenpipe-a", at(16), at(18))


def test_frames_alone_do_not_establish_display_inventory():
    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    end = start + timedelta(seconds=10)
    source = {
        "source_id": "screenpipe-a",
        "privacy_enabled_from": start,
        "privacy_tracks": ["one"],
    }
    rows = [
        {
            "source_id": "screenpipe-a",
            "track_id": "one",
            "segments": [dict(started_at=start, ended_at=end, state="allowed")],
        }
    ]
    assert not PrivacySnapshot([source], rows).permits("screenpipe-a", start, end)


def test_exclusion_preserves_allowed_audio_parts_and_holds_gaps():
    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    source = {
        "source_id": "screenpipe-a",
        "privacy_enabled_from": start,
        "privacy_tracks": ["screen"],
        "privacy_revision": 1,
    }
    intervals = [
        {
            "source_id": "screenpipe-a",
            "track_id": "screen",
            "segments": [
                {
                    "started_at": start,
                    "ended_at": start + timedelta(seconds=10),
                    "state": "allowed",
                },
                {
                    "started_at": start + timedelta(seconds=10),
                    "ended_at": start + timedelta(seconds=20),
                    "state": "excluded",
                },
                {
                    "started_at": start + timedelta(seconds=20),
                    "ended_at": start + timedelta(seconds=30),
                    "state": "allowed",
                },
            ],
        }
    ]
    policy = PrivacySnapshot([source], intervals, display_sets=inventory(source))
    assert policy.allowed_spans(
        "screenpipe-a:input:microphone", start, start + timedelta(seconds=40)
    ) == [
        (start, start + timedelta(seconds=10)),
        (start + timedelta(seconds=20), start + timedelta(seconds=30)),
    ]
    assert policy.allowed_spans(
        "screenpipe-b", start, start + timedelta(seconds=40)
    ) == [(start, start + timedelta(seconds=40))]


def test_every_display_requires_coverage_and_manual_override_is_independent():
    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    end = start + timedelta(seconds=10)
    source = {
        "source_id": "screenpipe-a",
        "privacy_enabled_from": start,
        "privacy_tracks": ["one", "two"],
    }
    interval = {
        "source_id": "screenpipe-a",
        "track_id": "one",
        "segments": [dict(started_at=start, ended_at=end, state="allowed")],
    }
    assert (
        PrivacySnapshot(
            [source], [interval], display_sets=inventory(source)
        ).allowed_spans("screenpipe-a", start, end)
        == []
    )
    override = {
        "source_id": "screenpipe-a",
        "started_at": start,
        "ended_at": end,
        "override": "allowed",
    }
    assert PrivacySnapshot(
        [source], [interval], [override], inventory(source)
    ).allowed_spans("screenpipe-a", start, end) == [(start, end)]


def test_audio_cut_preserves_timestamps_and_original_file(tmp_path):
    import wave
    from types import SimpleNamespace

    from backend.services.device_audio_ingest import _privacy_segments, _Segment

    start = datetime(2026, 9, 16, tzinfo=timezone.utc)
    original = tmp_path / "capture.wav"
    # Three distinct one-second signals make wrong offsets observable.
    with wave.open(str(original), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\x00" * 16000 + b"\x02\x00" * 16000 + b"\x03\x00" * 16000)
    before = original.read_bytes()
    source = {
        "source_id": "screenpipe-a",
        "privacy_enabled_from": start,
        "privacy_tracks": ["screen"],
    }
    intervals = [
        {
            "source_id": "screenpipe-a",
            "track_id": "screen",
            "segments": [
                dict(
                    started_at=start,
                    ended_at=start + timedelta(seconds=1),
                    state="allowed",
                ),
                dict(
                    started_at=start + timedelta(seconds=1),
                    ended_at=start + timedelta(seconds=2),
                    state="excluded",
                ),
                dict(
                    started_at=start + timedelta(seconds=2),
                    ended_at=start + timedelta(seconds=3),
                    state="allowed",
                ),
            ],
        }
    ]
    segment = _Segment(
        [], original, start, start + timedelta(seconds=3), SimpleNamespace()
    )
    pieces = _privacy_segments(
        [segment],
        PrivacySnapshot([source], intervals, display_sets=inventory(source)),
        "screenpipe-a",
    )
    assert [(p.started_at, p.ended_at) for p in pieces] == [
        (start, start + timedelta(seconds=1)),
        (start + timedelta(seconds=2), start + timedelta(seconds=3)),
    ]
    for piece, expected in zip(pieces, (b"\x01\x00", b"\x03\x00")):
        with wave.open(str(piece.path), "rb") as wav:
            assert wav.readframes(wav.getnframes()) == expected * 16000
    assert original.read_bytes() == before


def test_long_overlapping_hold_survives_narrow_interval_lookup():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at = lambda seconds: start + timedelta(seconds=seconds)
    source = dict(
        source_id="synthetic", privacy_enabled_from=start, privacy_tracks=["display"]
    )
    rows = [
        dict(
            source_id="synthetic",
            track_id="display",
            segments=[dict(started_at=at(i), ended_at=at(i + 1), state="allowed")],
        )
        for i in range(100)
    ]
    rows.append(
        dict(
            source_id="synthetic",
            track_id="display",
            segments=[dict(started_at=at(0), ended_at=at(90), state="excluded")],
        )
    )
    policy = PrivacySnapshot([source], rows, display_sets=inventory(source))
    assert not policy.permits("synthetic", at(80), at(81))
    assert policy.permits("synthetic", at(90), at(91))
