"""Order-independent source scope and explicit account preparation contracts."""

from datetime import datetime, timedelta, timezone
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models.timeline import EvidenceLocator, TimelineEvidenceRef
from backend.services.timeline import memory_sources, session_accounts

START = datetime(2026, 9, 4, tzinfo=timezone.utc)


def make_episode(key, policy="auto", role="user_statement", evidence_id="shared"):
    reference = TimelineEvidenceRef(
        evidence_id=evidence_id,
        kind="transcript",
        locator=EvidenceLocator(
            capture_source_id="phone", modality="transcript", track_id="input"
        ),
        started_at=START,
        ended_at=START + timedelta(minutes=10),
        role=role,
        excerpt="We agreed\nto ship tomorrow.",
        content_hash=f"hash-{evidence_id}",
        metadata={"direction": "input"},
    )
    return SimpleNamespace(
        episode_key=key,
        started_at=reference.started_at,
        ended_at=reference.ended_at,
        evidence_refs=[reference],
        audio_ranges=[],
        memory_policy=policy,
        status="provisional",
    )


def make_account():
    return session_accounts.SessionAccount(
        title="Planning",
        summary="A delivery decision.",
        claims=[
            session_accounts.AccountClaim(
                text="We agreed to ship tomorrow.",
                source_keys=["stale-key"],
                personal=True,
                citations=[
                    session_accounts.SourceQuote(
                        source_key="source", quote="We agreed to ship tomorrow."
                    )
                ],
            )
        ],
        questions=[],
        useful=True,
    )


def account_source():
    source = memory_sources.evidence_sources([make_episode("one")])[0]
    return {**source, "key": "source"}


def test_shared_reference_exclusion_is_order_independent_and_include_is_explicit():
    episodes = [
        make_episode("exclude", "reference"),
        make_episode("remember", "remember"),
        make_episode("independent", evidence_id="independent"),
    ]
    results = [
        memory_sources.evidence_sources(order) for order in permutations(episodes)
    ]
    assert all(result == results[0] for result in results)
    assert {
        source["evidence_id"] for source in memory_sources.memory_sources(results[0])
    } == {"independent"}
    shared = next(source for source in results[0] if source["evidence_id"] == "shared")
    attribution = SimpleNamespace(
        action="attribute", role="third_party", created_at=START, sources=[shared]
    )
    attributed = memory_sources.apply_dispositions(results[0], [attribution])
    assert (
        next(source for source in attributed if source["evidence_id"] == "shared")[
            "participation"
        ]
        == "excluded"
    )
    decision = SimpleNamespace(action="include", created_at=START, sources=[shared])
    included = memory_sources.apply_dispositions(results[0], [decision])
    assert {
        source["evidence_id"] for source in memory_sources.memory_sources(included)
    } == {"shared", "independent"}


def test_shared_source_unions_capture_references_and_preserves_identity():
    episodes = [make_episode("one"), make_episode("two")]
    before = memory_sources.evidence_sources(episodes[:1])[0]
    for episode, chunk in zip(episodes, ("chunk-one", "chunk-two")):
        episode.audio_ranges = [
            SimpleNamespace(
                started_at=START,
                ended_at=START + timedelta(minutes=10),
                capture_source_id="phone",
                conversation_ids=[],
                chunk_ids=[chunk],
            )
        ]
    forward = memory_sources.evidence_sources(episodes)
    assert forward == memory_sources.evidence_sources(list(reversed(episodes)))
    assert forward[0]["key"] == before["key"]
    assert forward[0]["capture_chunk_ids"] == ["chunk-one", "chunk-two"]


def test_conflicting_attribution_is_uncertain_and_preserves_both_interpretations():
    episodes = [
        make_episode("personal", "remember"),
        make_episode("media", "remember", role="media_content"),
    ]
    forward = memory_sources.evidence_sources(episodes)
    assert forward == memory_sources.evidence_sources(list(reversed(episodes)))
    assert len(forward) == 1
    assert forward[0]["role"] == "uncertain"
    assert forward[0]["participation"] == "uncertain"
    assert {row["role"] for row in forward[0]["metadata"]["attribution_variants"]} == {
        "user_statement",
        "media_content",
    }


@pytest.mark.parametrize("valid", [True, False])
def test_account_validation_does_not_mutate_its_input_even_on_failure(valid):
    account = make_account()
    if not valid:
        account.claims.append(
            session_accounts.AccountClaim(
                text="Unsupported claim.", personal=True, source_keys=["missing"]
            )
        )
    original = account.model_dump()
    if valid:
        normalized = session_accounts.validate_account(account, [account_source()])
        assert normalized["claims"][0]["source_keys"] == ["source"]
        assert (
            normalized["claims"][0]["citations"][0]["quote"]
            == "We agreed\nto ship tomorrow."
        )
    else:
        with pytest.raises(ValueError):
            session_accounts.validate_account(account, [account_source()])
    assert account.model_dump() == original


