from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.models.device_input import DeviceInputItem
from backend.models.timeline import AudioEvidenceSpan
from backend.services.timeline.context import compact_evidence_groups
from backend.services.timeline.contracts import (
    EvidenceAnchor,
    EvidenceCoverage,
    EvidenceLocator,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
)
from backend.services.timeline.evidence import (
    _audio_item,
    _device_item,
    adaptive_temporal_coverage,
    build_evidence_anchors,
)

START = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)


def _item(identifier: str, track_id: str) -> TimelineEvidenceItem:
    locator = EvidenceLocator(
        capture_source_id="screenpipe-capture-node",
        modality="screen",
        track_id=track_id,
    )
    return TimelineEvidenceItem(
        evidence_id=identifier,
        kind="observation",
        source_id="screenpipe-capture-node",
        locator=locator,
        started_at=START,
        ended_at=START + timedelta(minutes=1),
        role="application_state",
        metadata={"app_name": "Editor", "window_name": identifier},
        coverage=EvidenceCoverage(
            source_count=3, retained_count=3, agent_visible_count=2
        ),
    )


def test_device_input_requires_a_typed_locator_at_ingress():
    with pytest.raises(ValidationError, match="locator"):
        DeviceInputItem(
            user_id="user",
            source_id="screenpipe-capture-node",
            kind="observation",
            source_item_id="observation:1",
            captured_at=START,
            metadata={"device_name": "Display 2"},
        )


def test_device_input_rejects_locator_for_a_different_capture_source():
    with pytest.raises(ValueError, match="authenticated source"):
        DeviceInputItem(
            user_id="user",
            source_id="screenpipe-capture-node",
            kind="observation",
            source_item_id="observation:1",
            captured_at=START,
            locator={
                "capture_source_id": "screenpipe-other",
                "modality": "screen",
                "track_id": "Display 2",
            },
        )


def test_adaptive_temporal_coverage_keeps_edges_and_protected_markers():
    samples = [
        {
            "sample_id": str(index),
            "captured_at": (START + timedelta(minutes=index)).isoformat(),
            "capture_trigger": "idle" if index == 8 else None,
            "inactive": index == 8,
        }
        for index in range(20)
    ]

    retained = adaptive_temporal_coverage(samples, limit=6)

    assert [item["sample_id"] for item in retained][0] == "0"
    assert [item["sample_id"] for item in retained][-1] == "19"
    assert "8" in {item["sample_id"] for item in retained}
    assert len(retained) == 6
    moments = [datetime.fromisoformat(item["captured_at"]) for item in retained]
    assert moments == sorted(moments)


def test_context_compaction_never_combines_simultaneous_display_tracks():
    groups = compact_evidence_groups(
        _manifest(
            [_item("display-one", "Display 1"), _item("display-two", "Display 2")]
        )
    )

    assert len(groups) == 2
    assert {group["locators"][0]["track_id"] for group in groups} == {
        "Display 1",
        "Display 2",
    }


def test_device_evidence_retains_full_samples_and_resolvable_frame_anchors():
    row = SimpleNamespace(
        id="row-one",
        source_id="screenpipe-capture-node",
        kind="observation",
        source_item_id="observation:1",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-capture-node",
            modality="screen",
            track_id="Display 2",
        ),
        captured_at=START,
        ended_at=START + timedelta(minutes=20),
        metadata={"device_name": "Display 2", "app_name": "Editor"},
        samples=[
            {
                "captured_at": (START + timedelta(minutes=index)).isoformat(),
                "text": f"sample {index}",
                "content_fingerprint": f"fingerprint-{index}",
                "frame_id": 100 + index,
                "capture_trigger": "idle" if index == 8 else "",
                "inactive": index == 8,
            }
            for index in range(20)
        ],
        frame_candidates=[
            {
                "captured_at": (START + timedelta(minutes=10)).isoformat(),
                "frame_id": 500,
            }
        ],
        content_hash=None,
        curation_revision=None,
        media_data=None,
        curation="pending",
        media_content_type=None,
    )

    item = _device_item(row)
    anchors = build_evidence_anchors([item])

    assert item.locator.track_id == "Display 2"
    assert item.coverage.source_count == 21
    assert item.coverage.retained_count == 21
    assert item.coverage.agent_visible_count == 12
    assert len(item.metadata["temporal_samples"]) == 21
    assert item.metadata["agent_visible_samples"][0]["captured_at"] == START.isoformat()
    assert any(anchor.source_position == 500 for anchor in anchors)
    assert set(item.anchor_ids) == {anchor.anchor_id for anchor in anchors}


def test_manifest_resolves_exact_anchor_and_rejects_unsupported_interior_boundary():
    item = _item("screen-one", "Display 1")
    anchor = EvidenceAnchor(
        anchor_id="screen-one:start",
        evidence_id=item.evidence_id,
        locator=item.locator,
        support_type="frame",
        earliest_at=START,
        latest_at=START,
        source_position=42,
    )
    item.anchor_ids = [anchor.anchor_id]
    manifest = _manifest([item], anchors=[anchor])

    assert (
        manifest.resolve_boundary_anchor(
            evidence_id=item.evidence_id,
            anchor_id=anchor.anchor_id,
            boundary_at=START,
        )
        == anchor
    )
    with pytest.raises(ValueError, match="outside anchor"):
        manifest.resolve_boundary_anchor(
            evidence_id=item.evidence_id,
            anchor_id=anchor.anchor_id,
            boundary_at=START + timedelta(seconds=30),
        )


def test_manifest_rejects_anchor_for_unknown_evidence():
    locator = EvidenceLocator(
        capture_source_id="screenpipe-capture-node",
        modality="screen",
        track_id="Display 1",
    )
    with pytest.raises(ValidationError, match="unknown evidence"):
        _manifest(
            [],
            anchors=[
                EvidenceAnchor(
                    anchor_id="missing:start",
                    evidence_id="missing",
                    locator=locator,
                    support_type="source_edge",
                    earliest_at=START,
                    latest_at=START,
                    source_position="start",
                )
            ],
        )


def _manifest(
    items: list[TimelineEvidenceItem], *, anchors: list[EvidenceAnchor] | None = None
) -> TimelineEvidenceManifest:
    return TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 9, 3),
        timezone="UTC",
        started_at=START,
        ended_at=START + timedelta(hours=1),
        evidence_revision="revision",
        windows=[],
        evidence=items,
        anchors=anchors or [],
    )


@pytest.mark.parametrize(
    "state, expected",
    [
        ("no_speech", "uncertain"),
        ("unscored", "uncertain"),
        ("failed", "uncertain"),
        ("transcribed", "media_content"),
    ],
)
def test_output_capture_route_alone_is_not_media_content(state, expected):
    span = AudioEvidenceSpan.model_construct(
        id="capture",
        source_id="device-a",
        first_source_item_id="chunk",
        locator=EvidenceLocator(
            capture_source_id="device-a", modality="audio", track_id="output"
        ),
        started_at=START,
        ended_at=START + timedelta(hours=2),
        direction="output",
        state=state,
        source_range_hash="hash",
        covered_seconds=7200,
        missing_seconds=0,
    )
    item = _audio_item(span)
    assert item.role == expected
    assert item.metadata["state"] == state
    assert item.locator.capture_source_id == "device-a"
    assert item.ended_at - item.started_at == timedelta(hours=2)
