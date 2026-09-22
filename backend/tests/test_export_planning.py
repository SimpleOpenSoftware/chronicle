"""Tests for the shared export clip planner and the export-preview flow.

The planner (``utils/export_planning.py``) is the single source of clip
boundaries for both the export job and the preview endpoint, so these tests
pin the properties the preview → curate → export loop relies on: preview and
export agree, dropped clips disappear exactly, and privacy vs curation
carve-outs are accounted separately.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.controllers import data_audit_controller
from backend.models.conversation import Conversation
from backend.utils import export_planning
from backend.utils.export_planning import export_eligibility, plan_conversation_clips


def _conv(**overrides):
    base = dict(
        conversation_id="conv-1",
        title="Test conversation",
        client_id="user01-phone",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        user_id="user-1",
        deleted=False,
        audio_archived=False,
        audio_chunks_count=3,
        audio_total_duration=60.0,
        transcript_versions=[],
        active_transcript_version=None,
    )
    base.update(overrides)
    result = SimpleNamespace(**base)
    result.model_dump = lambda: dict(base)
    return result


def _chunk(start: float, end: float, scores, hop_ms: float = 100.0):
    return {
        "start_time": start,
        "end_time": end,
        "sample_rate": 16000,
        "vad": {"scores": scores, "frame_hop_ms": hop_ms},
    }


def _mock_chunks(monkeypatch, docs):
    claimed = []
    for document in docs:
        vad = document.get("vad")
        claimed.append(
            SimpleNamespace(
                chunk=SimpleNamespace(
                    sample_rate=document["sample_rate"],
                    vad=(SimpleNamespace(**vad) if vad is not None else None),
                ),
                conversation_start_seconds=document["start_time"],
                clip_start_seconds=0.0,
                duration_seconds=document["end_time"] - document["start_time"],
            )
        )

    async def resolve(_conversation_id):
        return claimed

    monkeypatch.setattr(export_planning, "resolve_conversation_audio", resolve)


# Two speech runs: 2.0–5.0s and 20.0–24.0s (frames at 100ms hop).
def _two_region_chunks():
    scores = [0.0] * 600
    for i in range(20, 50):
        scores[i] = 0.9
    for i in range(200, 240):
        scores[i] = 0.9
    return [_chunk(0.0, 60.0, scores)]


class TestPlanConversationClips:
    @pytest.mark.asyncio
    async def test_clips_mode_pads_and_keeps_separate_regions(self, monkeypatch):
        _mock_chunks(monkeypatch, _two_region_chunks())
        plan = await plan_conversation_clips(
            _conv(),
            "clips",
            pad_seconds=1.0,
            speech_threshold=0.5,
            merge_gap_seconds=3.0,
        )
        assert plan.skipped_reason is None
        assert [(c.start, c.end) for c in plan.clips] == [(1.0, 6.0), (19.0, 25.0)]
        assert plan.excluded_seconds == 0.0
        assert plan.dropped_seconds == 0.0
        assert plan.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_wide_merge_gap_joins_regions(self, monkeypatch):
        _mock_chunks(monkeypatch, _two_region_chunks())
        plan = await plan_conversation_clips(
            _conv(),
            "clips",
            pad_seconds=1.0,
            speech_threshold=0.5,
            merge_gap_seconds=30.0,
        )
        assert [(c.start, c.end) for c in plan.clips] == [(1.0, 25.0)]

    @pytest.mark.asyncio
    async def test_unanalyzed_audio_is_reported_not_analyzed(self, monkeypatch):
        chunk = _chunk(0.0, 60.0, [0.9] * 600)
        chunk["vad"] = None
        _mock_chunks(monkeypatch, [chunk])
        plan = await plan_conversation_clips(
            _conv(),
            "clips",
            pad_seconds=1.0,
            speech_threshold=0.5,
            merge_gap_seconds=3.0,
        )
        assert plan.skipped_reason == "not analyzed"
        assert plan.clips == []

    @pytest.mark.asyncio
    async def test_dropping_a_previewed_clip_removes_exactly_that_clip(
        self, monkeypatch
    ):
        """The curation contract: unticking a clip in the preview and passing
        its exact [start, end] as a dropped range removes that clip and only
        that clip from a recomputed plan."""
        _mock_chunks(monkeypatch, _two_region_chunks())
        preview = await plan_conversation_clips(
            _conv(),
            "clips",
            1.0,
            0.5,
            3.0,
        )
        dropped = preview.clips[0]
        plan = await plan_conversation_clips(
            _conv(),
            "clips",
            1.0,
            0.5,
            3.0,
            dropped_ranges=[[dropped.start, dropped.end]],
        )
        assert [(c.start, c.end) for c in plan.clips] == [(19.0, 25.0)]
        assert plan.dropped_seconds == 5.0
        assert plan.excluded_seconds == 0.0

    @pytest.mark.asyncio
    async def test_privacy_and_curation_carves_are_accounted_separately(
        self, monkeypatch
    ):
        _mock_chunks(monkeypatch, _two_region_chunks())
        plan = await plan_conversation_clips(
            _conv(),
            "clips",
            1.0,
            0.5,
            3.0,
            excluded_ranges=[[2.0, 4.0]],  # privacy: carve inside clip 1
            dropped_ranges=[[19.0, 25.0]],  # curation: drop clip 2 whole
        )
        assert plan.excluded_seconds == 2.0
        assert plan.dropped_seconds == 6.0
        # Clip 1 splits around the privacy cut; clip 2 is gone.
        assert [(c.start, c.end) for c in plan.clips] == [(1.0, 2.0), (4.0, 6.0)]

    @pytest.mark.asyncio
    async def test_full_mode_is_one_untouched_region(self, monkeypatch):
        _mock_chunks(monkeypatch, [_chunk(0.0, 42.5, [])])
        plan = await plan_conversation_clips(
            _conv(audio_total_duration=42.5),
            "full",
            1.0,
            0.5,
            3.0,
        )
        assert [(c.start, c.end) for c in plan.clips] == [(0.0, 42.5)]


class TestExportEligibility:
    def test_owner_with_audio_is_eligible(self):
        assert export_eligibility(_conv(), "user-1", False) is None

    def test_reasons(self):
        assert export_eligibility(None, "user-1", False) == "not found"
        assert (
            export_eligibility(_conv(user_id="other"), "user-1", False)
            == "access forbidden"
        )
        assert export_eligibility(_conv(user_id="other"), "user-1", True) is None
        assert export_eligibility(_conv(deleted=True), "user-1", False) == "deleted"
        assert (
            export_eligibility(_conv(audio_archived=True), "user-1", False)
            == "audio archived"
        )
        assert (
            export_eligibility(_conv(audio_chunks_count=0), "user-1", False)
            == "no audio"
        )


class _FakeConversationCls:
    """Stands in for the Beanie model in controller tests: the field's
    ``==`` returns the queried id so ``find_one`` can look it up."""

    docs: dict = {}

    class _Field:
        def __eq__(self, other):
            return other

    conversation_id = _Field()

    @classmethod
    async def find_one(cls, cid):
        return cls.docs.get(cid)


def _segment(start, end, text, speaker="speaker_0"):
    return Conversation.SpeakerSegment(start=start, end=end, text=text, speaker=speaker)


class TestPreviewExport:
    @pytest.fixture(autouse=True)
    def privacy_database(self, monkeypatch):
        from mongomock_motor import AsyncMongoMockClient

        from backend.services import privacy

        database = AsyncMongoMockClient().privacy_export_test
        monkeypatch.setattr(privacy, "database", lambda: database)

    @pytest.mark.asyncio
    async def test_preview_returns_clips_with_sliced_transcripts(self, monkeypatch):
        segments = [
            _segment(2.0, 4.0, "hello there"),
            _segment(21.0, 23.0, "second clip words"),
        ]
        version = SimpleNamespace(version_id="v1", segments=segments)
        version.model_dump = lambda: {"version_id": "v1", "segments": segments}
        conv = _conv(transcript_versions=[version], active_transcript_version="v1")
        _FakeConversationCls.docs = {"conv-1": conv}
        monkeypatch.setattr(data_audit_controller, "Conversation", _FakeConversationCls)
        _mock_chunks(monkeypatch, _two_region_chunks())
        user = SimpleNamespace(is_superuser=False, user_id="user-1")

        result = await data_audit_controller.preview_export(
            user,
            ["conv-1", "missing"],
            mode="clips",
        )

        assert result["totals"]["conversation_count"] == 2
        assert result["totals"]["exported_conversations"] == 1
        assert result["totals"]["clip_count"] == 2
        previewed, missing = result["conversations"]
        assert missing["skipped_reason"] == "not found"
        clips = previewed["clips"]
        assert [c["clip_id"] for c in clips] == ["conv-1_000", "conv-1_001"]
        assert clips[0]["text"] == "hello there"
        assert clips[1]["text"] == "second clip words"
        assert clips[0]["segment_count"] == 1
        assert previewed["clip_seconds"] == 11.0

    @pytest.mark.asyncio
    async def test_preview_reports_unanalyzed_instead_of_running_vad(self, monkeypatch):
        conv = _conv()
        _FakeConversationCls.docs = {"conv-1": conv}
        monkeypatch.setattr(data_audit_controller, "Conversation", _FakeConversationCls)
        chunk = _chunk(0.0, 60.0, [])
        chunk["vad"] = None
        _mock_chunks(monkeypatch, [chunk])
        user = SimpleNamespace(is_superuser=False, user_id="user-1")

        result = await data_audit_controller.preview_export(user, ["conv-1"])

        assert result["conversations"][0]["skipped_reason"] == "not analyzed"
        assert result["totals"]["exported_conversations"] == 0


class TestLatestExportsByConversation:
    def _write_export(self, exports_dir, export_id, created_at, conversations):
        d = exports_dir / export_id
        d.mkdir(parents=True)
        (d / "export.json").write_text(
            json.dumps(
                {
                    "export_id": export_id,
                    "created_at": created_at,
                    "created_by": "user-1",
                    "conversations": conversations,
                }
            )
        )

    def test_latest_export_wins_and_skipped_do_not_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data_audit_controller, "EXPORTS_DIR", tmp_path)
        self._write_export(
            tmp_path,
            "annotation_20260601_000000_aaaa",
            "2026-06-01T00:00:00+00:00",
            [
                {"conversation_id": "c1"},
                {"conversation_id": "c2", "skipped_reason": "no audio"},
            ],
        )
        self._write_export(
            tmp_path,
            "annotation_20260701_000000_bbbb",
            "2026-07-01T00:00:00+00:00",
            [{"conversation_id": "c1"}, {"conversation_id": "c3"}],
        )
        user = SimpleNamespace(is_superuser=False, user_id="user-1")

        latest = data_audit_controller._latest_exports_by_conversation(user)

        assert latest["c1"]["export_id"] == "annotation_20260701_000000_bbbb"
        assert latest["c3"]["export_id"] == "annotation_20260701_000000_bbbb"
        assert "c2" not in latest  # skipped conversations were never shipped

    def test_other_users_exports_are_invisible(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data_audit_controller, "EXPORTS_DIR", tmp_path)
        self._write_export(
            tmp_path,
            "annotation_20260601_000000_aaaa",
            "2026-06-01T00:00:00+00:00",
            [{"conversation_id": "c1"}],
        )
        stranger = SimpleNamespace(is_superuser=False, user_id="user-2")
        superuser = SimpleNamespace(is_superuser=True, user_id="admin")

        assert data_audit_controller._latest_exports_by_conversation(stranger) == {}
        assert "c1" in data_audit_controller._latest_exports_by_conversation(superuser)
