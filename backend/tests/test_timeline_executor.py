import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.services.inference_artifacts import ReusableInferenceRun
from backend.services.memory.agent.pi_agent import _PiRuntimeConfig
from backend.services.timeline import contracts as timeline_contracts
from backend.services.timeline.codex_executor import (
    CodexTimelineExecutor,
    _parse_usage,
    _workspace_fingerprint,
)
from backend.services.timeline.context import (
    TimelineContextEvent,
    TimelineContextSummary,
)
from backend.services.timeline.contracts import (
    AgentAssertion,
    AgentAttribute,
    AgentEpisode,
    EpisodeLineageProposal,
    EpisodeRevisionRef,
    EvidenceAnchor,
    EvidenceBundle,
    EvidenceLocator,
    InterpretationResult,
    InterpretedEpisode,
    Publish,
    RejectedHypothesis,
    RequestMoreContext,
    SeparatedEpisode,
    SeparationResult,
    StageContextRequest,
    StageInferenceProvenance,
    TimelineCoverageWindow,
    TimelineEvidenceItem,
    TimelineEvidenceManifest,
    UnassignedInterval,
    ValidatedTimelineProjection,
)
from backend.services.timeline.executor import (
    RangeReconcileExecutor,
    TimelineIncompleteSegmentation,
    build_executor,
    project_validated_stages,
    settings_dict,
    validate_agent_result,
    validate_interpretation_result,
    validate_separation_result,
)
from backend.services.timeline.pi_executor import (
    PiTimelineExecutor,
    TimelineWorkspaceError,
    _compact_stage_context,
    _encode_stage_text,
    _encode_validation_feedback,
    _local_day_instruction,
    _model_episode_revisions,
    _repair_context_json_scaffolding,
    _repair_quoted_object_delimiters,
    _TimelineWorkspaceTools,
)
from backend.services.timeline.pi_executor import (
    _workspace_fingerprint as _pi_workspace_fingerprint,
)


def _locator(modality: str = "screen") -> EvidenceLocator:
    return EvidenceLocator(
        capture_source_id="screenpipe-one",
        modality=modality,
        track_id="display-one" if modality == "screen" else None,
    )


def _stage_provenance(stage: str) -> StageInferenceProvenance:
    return StageInferenceProvenance(
        operation=f"test_timeline_{stage}",
        request_hash=f"{stage}-request",
        artifact_hash=f"{stage}-artifact",
    )


def _manifest() -> TimelineEvidenceManifest:
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    item = TimelineEvidenceItem(
        evidence_id="observation:one",
        kind="observation",
        locator=_locator(),
        started_at=start + timedelta(minutes=2),
        ended_at=start + timedelta(minutes=25),
        role="application_state",
        excerpt="Terminator playback",
    )
    return TimelineEvidenceManifest(
        user_id="user",
        local_date=date(2026, 8, 6),
        timezone="UTC",
        started_at=start,
        ended_at=end,
        evidence_revision="revision",
        windows=[
            TimelineCoverageWindow(
                window_id="window:one",
                started_at=start,
                ended_at=end,
                evidence_ids=[item.evidence_id],
            )
        ],
        evidence=[item],
    )


def _result(evidence_id: str = "observation:one") -> ValidatedTimelineProjection:
    manifest = _manifest()
    return ValidatedTimelineProjection(
        episodes=[
            AgentEpisode(
                kind="media_watched",
                title="Watched Terminator",
                summary="A long movie playback session.",
                started_at=manifest.started_at + timedelta(minutes=2),
                ended_at=manifest.started_at + timedelta(minutes=25),
                salience="routine",
                activity_mode="background",
                confidence=0.9,
                attributes=[AgentAttribute(key="media", value="Terminator")],
                evidence_ids=[evidence_id],
                assertions=[
                    AgentAssertion(
                        claim="Terminator was playing",
                        role="application_state",
                        confidence=0.9,
                        evidence_ids=[evidence_id],
                    )
                ],
            )
        ],
    )


def test_pi_prompt_names_cross_utc_midnight_local_day_bounds():
    manifest = _manifest()
    manifest.local_date = date(2026, 8, 12)
    manifest.timezone = "Asia/Kolkata"
    manifest.started_at = datetime(2026, 8, 11, 18, 30, tzinfo=timezone.utc)
    manifest.ended_at = datetime(2026, 8, 12, 18, 30, tzinfo=timezone.utc)

    instruction = _local_day_instruction(manifest)

    assert "2026-08-12 in Asia/Kolkata" in instruction
    assert "2026-08-11T18:30:00+00:00" in instruction
    assert "previous calendar date" in instruction


def test_glimmer_quoted_object_delimiter_repair_is_lexically_scoped():
    raw = (
        '{"events":[{"summary":"literal },\\"{ text"},'
        '"{"summary":"next"}],"unresolved_evidence_ids":[]}'
    )

    repaired, count = _repair_quoted_object_delimiters(raw)

    assert count == 1
    assert repaired == (
        '{"events":[{"summary":"literal },\\"{ text"},'
        '{"summary":"next"}],"unresolved_evidence_ids":[]}'
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{"events":[{"summary":"one"},"events":[{"summary":"two"}],'
            '"unresolved_evidence_ids":[]}',
            {
                "events": [{"summary": "one"}, {"summary": "two"}],
                "unresolved_evidence_ids": [],
            },
        ),
        (
            '{"events":[{"summary":"one"},"unresolved_evidence_ids":[],'
            '"events":[{"summary":"two"}]}}',
            {"events": [{"summary": "one"}, {"summary": "two"}]},
        ),
        (
            '{"events":[{"summary":"one"},"unresolved_evidence_ids":[]}',
            {"events": [{"summary": "one"}], "unresolved_evidence_ids": []},
        ),
        (
            '{"events":[{"summary":"one"}]',
            {"events": [{"summary": "one"}]},
        ),
    ],
)
def test_glimmer_context_scaffolding_repair_matches_retained_failures(raw, expected):
    repaired, count = _repair_context_json_scaffolding(raw)

    assert count >= 1
    assert json.loads(repaired) == expected


def test_context_scaffolding_repair_does_not_touch_quoted_evidence_text():
    raw = (
        '{"events":[{"summary":"literal },\\"events\\":[{ text"}],'
        '"unresolved_evidence_ids":[]}'
    )

    repaired, count = _repair_context_json_scaffolding(raw)

    assert count == 0
    assert repaired == raw


@pytest.mark.parametrize("configured", [True, False])
def test_build_executor_selects_pi_without_codex(monkeypatch, configured):
    monkeypatch.setattr(
        "backend.services.timeline.executor.settings_dict",
        lambda: {
            **({"executor": "pi"} if configured else {}),
            "pi": {"operation": "timeline_segmentation"},
        },
    )

    executor = build_executor()

    assert isinstance(executor, PiTimelineExecutor)
    assert executor.settings["operation"] == "timeline_segmentation"


def test_timeline_settings_accept_plain_mapping_from_config_loader(monkeypatch):
    monkeypatch.setattr(
        "backend.services.timeline.executor.load_config",
        lambda: {"timeline": {"executor": "pi"}},
    )

    assert settings_dict() == {"executor": "pi"}


def test_timeline_workspace_tools_read_json_without_markdown_normalization(tmp_path):
    windows = tmp_path / "windows"
    windows.mkdir()
    (windows / "0000.json").write_text('{"evidence": ["one"]}\n', encoding="utf-8")
    tools = _TimelineWorkspaceTools(tmp_path)

    result = tools.dispatch("read_note", {"path": "windows/0000.json"})

    assert result == '{"evidence": ["one"]}\n'


def test_timeline_workspace_tools_only_write_work_notes(tmp_path):
    tools = _TimelineWorkspaceTools(tmp_path)

    tools.dispatch(
        "write_note",
        {"path": "work/summary.txt", "content": "compact notes"},
    )

    assert (tmp_path / "work" / "summary.txt").is_file()
    with pytest.raises(TimelineWorkspaceError, match="write path"):
        tools.dispatch(
            "write_note",
            {"path": "timeline-result.json", "content": '{"episodes": []}'},
        )
    with pytest.raises(TimelineWorkspaceError, match="write path"):
        tools.dispatch(
            "write_note",
            {"path": "windows/0000.json", "content": "overwrite evidence"},
        )
    with pytest.raises(TimelineWorkspaceError, match="inside the workspace"):
        tools.dispatch("read_note", {"path": "../secret"})


