import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models.timeline import EvidenceLocator, TimelineEpisode
from backend.services.timeline import evidence
from backend.services.timeline.contracts import (
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)
from backend.services.timeline.evidence import (
    _audio_item,
    _clip_evidence_to_range,
    _coalesce_application_evidence,
    _device_item,
    _device_items,
    _transcript_item,
    _transcript_items,
    _window_items,
    day_bounds,
    summarize_immich_evidence,
)
from backend.services.transcript_time import AnchorMap, ChunkAnchor


def _screen_locator(source_id: str) -> EvidenceLocator:
    return EvidenceLocator(
        capture_source_id=source_id,
        modality="screen",
        track_id="display-1",
    )


def _photo_locator(source_id: str = "immich-default") -> EvidenceLocator:
    return EvidenceLocator(capture_source_id=source_id, modality="photo")


def test_local_day_bounds_respect_non_utc_timezone():
    start, end = day_bounds(date(2026, 8, 6), "Asia/Kolkata")

    assert start == datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 6, 18, 30, tzinfo=timezone.utc)


def test_dst_day_uses_real_local_midnights():
    start, end = day_bounds(date(2026, 11, 1), "America/New_York")

    assert (end - start).total_seconds() == 25 * 60 * 60


def test_adjacent_app_rows_are_compacted_without_becoming_episode_boundaries():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        TimelineEvidenceItem(
            evidence_id=f"observation:{index}",
            kind="observation",
            source_id="capture-node",
            locator=_screen_locator("capture-node"),
            started_at=start,
            ended_at=start,
            role="application_state",
            excerpt=f"state {index}",
            metadata={
                "source_kind": "activity",
                "app_name": "VLC",
                "window_name": "Terminator",
            },
        )
        for index in range(2)
    ]

    compacted = _coalesce_application_evidence(rows)

    assert len(compacted) == 1
    assert compacted[0].metadata["coalesced_count"] == 2
    assert "state 0" in compacted[0].excerpt
    assert "state 1" in compacted[0].excerpt


def _app_item(
    evidence_id: str,
    source_id: str,
    started_at: datetime,
    *,
    browser_url: str = "",
) -> TimelineEvidenceItem:
    return TimelineEvidenceItem(
        evidence_id=evidence_id,
        kind="observation",
        source_id=source_id,
        locator=_screen_locator(source_id),
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10),
        role="application_state",
        excerpt=evidence_id,
        metadata={
            "source_kind": "screen_context",
            "app_name": "Editor",
            "window_name": "Project",
            "browser_url": browser_url,
        },
    )


def test_reverse_order_does_not_merge_distant_screen_evidence():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        _app_item("later", "laptop", start + timedelta(hours=2)),
        _app_item("earlier", "laptop", start),
    ]

    compacted = _coalesce_application_evidence(rows)

    assert [item.evidence_id for item in compacted] == ["earlier", "later"]


def test_interleaved_sources_are_compacted_independently_then_merged_for_user():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        _app_item("desktop-1", "desktop", start),
        _app_item("laptop-1", "laptop", start + timedelta(seconds=10)),
        _app_item("desktop-2", "desktop", start + timedelta(seconds=30)),
        _app_item("laptop-2", "laptop", start + timedelta(seconds=40)),
    ]

    compacted = _coalesce_application_evidence(rows)

    assert [
        (item.source_id, item.metadata["coalesced_count"]) for item in compacted
    ] == [
        ("desktop", 2),
        ("laptop", 2),
    ]
    assert compacted[0].started_at == start
    assert compacted[0].ended_at == start + timedelta(seconds=40)
    assert compacted[1].started_at == start + timedelta(seconds=10)
    assert compacted[1].ended_at == start + timedelta(seconds=50)


def test_same_window_on_different_browser_pages_is_not_compacted():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    rows = [
        _app_item("one", "laptop", start, browser_url="https://one.example"),
        _app_item(
            "two",
            "laptop",
            start + timedelta(seconds=20),
            browser_url="https://two.example",
        ),
    ]

    assert len(_coalesce_application_evidence(rows)) == 2