@pytest.mark.asyncio
async def test_reviewer_receives_normalized_account_without_mutating_candidate(
    monkeypatch,
):
    from backend.services.timeline import pi_tasks

    account = make_account()
    original = account.model_dump()

    async def review(**request):
        claim = request["payload"]["candidate"]["claims"][0]
        assert claim["source_keys"] == ["source"]
        assert claim["citations"][0]["quote"] == "We agreed\nto ship tomorrow."
        return pi_tasks.TaskOutcome(
            result=session_accounts.AccountReview(
                checks=[
                    session_accounts.AccountReviewCheck(
                        target="claim", index=0, action="keep", reason="Supported."
                    )
                ],
                account_issues=[],
                reason="Ready.",
            ),
            context=dict(request.get("accepted_context") or {}),
        )

    monkeypatch.setattr(pi_tasks, "run_task", review)
    monkeypatch.setattr(pi_tasks, "settings", lambda: {"max_attempts": 1})
    result = await session_accounts.verify_session_claims(
        account, [account_source()], record=AsyncMock()
    )
    assert result.claims[0].source_keys == ["source"]
    assert account.model_dump() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_prior", [True, False])
async def test_feedback_account_is_reviewed_from_both_preparation_paths(
    monkeypatch, reuse_prior
):
    from backend.services.timeline import pi_tasks

    source = account_source()
    account = make_account()
    original = account.model_dump()
    inspected = []

    async def investigate(**request):
        inspected.append(request)
        if request["stage"] == "session_review":
            return pi_tasks.TaskOutcome(
                result=session_accounts.AccountReview(
                    checks=[
                        session_accounts.AccountReviewCheck(
                            target="claim", index=0, action="keep", reason="Supported."
                        )
                    ],
                    account_issues=[],
                    reason="Ready.",
                ),
                context=dict(request.get("accepted_context") or {}),
            )
        if request["initial_result"] is not None:
            candidate = request["initial_result"]
            assert candidate["claims"][0]["source_keys"] == ["source"]
            assert request["payload"]["review_findings"] == {
                "origin": "draft_review",
                "feedback": "Recheck the decision.",
            }
        return pi_tasks.TaskOutcome(
            result=account, context=dict(request.get("accepted_context") or {})
        )

    monkeypatch.setattr(pi_tasks, "run_task", investigate)
    monkeypatch.setattr(pi_tasks, "settings", lambda: {"max_attempts": 1})
    progress, stage = AsyncMock(), AsyncMock()
    result = await session_accounts.build_session_account(
        [source],
        record=AsyncMock(),
        progress=progress,
        stage=stage,
        prior_account=original if reuse_prior else None,
        revision_feedback="Recheck the decision.",
    )
    assert len(inspected) == (2 if reuse_prior else 3)
    assert inspected[-1]["stage"] == "session_review"
    assert result.claims[0].source_keys == ["source"]
    assert [call.args[0] for call in stage.await_args_list] == [
        "revising",
        "checking_claims",
    ]
    assert account.model_dump() == original


@pytest.mark.asyncio
async def test_oversized_account_reduction_keeps_every_assigned_source(monkeypatch):
    from backend.services.timeline import pi_tasks

    sources = [{**account_source(), "key": f"source-{index}"} for index in range(5)]
    seen = []

    async def investigate(**request):
        payload = request["payload"]
        if request["stage"] == "session_review":
            candidate = payload["candidate"]
            return pi_tasks.TaskOutcome(
                result=session_accounts.AccountReview(
                    checks=[
                        session_accounts.AccountReviewCheck(
                            target="claim",
                            index=index,
                            action="keep",
                            reason="Supported.",
                        )
                        for index in range(len(candidate["claims"]))
                    ],
                    account_issues=[],
                    reason="Ready.",
                ),
                context=dict(request.get("accepted_context") or {}),
            )
        if "partial_accounts" in payload:
            claims = [
                claim
                for account in payload["partial_accounts"]
                for claim in account["claims"]
            ]
        else:
            passages = payload["sources"]
            seen.extend(source["key"] for source in passages)
            claims = [
                session_accounts.AccountClaim(
                    text="A delivery decision.",
                    personal=True,
                    citations=[
                        session_accounts.SourceQuote(
                            source_key=source["key"], quote=source["excerpt"]
                        )
                    ],
                ).model_dump()
                for source in passages
            ]
        return pi_tasks.TaskOutcome(
            result=session_accounts.SessionAccount(
                title="Planning",
                summary="A delivery decision.",
                claims=claims,
                questions=[],
                useful=True,
            ),
            context=dict(request.get("accepted_context") or {}),
        )

    # Isolate combining behavior: each source is assigned whole, and every account
    # exceeds the preferred combining budget. The process must still converge.
    monkeypatch.setattr(
        session_accounts,
        "source_batches",
        lambda sources: ([source] for source in sources),
    )
    monkeypatch.setattr(session_accounts, "SOURCE_BUDGET", 10)
    monkeypatch.setattr(pi_tasks, "run_task", investigate)
    monkeypatch.setattr(pi_tasks, "settings", lambda: {"max_attempts": 1})
    result = await session_accounts.build_session_account(
        sources,
        record=AsyncMock(),
        progress=AsyncMock(),
    )
    assert seen == [source["key"] for source in sources]
    assert {key for claim in result.claims for key in claim.source_keys} == set(seen)
