"""Session memory contracts through source, API and registered worker entry points."""

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from pi_task_helpers import install_pi, review_result

from backend.models.timeline import (
    EpisodeRevisionRef,
    EvidenceLocator,
    TimelineEvidenceRef,
    TimelineSemanticGroupRevision,
)
from backend.services.memory.agent.vault_tools import VaultToolError
from backend.services.timeline import memory_sources as source
from backend.services.timeline import review
from backend.services.timeline import session_accounts as accounts
from backend.services.timeline import sessions
from backend.workers import session_jobs

START = datetime(2026, 9, 4, 7, tzinfo=timezone.utc)


def evidence(
    key,
    *,
    role="user_statement",
    text="We agreed to ship the fix tomorrow.",
    device="phone",
    start=START,
):
    return TimelineEvidenceRef(
        evidence_id=key,
        kind="transcript",
        locator=EvidenceLocator(
            capture_source_id=device, modality="transcript", track_id="input"
        ),
        started_at=start,
        ended_at=start + timedelta(minutes=20),
        role=role,
        excerpt=text,
        content_hash=f"hash-{key}",
        metadata={"direction": "input"},
    )


def episode(key="call", refs=None, start=START):
    return NS(
        episode_key=key,
        episode_id=key,
        revision=1,
        started_at=start,
        ended_at=start + timedelta(minutes=30),
        evidence_refs=refs if refs is not None else [evidence(key)],
        memory_policy="auto",
        audio_ranges=[],
        status="provisional",
        title="Call",
        summary="Old account must not be used",
        confirmed_fields=[],
    )


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield


class Query:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self):
        return self.rows


def test_tv_cannot_exclude_a_call_in_the_same_episode_or_on_another_device():
    mixed = episode(
        refs=[
            evidence(
                "tv",
                role="media_content",
                device="macbook",
                text="Daryl and Rosita run away.",
            ),
            evidence("call"),
        ]
    )
    rows = source.evidence_sources([mixed])
    assert {s["evidence_id"] for s in source.memory_sources(rows)} == {"call"}
    assert len(rows) == 2
    assert (
        next(s for s in rows if s["evidence_id"] == "tv")["participation"]
        == "background"
    )


def test_different_capture_sources_and_unique_local_speech_survive():
    rows = source.evidence_sources(
        [
            episode(
                refs=[
                    evidence("left"),
                    evidence("right", device="laptop"),
                    evidence("unique", text="I will send the proposal."),
                ]
            )
        ]
    )
    assert len(source.memory_sources(rows)) == 3
    assert len(source.evidence_sources([episode(), episode()])) == 1


def test_exclusion_survives_regroup_and_split_but_not_new_evidence():
    original = source.evidence_sources([episode()])
    decision = NS(created_at=START, action="exclude", sources=original)
    split = episode("new-key", refs=[evidence("call"), evidence("late")])
    split.started_at += timedelta(minutes=5)
    split.ended_at -= timedelta(minutes=5)
    rows = source.apply_dispositions(source.evidence_sources([split]), [decision])
    assert {s["evidence_id"] for s in source.memory_sources(rows)} == {"late"}


def test_uncertain_attribution_requires_a_question_and_correction_changes_scope():
    rows = source.evidence_sources(
        [episode(refs=[evidence("speech", role="uncertain")])]
    )
    assert source.scope_questions(rows)
    corrected = source.apply_dispositions(
        rows,
        [NS(created_at=START, action="attribute", role="third_party", sources=rows)],
    )
    assert not source.scope_questions(corrected)
    assert source.scope_hash(rows) != source.scope_hash(corrected)
    assert source.memory_sources(corrected)


def test_media_opt_in_preserves_attribution():
    rows = source.evidence_sources(
        [episode(refs=[evidence("tv", role="media_content")])]
    )
    opted_in = source.apply_dispositions(
        rows, [NS(created_at=START, action="include", sources=rows)]
    )
    assert source.memory_sources(opted_in)[0]["role"] == "media_content"


def test_large_cross_midnight_source_batches_retain_every_character():
    text = "A boundary is a claim. " * 4000
    start = datetime(2026, 9, 4, 18, 20, tzinfo=timezone.utc)
    rows = source.evidence_sources(
        [episode(refs=[evidence("long", text=text, start=start)], start=start)]
    )
    batches = list(accounts.source_batches(rows))
    assert len(batches) > 1
    assert "".join(item["excerpt"] for batch in batches for item in batch) == text
    assert {item["key"] for batch in batches for item in batch} == {rows[0]["key"]}


@pytest.mark.asyncio
async def test_model_cannot_promote_media_only_evidence_to_personal_claim(
    monkeypatch, tmp_path
):
    rows = source.evidence_sources(
        [episode(refs=[evidence("tv", role="media_content")])]
    )
    candidate = dict(
        title="Wrong",
        summary="",
        claims=[
            dict(text="The user escaped", personal=True, source_keys=[rows[0]["key"]])
        ],
        questions=[],
        useful=True,
    )
    install_pi(monkeypatch, tmp_path, [candidate])
    with pytest.raises(VaultToolError, match="Personal claim"):
        await accounts.prepare_source_account(rows, rows, record=AsyncMock())