@pytest.mark.asyncio
async def test_dense_context_uses_a_separate_local_agent_before_final_segmentation(
    tmp_path, monkeypatch
):
    manifest = _manifest()
    executor = PiTimelineExecutor({"condense_min_items": 1})
    calls = []

    async def fake_condense(block, *, config, manifest):
        calls.append(block["block_id"])
        item = block["evidence"][0]
        return (
            TimelineContextSummary(
                events=[
                    TimelineContextEvent(
                        started_at=item["started_at"],
                        ended_at=item["ended_at"],
                        summary="Local Glimmer condensed the screen activity.",
                        evidence_ids=item["evidence_ids"],
                        modalities=[item["kind"]],
                    )
                ]
            ),
            {"input_tokens": 50, "output_tokens": 10},
        )

    monkeypatch.setattr(executor, "_condense_context_block", fake_condense)

    stats, usage = await executor._prepare_context_workspace(
        tmp_path, manifest, config=SimpleNamespace()
    )

    payload = json.loads((tmp_path / "context" / "0000.json").read_text())
    assert calls == ["context-0000"]
    assert payload["mode"] == "local_agent_summary"
    assert payload["events"][0]["evidence_ids"] == ["observation:one"]
    assert stats == {
        "block_count": 1,
        "dense_block_count": 1,
        "source_evidence_count": 1,
    }
    assert usage == {"input_tokens": 50, "output_tokens": 10}


@pytest.mark.asyncio
async def test_context_workspace_can_be_rebuilt_for_reasoning_escalation(tmp_path):
    executor = PiTimelineExecutor({})

    first_stats, first_usage = await executor._prepare_context_workspace(
        tmp_path, _manifest(), config=SimpleNamespace()
    )
    second_stats, second_usage = await executor._prepare_context_workspace(
        tmp_path, _manifest(), config=SimpleNamespace()
    )

    assert second_stats == first_stats
    assert second_usage == first_usage
    assert json.loads((tmp_path / "context" / "index.json").read_text())["blocks"]


@pytest.mark.asyncio
async def test_context_condenser_passes_bounded_block_directly_without_tool_loop(
    monkeypatch,
):
    """A condenser must not spend repeated Pi rounds reading one already-bounded blob."""

    manifest = _manifest()
    block = {
        "block_id": "context-0000",
        "started_at": manifest.started_at.isoformat(),
        "ended_at": manifest.ended_at.isoformat(),
        "evidence": [
            {
                "evidence_ids": ["observation:one"],
                "kind": "observation",
                "started_at": manifest.started_at.isoformat(),
                "ended_at": manifest.ended_at.isoformat(),
                "excerpt": "Terminator playback",
            }
        ],
    }
    expected = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                summary="Terminator was playing.",
                evidence_ids=["observation:one"],
                modalities=["observation"],
            )
        ]
    )
    calls = []

    monkeypatch.setattr(
        "backend.services.timeline.pi_executor.load_reusable_result",
        lambda operation, request: None,
    )
    monkeypatch.setattr(
        "backend.services.timeline.pi_executor.persist_inference_run",
        lambda **kwargs: ("request", "artifact"),
    )

    async def fake_invoke(root, **kwargs):
        calls.append(kwargs)
        return (
            SimpleNamespace(
                truncated=False,
                fatal_errors=[],
                errors=[],
                summary=json.dumps(
                    {
                        **expected.model_dump(mode="json"),
                        "events": [
                            {
                                **event,
                                "coverage": {
                                    "source_count": 0,
                                    "retained_count": 0,
                                    "agent_visible_count": 0,
                                    "cited_count": 7,
                                },
                            }
                            for event in expected.model_dump(mode="json")["events"]
                        ],
                    }
                ),
                usage={"input_tokens": 100, "output_tokens": 20},
                rounds=1,
                tool_calls=0,
            ),
            SimpleNamespace(call_count=0),
        )

    monkeypatch.setattr("backend.services.timeline.pi_executor._invoke_pi", fake_invoke)
    config = _PiRuntimeConfig(
        binary="pi",
        model="muse-glimmer",
        provider="chronicle-llamacpp",
        base_url="http://llama.cpp/v1",
        api_key="no-key",
        thinking="high",
        max_tokens=12000,
        context_window=131072,
        timeout_seconds=900,
        reasoning=True,
        reasoning_allowed=True,
        temperature=1.0,
        system_prompt_prefix="Reasoning strength: high",
    )

    summary, usage = await PiTimelineExecutor({})._condense_context_block(
        block, config=config, manifest=manifest
    )

    assert summary == expected
    assert usage == {"input_tokens": 100, "output_tokens": 20}
    assert calls[0]["schemas"] == ()
    assert calls[0]["max_tool_rounds"] == 1
    assert calls[0]["config"].max_tokens == 12000
    assert calls[0]["config"].thinking == "low"
    assert calls[0]["config"].system_prompt_prefix == "Reasoning strength: low"
    assert "representative IDs" in calls[0]["system_prompt"]
    assert '"observation:one"' in calls[0]["prompt"]


@pytest.mark.asyncio
async def test_context_condenser_retries_invalid_json_and_archives_bad_output(
    monkeypatch,
):
    manifest = _manifest()
    block = {
        "block_id": "context-0000",
        "started_at": manifest.started_at.isoformat(),
        "ended_at": manifest.ended_at.isoformat(),
        "evidence": [
            {
                "evidence_ids": ["observation:one"],
                "kind": "observation",
                "started_at": manifest.started_at.isoformat(),
                "ended_at": manifest.ended_at.isoformat(),
                "excerpt": "Terminator playback",
            }
        ],
    }
    expected = TimelineContextSummary(
        events=[
            TimelineContextEvent(
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                summary="Terminator was playing.",
                evidence_ids=["observation:one"],
                modalities=["observation"],
            )
        ]
    )
    calls = []
    artifacts = []
    responses = ['{"events":[}', expected.model_dump_json()]

    monkeypatch.setattr(
        "backend.services.timeline.pi_executor.load_reusable_result",
        lambda operation, request: None,
    )
    monkeypatch.setattr(
        "backend.services.timeline.pi_executor.persist_inference_run",
        lambda **kwargs: artifacts.append(kwargs) or ("request", "artifact"),
    )

    async def fake_invoke(root, **kwargs):
        calls.append(kwargs)
        return (
            SimpleNamespace(
                truncated=False,
                fatal_errors=[],
                errors=[],
                summary=responses[len(calls) - 1],
                usage={"input_tokens": 10, "output_tokens": 20},
                rounds=1,
                tool_calls=0,
            ),
            SimpleNamespace(call_count=0),
        )

    monkeypatch.setattr("backend.services.timeline.pi_executor._invoke_pi", fake_invoke)
    config = _PiRuntimeConfig(
        binary="pi",
        model="muse-glimmer",
        provider="chronicle-llamacpp",
        base_url="http://llama.cpp/v1",
        api_key="no-key",
        thinking="high",
        max_tokens=12000,
        context_window=131072,
        timeout_seconds=900,
        reasoning=True,
        reasoning_allowed=True,
        temperature=1.0,
        system_prompt_prefix="Reasoning strength: high",
    )

    summary, usage = await PiTimelineExecutor({})._condense_context_block(
        block, config=config, manifest=manifest
    )

    assert summary == expected
    assert usage == {"input_tokens": 20, "output_tokens": 40}
    assert len(calls) == 2
    assert "previous response was invalid json" in calls[1]["prompt"].lower()
    assert artifacts[0]["reusable"] is False
    assert artifacts[0]["stdout"] == '{"events":[}'
    assert artifacts[-1]["reusable"] is True


def test_workspace_fingerprint_tracks_all_combined_workspace_files(tmp_path):
    (tmp_path / "evidence.json").write_text("first", encoding="utf-8")
    (tmp_path / "timeline-result.json").write_text("generated", encoding="utf-8")

    first = _workspace_fingerprint(tmp_path)
    (tmp_path / "evidence.json").write_text("second", encoding="utf-8")
    second = _workspace_fingerprint(tmp_path)

    assert [entry["path"] for entry in first] == [
        "evidence.json",
        "timeline-result.json",
    ]
    assert first != second