def test_adjacent_distinct_games_remain_separate_evidence():
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    aoe = _app_item("aoe", "capture-node", start)
    aoe.metadata.update(
        {"app_name": "steam_app_1466860", "window_name": "Age of Empires IV"}
    )
    rematch = _app_item("rematch", "capture-node", start + timedelta(seconds=10))
    rematch.metadata.update({"app_name": "steam_app_2138720", "window_name": "REMATCH"})

    compacted = _coalesce_application_evidence([aoe, rematch])

    assert [item.evidence_id for item in compacted] == ["aoe", "rematch"]


def test_stale_observation_is_split_at_unsupported_liveness_gap():
    start = datetime(2026, 8, 5, 10, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    row = SimpleNamespace(
        id="row-one",
        user_id="user",
        source_id="laptop",
        locator=_screen_locator("laptop"),
        kind="observation",
        source_item_id="observation:one",
        captured_at=start,
        ended_at=end,
        metadata={
            "app_name": "Editor",
            "window_name": "Project",
            "preview_frame_id": 2,
        },
        samples=[
            {"captured_at": start.isoformat(), "liveness": False, "text": "first"},
            {"captured_at": end.isoformat(), "liveness": True, "text": "second"},
        ],
        frame_candidates=[
            {"captured_at": start.isoformat(), "frame_id": 1},
            {"captured_at": end.isoformat(), "frame_id": 2},
        ],
        content_hash=None,
        curation_revision=None,
        media_data=b"preview",
        curation=None,
        media_content_type="image/jpeg",
        lifecycle="closed",
    )

    items = _device_items(row)

    assert len(items) == 2
    assert [item.started_at for item in items] == [start, end]
    assert all(item.ended_at is None for item in items)
    assert "first" in (items[0].excerpt or "")
    assert "second" not in (items[0].excerpt or "")
    assert "second" in (items[1].excerpt or "")
    assert "first" not in (items[1].excerpt or "")
    assert [item.image_filename is not None for item in items] == [False, True]


def test_open_observation_uses_latest_marker_as_provisional_end():
    start = datetime(2026, 8, 6, 19, tzinfo=timezone.utc)
    latest = start + timedelta(minutes=18)
    row = SimpleNamespace(
        id="open-game",
        user_id="user",
        source_id="capture-node",
        locator=_screen_locator("capture-node"),
        kind="observation",
        source_item_id="observation:open-game",
        captured_at=start,
        ended_at=None,
        metadata={"app_name": "steam_app_1466860", "window_name": "Age of Empires IV"},
        samples=[
            {"captured_at": start.isoformat(), "text": "match loading"},
            {"captured_at": latest.isoformat(), "text": "victory"},
        ],
        frame_candidates=[],
        content_hash=None,
        curation_revision=None,
        media_data=None,
        curation="pending",
        media_content_type=None,
        lifecycle="open",
    )

    items = _device_items(row)

    assert len(items) == 1
    assert items[0].started_at == start
    assert items[0].ended_at == latest
    assert items[0].metadata["provisional_end"] is True
    assert items[0].metadata["observation_scope"] == "coarse_application_session"


def test_audio_from_multiple_sources_remains_separate():
    started_at = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            id="audio-a",
            source_id="microphone-a",
            locator=EvidenceLocator(
                capture_source_id="screenpipe-a",
                modality="audio",
                track_id="microphone-a",
            ),
            first_source_item_id="chunk-a",
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=10),
            direction="input",
            source_range_hash="hash-a",
            meeting_id=None,
            state="transcribed",
            covered_seconds=600,
            missing_seconds=0,
            bucket_seconds=10,
            coverage_fraction=[1.0],
            speech_fraction=[0.5],
            acoustic_active_fraction=[0.7],
            rms_dbfs=[-20.0],
            peak_dbfs=[-5.0],
            speech_seconds=0,
            acoustic_active_seconds=0,
            acoustic_quiet_seconds=0,
            longest_no_speech_seconds=20,
            conversation_id=None,
        ),
        SimpleNamespace(
            id="audio-b",
            source_id="microphone-b",
            locator=EvidenceLocator(
                capture_source_id="screenpipe-b",
                modality="audio",
                track_id="microphone-b",
            ),
            first_source_item_id="chunk-b",
            started_at=started_at + timedelta(minutes=2),
            ended_at=started_at + timedelta(minutes=8),
            direction="input",
            source_range_hash="hash-b",
            meeting_id=None,
            state="transcribed",
            covered_seconds=360,
            missing_seconds=0,
            bucket_seconds=10,
            coverage_fraction=[1.0],
            speech_fraction=[0.5],
            acoustic_active_fraction=[0.7],
            rms_dbfs=[-20.0],
            peak_dbfs=[-5.0],
            speech_seconds=0,
            acoustic_active_seconds=0,
            acoustic_quiet_seconds=0,
            longest_no_speech_seconds=20,
            conversation_id=None,
        ),
    ]

    evidence = [_audio_item(row) for row in rows]

    assert [item.evidence_id for item in evidence] == [
        "audio_span:audio-a",
        "audio_span:audio-b",
    ]
    assert [item.source_id for item in evidence] == ["microphone-a", "microphone-b"]


