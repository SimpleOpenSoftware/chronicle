from chronicle_screenpipe.observations import (
    MAX_FRAME_CANDIDATES,
    ObservationTracker,
    content_fingerprint,
    normalize_text,
    stratify_candidates,
    text_is_novel,
)


def frame(
    identifier: int,
    second: int,
    app: str,
    title: str,
    *,
    trigger: str = "app_switch",
    text: str = "",
    text_source: str = "accessibility",
    device_name: str | None = None,
):
    return {
        "id": identifier,
        "timestamp": f"2026-07-23T10:00:{second:02d}Z",
        "app_name": app,
        "window_name": title,
        "browser_url": "",
        "capture_trigger": trigger,
        "full_text": text,
        "text_source": text_source,
        "device_name": device_name,
    }


def test_text_normalization_fingerprinting_and_novelty():
    assert normalize_text("  hello\n world ") == "hello world"
    assert content_fingerprint("hello  world") == content_fingerprint("hello world")
    assert not text_is_novel("editing the same module", "editing the same module.")
    assert text_is_novel("editing collector", "implementing observation state machine")


def test_passive_short_switch_is_folded_into_open_observation():
    tracker = ObservationTracker()
    opened = tracker.process_rows(
        [frame(1, 0, "Code", "collector.py", text="collector")],
        "2026-07-23T10:00:11Z",
    )
    assert [event["event"] for event in opened] == ["open"]

    events = tracker.process_rows(
        [
            frame(2, 20, "Switcher", "Alt-Tab"),
            frame(3, 23, "Code", "collector.py", text="collector"),
        ],
        "2026-07-23T10:00:23Z",
    )
    assert events == []
    assert tracker.active["source_item_id"] == "observation:1"


def test_meaningful_short_music_excursion_bypasses_stability_and_sample_cooldown():
    tracker = ObservationTracker()
    tracker.process_rows(
        [frame(1, 0, "Code", "collector.py", text="collector")],
        "2026-07-23T10:00:11Z",
    )
    events = tracker.process_rows(
        [
            frame(2, 20, "Music", "Album", text="Track one"),
            frame(
                3,
                22,
                "Music",
                "Album",
                trigger="click",
                text="Track two playing",
            ),
            frame(4, 24, "Code", "collector.py", text="collector"),
        ],
        "2026-07-23T10:00:24Z",
    )

    assert [(event["event"], event["source_item_id"]) for event in events] == [
        ("close", "observation:1"),
        ("open", "observation:2"),
        ("close", "observation:2"),
    ]
    assert tracker.candidate["source_item_id"] == "observation:4"


def test_long_activity_stays_one_observation_with_novel_and_liveness_samples():
    tracker = ObservationTracker()
    tracker.process_rows(
        [frame(1, 0, "Code", "service.py", text="initial code")],
        "2026-07-23T10:00:11Z",
    )
    inside_cooldown = tracker.process_rows(
        [
            frame(
                2,
                30,
                "Code",
                "service.py",
                trigger="typing_pause",
                text="a materially different implementation",
            )
        ],
        "2026-07-23T10:00:30Z",
    )
    assert inside_cooldown == []

    novel = tracker.process_rows(
        [
            {
                **frame(
                    3,
                    0,
                    "Code",
                    "service.py",
                    trigger="typing_pause",
                    text="tests and backend integration are now implemented",
                ),
                "timestamp": "2026-07-23T10:03:00Z",
            }
        ],
        "2026-07-23T10:03:00Z",
    )
    assert [(event["event"], event["source_item_id"]) for event in novel] == [
        ("sample", "observation:1")
    ]

    liveness = tracker.process_rows(
        [
            {
                **frame(
                    4,
                    0,
                    "Code",
                    "service.py",
                    text="tests and backend integration are now implemented",
                ),
                "timestamp": "2026-07-23T10:18:01Z",
            }
        ],
        "2026-07-23T10:18:01Z",
    )
    assert len(liveness) == 1
    assert liveness[0]["event"] == "sample"
    assert liveness[0]["sample"]["liveness"] is True
    assert tracker.active["source_item_id"] == "observation:1"


def test_restart_gap_closes_old_observation_at_last_real_frame():
    tracker = ObservationTracker(max_continuity_gap_seconds=300)
    tracker.process_rows(
        [frame(1, 0, "Code", "service.py", text="initial code")],
        "2026-07-23T10:00:11Z",
    )
    next_day = {
        **frame(2, 0, "Code", "service.py", trigger="click", text="new session"),
        "timestamp": "2026-07-24T10:00:00Z",
    }

    events = tracker.process_rows([next_day], "2026-07-24T10:00:00Z")

    assert [(event["event"], event["source_item_id"]) for event in events] == [
        ("close", "observation:1"),
        ("open", "observation:2"),
    ]
    assert events[0]["ended_at"] == "2026-07-23T10:00:00Z"
    assert tracker.active["captured_at"] == "2026-07-24T10:00:00Z"