def test_pi_workspace_fingerprint_ignores_generated_context(tmp_path):
    (tmp_path / "evidence.json").write_text("source", encoding="utf-8")
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "index.json").write_text("generated", encoding="utf-8")

    assert [entry["path"] for entry in _pi_workspace_fingerprint(tmp_path)] == [
        "evidence.json"
    ]


def test_valid_overlapping_episode_result_is_accepted():
    validate_agent_result(_result(), _manifest())


def test_sub_millisecond_episode_remains_positive_after_bson_round_trip():
    manifest = _manifest()
    point = manifest.evidence[0]
    point.started_at = point.started_at.replace(microsecond=792974)
    point.ended_at = None
    result = _result()
    result.episodes[0].started_at = point.started_at
    result.episodes[0].ended_at = point.started_at + timedelta(microseconds=1)

    validate_agent_result(result, manifest)

    episode = result.episodes[0]
    assert episode.started_at.microsecond == 792000
    assert episode.ended_at.microsecond == 793000
    assert episode.ended_at > episode.started_at


def test_episode_can_bridge_a_large_interval_when_boundary_evidence_supports_it():
    manifest = _manifest()
    manifest.ended_at = manifest.started_at + timedelta(hours=12)
    first = manifest.evidence[0]
    first.ended_at = first.started_at + timedelta(minutes=2)
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        locator=_locator(),
        started_at=manifest.started_at + timedelta(hours=11),
        ended_at=manifest.started_at + timedelta(hours=11, minutes=1),
        role="application_state",
        excerpt="Unrelated activity much later",
    )
    manifest.evidence.append(later)
    result = _result()
    episode = result.episodes[0]
    episode.started_at = first.started_at
    episode.ended_at = later.ended_at
    episode.evidence_ids.append(later.evidence_id)

    validate_agent_result(result, manifest)

    assert len(result.episodes) == 1


def test_gap_salvage_flag_does_not_turn_elapsed_time_into_semantics():
    manifest = _manifest()
    manifest.ended_at = manifest.started_at + timedelta(hours=12)
    first = manifest.evidence[0]
    first.ended_at = first.started_at + timedelta(minutes=2)
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        locator=_locator(),
        started_at=manifest.started_at + timedelta(hours=11),
        ended_at=manifest.started_at + timedelta(hours=11, minutes=1),
        role="application_state",
        excerpt="Unrelated activity much later",
    )
    manifest.evidence.append(later)
    result = _result()
    bridged = result.episodes[0]
    bridged.started_at = first.started_at
    bridged.ended_at = later.ended_at
    bridged.evidence_ids.append(later.evidence_id)
    valid = _result(later.evidence_id).episodes[0]
    valid.started_at = later.started_at
    valid.ended_at = later.ended_at
    valid.title = "Later valid activity"
    result.episodes.append(valid)

    validate_agent_result(
        result,
        manifest,
        salvage_gap_bridging_episodes=True,
    )

    assert [episode.title for episode in result.episodes] == [
        "Watched Terminator",
        "Later valid activity",
    ]


def test_uncited_intermediate_evidence_can_support_one_continuous_episode():
    manifest = _manifest()
    start = manifest.started_at + timedelta(minutes=2)
    manifest.evidence = [
        TimelineEvidenceItem(
            evidence_id=f"observation:{index}",
            kind="observation",
            locator=_locator(),
            started_at=start + timedelta(minutes=4 * index),
            ended_at=start + timedelta(minutes=4 * index + 5),
            role="application_state",
            excerpt="One continuous application session",
        )
        for index in range(4)
    ]
    result = _result("observation:0")
    result.episodes[0].started_at = manifest.evidence[0].started_at
    result.episodes[0].ended_at = manifest.evidence[-1].ended_at
    result.episodes[0].evidence_ids.append("observation:3")

    validate_agent_result(result, manifest)


def test_a_day_whose_only_episode_is_malformed_fails():
    """Dropping bad episodes must not quietly turn a broken run into an empty day."""

    with pytest.raises(TimelineIncompleteSegmentation, match="unknown evidence"):
        validate_agent_result(_result("observation:invented"), _manifest())


def test_unknown_citation_is_removed_when_valid_evidence_remains():
    """One hallucinated citation must not discard an otherwise grounded episode."""

    result = _result()
    result.episodes[0].evidence_ids.append("observation:invented")
    result.episodes[0].assertions[0].evidence_ids.append("observation:invented")

    validate_agent_result(result, _manifest())

    episode = result.episodes[0]
    assert episode.evidence_ids == ["observation:one"]
    assert episode.assertions[0].evidence_ids == ["observation:one"]


def test_unique_evidence_suffix_is_restored_when_model_drops_kind_prefix():
    result = _result("one")

    validate_agent_result(result, _manifest())

    episode = result.episodes[0]
    assert episode.evidence_ids == ["observation:one"]
    assert episode.assertions[0].evidence_ids == ["observation:one"]


def test_ambiguous_evidence_suffix_is_not_guessed():
    manifest = _manifest()
    original = manifest.evidence[0]
    manifest.evidence.append(
        TimelineEvidenceItem(
            evidence_id="transcript:one",
            kind="transcript",
            locator=_locator("transcript"),
            started_at=original.started_at,
            ended_at=original.ended_at,
            role="uncertain",
            excerpt="Same suffix from a different evidence kind",
        )
    )

    with pytest.raises(TimelineIncompleteSegmentation, match="unknown evidence"):
        validate_agent_result(_result("one"), manifest)


def test_saying_nothing_about_a_day_with_evidence_is_a_failure_not_an_answer():
    """Regression: this was tolerated, and every empty run silently blanked a day.

    Materializing the gap as "unassigned" made a model failure look like a successful
    analysis, so the day published zero episodes and superseded a good generation.
    """

    result = ValidatedTimelineProjection(episodes=[], unassigned_intervals=[])

    with pytest.raises(TimelineIncompleteSegmentation):
        validate_agent_result(result, _manifest())


def test_evidence_the_agent_partially_explained_is_still_materialized():
    """The materialize behaviour is right once the agent has said *something*."""

    manifest = _manifest()
    result = _result()
    # Cited evidence spans 2-25min; the episode only covers 2-10min.
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=10)

    validate_agent_result(result, manifest)

    assert result.unassigned_intervals
    assert result.unassigned_intervals[-1].ended_at == manifest.started_at + timedelta(
        minutes=25
    )


def test_unavailable_representative_image_is_dropped_without_losing_episode():
    result = _result()
    result.episodes[0].representative_evidence_id = "observation:one"
    validate_agent_result(result, _manifest())
    assert result.episodes[0].representative_evidence_id is None


def test_non_overlapping_citation_is_pruned_when_episode_remains_grounded():
    manifest = _manifest()
    outside = TimelineEvidenceItem(
        evidence_id="observation:outside",
        kind="observation",
        locator=_locator(),
        started_at=manifest.started_at + timedelta(minutes=27),
        ended_at=manifest.started_at + timedelta(minutes=28),
        role="application_state",
        excerpt="Later application state",
    )
    manifest.evidence.append(outside)
    result = _result()
    result.episodes[0].evidence_ids.append(outside.evidence_id)
    result.unassigned_intervals.append(
        UnassignedInterval(
            started_at=outside.started_at,
            ended_at=outside.ended_at,
            reason="Separate later evidence",
        )
    )
    validate_agent_result(result, manifest)
    assert result.episodes[0].evidence_ids == ["observation:one"]


def test_episode_without_overlapping_citation_is_rejected():
    manifest = _manifest()
    result = _result()
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=26)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=27)
    with pytest.raises(
        TimelineIncompleteSegmentation, match="no temporally overlapping evidence"
    ):
        validate_agent_result(result, manifest)


def test_exact_boundary_observations_ground_an_episode():
    """Zero-duration screen observations may define both closed boundaries."""

    manifest = _manifest()
    start = manifest.started_at + timedelta(minutes=10)
    end = start + timedelta(seconds=9)
    manifest.evidence = [
        TimelineEvidenceItem(
            evidence_id="observation:start",
            kind="observation",
            locator=_locator(),
            started_at=start,
            ended_at=start,
            role="application_state",
            excerpt="Search view opened",
        ),
        TimelineEvidenceItem(
            evidence_id="observation:end",
            kind="observation",
            locator=_locator(),
            started_at=end,
            ended_at=end + timedelta(minutes=5),
            role="application_state",
            excerpt="Search view closed",
        ),
    ]
    manifest.windows[0].evidence_ids = [item.evidence_id for item in manifest.evidence]
    result = _result()
    episode = result.episodes[0]
    episode.started_at = start
    episode.ended_at = end
    episode.evidence_ids = [item.evidence_id for item in manifest.evidence]
    episode.assertions[0].evidence_ids = list(episode.evidence_ids)

    validate_agent_result(result, manifest)

    assert result.episodes[0].evidence_ids == [
        "observation:start",
        "observation:end",
    ]