@pytest.mark.asyncio
async def test_incomplete_model_output_is_failure_not_no_changes(monkeypatch, tmp_path):
    from backend.services.memory.agent.pi_agent import _PiEventResult

    install_pi(
        monkeypatch,
        tmp_path,
        [lambda _: _PiEventResult(returncode=1, fatal_errors=["interrupted"])],
    )
    with pytest.raises(ValueError, match="incomplete"):
        await accounts.prepare_source_account([], [], record=AsyncMock())


@pytest.mark.asyncio
async def test_registered_memory_job_looks_up_exact_proposal_and_runs_generation(
    monkeypatch,
):
    proposal = NS(state="queued", attempts=0)
    monkeypatch.setattr(
        session_jobs.MemoryReviewProposal, "find_one", AsyncMock(return_value=proposal)
    )
    generate = AsyncMock(return_value="no_changes")
    monkeypatch.setattr(review, "generate_memory_review", generate)
    assert (
        await session_jobs.generate_session_memory_job.__wrapped__("proposal-id")
        == "no_changes"
    )
    generate.assert_awaited_once_with(proposal)


@pytest.mark.asyncio
async def test_registered_preparation_job_persists_failure_and_bounded_attempt(
    monkeypatch,
):
    item = NS(
        state="queued", attempts=0, requested_revision=0, id="a" * 24, save=AsyncMock()
    )
    monkeypatch.setattr(
        session_jobs.SessionPreparation,
        "get_pymongo_collection",
        lambda: NS(update_one=AsyncMock()),
    )

    @asynccontextmanager
    async def renewable_preparation_lock(key, **kwargs):
        assert key.startswith("session-preparation:")
        assert kwargs == {"timeout": 60, "blocking_timeout": 1, "renew": True}
        yield

    monkeypatch.setattr(session_jobs, "distributed_lock", renewable_preparation_lock)
    monkeypatch.setattr(
        session_jobs.SessionPreparation, "get", AsyncMock(return_value=item)
    )
    monkeypatch.setattr(
        sessions,
        "prepare_sessions",
        AsyncMock(side_effect=ValueError("invalid membership")),
    )
    monkeypatch.setattr(sessions, "persist_preparation", item.save)
    with pytest.raises(ValueError):
        await session_jobs.prepare_sessions_job.__wrapped__("a" * 24)
    assert item.state == "failed" and item.attempts == 1
    assert "membership" in item.error


@pytest.mark.asyncio
async def test_api_disposition_fences_revision_and_scope(monkeypatch):
    from backend.routers.modules import timeline_routes as routes

    save = AsyncMock(side_effect=ValueError("Source scope changed"))
    monkeypatch.setattr(sessions, "decide_session", save)
    body = routes.SessionDispositionRequest(
        timezone="Asia/Kolkata",
        session_key="session",
        revision=2,
        action="exclude",
        source_keys=["source"],
        scope_hash="old",
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await routes.decide_timeline_session_memory(
            date(2026, 9, 4), body, NS(id="user")
        )
    assert error.value.status_code == 409
    assert save.call_args.args[4:8] == (2, "exclude", ["source"], "old")


@pytest.mark.asyncio
async def test_recent_recovery_does_not_enqueue_old_backlog(monkeypatch):
    from backend.services import source_search

    monkeypatch.setattr(
        source_search,
        "db",
        lambda: NS(recording_organization_intents=NS(find=lambda *a: Query([]))),
    )
    now = datetime.now(timezone.utc)
    today = now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).date()
    days = [
        NS(local_date=today, timezone="Asia/Kolkata", pending_publication_id=None),
        NS(
            local_date=today - timedelta(days=30),
            timezone="Asia/Kolkata",
            pending_publication_id=None,
        ),
    ]
    monkeypatch.setattr(sessions.TimelineDay, "find", lambda *a: Query(days))
    monkeypatch.setattr(sessions.MemoryReviewProposal, "find", lambda *a: Query([]))
    monkeypatch.setattr(sessions.SessionPreparation, "find", lambda *a: Query([]))
    prepare = AsyncMock()
    monkeypatch.setattr(sessions, "request_preparation", prepare)
    await sessions.prepare_recent_sessions()
    prepare.assert_awaited_once_with(days[0])