def test_contextless_ocr_enriches_context_without_replacing_accessibility_text():
    tracker = ObservationTracker()
    opened = tracker.process_rows(
        [
            frame(
                1,
                0,
                "Zen",
                "Chronicle — Zen Browser",
                text="Structured Chronicle page text",
            )
        ],
        "2026-07-23T10:00:11Z",
    )
    assert opened[0]["sample"]["text_source"] == "accessibility"

    events = tracker.process_rows(
        [
            frame(
                2,
                15,
                "",
                "",
                trigger="visual_change",
                text="N01sy full-screen OCR g1bb3rish",
                text_source="ocr",
            )
        ],
        "2026-07-23T10:00:15Z",
    )

    assert events == []
    assert tracker.active["source_item_id"] == "observation:1"
    assert tracker.active["key"] == ["Zen", "Chronicle — Zen Browser", ""]
    assert tracker.active["text"] == "Structured Chronicle page text"
    assert tracker.active["text_source"] == "accessibility"


def test_contextless_ocr_opens_after_stability_when_it_is_the_only_source():
    tracker = ObservationTracker()

    buffered = tracker.process_rows(
        [
            frame(
                1,
                0,
                "",
                "",
                trigger="visual_change",
                text="Visual-only application text",
                text_source="ocr",
            )
        ],
        "2026-07-23T10:00:05Z",
    )
    opened = tracker.process_rows([], "2026-07-23T10:00:11Z")

    assert buffered == []
    assert [event["event"] for event in opened] == ["open"]
    assert opened[0]["sample"]["text"] == "Visual-only application text"
    assert opened[0]["sample"]["text_source"] == "ocr"


def test_shortlist_samples_the_session_instead_of_the_best_scoring_burst():
    """Frame candidates must span the observation, not cluster on one moment.

    Consecutive frames of an unchanged window score almost identically, so keeping the
    top N by score alone returns neighbours. Measured on a live deployment, that left
    observations longer than 15 minutes represented by frames spanning 5.8% of their
    duration — a 45-minute session summarised from a single 2.5-minute slice.
    """

    burst = [
        {
            "frame_id": index,
            "score": 0.99,
            "captured_at": f"2026-07-23T10:00:{index:02d}Z",
        }
        for index in range(12)
    ]
    spread = [
        {
            "frame_id": 100 + minute,
            "score": 0.30,
            "captured_at": f"2026-07-23T10:{minute:02d}:00Z",
        }
        for minute in (10, 20, 30, 40, 50)
    ]

    result = stratify_candidates(burst + spread)

    assert len(result) == MAX_FRAME_CANDIDATES
    assert sum(1 for candidate in result if candidate["frame_id"] < 100) == 1
    # Every later slice of the hour keeps its representative despite scoring lower.
    assert [c["frame_id"] for c in result if c["frame_id"] >= 100] == [
        100 + minute for minute in (10, 20, 30, 40, 50)
    ]


def test_short_shortlist_is_returned_whole():
    candidates = [
        {"frame_id": 3, "score": 0.1, "captured_at": "2026-07-23T10:00:03Z"},
        {"frame_id": 1, "score": 0.9, "captured_at": "2026-07-23T10:00:01Z"},
    ]
    assert [c["frame_id"] for c in stratify_candidates(candidates)] == [1, 3]


def test_tracker_rejects_frames_from_another_display_track():
    tracker = ObservationTracker(
        capture_source_id="screenpipe-capture-node", track_id="Display 1"
    )
    tracker.process_rows(
        [frame(1, 0, "Code", "collector.py", device_name="Display 1")],
        "2026-07-23T10:00:11Z",
    )

    try:
        tracker.process_rows(
            [frame(2, 12, "Video", "Movie", device_name="Display 2")],
            "2026-07-23T10:00:12Z",
        )
    except ValueError as error:
        assert "Display 2" in str(error)
        assert "Display 1" in str(error)
    else:
        raise AssertionError("a display-local tracker accepted another display")

    assert tracker.active is not None
    assert tracker.active["device_name"] == "Display 1"


def test_observation_event_carries_a_typed_display_locator():
    tracker = ObservationTracker(
        capture_source_id="screenpipe-capture-node", track_id="Display 2"
    )

    [opened] = tracker.process_rows(
        [
            frame(
                7,
                0,
                "Video",
                "Movie",
                trigger="click",
                device_name="Display 2",
            )
        ],
        "2026-07-23T10:00:00Z",
    )

    assert opened["locator"] == {
        "capture_source_id": "screenpipe-capture-node",
        "modality": "screen",
        "track_id": "Display 2",
    }
    assert opened["metadata"]["device_name"] == "Display 2"
    assert opened["metadata"]["locator"] == opened["locator"]