def test_a_malformed_episode_is_dropped_without_losing_the_good_ones():
    """The whole day used to be discarded over one bad episode.

    Under --output-schema the agent's planning narration is schema-valid, so a stray
    entry can appear beside real episodes; rejecting the run threw all of them away.
    """

    manifest = _manifest()
    result = _result()
    good = result.episodes[0]
    narration = good.model_copy(
        update={
            "kind": "task",
            "title": "Inspect Chronicle day inputs",
            # Cites real evidence but sits outside it, as the leaked planning entries did.
            "started_at": manifest.started_at + timedelta(minutes=26),
            "ended_at": manifest.started_at + timedelta(minutes=27),
        }
    )
    result.episodes = [narration, good]

    validate_agent_result(result, manifest)

    assert [episode.title for episode in result.episodes] == ["Watched Terminator"]


def test_dropping_a_malformed_episode_remaps_parent_indices():
    manifest = _manifest()
    result = _result()
    good = result.episodes[0]
    narration = good.model_copy(
        update={
            "title": "planning noise",
            "started_at": manifest.started_at + timedelta(minutes=26),
            "ended_at": manifest.started_at + timedelta(minutes=27),
        }
    )
    child = good.model_copy(update={"title": "child", "parent_episode_index": 1})
    result.episodes = [narration, good, child]

    validate_agent_result(result, manifest)

    titles = [episode.title for episode in result.episodes]
    assert titles == ["Watched Terminator", "child"]
    # `good` moved from index 1 to 0, so the child's parent pointer must follow it.
    assert result.episodes[1].parent_episode_index == 0


def test_unsupported_episode_end_is_rejected_instead_of_silently_shortened():
    manifest = _manifest()
    manifest.evidence[0].ended_at = manifest.started_at + timedelta(minutes=5)
    result = _result()

    with pytest.raises(
        TimelineIncompleteSegmentation,
        match="episode 0 end .*latest cited boundary",
    ):
        validate_agent_result(result, manifest)

    assert result.episodes[0].ended_at == manifest.started_at + timedelta(minutes=25)


def test_unsupported_episode_start_is_rejected_instead_of_silently_shifted():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    with pytest.raises(
        TimelineIncompleteSegmentation,
        match="episode 0 start .*earliest cited boundary",
    ):
        validate_agent_result(result, manifest)

    assert result.episodes[0].started_at == manifest.started_at


def test_all_unsupported_boundaries_are_reported_in_one_validation_error():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    manifest.evidence[0].ended_at = manifest.started_at + timedelta(minutes=5)
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    with pytest.raises(TimelineIncompleteSegmentation) as raised:
        validate_agent_result(result, manifest)

    diagnostic = str(raised.value)
    assert "episode 0 start" in diagnostic
    assert "earliest cited boundary" in diagnostic
    assert "episode 0 end" in diagnostic
    assert "latest cited boundary" in diagnostic
    assert raised.value.episode_index == 0


def test_boundary_citation_is_not_discarded_for_sub_millisecond_rounding():
    manifest = _manifest()
    boundary = manifest.started_at + timedelta(minutes=25, microseconds=734)
    manifest.evidence[0].started_at = boundary
    manifest.evidence[0].ended_at = boundary
    result = _result()
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=24)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=25)

    validate_agent_result(result, manifest)

    assert len(result.episodes) == 1


def test_point_only_citation_cannot_be_repaired_into_a_positive_episode():
    manifest = _manifest()
    manifest.evidence[0].started_at = manifest.started_at + timedelta(minutes=3)
    manifest.evidence[0].ended_at = manifest.evidence[0].started_at
    result = _result()
    result.episodes[0].started_at = manifest.started_at

    with pytest.raises(TimelineIncompleteSegmentation, match="earliest cited boundary"):
        validate_agent_result(result, manifest)


def test_naive_agent_timestamps_are_interpreted_as_utc():
    manifest = _manifest()
    result = _result()
    result.episodes[0].started_at = result.episodes[0].started_at.replace(tzinfo=None)
    result.episodes[0].ended_at = result.episodes[0].ended_at.replace(tzinfo=None)

    validate_agent_result(result, manifest)

    assert result.episodes[0].started_at.tzinfo == timezone.utc
    assert result.episodes[0].ended_at.tzinfo == timezone.utc


def test_unexplained_evidence_is_materialized_as_an_unassigned_interval():
    manifest = _manifest()
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        locator=_locator(),
        started_at=manifest.started_at + timedelta(minutes=27),
        ended_at=manifest.started_at + timedelta(minutes=29),
        role="application_state",
    )
    manifest.evidence.append(later)

    result = _result()

    validate_agent_result(result, manifest)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].started_at == later.started_at
    assert result.unassigned_intervals[0].ended_at == later.ended_at
    assert result.unassigned_intervals[0].reason == (
        "Evidence was not assigned by semantic analysis"
    )


def test_unassigned_intervals_outside_manifest_are_discarded():
    manifest = _manifest()
    result = _result()
    result.unassigned_intervals.append(
        UnassignedInterval(
            started_at=manifest.started_at - timedelta(minutes=1),
            ended_at=manifest.started_at,
            reason="Invalid outer interval",
        )
    )

    validate_agent_result(result, manifest)

    assert result.unassigned_intervals == []


def test_pinned_intervals_account_for_their_evidence():
    """A day fully covered by a confirmed episode needs no drafted episode."""

    manifest = _manifest()
    result = ValidatedTimelineProjection(episodes=[])
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert result.episodes == []
    assert result.unassigned_intervals == []


def test_evidence_outside_a_pinned_interval_is_still_unaccounted():
    manifest = _manifest()
    result = ValidatedTimelineProjection(episodes=[])
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=10),
        )
    ]

    validate_agent_result(result, manifest, pinned)

    assert len(result.unassigned_intervals) == 1
    assert result.unassigned_intervals[0].ended_at == manifest.started_at + timedelta(
        minutes=25
    )


def test_drafted_episode_may_overlap_a_pinned_interval():
    manifest = _manifest()
    result = _result()
    pinned = [(result.episodes[0].started_at, result.episodes[0].ended_at)]

    validate_agent_result(result, manifest, pinned)

    assert len(result.episodes) == 1


def test_a_draft_inside_a_pinned_interval_is_not_clamped_before_deduplication():
    """An unsupported draft is retried, even if clamping would make it a duplicate."""

    manifest = _manifest()
    result = _result()
    # Cited evidence ends at minute 25. The proposed minute-29 boundary must not be
    # silently rewritten to make this draft fit inside the pinned interval.
    result.episodes[0].started_at = manifest.started_at + timedelta(minutes=24)
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=29)
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    with pytest.raises(TimelineIncompleteSegmentation, match="latest cited boundary"):
        validate_agent_result(result, manifest, pinned)


def test_pinned_time_does_not_remove_overlapping_parent_or_child():
    manifest = _manifest()
    result = _result()
    duplicate = result.episodes[0].model_copy(
        update={
            "started_at": manifest.started_at + timedelta(minutes=2),
            "ended_at": manifest.started_at + timedelta(minutes=10),
        }
    )
    child = result.episodes[0].model_copy(
        update={
            "title": "Later event",
            "started_at": manifest.started_at + timedelta(minutes=12),
            "ended_at": manifest.started_at + timedelta(minutes=25),
            "parent_episode_index": 0,
        }
    )
    # A pin owns fields, not the interval, so both drafts remain structurally valid.
    result.episodes = [duplicate, child]
    pinned = [(duplicate.started_at, duplicate.ended_at)]

    validate_agent_result(result, manifest, pinned)

    assert [episode.title for episode in result.episodes] == [
        "Watched Terminator",
        "Later event",
    ]
    assert result.episodes[1].parent_episode_index == 0