def test_evidence_is_clipped_to_the_requested_local_day():
    day_start = datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    item = _app_item("crossing", "laptop", day_start - timedelta(hours=1))
    item.ended_at = day_start + timedelta(hours=2)

    clipped = _clip_evidence_to_range([item], day_start, day_end)

    assert len(clipped) == 1
    assert clipped[0].started_at == day_start
    assert clipped[0].ended_at == day_start + timedelta(hours=2)


def test_transcript_bounds_come_from_capture_time_not_record_creation():
    """A re-bound child's ``created_at`` is the operation time, so it must not
    decide which day the recording is filed under."""
    captured = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    conversation = SimpleNamespace(
        conversation_id="conv-1",
        transcript="we talked about the release",
        created_at=datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc),
        audio_total_duration=120.0,
        external_source_id="",
        external_source_type=None,
        active_transcript=None,
    )

    item = _transcript_item(conversation, (captured, captured + timedelta(minutes=2)))

    assert item is not None
    assert item.started_at == captured
    assert item.ended_at == captured + timedelta(minutes=2)
    # No screenpipe source id, so no direction can be parsed and the speech is
    # not assumed to be media playback.
    assert item.role == "uncertain"
    assert item.metadata["conversation_id"] == "conv-1"


def test_oldest_screenpipe_transcript_does_not_treat_range_as_direction_or_track():
    captured = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    conversation = SimpleNamespace(
        conversation_id="conv-oldest",
        transcript="legacy capture",
        external_source_id="screenpipe:screenpipe-7423630a12f57567:1939-1959",
        active_transcript=None,
    )

    item = _transcript_item(conversation, (captured, captured + timedelta(minutes=2)))

    assert item is not None
    assert item.metadata["direction"] == "unknown"
    assert item.locator.capture_source_id == "screenpipe-7423630a12f57567"
    assert item.locator.track_id is None
    assert item.role == "uncertain"


def test_timestamped_transcript_blocks_give_the_agent_real_internal_cut_points():
    captured = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    segments = [
        SimpleNamespace(
            start=0.0,
            end=60.0,
            text="First topic",
            speaker="Speaker 1",
            identified_as="Alex",
            segment_type="speech",
        ),
        SimpleNamespace(
            start=120.0,
            end=180.0,
            text="Still first topic",
            speaker="Speaker 2",
            identified_as="Aryan",
            segment_type="speech",
        ),
        SimpleNamespace(
            start=360.0,
            end=420.0,
            text="New topic after a real gap",
            speaker="Speaker 1",
            identified_as="Alex",
            segment_type="speech",
        ),
    ]
    version = SimpleNamespace(version_id="speaker-v2", segments=segments)
    conversation = SimpleNamespace(
        conversation_id="conv-long",
        transcript="fallback",
        external_source_id="screenpipe:host:input:stream",
        active_transcript=version,
        segments=segments,
    )
    anchors = AnchorMap(
        conversation_id="conv-long",
        anchors=[ChunkAnchor(0.0, 600.0, captured)],
    )

    items = _transcript_items(
        conversation,
        (captured, captured + timedelta(minutes=10)),
        anchors,
    )

    assert len(items) == 2
    assert [(item.started_at, item.ended_at) for item in items] == [
        (captured, captured + timedelta(minutes=3)),
        (captured + timedelta(minutes=6), captured + timedelta(minutes=7)),
    ]
    assert "Alex: First topic" in items[0].excerpt
    assert "Aryan: Still first topic" in items[0].excerpt
    assert all(item.metadata["conversation_id"] == "conv-long" for item in items)
    assert [item.metadata["segment_count"] for item in items] == [2, 1]