@pytest.mark.asyncio
async def test_generation_finishes_reference_only_without_writer_or_approval(
    monkeypatch,
):
    ep = episode(refs=[evidence("tv", role="media_content")])
    proposal = NS(
        proposal_id="p",
        user_id="user",
        state="queued",
        attempts=0,
        excluded_source_keys=[],
        source_kind="timeline",
        revision_feedback=None,
        timezone="Asia/Kolkata",
        memory_space_id=None,
        questions=[],
        correction_of=[],
        withdrawn=False,
        save=AsyncMock(),
        id="p",
    )

    @asynccontextmanager
    async def renewable_generation_lock(key, **kwargs):
        assert key.startswith("memory:review-generation:")
        assert kwargs == {"timeout": 60, "blocking_timeout": 1, "renew": True}
        yield

    monkeypatch.setattr(review, "distributed_lock", renewable_generation_lock)
    monkeypatch.setattr(
        review.MemoryReviewProposal, "get", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(review, "persist_generation", AsyncMock())
    monkeypatch.setattr(
        review, "validate_selection", AsyncMock(return_value=([ep], []))
    )
    monkeypatch.setattr(review, "source_decisions", AsyncMock(return_value=[]))
    writer = Mock(
        side_effect=AssertionError("Reference media must not invoke the vault writer")
    )
    monkeypatch.setattr(review, "_service", writer)
    assert await review.generate_memory_review(proposal) == "no_changes"
    assert not proposal.active
    assert proposal.source_scope[0]["role"] == "media_content"
    writer.assert_not_called()


def test_partial_exclusion_cannot_leak_when_new_episode_enlarges_the_same_excerpt():
    original = episode()
    original.started_at += timedelta(minutes=5)
    original.ended_at -= timedelta(minutes=15)
    decision = NS(
        created_at=START, action="exclude", sources=source.evidence_sources([original])
    )
    sources = source.apply_dispositions(
        source.evidence_sources([episode()]), [decision]
    )
    assert not source.memory_sources(sources)
    assert "cannot be separated" in sources[0]["scope_note"]


def test_later_and_resume_do_not_change_memory_scope_or_override_exclusions():
    original = source.evidence_sources([episode()])
    excluded = NS(created_at=START, action="exclude", sources=original)
    deferred = NS(
        created_at=START + timedelta(seconds=1), action="defer", sources=original
    )
    resumed = NS(
        created_at=START + timedelta(seconds=2), action="resume", sources=original
    )
    before = source.apply_dispositions(original, [excluded])
    waiting = source.apply_dispositions(original, [excluded, deferred])
    after = source.apply_dispositions(original, [excluded, deferred, resumed])
    assert waiting[0]["deferred"] is True
    assert after[0]["deferred"] is False
    assert (
        source.scope_hash(before)
        == source.scope_hash(waiting)
        == source.scope_hash(after)
    )
    assert not source.memory_sources(after)


def test_account_content_budget_does_not_charge_source_identifiers():
    key = "stable-evidence-reference:" + "x" * 1000
    sources = [
        {
            "key": key,
            "excerpt": "I agreed.",
            "kind": "transcript",
            "participation": "supporting",
            "role": "user_statement",
        }
    ]
    candidate = accounts.SessionAccount(
        title="Decisions",
        summary="",
        useful=True,
        questions=[],
        claims=[
            accounts.AccountClaim(
                text="The user agreed.",
                personal=True,
                citations=[accounts.SourceQuote(source_key=key, quote="I agreed.")],
            )
            for _ in range(10)
        ],
    )
    assert (
        accounts.validate_account(candidate, sources)["claims"][0]["citations"][0][
            "source_key"
        ]
        == key
    )


def test_account_content_budget_reports_the_actual_limit():
    text = "x" * 600
    sources = [
        {
            "key": "s",
            "excerpt": text,
            "kind": "transcript",
            "participation": "supporting",
            "role": "user_statement",
        }
    ]
    candidate = accounts.SessionAccount(
        title="Decisions",
        summary="",
        useful=True,
        questions=[],
        claims=[
            accounts.AccountClaim(
                text="y" * 500,
                personal=True,
                citations=[accounts.SourceQuote(source_key="s", quote=text)],
            )
            for _ in range(20)
        ],
    )
    with pytest.raises(ValueError, match="22009.*18000"):
        accounts.validate_account(candidate, sources)


def test_account_reads_uncertain_speech_only_to_assess_material_questions():
    sources = source.evidence_sources(
        [episode(refs=[evidence("uncertain", role="uncertain")])]
    )
    assert source.account_sources(sources)
    assert not source.memory_sources(sources)
    account = accounts.SessionAccount(
        title="Unsupported",
        summary="",
        useful=True,
        questions=[],
        claims=[
            accounts.AccountClaim(
                text="The user agreed", source_keys=[sources[0]["key"]], personal=False
            )
        ],
    )
    with pytest.raises(ValueError, match="Unresolved attribution"):
        accounts.validate_account(account, sources)


def test_verbatim_citation_retains_repeated_speaker_label_but_cannot_join_different_speakers():
    row = {
        "kind": "transcript",
        "excerpt": "Avery: I agreed\nAvery: to send it tomorrow",
        "metadata": {"speakers": ["Avery", "Other"]},
    }
    assert (
        accounts.canonical_quote(row, "I agreed to send it tomorrow")
        == "I agreed\nAvery: to send it tomorrow"
    )
    row["excerpt"] = "Avery: I agreed\nOther: to send it tomorrow"
    assert accounts.canonical_quote(row, "I agreed to send it tomorrow") is None
    assert accounts.canonical_quote(row, "I agreed to send it today") is None


def test_retained_named_speakers_resolve_speech_without_assuming_the_owner():
    ref = evidence("named", role="uncertain")
    ref.metadata["speakers"] = ["Sidhanta"]
    rows = source.evidence_sources([episode(refs=[ref])])
    assert source.memory_sources(rows)[0]["role"] == "third_party"
    assert rows[0]["original_role"] == "uncertain"
    assert rows[0]["attribution_origin"] == "retained_speaker_attribution"
    ref.metadata["speakers"] = ["Unknown Speaker 1"]
    assert not source.memory_sources(source.evidence_sources([episode(refs=[ref])]))


def test_meeting_output_is_not_automatically_treated_as_tv():
    ref = evidence("remote", role="media_content")
    ref.metadata.update(
        direction="output", meeting_id="meeting-1", speakers=["Sidhanta"]
    )
    assert source.memory_sources(source.evidence_sources([episode(refs=[ref])]))
    ref.metadata.pop("meeting_id")
    assert not source.memory_sources(source.evidence_sources([episode(refs=[ref])]))


def test_exclusion_follows_physical_audio_when_transcript_identity_changes():
    from backend.models.timeline import TimelineAudioRange

    original = episode(refs=[evidence("transcript-one")])
    original.audio_ranges = [
        TimelineAudioRange(
            capture_source_id="phone",
            time_basis="captured",
            started_at=START,
            ended_at=START + timedelta(minutes=20),
            chunk_ids=["immutable-chunk"],
        )
    ]
    decision = NS(
        created_at=START, action="exclude", sources=source.evidence_sources([original])
    )
    successor = episode(
        "regrouped", refs=[evidence("new-transcript", text="Improved retranscription")]
    )
    successor.audio_ranges = original.audio_ranges
    assert not source.memory_sources(
        source.apply_dispositions(source.evidence_sources([successor]), [decision])
    )
    independent = episode("independent", refs=[evidence("independent")])
    independent.audio_ranges = [
        original.audio_ranges[0].model_copy(
            update={"chunk_ids": ["new-independent-chunk"]}
        )
    ]
    assert source.memory_sources(
        source.apply_dispositions(source.evidence_sources([independent]), [decision])
    )


def test_session_note_mutations_require_exact_claim_provenance(tmp_path):
    from backend.services.memory.agent.vault_tools import VaultToolError, VaultTools

    tools = VaultTools(tmp_path)
    tools.source_claims = {"C001": ["phone-speech"], "C002": ["work-ocr"]}
    tools.require_source_episode_keys = True
    tools.allowed_source_episode_keys = {"mixed-episode"}
    args = {
        "path": "Topics/Decision.md",
        "content": "# Decision\n\n## About\nWe agreed to ship tomorrow.\n\n## Conversations\n![[Conversations.base#Topic]]",
        "source_episode_keys": ["mixed-episode"],
    }
    with pytest.raises(VaultToolError, match="source_claim_ids"):
        tools.dispatch("write_note", args)
    assert not (tmp_path / args["path"]).exists()
    tools.dispatch("write_note", {**args, "source_claim_ids": ["C001"]})
    assert tools.source_evidence_keys_by_path == {
        "Topics/Decision.md": {"phone-speech"}
    }


def test_clarification_is_distinct_user_evidence_and_remains_excludable_after_regroup():
    original = source.evidence_sources([episode()])
    answer = NS(
        id="answer-one",
        created_at=START,
        action="clarify",
        clarification="She is my wife.",
        sources=original,
    )
    rows = source.apply_dispositions(original, [answer])
    annotation = next(s for s in rows if s["kind"] == "annotation")
    assert annotation["excerpt"] == "She is my wife."
    assert rows[0]["excerpt"] == original[0]["excerpt"]
    exclude = NS(
        created_at=START + timedelta(seconds=1), action="exclude", sources=[annotation]
    )
    regrouped = source.evidence_sources([episode("regrouped", refs=[evidence("call")])])
    result = source.apply_dispositions(regrouped, [answer, exclude])
    assert (
        next(s for s in result if s["kind"] == "annotation")["participation"]
        == "excluded"
    )
    assert source.memory_sources(result)[0]["kind"] == "transcript"


@pytest.mark.asyncio
async def test_claim_check_cannot_establish_family_identity_from_a_pronoun(
    monkeypatch, tmp_path
):
    rows = source.evidence_sources(
        [episode(refs=[evidence("speech", text="Avery: She arrived yesterday.")])]
    )
    candidate = accounts.SessionAccount(
        title="Visit",
        summary="Avery's mother arrived.",
        claims=[],
        questions=[],
        useful=False,
    )
    revised = dict(
        title="Visit", summary="", claims=[], questions=["Who arrived?"], useful=False
    )
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [
            review_result("revise", "The relationship is not established."),
            revised,
            review_result("ready", "The revised question preserves uncertainty."),
        ],
    )
    result = await accounts.verify_session_claims(candidate, rows, record=AsyncMock())
    assert result.questions == ["Who arrived?"]
    assert not result.claims
    assert calls[0]["tool_handler"] is not calls[-1]["tool_handler"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,checkpoint,expected,attempts",
    [
        (None, False, "queued", 0),
        ("budget_exhausted", True, "paused", 1),
        ("repeated_tool_call", True, "paused", 1),
        ("time_slice", True, "queued", 0),
    ],
)
async def test_registered_generation_job_distinguishes_continuation_from_budget_exhaustion(
    monkeypatch,
    kind,
    checkpoint,
    expected,
    attempts,
):
    from backend.services.timeline.investigation_state import InvestigationIncomplete

    ep = episode()
    proposal = NS(
        proposal_id="bounded",
        error="Previous time slice ended",
        failure_kind="time_slice",
        user_id="user",
        id="bounded",
        state="queued",
        attempts=0,
        excluded_source_keys=[],
        source_kind="timeline",
        revision_feedback=None,
        timezone="Asia/Kolkata",
        memory_space_id=None,
        questions=[],
        correction_of=[],
        withdrawn=False,
    )
    monkeypatch.setattr(review, "distributed_lock", unlocked)
    monkeypatch.setattr(
        review.MemoryReviewProposal, "get", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(
        review.MemoryReviewProposal, "find_one", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(review, "persist_generation", AsyncMock())
    monkeypatch.setattr(
        review, "validate_selection", AsyncMock(return_value=([ep], []))
    )
    monkeypatch.setattr(review, "source_decisions", AsyncMock(return_value=[]))

    async def continue_account(*args, **kwargs):
        assert proposal.error is None
        assert proposal.failure_kind is None
        raise (
            InvestigationIncomplete(kind, "Work incomplete", checkpoint=checkpoint)
            if kind
            else accounts.AccountWorkPending()
        )

    monkeypatch.setattr(review, "build_session_account", continue_account)
    assert (
        await session_jobs.generate_session_memory_job.__wrapped__("bounded")
        == expected
    )
    assert proposal.attempts == attempts
    assert proposal.error == ("Work incomplete" if kind else None)


@pytest.mark.asyncio
async def test_overlap_and_identical_words_do_not_authorize_duplicate_capture(
    monkeypatch, tmp_path
):
    rows = source.evidence_sources(
        [
            episode(
                refs=[evidence("one", device="phone"), evidence("two", device="laptop")]
            )
        ]
    )
    candidate = accounts.SessionAccount(
        title="Two captures",
        summary="",
        claims=[],
        questions=[],
        useful=False,
        relationships=[
            accounts.SourceRelationship(
                source_keys=[s["key"] for s in rows],
                relationship="duplicate",
                reason="The words match",
            )
        ],
    )
    fixed = candidate.model_dump()
    fixed["relationships"][0].update(
        relationship="unresolved", reason="Shared capture is not established"
    )
    install_pi(
        monkeypatch,
        tmp_path,
        [
            review_result(
                "revise", "Matching words do not establish duplicate capture"
            ),
            fixed,
            review_result("ready", "Uncertainty is preserved"),
        ],
    )
    result = await accounts.verify_session_claims(candidate, rows, record=AsyncMock())
    assert result.relationships[0].relationship == "unresolved"
    assert len(result.relationships[0].source_keys) == 2


@pytest.mark.asyncio
async def test_large_organization_unit_remains_intact(monkeypatch):
    from backend.services.timeline import session_organization as organization

    big, small = episode("big"), episode("small")
    big.summary = "Large account " * 4000

    async def inspect(units, record):
        assert [len(u.members) for u in units] == [1, 1]
        return units

    monkeypatch.setattr(organization, "organize_batch", inspect)
    result = await organization.organize_sessions([big, small], record=AsyncMock())
    assert {e.episode_id for u in result for e in u.members} == {"big", "small"}


def test_point_in_time_evidence_can_form_a_single_session():
    ep = episode(refs=[evidence("moment")])
    ep.ended_at = ep.started_at
    day = NS(current_snapshot=None, current_snapshot_id="a" * 64)
    group = sessions.session_groups(day, [ep])[0]
    assert group.started_at == group.ended_at
    assert source.memory_sources(source.evidence_sources([ep]))


def test_ocr_visual_line_wrapping_retains_original_quote_without_changing_words():
    row = {
        "kind": "screen",
        "excerpt": "Section\n6\nis now split into separate markdown + code cells",
        "metadata": {},
    }
    assert (
        accounts.canonical_quote(row, "Section 6 is now split into separate markdown")
        == "Section\n6\nis now split into separate markdown"
    )
    assert (
        accounts.canonical_quote(row, "Section 6 is now split into different markdown")
        is None
    )


def test_photo_exclusion_survives_new_caption_and_corrected_capture_time():
    original = source.evidence_sources([episode()])[0]
    original.update(
        kind="immich",
        locator={"capture_source_id": "immich", "modality": "photo", "track_id": None},
    )
    later = {
        **original,
        "content_hash": "improved-caption",
        "started_at": (START + timedelta(hours=1)).isoformat(),
        "ended_at": (START + timedelta(hours=1)).isoformat(),
    }
    decided = NS(action="exclude", created_at=START, sources=[original])
    assert not source.memory_sources(source.apply_dispositions([later], [decided]))
    independent = {**later, "key": "independent", "evidence_id": "another-photo"}
    assert source.memory_sources(source.apply_dispositions([independent], [decided]))


@pytest.mark.asyncio
async def test_organization_terminal_result_requires_complete_partition(
    monkeypatch, tmp_path
):
    from backend.services.timeline import session_organization as organization

    units = [
        NS(key=k, title=k, summary=k, members=[episode(k)]) for k in ["one", "two"]
    ]
    good = dict(
        groups=[
            dict(
                units=["one", "two"],
                title="Investigation",
                reason="Supported continuation",
            )
        ]
    )
    bad = dict(
        groups=[
            dict(units=["one"], title="Investigation", reason="Omitted second unit")
        ]
    )
    install_pi(monkeypatch, tmp_path, [good, bad])
    result = await organization.organize_batch(units, AsyncMock())
    assert {e.episode_id for e in result[0].members} == {"one", "two"}
    units[0].summary = "Changed account"
    with pytest.raises(VaultToolError, match="omitted"):
        await organization.organize_batch(units, AsyncMock())


def test_ocr_bracket_line_breaks_keep_raw_citation_and_reject_changed_words():
    row = {
        "kind": "observation",
        "excerpt": "The completed run (\nrun-123\n) was trained",
        "metadata": {},
    }
    assert (
        accounts.canonical_quote(row, "The completed run (run-123) was trained")
        == row["excerpt"]
    )
    assert (
        accounts.canonical_quote(row, "The completed run (run-124) was trained") is None
    )
    assert (
        accounts.canonical_quote(
            {"kind": "observation", "excerpt": "not able", "metadata": {}}, "notable"
        )
        is None
    )


@pytest.mark.asyncio
async def test_review_finding_requires_revision_even_when_overall_assessment_is_positive(
    monkeypatch, tmp_path
):
    candidate = accounts.SessionAccount(
        title="Discussion",
        summary="An unsupported detail",
        claims=[],
        questions=[],
        useful=False,
    )
    revised = candidate.model_dump()
    revised["summary"] = ""
    actions = [
        {
            "verdict": "ready",
            "reason": "Useful overall; the detail is uncertain",
            "checks": [],
            "account_issues": ["The source does not establish the asserted detail."],
        },
        revised,
        {"verdict": "ready", "reason": "Grounded", "checks": [], "account_issues": []},
    ]
    calls = install_pi(monkeypatch, tmp_path, actions)
    result = await accounts.verify_session_claims(candidate, [], record=AsyncMock())
    assert result.summary == ""
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_review_cannot_skip_or_duplicate_question_checks(monkeypatch, tmp_path):
    from backend.services.timeline import pi_tasks

    candidate = accounts.SessionAccount(
        title="Account",
        summary="",
        useful=False,
        claims=[],
        questions=["Who was involved?", "What should be retained?"],
    )

    def check(index):
        return {
            "target": "question",
            "index": index,
            "action": "remove",
            "reason": "No supported useful memory depends on this answer",
        }

    def investigate(tools):
        for checks in [[check(0)], [check(0), check(0)], [check(0), check(2)]]:
            with pytest.raises(VaultToolError, match="exactly once") as failure:
                tools.dispatch(
                    "finish_task",
                    {
                        "result": {
                            "checks": checks,
                            "account_issues": [],
                            "reason": "Review",
                        }
                    },
                )
            assert '"missing": [["question", 1]]' in str(failure.value)
        tools.dispatch(
            "finish_task",
            {
                "result": {
                    "checks": [check(0), check(1)],
                    "account_issues": [],
                    "reason": "Both questions are unnecessary",
                }
            },
        )

    install_pi(monkeypatch, tmp_path, [investigate])
    result = (
        await pi_tasks.run_task(
            stage="review-contract",
            instruction="Review",
            payload={},
            result_type=accounts.AccountReview,
            validate=lambda result: accounts.validate_account_review(result, candidate),
        )
    ).result
    assert accounts.validate_account_review(result, candidate)


@pytest.mark.asyncio
async def test_review_applies_question_removals_without_rewriting_grounded_account(
    monkeypatch, tmp_path
):
    candidate = accounts.SessionAccount(
        title="Discussion",
        summary="",
        useful=False,
        claims=[],
        questions=["Who made the decision?", "What happened afterward?"],
    )
    context = {}
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [
            {
                "checks": [
                    {
                        "target": "question",
                        "index": 0,
                        "action": "keep",
                        "reason": "Attribution remains material",
                    },
                    {
                        "target": "question",
                        "index": 1,
                        "action": "remove",
                        "reason": "Later events are unnecessary",
                    },
                ],
                "account_issues": [],
                "advisories": ["The title could be shorter"],
                "reason": "Grounded after omitting the unnecessary question",
            }
        ],
    )
    result = await accounts.verify_session_claims(
        candidate, [], record=AsyncMock(), accepted_context=context
    )
    assert result.questions == ["Who made the decision?"]
    assert candidate.questions == ["Who made the decision?", "What happened afterward?"]
    assert result.summary == candidate.summary
    assert len(calls) == 1
    assert context["review"]["advisories"] == ["The title could be shorter"]


def test_review_claim_removal_still_requires_account_revision():
    candidate = accounts.SessionAccount(
        title="Discussion",
        summary="Contains an unsupported claim",
        useful=True,
        claims=[dict(text="Unsupported claim", personal=True)],
        questions=[],
    )
    review = accounts.AccountReview(
        checks=[dict(target="claim", index=0, action="remove", reason="Unsupported")],
        account_issues=[],
        advisories=[],
        reason="Summary must be refreshed",
    )
    assert not accounts.validate_account_review(review, candidate)


@pytest.mark.asyncio
async def test_review_text_budget_preserves_complete_explanations(
    monkeypatch, tmp_path
):
    from backend.services.timeline import pi_tasks

    candidate = accounts.SessionAccount(
        title="Discussion",
        summary="",
        useful=False,
        claims=[],
        questions=["Who made the decision?"],
    )
    explanation = "The attribution changes which person the note concerns. " * 8
    result = dict(
        checks=[dict(target="question", index=0, action="keep", reason=explanation)],
        account_issues=[],
        advisories=[],
        reason="A consequential unresolved reference",
    )

    def investigate(tools):
        too_large = {**result, "advisories": ["Additional wording. " * 1500]}
        with pytest.raises(VaultToolError, match="review text budget"):
            tools.dispatch("finish_task", {"result": too_large})
        tools.dispatch("finish_task", {"result": result})

    install_pi(monkeypatch, tmp_path, [investigate])
    review = (
        await pi_tasks.run_task(
            stage="review-budget",
            instruction="Review",
            payload={},
            result_type=accounts.AccountReview,
            validate=lambda value: accounts.validate_account_review(value, candidate),
        )
    ).result
    assert review.checks[0].reason == explanation


@pytest.mark.asyncio
async def test_reviewer_initial_brief_contains_the_entire_account_scope(
    monkeypatch, tmp_path
):
    quote = "A supporting passage with additional source detail. " * 10
    rows = source.evidence_sources(
        [episode(refs=[evidence("review-source", text=quote)])]
    )
    candidate = accounts.SessionAccount(
        title="Review scope",
        summary="A source-backed account",
        useful=True,
        claims=[
            dict(
                text=f"Claim number {i} to assess completely",
                personal=True,
                source_keys=[rows[0]["key"]],
                citations=[dict(source_key=rows[0]["key"], quote=quote)],
            )
            for i in range(28)
        ],
        questions=["Who made the final decision?"],
    )
    calls = install_pi(monkeypatch, tmp_path, [review_result("ready", "Grounded")])
    await accounts.verify_session_claims(candidate, rows, record=AsyncMock())
    prompt = calls[0]["prompt"]
    assert all(claim.text in prompt for claim in candidate.claims)
    assert candidate.questions[0] in prompt
    assert "S001" in prompt
    assert quote in prompt
    assert quote in calls[0]["tool_handler"].materials["task.json"]


@pytest.mark.asyncio
async def test_reviewer_can_remove_redundant_questions(monkeypatch, tmp_path):
    candidate = accounts.SessionAccount(
        title="Routine",
        summary="",
        claims=[],
        questions=["Who owns this vault?"],
        useful=False,
    )
    revised = candidate.model_dump()
    revised["questions"] = []
    calls = install_pi(
        monkeypatch,
        tmp_path,
        [
            review_result("revise", "Accepted knowledge already resolves the question"),
            revised,
            review_result("ready", "No consequential uncertainty remains"),
        ],
    )
    result = await accounts.verify_session_claims(candidate, [], record=AsyncMock())
    assert not result.questions
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_account_repairs_citations_inside_the_pi_run(monkeypatch, tmp_path):
    rows = source.evidence_sources(
        [
            episode(
                refs=[
                    evidence(
                        "ocr",
                        text="The deployment checklist\nrequires approval.",
                        role="application_state",
                    )
                ]
            )
        ]
    )
    candidate = dict(
        title="Checklist",
        summary="Approval requirement",
        claims=[
            dict(
                text="The checklist requires approval",
                personal=True,
                citations=[dict(source_key=rows[0]["key"], quote="invented words")],
            )
        ],
        questions=[],
        useful=True,
    )

    def repair(handler):
        candidate["claims"].append(
            {
                **candidate["claims"][0],
                "citations": [
                    dict(source_key=rows[0]["key"], quote="another invented quote")
                ],
            }
        )
        with pytest.raises(VaultToolError, match="not verbatim") as failure:
            handler.dispatch("finish_task", {"result": candidate})
        assert "Claim 1" in str(failure.value)
        assert "Claim 2" in str(failure.value)
        assert "invented words" not in str(failure.value)
        assert "another invented quote" not in str(failure.value)
        assert handler.trace[0]["arguments"]["result"] == candidate
        page = json.loads(
            handler.dispatch("read_material", {"store": "evidence", "key": "S001"})
        )
        handler.dispatch(
            "revise_result",
            {
                "edits": [
                    {
                        "op": "replace",
                        "path": f"/claims/{index}/citations/0/quote",
                        "value_from": {
                            "result_ref": page["result_ref"],
                            "pointer": "/text",
                        },
                    }
                    for index in range(len(candidate["claims"]))
                ]
            },
        )

    calls = install_pi(monkeypatch, tmp_path, [repair])
    result = await accounts.prepare_source_account(rows, rows, record=AsyncMock())
    assert result.claims[0].citations[0].quote == rows[0]["excerpt"]
    assert len(calls) == 1
    assert calls[0]["tool_handler"].trace[0]["error"]


@pytest.mark.asyncio
async def test_claim_checker_exhausted_repairs_never_become_no_changes(
    monkeypatch, tmp_path
):
    candidate = accounts.SessionAccount(
        title="Delivery", summary="", claims=[], questions=[], useful=False
    )
    actions = [review_result("revise", "Unresolved support")]
    for i in range(2):
        actions.extend(
            [
                {**candidate.model_dump(), "summary": str(i)},
                review_result("revise", "Unresolved support"),
            ]
        )
    calls = install_pi(monkeypatch, tmp_path, actions)
    with pytest.raises(ValueError, match="requires revision"):
        await accounts.verify_session_claims(candidate, [], record=AsyncMock())
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_clarification_api_returns_retryable_busy_status_when_publication_is_locked(
    monkeypatch,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.routers.modules import timeline_routes as routes
    from backend.services.redis_lock import LockUnavailable

    @asynccontextmanager
    async def busy(*args, **kwargs):
        raise LockUnavailable("publication is busy")
        yield

    monkeypatch.setattr(sessions, "distributed_lock", busy)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.current_active_user] = lambda: NS(id="owner")
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/timeline/sessions/2026-09-04/disposition",
            json={
                "timezone": "Asia/Kolkata",
                "session_key": "session",
                "revision": 1,
                "action": "clarify",
                "source_keys": ["source"],
                "scope_hash": "scope",
                "clarification": "The project owner made that decision.",
            },
        )
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == "2"
    assert "saved" in response.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_scope", [True, False])