def test_codex_usage_is_summed_across_turns():
    """An agentic run emits one turn.completed per turn; cost is their sum."""

    stdout = b"""{"type":"thread.started","thread_id":"t"}
{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10,"reasoning_output_tokens":2}}
{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}
{"type":"turn.completed","usage":{"input_tokens":250,"cached_input_tokens":200,"output_tokens":30,"reasoning_output_tokens":5}}
"""

    assert _parse_usage(stdout) == {
        "input_tokens": 350,
        "cached_input_tokens": 240,
        "output_tokens": 40,
        "reasoning_output_tokens": 7,
        "turns": 2,
    }


def test_missing_or_malformed_usage_events_are_not_an_error():
    """Usage is accounting; losing it must never fail a completed analysis."""

    assert _parse_usage(b"") == {}
    assert _parse_usage(b"not json\n{broken\n") == {}
    assert _parse_usage(b'{"type":"turn.completed"}\n') == {"turns": 1}


def test_a_day_fully_covered_by_confirmed_episodes_may_add_nothing():
    """Pinned coverage is a real account of the day, so silence is legitimate."""

    manifest = _manifest()
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=25),
        )
    ]

    validate_agent_result(ValidatedTimelineProjection(episodes=[]), manifest, pinned)


def test_partially_pinned_coverage_still_rejects_unexplained_evidence():
    manifest = _manifest()
    pinned = [
        (
            manifest.started_at + timedelta(minutes=2),
            manifest.started_at + timedelta(minutes=6),
        )
    ]
    result = ValidatedTimelineProjection(episodes=[])

    validate_agent_result(result, manifest, pinned)

    # Not silently dropped: the unexplained tail is materialized as unknown time.
    assert result.unassigned_intervals


def test_unassigned_cause_distinguishes_blackout_from_unexplained_capture():
    """The agent's prose is not trusted for *why* an interval is unassigned.

    A run was observed labelling a multi-hour recording blackout "no evidence supports
    a coherent episode", which reads as a segmentation failure rather than a gap in
    capture. The cause is derived from the manifest instead.
    """

    manifest = _manifest()
    blackout = TimelineEvidenceItem(
        evidence_id="capture_gap:one",
        kind="capture_gap",
        locator=_locator("context"),
        started_at=manifest.started_at + timedelta(minutes=26),
        ended_at=manifest.ended_at,
        role="application_state",
        excerpt="no capture",
    )
    manifest.evidence.append(blackout)
    manifest.windows[0].evidence_ids.append(blackout.evidence_id)

    result = _result()
    # Leave the tail of the observation to the unassigned interval below.
    result.episodes[0].ended_at = manifest.started_at + timedelta(minutes=15)
    result.unassigned_intervals = [
        UnassignedInterval(
            started_at=manifest.started_at + timedelta(minutes=15),
            ended_at=manifest.started_at + timedelta(minutes=25),
            reason="No evidence supports a coherent episode in this gap.",
        ),
        UnassignedInterval(
            started_at=manifest.started_at + timedelta(minutes=26),
            ended_at=manifest.ended_at,
            reason="No evidence supports a coherent episode in this gap.",
        ),
    ]

    validate_agent_result(result, manifest)

    causes = {
        interval.started_at: interval.cause for interval in result.unassigned_intervals
    }
    # Overlaps the observation: captured, just not explained.
    assert causes[manifest.started_at + timedelta(minutes=15)] == "unexplained"
    # Overlaps only a capture_gap: nothing was recorded to explain.
    assert causes[manifest.started_at + timedelta(minutes=26)] == "no_capture"


def _staged_bundle(*, pinned=None, existing=None) -> EvidenceBundle:
    manifest = _manifest()
    item = manifest.evidence[0]
    start_anchor = EvidenceAnchor(
        anchor_id="anchor:start",
        evidence_id=item.evidence_id,
        locator=item.locator,
        support_type="source_edge",
        earliest_at=item.started_at,
        latest_at=item.started_at,
    )
    end_anchor = EvidenceAnchor(
        anchor_id="anchor:end",
        evidence_id=item.evidence_id,
        locator=item.locator,
        support_type="source_edge",
        earliest_at=item.ended_at,
        latest_at=item.ended_at,
    )
    item.anchor_ids = [start_anchor.anchor_id, end_anchor.anchor_id]
    manifest.anchors = [start_anchor, end_anchor]
    return EvidenceBundle(
        manifest=manifest,
        existing_episodes=existing or [],
        pinned_episodes=pinned or [],
        evidence_revision=7,
    )


def _separated(*, hypothesis_id="hypothesis:one", lineage=None) -> SeparatedEpisode:
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    return SeparatedEpisode(
        hypothesis_id=hypothesis_id,
        started_at=item.started_at,
        ended_at=item.ended_at,
        evidence_ids=[item.evidence_id],
        start_anchor_ids=["anchor:start"],
        end_anchor_ids=["anchor:end"],
        lineage=lineage or EpisodeLineageProposal(action="new"),
        confidence=0.9,
    )


def _interpreted(hypothesis_id="hypothesis:one", *, title="Watched Terminator"):
    return InterpretedEpisode(
        hypothesis_id=hypothesis_id,
        kind="media_watched",
        title=title,
        summary="A long movie playback session.",
        conversational=False,
        salience="routine",
        activity_mode="background",
        confidence=0.9,
        entities=["Terminator"],
        attributes=[AgentAttribute(key="media", value="Terminator")],
        assertions=[
            AgentAssertion(
                claim="Terminator was playing",
                role="application_state",
                confidence=0.9,
                evidence_ids=["observation:one"],
            )
        ],
        related_conversation_ids=[],
        parent_episode_index=None,
        representative_evidence_id=None,
    )


def test_separation_normalizes_redundant_anchors_and_preserves_rejected_evidence():
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    redundant = EvidenceAnchor(
        anchor_id="anchor:redundant-start",
        evidence_id=item.evidence_id,
        locator=item.locator,
        support_type="sample",
        earliest_at=item.started_at + timedelta(seconds=30),
        latest_at=item.started_at + timedelta(seconds=30),
    )
    outside = TimelineEvidenceItem(
        evidence_id="observation:outside",
        kind="observation",
        locator=item.locator,
        started_at=item.ended_at + timedelta(minutes=1),
        ended_at=item.ended_at + timedelta(minutes=2),
        role="application_state",
        excerpt="Later activity",
    )
    bundle.manifest.anchors.append(redundant)
    bundle.manifest.evidence.append(outside)
    hypothesis = _separated().model_copy(
        update={
            "start_anchor_ids": ["anchor:start", redundant.anchor_id],
            "evidence_ids": [item.evidence_id, outside.evidence_id],
        }
    )
    result = SeparationResult(hypotheses=[hypothesis])

    validate_separation_result(result, bundle)

    assert hypothesis.start_anchor_ids == ["anchor:start"]
    assert hypothesis.evidence_ids == [item.evidence_id]
    assert result.unassigned_evidence_ids == [outside.evidence_id]


def test_separation_snaps_nearby_boundary_and_adds_anchor_evidence():
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    boundary_at = item.started_at + timedelta(seconds=30)
    boundary_item = TimelineEvidenceItem(
        evidence_id="observation:boundary",
        kind="observation",
        locator=item.locator,
        started_at=boundary_at,
        ended_at=boundary_at,
        role="application_state",
        excerpt="Boundary transition",
    )
    boundary_anchor = EvidenceAnchor(
        anchor_id="anchor:nearby-start",
        evidence_id=boundary_item.evidence_id,
        locator=boundary_item.locator,
        support_type="transition",
        earliest_at=boundary_at,
        latest_at=boundary_at,
    )
    bundle.manifest.evidence.append(boundary_item)
    bundle.manifest.anchors.append(boundary_anchor)
    hypothesis = _separated().model_copy(
        update={"start_anchor_ids": [boundary_anchor.anchor_id]}
    )
    result = SeparationResult(hypotheses=[hypothesis])

    validate_separation_result(result, bundle)

    assert hypothesis.started_at == boundary_at
    assert hypothesis.evidence_ids == [item.evidence_id, boundary_item.evidence_id]
    assert result.unassigned_evidence_ids == []


@pytest.mark.asyncio
async def test_range_executor_enforces_separation_barrier_before_interpretation():
    bundle = _staged_bundle()
    invalid = _separated().model_copy(update={"start_anchor_ids": ["anchor:missing"]})

    class Provider:
        async def separate(self, *args, **kwargs):
            return SeparationResult(hypotheses=[invalid])

        async def interpret(self, *args, **kwargs):
            pytest.fail("interpretation must not run before structure validates")

    with pytest.raises(ValueError, match="unknown anchor"):
        await RangeReconcileExecutor(Provider()).reconcile(bundle)