def test_transcript_blocks_carry_an_unambiguous_meeting_hint():
    captured = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    segment = SimpleNamespace(
        start=0.0,
        end=60.0,
        text="Meeting discussion",
        speaker="Speaker 1",
        identified_as="Rishon",
        segment_type="speech",
    )
    conversation = SimpleNamespace(
        conversation_id="conv-meeting",
        transcript="Meeting discussion",
        external_source_id="screenpipe:host:input:stream",
        active_transcript=SimpleNamespace(version_id="v1", segments=[segment]),
        segments=[segment],
    )

    items = _transcript_items(
        conversation,
        (captured, captured + timedelta(minutes=1)),
        AnchorMap(
            conversation_id="conv-meeting",
            anchors=[ChunkAnchor(0.0, 60.0, captured)],
        ),
        meeting_id="meeting:recorder:21",
    )

    assert items[0].metadata["meeting_id"] == "meeting:recorder:21"


def test_coverage_windows_skip_hours_with_no_evidence():
    started = datetime(2026, 8, 6, tzinfo=timezone.utc)
    evidence_item = TimelineEvidenceItem(
        evidence_id="one-event",
        kind="observation",
        locator=_screen_locator("screenpipe-default"),
        started_at=started + timedelta(hours=12),
        ended_at=started + timedelta(hours=12, minutes=25),
        role="application_state",
    )

    windows = _window_items(
        started, started + timedelta(days=1), 20, 3, [evidence_item]
    )

    assert len(windows) == 2
    assert all(window.evidence_ids == ["one-event"] for window in windows)


def test_immich_visual_description_and_ocr_become_timeline_text():
    captured = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id="photo-row",
        kind="immich_memory",
        source_id="immich-default",
        locator=_photo_locator(),
        source_item_id="dog-photo",
        captured_at=captured,
        ended_at=None,
        metadata={
            "description": "A brown dog playing with a red ball in a garden.",
            "ocr_text": "DOG PARK",
            "timeline_relevance": "high",
        },
        samples=[],
        content_hash="digest",
        curation_revision=None,
        media_data=None,
        media_content_type=None,
        frame_candidates=[],
        curation=None,
    )

    item = _device_item(row)

    assert item.kind == "immich"
    assert "brown dog" in item.excerpt
    assert "DOG PARK" in item.excerpt
    assert item.metadata["timeline_relevance"] == "high"


def test_immich_summary_reports_actual_helpful_windows():
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    items = [
        TimelineEvidenceItem(
            evidence_id="immich:dog",
            kind="immich",
            locator=_photo_locator(),
            started_at=start + timedelta(minutes=4),
            role="user_action",
            metadata={"timeline_relevance": "high"},
        ),
        TimelineEvidenceItem(
            evidence_id="immich:blur",
            kind="immich",
            locator=_photo_locator(),
            started_at=start + timedelta(minutes=25),
            role="user_action",
            metadata={"timeline_relevance": "none"},
        ),
    ]
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=start,
        ended_at=start + timedelta(hours=1),
        evidence_revision="revision",
        evidence=items,
        windows=[
            TimelineCoverageWindow(
                window_id="one",
                started_at=start,
                ended_at=start + timedelta(minutes=20),
                evidence_ids=["immich:dog"],
            ),
            TimelineCoverageWindow(
                window_id="two",
                started_at=start + timedelta(minutes=17),
                ended_at=start + timedelta(minutes=37),
                evidence_ids=["immich:blur"],
            ),
        ],
    )

    summary = summarize_immich_evidence(manifest)

    assert summary.evidence_count == 2
    assert summary.helpful_evidence_count == 1
    assert summary.window_count == 2
    assert [window.helpful_asset_count for window in summary.windows] == [1, 0]


class _FakeAggregateCollection:
    def __init__(self, rows):
        self.rows = rows
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        rows = self.rows

        class _Cursor:
            async def to_list(self, length=None):
                return list(rows)

        return _Cursor()


class _FakeFindCollection:
    def __init__(self, rows):
        self.rows = rows
        self.query = None

    def find(self, query):
        self.query = query
        rows = self.rows

        class _Cursor:
            async def to_list(self, length=None):
                return list(rows)

        return _Cursor()