async def test_feedback_revision_worker_reuses_only_matching_sources(
    monkeypatch, tmp_path, same_scope
):
    from copy import deepcopy

    ep = episode()
    original = source.evidence_sources([ep])
    prior_scope = deepcopy(original)
    if not same_scope:
        prior_scope[0]["content_hash"] = "superseded-source"
    old_account = accounts.SessionAccount(
        title="Old candidate",
        summary="Old unsupported assertion",
        claims=[],
        questions=[],
        useful=False,
    ).model_dump()
    p = NS(
        proposal_id="feedback",
        id="feedback",
        user_id="user",
        memory_space_id=None,
        state="queued",
        attempts=0,
        excluded_source_keys=[],
        source_kind="timeline",
        timezone="Asia/Kolkata",
        questions=[],
        correction_of=[],
        withdrawn=False,
        revision_feedback="Keep only what is useful and supported",
        supersedes_proposal_id="prior",
        inference_runs=[],
    )
    prior = NS(account=old_account, source_scope=prior_scope)
    monkeypatch.setattr(review, "distributed_lock", unlocked)
    monkeypatch.setattr(review.MemoryReviewProposal, "get", AsyncMock(return_value=p))
    monkeypatch.setattr(
        review.MemoryReviewProposal,
        "find_one",
        AsyncMock(
            side_effect=lambda query: (
                prior
                if isinstance(query, dict) and query.get("proposal_id") == "prior"
                else p
            )
        ),
    )
    monkeypatch.setattr(review, "persist_generation", AsyncMock())
    monkeypatch.setattr(
        review, "validate_selection", AsyncMock(return_value=([ep], []))
    )
    monkeypatch.setattr(review, "source_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(sessions, "publish_progress", AsyncMock())
    repaired = {
        "title": "Routine work",
        "summary": "No useful new fact",
        "claims": [],
        "questions": [],
        "useful": False,
    }

    def revise_seed(tools):
        assert tools.result is None
        assert "revise_result" in tools.available_tools
        assert "finish_task" not in tools.available_tools
        assert tools.draft["result"]["summary"] == (
            old_account["summary"] if same_scope else repaired["summary"]
        )
        tools.dispatch(
            "revise_result",
            {
                "edits": [
                    {"op": "replace", "path": "/title", "value": repaired["title"]},
                    {"op": "replace", "path": "/summary", "value": repaired["summary"]},
                ]
            },
        )

    calls = install_pi(
        monkeypatch,
        tmp_path,
        ([repaired] if not same_scope else [])
        + [revise_seed, review_result("ready", "Grounded")],
    )
    assert (
        await session_jobs.generate_session_memory_job.__wrapped__("feedback")
        == "no_changes"
    )
    assert p.accepted_context["review"]["verdict"] == "ready"
    assert old_account["summary"] == "Old unsupported assertion"
    assert any('"origin": "draft_review"' in c["prompt"] for c in calls)
    assert ("Old unsupported assertion" in calls[0]["prompt"]) == same_scope
    assert all(
        "Keep only what is useful and supported" not in s["excerpt"]
        for s in p.source_scope
    )