@pytest.mark.asyncio
async def test_invalid_pi_stage_artifact_cannot_poison_the_next_range_retry(
    tmp_path, monkeypatch
):
    from pi_task_helpers import install_pi

    from backend.services.memory.agent.vault_tools import VaultToolError
    from backend.services.timeline import accepted_context, pi_executor, pi_tasks

    bundle = _staged_bundle()
    invalid = SeparationResult(
        hypotheses=[
            _separated().model_copy(update={"start_anchor_ids": ["anchor:missing"]})
        ]
    )
    valid = SeparationResult(hypotheses=[_separated()])
    interpreted = InterpretationResult(accepted=[_interpreted()])

    def repair(tools):
        with pytest.raises(VaultToolError, match="unknown anchor"):
            tools.dispatch("finish_task", {"result": invalid.model_dump(mode="json")})
        tools.dispatch("finish_task", {"result": valid.model_dump(mode="json")})

    calls = install_pi(
        monkeypatch, tmp_path, [repair, interpreted.model_dump(mode="json")]
    )
    monkeypatch.setattr(pi_executor, "_resolve_pi_config", pi_tasks._resolve_pi_config)
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: {})
    executor = RangeReconcileExecutor(PiTimelineExecutor({}))
    action = await executor.reconcile(bundle)
    assert len(calls) == 2
    assert calls[0]["tool_handler"].trace[0]["error"]
    assert action.separation.model_dump() == valid.model_dump()
    assert action.interpretation.model_dump() == interpreted.model_dump()
    await executor.reconcile(bundle)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_fence", [True, False])
async def test_range_executor_returns_typed_context_before_interpretation(valid_fence):
    bundle = _staged_bundle()
    request = StageContextRequest(
        context_request_id="canonicalized-by-reconciliation",
        hypothesis_id=None,
        stage="separation",
        locator=EvidenceLocator(
            capture_source_id="screenpipe-one",
            modality="screen",
            track_id="display-one",
        ),
        started_at=bundle.manifest.started_at,
        ended_at=bundle.manifest.ended_at,
        base_manifest_hash=(
            bundle.manifest.evidence_revision if valid_fence else "invented"
        ),
        leased_evidence_revision=bundle.evidence_revision,
        target_resolution="one_frame_per_10_seconds",
        max_items=12,
        reason="boundary needs denser screen evidence",
    )

    class Provider:
        async def separate(self, *args, **kwargs):
            return SeparationResult(context_requests=[request])

        async def interpret(self, *args, **kwargs):
            pytest.fail("interpretation must wait for requested context")

    if not valid_fence:
        with pytest.raises(ValueError, match="base_manifest_hash"):
            await RangeReconcileExecutor(Provider()).reconcile(bundle)
        return
    action = await RangeReconcileExecutor(Provider()).reconcile(bundle)

    assert isinstance(action, RequestMoreContext)
    assert action.request is request


@pytest.mark.asyncio
async def test_range_executor_joins_by_hypothesis_without_moving_structure():
    bundle = _staged_bundle()
    separation = SeparationResult(hypotheses=[_separated()])
    interpretation = InterpretationResult(accepted=[_interpreted()])
    separation.inference_provenance = _stage_provenance("separation")
    interpretation.inference_provenance = _stage_provenance("interpretation")

    class Provider:
        async def separate(self, *args, **kwargs):
            return separation

        async def interpret(self, _workspace, _bundle, received, **kwargs):
            assert received is separation
            return interpretation

    action = await RangeReconcileExecutor(Provider()).reconcile(bundle)

    assert action.separation is separation
    assert action.interpretation is interpretation
    assert (
        action.projection.episodes[0].started_at == separation.hypotheses[0].started_at
    )
    assert action.projection.episodes[0].ended_at == separation.hypotheses[0].ended_at
    assert action.projection.episodes[0].evidence_ids == ["observation:one"]


def test_legacy_combined_agent_result_contract_is_removed():
    assert not hasattr(timeline_contracts, "TimelineAgentResult")
    assert "result" not in Publish.model_fields
    assert "projection" in Publish.model_fields


def test_interpretation_cannot_smuggle_structural_fields():
    payload = _interpreted().model_dump(mode="json")
    payload["started_at"] = _manifest().started_at.isoformat()
    payload["evidence_ids"] = ["observation:other"]

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        InterpretedEpisode.model_validate(payload)


def test_local_interpretation_rejection_preserves_accepted_sibling():
    separation = SeparationResult(
        hypotheses=[
            _separated(hypothesis_id="hypothesis:accepted"),
            _separated(hypothesis_id="hypothesis:rejected"),
        ]
    )
    interpretation = InterpretationResult(
        accepted=[_interpreted("hypothesis:accepted")],
        rejected=[
            RejectedHypothesis(
                hypothesis_id="hypothesis:rejected",
                reason_code="mixed_activities",
                explanation="The assigned evidence contains two activities.",
                implicated_evidence_ids=["observation:one"],
            )
        ],
    )
    bundle = _staged_bundle()

    validate_interpretation_result(interpretation, separation, bundle)
    projection = project_validated_stages(separation, interpretation)

    assert [episode.title for episode in projection.episodes] == ["Watched Terminator"]
    assert interpretation.rejected[0].hypothesis_id == "hypothesis:rejected"


def test_supported_discontinuous_hypothesis_has_no_elapsed_time_cutoff():
    bundle = _staged_bundle()
    manifest = bundle.manifest
    manifest.ended_at = manifest.started_at + timedelta(hours=3)
    first = manifest.evidence[0]
    first.ended_at = first.started_at + timedelta(minutes=1)
    manifest.anchors[1].earliest_at = first.ended_at
    manifest.anchors[1].latest_at = first.ended_at
    later = TimelineEvidenceItem(
        evidence_id="observation:later",
        kind="observation",
        locator=_locator(),
        started_at=manifest.started_at + timedelta(hours=2),
        ended_at=manifest.started_at + timedelta(hours=2, minutes=5),
        role="application_state",
        excerpt="The same long-running project resumed",
    )
    end_anchor = EvidenceAnchor(
        anchor_id="anchor:later-end",
        evidence_id=later.evidence_id,
        locator=later.locator,
        support_type="source_edge",
        earliest_at=later.ended_at,
        latest_at=later.ended_at,
    )
    later.anchor_ids = [end_anchor.anchor_id]
    manifest.evidence.append(later)
    manifest.anchors.append(end_anchor)
    hypothesis = _separated().model_copy(
        update={
            "ended_at": later.ended_at,
            "evidence_ids": [first.evidence_id, later.evidence_id],
            "end_anchor_ids": [end_anchor.anchor_id],
        }
    )

    validate_separation_result(SeparationResult(hypotheses=[hypothesis]), bundle)


def test_new_hypothesis_may_overlap_field_pinned_episode():
    pinned = {
        "episode_key": "existing:key",
        "revision": 3,
        "started_at": _manifest().started_at + timedelta(minutes=2),
        "ended_at": _manifest().started_at + timedelta(minutes=25),
        "title": "Pinned title",
        "confirmed_fields": ["title"],
    }
    bundle = _staged_bundle(pinned=[pinned])

    validate_separation_result(
        SeparationResult(hypotheses=[_separated()]),
        bundle,
    )


def test_replacement_cannot_discard_predecessor_activity_outside_manifest():
    bundle = _staged_bundle()
    prior = {
        "episode_key": "outside:work",
        "revision": 1,
        "started_at": bundle.manifest.started_at,
        "ended_at": bundle.manifest.ended_at + timedelta(minutes=40),
        "confirmed_fields": [],
    }
    bundle.existing_episodes.append(prior)
    hypothesis = _separated(
        lineage=EpisodeLineageProposal(
            action="carry",
            predecessor_revisions=[
                EpisodeRevisionRef(episode_key="outside:work", revision=1)
            ],
        )
    )
    with pytest.raises(ValueError, match="extends outside the manifest"):
        validate_separation_result(SeparationResult(hypotheses=[hypothesis]), bundle)
    with pytest.raises(ValueError, match="extends outside the manifest"):
        validate_separation_result(
            SeparationResult(
                hypotheses=[_separated()],
                retirements=[
                    timeline_contracts.EpisodeRetirementProposal(
                        predecessor_revision=EpisodeRevisionRef(
                            episode_key="outside:work", revision=1
                        ),
                        reason="absorbed into bounded activity",
                    )
                ],
            ),
            bundle,
        )
    # Keeping an independent bounded activity does not consume the outside claim.
    validate_separation_result(SeparationResult(hypotheses=[_separated()]), bundle)