@pytest.mark.asyncio
async def test_large_device_rows_are_parsed_off_the_backend_event_loop(monkeypatch):
    collection = _FakeFindCollection([{"_id": "one"}, {"_id": "two"}])
    main_thread = threading.get_ident()
    parser_threads = []

    monkeypatch.setattr(
        evidence.DeviceInputItem,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    monkeypatch.setattr(
        evidence.DeviceInputItem,
        "model_validate",
        classmethod(
            lambda cls, row: parser_threads.append(threading.get_ident())
            or SimpleNamespace(id=row["_id"])
        ),
    )

    rows = await evidence._device_input_rows(
        "user",
        datetime(2026, 8, 6, tzinfo=timezone.utc),
        datetime(2026, 8, 7, tzinfo=timezone.utc),
    )

    assert [row.id for row in rows] == ["one", "two"]
    assert parser_threads and all(thread != main_thread for thread in parser_threads)
    assert collection.query["user_id"] == "user"


@pytest.mark.asyncio
async def test_audio_bounds_are_taken_from_semantic_range_claims(monkeypatch):
    day_start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    rows = [
        {
            "conversation_id": "screen-capture",
            "audio_ranges": [
                {
                    "started_at": day_start + timedelta(hours=1),
                    "ended_at": day_start + timedelta(hours=1, seconds=10),
                    "time_basis": "received",
                }
            ],
        },
        {
            "conversation_id": "phone-recording",
            "audio_ranges": [
                {
                    "started_at": day_start + timedelta(hours=5),
                    "ended_at": day_start + timedelta(hours=5, seconds=10),
                    "time_basis": "recorded",
                }
            ],
        },
    ]

    class Collection:
        query = None

        def find(self, query, projection):
            self.query = query

            class Cursor:
                async def to_list(self, length=None):
                    return rows

            return Cursor()

    collection = Collection()
    monkeypatch.setattr(
        evidence.Conversation,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    bounds = await evidence._conversation_audio_bounds("user", day_start, day_end)

    assert set(bounds) == {"screen-capture", "phone-recording"}
    assert bounds["phone-recording"][0] == day_start + timedelta(hours=5)
    # Selection is on claim time only; source type never changes the rule.
    assert collection.query["user_id"] == "user"
    assert collection.query["audio_ranges"]["$elemMatch"]["time_basis"] == {
        "$ne": "unknown"
    }


@pytest.mark.asyncio
async def test_range_loader_preserves_exact_predecessors_and_confirmed_fields(
    monkeypatch,
):
    start = datetime(2026, 9, 4, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    episode = TimelineEpisode.model_construct(
        episode_id="row-id",
        episode_key="stable-key",
        revision=7,
        run_id="run",
        user_id="user",
        local_date=start.date(),
        timezone="UTC",
        started_at=start,
        ended_at=end,
        kind="conversation",
        title="Session",
        summary="A session",
        confidence=1,
        activity_mode="foreground",
        confirmed_fields=["title", "conversational"],
        conversational=True,
    )
    manifest = TimelineEvidenceManifest(
        user_id="user",
        local_date=start.date(),
        timezone="UTC",
        started_at=start,
        ended_at=end,
        evidence_revision="hash",
        windows=[],
        evidence=[],
    )

    async def assemble(*args):
        return manifest, {}

    queries = []

    def find(query):
        queries.append(query)

        async def rows():
            return [episode]

        return SimpleNamespace(to_list=rows)

    monkeypatch.setattr(evidence, "_assemble_range_manifest", assemble)
    monkeypatch.setattr(evidence, "TimelineEpisode", SimpleNamespace(find=find))
    monkeypatch.setattr(
        "backend.services.timeline.activity_policy.activity_policy_days",
        AsyncMock(return_value=[]),
    )
    bundle = await evidence.load_reconciliation_evidence(
        "user", start, end, timezone_name="UTC"
    )
    for payload in (bundle.existing_episodes[0], bundle.pinned_episodes[0]):
        assert payload["episode_key"] == "stable-key"
        assert payload["revision"] == 7
        assert payload["conversational"] is True
        assert payload["confirmed_fields"] == ["title", "conversational"]
        assert payload["evidence_ids"] == []
    assert all(query["status"] == {"$ne": "superseded"} for query in queries)