def test_redundant_activity_must_be_fully_covered_and_have_no_predecessor():
    bundle = _staged_bundle()
    duplicate = _separated(hypothesis_id="duplicate")
    separation = SeparationResult(hypotheses=[_separated(), duplicate])
    interpretation = InterpretationResult(
        accepted=[_interpreted()],
        rejected=[
            RejectedHypothesis(
                hypothesis_id="duplicate",
                reason_code="redundant_activity",
                explanation="Already covered by accepted activity",
            )
        ],
    )
    validate_interpretation_result(interpretation, separation, bundle)
    duplicate.ended_at += timedelta(minutes=1)
    with pytest.raises(ValueError, match="fully covered"):
        validate_interpretation_result(interpretation, separation, bundle)
    duplicate.ended_at -= timedelta(minutes=1)
    duplicate.lineage = EpisodeLineageProposal(
        action="carry",
        predecessor_revisions=[EpisodeRevisionRef(episode_key="existing", revision=1)],
    )
    with pytest.raises(ValueError, match="fully covered"):
        validate_interpretation_result(interpretation, separation, bundle)


def test_confirmed_semantic_field_is_checked_only_on_exact_predecessor():
    prior = {
        "episode_key": "existing:key",
        "revision": 3,
        "title": "Pinned title",
        "confirmed_fields": ["title"],
    }
    bundle = _staged_bundle(existing=[prior], pinned=[prior])
    separation = SeparationResult(
        hypotheses=[
            _separated(
                lineage=EpisodeLineageProposal(
                    action="carry",
                    predecessor_revisions=[
                        EpisodeRevisionRef(episode_key="existing:key", revision=3)
                    ],
                )
            )
        ]
    )

    with pytest.raises(ValueError, match="changes confirmed field 'title'"):
        validate_interpretation_result(
            InterpretationResult(accepted=[_interpreted(title="Changed title")]),
            separation,
            bundle,
        )


@pytest.mark.asyncio
async def test_codex_range_stages_use_distinct_cache_namespaces(tmp_path, monkeypatch):
    bundle = _staged_bundle()
    separation = SeparationResult(hypotheses=[_separated()])
    interpretation = InterpretationResult(accepted=[_interpreted()])
    lookups = []

    def cached(operation, request):
        lookups.append((operation, request))
        result = (
            separation.model_dump(mode="json")
            if operation.endswith("separation")
            else interpretation.model_dump(mode="json")
        )
        return ReusableInferenceRun(
            result=result,
            request_hash=f"request:{operation}",
            artifact_hash=f"artifact:{operation}",
        )

    monkeypatch.setattr(
        "backend.services.timeline.codex_executor.load_reusable_run",
        cached,
    )
    executor = CodexTimelineExecutor({})

    separated = await executor.separate(tmp_path, bundle)
    interpreted = await executor.interpret(tmp_path, bundle, separated)

    assert [item[0] for item in lookups] == [
        "codex_timeline_separation",
        "codex_timeline_interpretation",
    ]
    assert lookups[0][1]["prompt_version"] == "timeline-separation-pi-v1"
    assert lookups[1][1]["prompt_version"] == "timeline-interpretation-pi-v1"
    assert separated.inference_provenance == StageInferenceProvenance(
        operation="codex_timeline_separation",
        request_hash="request:codex_timeline_separation",
        artifact_hash="artifact:codex_timeline_separation",
        cache_hit=True,
    )
    assert (
        interpreted.accepted[0].hypothesis_id == separated.hypotheses[0].hypothesis_id
    )


@pytest.mark.asyncio
async def test_pi_range_stages_use_distinct_cache_namespaces(tmp_path, monkeypatch):
    from pi_task_helpers import install_pi

    from backend.services.timeline import accepted_context, pi_executor, pi_tasks

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = _staged_bundle()
    separation = SeparationResult(hypotheses=[_separated()])
    interpretation = InterpretationResult(accepted=[_interpreted()])
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [separation.model_dump(mode="json"), interpretation.model_dump(mode="json")],
    )
    monkeypatch.setattr(pi_executor, "_resolve_pi_config", pi_tasks._resolve_pi_config)
    monkeypatch.setattr(accepted_context, "snapshot", lambda *a: {})
    executor = PiTimelineExecutor({})
    separated = await executor.separate(workspace, bundle)
    interpreted = await executor.interpret(workspace, bundle, separated)
    assert separated.inference_provenance.operation == "pi_timeline_separation"
    assert interpreted.inference_provenance.operation == "pi_timeline_interpretation"
    cached = await executor.separate(workspace, bundle)
    assert cached.inference_provenance.cache_hit
    assert len(calls) == 2


def test_compact_context_preserves_all_anchor_windows_and_source_identity():
    bundle = _staged_bundle()
    anchor = bundle.manifest.anchors[0]
    locator = bundle.manifest.evidence[0].locator.model_dump(mode="json")
    context = {
        "blocks": [
            {
                "events": [
                    {
                        "evidence_ids": [anchor.evidence_id],
                        "anchor_ids": [anchor.anchor_id],
                        "locators": [locator, locator],
                        "summary": "Keep the complete semantic account",
                    }
                ]
            }
        ]
    }
    compact, table, aliases = _compact_stage_context(context, bundle.manifest)
    event = compact["blocks"][0]["events"][0]
    assert aliases[event["evidence_ids"][0]] == anchor.evidence_id
    assert compact["locators"][event["locators"][0]] == locator
    assert len(event["locators"]) == 1
    assert event["summary"] == context["blocks"][0]["events"][0]["summary"]
    assert context["blocks"][0]["events"][0]["anchor_ids"] == [anchor.anchor_id]
    for index, original in enumerate(bundle.manifest.anchors):
        evidence_alias = next(
            alias for alias, source in aliases.items() if source == original.evidence_id
        )
        assert f"a{index}" in table["evidence_anchors"][evidence_alias]
        offsets = table["anchors"][f"a{index}"]
        lo, hi = offsets if isinstance(offsets, list) else (offsets, offsets)
        assert (
            bundle.manifest.started_at + timedelta(seconds=lo) == original.earliest_at
        )
        assert bundle.manifest.started_at + timedelta(seconds=hi) == original.latest_at
        assert aliases[f"a{index}"] == original.anchor_id


def test_zero_duration_hypothesis_reports_actionable_boundary_error():
    bundle = _staged_bundle()
    hypothesis = _separated()
    hypothesis.ended_at = hypothesis.started_at + timedelta(seconds=1)
    hypothesis.end_anchor_ids = list(hypothesis.start_anchor_ids)
    with pytest.raises(ValueError, match="positive duration"):
        validate_separation_result(SeparationResult(hypotheses=[hypothesis]), bundle)


def test_compact_context_retains_prior_boundaries_without_unshown_anchor_bulk():
    bundle = _staged_bundle()
    first = bundle.manifest.anchors[0]
    _, table, aliases = _compact_stage_context(
        {"blocks": []},
        bundle.manifest,
        [
            {
                "episode_key": "prior:exact",
                "started_at": first.earliest_at.replace(tzinfo=None).isoformat(),
            }
        ],
    )
    assert set(table["anchors"]) == {"a0"}
    assert aliases["a0"] == first.anchor_id
    assert aliases["p0"] == "prior:exact"
    assert len(bundle.manifest.anchors) == 2


def test_validation_feedback_uses_prompt_aliases_for_repr_quoted_ids():
    assert (
        _encode_validation_feedback(
            "predecessor ('episode:long-key', 4) references anchor 'anchor:long-key' and observation:long-key",
            {
                "p2": "episode:long-key",
                "a9": "anchor:long-key",
                "e3": "observation:long-key",
            },
        )
        == "predecessor ('p2', 4) references anchor 'a9' and e3"
    )


def test_compact_context_keeps_prior_evidence_and_marks_outside_revisions():
    bundle = _staged_bundle()
    evidence = bundle.manifest.evidence[0].evidence_id
    prior = {
        "episode_key": "prior:exact",
        "revision": 4,
        "started_at": (bundle.manifest.started_at - timedelta(minutes=1)).isoformat(),
        "evidence_ids": [evidence, "observation:outside-manifest"],
    }
    inside = {
        **prior,
        "episode_key": "prior:inside",
        "started_at": bundle.manifest.started_at.isoformat(),
    }
    compact, _, aliases = _compact_stage_context(
        {"blocks": []}, bundle.manifest, [prior, inside]
    )
    assert compact["unchanged_outside_activity"][0]["started_at"] == prior["started_at"]
    assert "episode_key" not in compact["unchanged_outside_activity"][0]
    assert compact["prior_evidence"] == [
        {"episode_key": "p0", "revision": 4, "evidence_ids": ["e0"]}
    ]
    assert aliases["p0"] == inside["episode_key"]
    assert prior["episode_key"] not in aliases.values()
    assert [
        x["episode_key"]
        for x in _model_episode_revisions([prior, inside], bundle.manifest)
    ] == [inside["episode_key"]]
    assert prior["evidence_ids"] == [evidence, "observation:outside-manifest"]


def test_compact_context_preserves_summary_boundary_anchor_from_unshown_evidence():
    bundle = _staged_bundle()
    start, end = bundle.manifest.anchors
    extra = bundle.manifest.evidence[0].model_copy(
        update={"evidence_id": "observation:boundary"}
    )
    bundle.manifest.evidence.append(extra)
    end.evidence_id = extra.evidence_id
    event = {
        "started_at": start.earliest_at.isoformat(),
        "ended_at": end.latest_at.isoformat(),
        "evidence_ids": [start.evidence_id],
        "anchor_ids": [start.anchor_id, end.anchor_id],
        "locators": [],
    }
    compact, table, aliases = _compact_stage_context(
        {"blocks": [{"events": [event]}]}, bundle.manifest
    )
    assert "a1" in table["anchors"]
    shown = compact["blocks"][0]["events"][0]
    assert "e1" in shown["evidence_ids"]
    assert shown["boundary_candidates"]["end"][0] == "a1"
    assert shown["ended_at"] == end.latest_at.isoformat()


def test_separation_repairs_known_wrong_anchor_using_exact_cited_evidence_boundary():
    bundle = _staged_bundle()
    hypothesis = _separated()
    hypothesis.end_anchor_ids = list(hypothesis.start_anchor_ids)
    result = SeparationResult(hypotheses=[hypothesis])
    validate_separation_result(result, bundle)
    assert hypothesis.end_anchor_ids == ["anchor:end"]
    assert hypothesis.ended_at == bundle.manifest.evidence[0].ended_at


def test_separation_keeps_point_only_new_hypothesis_as_unassigned_evidence():
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.ended_at = item.started_at
    bundle.manifest.anchors = bundle.manifest.anchors[:1]
    hypothesis = _separated()
    hypothesis.started_at = item.started_at
    hypothesis.ended_at = item.started_at + timedelta(seconds=1)
    hypothesis.start_anchor_ids = ["anchor:start"]
    hypothesis.end_anchor_ids = ["anchor:start"]
    result = SeparationResult(hypotheses=[hypothesis])
    validate_separation_result(result, bundle)
    assert result.hypotheses == []
    assert result.unassigned_evidence_ids == [item.evidence_id]


def test_compact_context_preserves_source_state_when_summary_omits_exit():
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.excerpt = "Zoom · Leave meeting · Thank you for using Zoom"
    item.metadata = {
        "observation_scope": "coarse_application_session",
        "app_name": "Zoom",
    }
    compact, anchors, aliases = _compact_stage_context({"blocks": []}, bundle.manifest)
    state = compact["source_states"][0]
    assert state["text"] == item.excerpt
    assert aliases[state["evidence_id"]] == item.evidence_id
    assert state["observed_at"] == item.started_at.isoformat()
    assert state["anchor_ids"] == ["a0"]
    assert compact["locators"][state["locator"]] == item.locator.model_dump(mode="json")
    assert "a0" in anchors["anchors"]


def test_compact_context_scopes_session_continuity_by_source():
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.metadata = {"meeting_id": "recorder:24"}
    other = item.model_copy(deep=True)
    other.evidence_id = "observation:other-device"
    other.locator.capture_source_id = "second-device"
    bundle.manifest.evidence.append(other)
    unknown = item.model_copy(deep=True)
    unknown.evidence_id = "observation:unknown-device"
    unknown.locator.capture_source_id = None
    bundle.manifest.evidence.append(unknown)
    compact, _, _ = _compact_stage_context({"blocks": []}, bundle.manifest)
    assert len(compact["capture_sessions"]) == 2
    assert {s["capture_source_id"] for s in compact["capture_sessions"]} == {
        "screenpipe-one",
        "second-device",
    }


def test_compact_context_keeps_no_speech_state_separate_from_other_device():
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.kind = "audio_span"
    item.excerpt = None
    item.metadata = {
        "state": "no_speech",
        "direction": "output",
        "covered_seconds": 7200,
    }
    other = item.model_copy(deep=True)
    other.evidence_id = "audio_span:active-other-device"
    other.locator.capture_source_id = "second-device"
    other.metadata = {"state": "transcribed", "direction": "input"}
    other.excerpt = "An actual conversation"
    bundle.manifest.evidence.append(other)
    compact, _, aliases = _compact_stage_context({"blocks": []}, bundle.manifest)
    quiet, active = compact["audio_capture"]
    assert quiet["state"] == "no_speech"
    assert quiet["has_excerpt"] is False
    assert active["state"] == "transcribed"
    assert active["has_excerpt"] is True
    assert aliases[quiet["evidence_id"]] == item.evidence_id
    assert (
        compact["locators"][quiet["locator"]]["capture_source_id"] == "screenpipe-one"
    )
    assert (
        compact["locators"][active["locator"]]["capture_source_id"] == "second-device"
    )


@pytest.mark.parametrize("state", ["no_speech", "transcribed"])
def test_separation_rejects_no_speech_capture_as_standalone_activity(state):
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.kind = "audio_span"
    item.excerpt = None
    item.metadata = {
        "state": state,
        "direction": "output",
        "conversation_id": "pointer-only" if state == "transcribed" else None,
    }
    with pytest.raises(ValueError, match="capture coverage alone"):
        validate_separation_result(SeparationResult(hypotheses=[_separated()]), bundle)


@pytest.mark.parametrize("support", ["acoustic", "screen"])
def test_separation_preserves_positive_activity_alongside_no_speech(support):
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.kind = "audio_span"
    item.excerpt = None
    item.metadata = {"state": "no_speech", "direction": "output"}
    hypothesis = _separated()
    if support == "acoustic":
        item.metadata["acoustic_active_fraction"] = [0.8]
    else:
        other = item.model_copy(deep=True)
        other.evidence_id = "observation:other-device"
        other.kind = "observation"
        other.locator.capture_source_id = "another-device"
        other.excerpt = "Working in the editor"
        other.metadata = {}
        bundle.manifest.evidence.append(other)
        hypothesis.evidence_ids.append(other.evidence_id)
    result = SeparationResult(hypotheses=[hypothesis])
    validate_separation_result(result, bundle)
    assert len(result.hypotheses) == 1


@pytest.mark.parametrize("energy", [0.0, 0.8])
def test_capture_only_idle_prior_cannot_survive_by_omission(energy):
    bundle = _staged_bundle()
    item = bundle.manifest.evidence[0]
    item.kind = "audio_span"
    item.excerpt = None
    item.metadata = {"state": "no_speech", "acoustic_active_fraction": [energy]}
    bundle.existing_episodes = [
        {
            "episode_key": "idle-prior",
            "revision": 1,
            "kind": "idle",
            "started_at": item.started_at,
            "ended_at": item.ended_at,
            "evidence_ids": [item.evidence_id],
            "confirmed_fields": [],
        }
    ]
    result = SeparationResult(unassigned_evidence_ids=[item.evidence_id])
    with pytest.raises(ValueError, match="omission preserves"):
        validate_separation_result(result, bundle)
    result = SeparationResult.model_validate(
        {
            "unassigned_evidence_ids": [item.evidence_id],
            "retirements": [
                {
                    "predecessor_revision": {
                        "episode_key": "idle-prior",
                        "revision": 1,
                    },
                    "reason": "No positive activity evidence",
                }
            ],
        }
    )
    validate_separation_result(result, bundle)
