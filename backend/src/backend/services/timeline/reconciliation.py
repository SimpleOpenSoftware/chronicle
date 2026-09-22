"""One reconciliation run over one dirty evidence range.

A run starts from the dirty interval plus five minutes of context per side, asks the
agent to *revise* the prior interpretation rather than rederive it, and either
publishes a revisioned generation, requests bounded device context, or parks until
future evidence exists.

Chronicle owns the budgets, staged validation barriers, and fencing; the model proposes
semantics:

- denser evidence is acquired only through a typed, bounded device-input request;
- separation resolves evidence and boundary anchors before interpretation runs;
- pins protect only their confirmed fields and never reserve elapsed-time territory;
- the publish compare-and-swaps on the episode revisions the bundle saw, so a run
  holding a stale leased snapshot cannot overwrite newer work.

Publication persists a complete journal, marks every affected day dirty, inserts exact
successor revisions before superseding named predecessors, and finally installs the
canonical day snapshots. Recovery rolls that same intent forward after a crash.

See ``docs/backend/rolling-reconciliation.md`` → "Agentic context expansion",
"Episode revisions and stable navigation", and "Settlement is a policy decision".
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Optional, Sequence
from zoneinfo import ZoneInfo

import backend.services.privacy as privacy
from backend.models.conversation import Conversation
from backend.models.timeline import (
    AudioEvidenceSpan,
    DirtyEvidenceRange,
    EpisodeRevisionRef,
    ResolvedBoundarySupport,
    TimelineAssertion,
    TimelineDay,
    TimelineEpisode,
    TimelineEvidenceRef,
    TimelineInterpretationRejectionState,
    TimelinePublicationDayPlan,
    TimelinePublicationEvidenceFence,
    TimelinePublicationOperation,
    utcnow,
)
from backend.models.user import User
from backend.services.inference_artifacts import canonical_hash
from backend.services.memory.visibility import conversation_scope_filter

from . import activity_policy
from .contracts import (
    AgentEpisode,
    EvidenceBundle,
    Publish,
    PublishResult,
    ReconcileAction,
    RequestMoreContext,
    WaitForFutureEvidence,
)
from .dirty_ranges import (
    DirtyRangeLeaseLost,
    bind_context_request,
    claim_range_publication_fence,
    complete_range,
    park_for_context,
    park_waiting,
    release_range_for_retry,
    update_leased_range_fields,
)
from .dispatch import (
    dispatch_classified_episodes,
    mark_episode_publications_dispatch_pending,
)
from .episode_bounds import speech_profile_for_range
from .evidence import load_reconciliation_evidence, summarize_immich_evidence
from .executor import build_range_executor
from .projection import active_day_episodes, affected_local_dates
from .publication import (
    PublicationConflict,
    apply_rejected_retry_operation,
    build_publication_operation,
    publish_timeline_revision,
    run_guarded_publication_action,
)
from .recording_refs import build_audio_ranges
from .snapshots import build_day_snapshot, evidence_state_hash_for_episodes

logger = logging.getLogger(__name__)

# Context Chronicle adds around the dirty interval before the first agent look.
CONTEXT_PADDING = timedelta(minutes=5)
# Settlement watermark: an episode ending within this of the newest evidence is still
# at the live edge, and this is also the quiet window a settled boundary needs.
SETTLE_QUIET_MINUTES = 10
MAX_INTERPRETATION_REJECTION_RETRIES = 2
INTERPRETATION_RETRY_DELAY = timedelta(seconds=30)

SettlementStatus = Literal["open", "provisional", "settled"]

LoadEvidence = Callable[..., Awaitable[EvidenceBundle]]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _evidence_end(item: Any) -> datetime:
    return _utc(item.ended_at or item.started_at)


async def _user_timezone(user_id: str) -> str:
    """The user's IANA timezone, or UTC when unknown."""

    try:
        user = await User.get(user_id)
    except Exception:
        logger.debug("Could not resolve timezone for user %s", user_id, exc_info=True)
        return "UTC"
    return (user.timezone if user and user.timezone else None) or "UTC"


# ── Settlement policy v1 ─────────────────────────────────────────────────────


def _newest_evidence_at(bundle: EvidenceBundle) -> datetime:
    ends = [_evidence_end(item) for item in bundle.manifest.evidence]
    return max(ends) if ends else _utc(bundle.manifest.ended_at)


def _closed_by_meeting(episode: AgentEpisode, bundle: EvidenceBundle) -> bool:
    """A meeting evidence item ending at this boundary is an authoritative close."""

    end = _utc(episode.ended_at)
    tolerance = timedelta(minutes=2)
    return any(
        item.kind == "meeting" and abs(_evidence_end(item) - end) <= tolerance
        for item in bundle.manifest.evidence
    )


def _evidence_after(
    bundle: EvidenceBundle, start: datetime, end: datetime
) -> list[Any]:
    return [
        item
        for item in bundle.manifest.evidence
        if item.kind != "capture_gap"
        and _utc(item.started_at) < end
        and _evidence_end(item) > start
    ]


async def _pending_prerequisites(user_id: str, start: datetime, end: datetime) -> bool:
    """Whether a known artifact for this interval has not finished yet.

    Exactly two queries, both scoped to intervals intersecting ``[start, end)``:

    - a ``Conversation`` whose ``processing_status`` is ``active`` or unset — its
      transcript, speakers, or summary may still change the range's meaning;
    - an ``AudioEvidenceSpan`` in state ``unscored`` — audio Chronicle has stored but
      has not profiled, so its silence and speech are unknown rather than quiet.
    """

    conversations = await Conversation.find(
        {
            "$and": [conversation_scope_filter()],
            "user_id": user_id,
            "started_at": {"$lt": end},
            "ended_at": {"$gt": start},
            "$or": [
                {"processing_status": Conversation.ConversationStatus.ACTIVE.value},
                {"processing_status": None},
                {"processing_status": {"$exists": False}},
            ],
        }
    ).count()
    if conversations:
        return True
    spans = await AudioEvidenceSpan.find(
        {
            "user_id": user_id,
            "state": "unscored",
            "started_at": {"$lt": end},
            "ended_at": {"$gt": start},
        }
    ).count()
    return bool(spans)


async def _boundary_is_quiet(end: datetime, window: timedelta) -> Optional[bool]:
    """Whether VAD says the ``window`` after ``end`` is quiet.

    ``None`` means the audio was never measured there, so the caller falls back to the
    evidence watermark alone.
    """

    try:
        profile = await speech_profile_for_range(end, end + window)
    except Exception:
        logger.debug("Speech profile unavailable at %s", end.isoformat(), exc_info=True)
        return None
    if not profile or not profile.measured_buckets:
        return None
    return profile.speech_buckets == 0


async def assess_settlement(
    episode: AgentEpisode,
    bundle: EvidenceBundle,
    now: Optional[datetime] = None,
    *,
    user_id: Optional[str] = None,
) -> SettlementStatus:
    """Settlement policy v1 for one published episode.

    ``open`` when the episode's right edge sits within ``SETTLE_QUIET_MINUTES`` of the
    newest evidence in the bundle — evidence is still accumulating there. Otherwise
    ``settled`` when both (a) ten quiet minutes follow the end, or a meeting bound
    closes it, and (b) no pending prerequisite intersects the interval; else
    ``provisional``.

    Where stored VAD covers the minutes after the boundary, quiet is confirmed from the
    bucket series rather than from the absence of assembled evidence; where it does not,
    the evidence watermark decides alone.
    """

    now = _utc(now) if now else utcnow()
    quiet = timedelta(minutes=SETTLE_QUIET_MINUTES)
    start, end = _utc(episode.started_at), _utc(episode.ended_at)
    horizon = min(_newest_evidence_at(bundle), now)

    if horizon - end < quiet:
        return "open"

    closed = _closed_by_meeting(episode, bundle)
    if not closed:
        measured_quiet = await _boundary_is_quiet(end, quiet)
        if measured_quiet is False:
            return "provisional"
        # ScreenPipe keeps producing frames and application observations while the
        # microphone is quiet. When VAD measured this exact post-boundary window, its
        # result is the authoritative speech signal; generic evidence after the
        # boundary must not keep every episode provisional forever. Only fall back to
        # the evidence watermark when audio was not measured there.
        if measured_quiet is None and _evidence_after(bundle, end, end + quiet):
            return "provisional"

    if user_id and await _pending_prerequisites(user_id, start, end):
        return "provisional"
    return "settled"


# ── Publishing one generation ────────────────────────────────────────────────


def _resolved_support(anchor: Any, *, artifact_hash: str) -> ResolvedBoundarySupport:
    earliest = _utc(anchor.earliest_at)
    latest = _utc(anchor.latest_at)
    return ResolvedBoundarySupport(
        anchor_id=anchor.anchor_id,
        evidence_id=anchor.evidence_id,
        locator=anchor.locator,
        support_type=anchor.support_type,
        resolved_source_position=anchor.source_position,
        earliest_at=earliest,
        latest_at=latest,
        resolved_at=earliest + (latest - earliest) / 2,
        uncertainty_seconds=(latest - earliest).total_seconds(),
        separation_artifact_hash=artifact_hash,
    )


def _evidence_ref(
    item: Any,
    *,
    start_support: list[ResolvedBoundarySupport] | None = None,
    end_support: list[ResolvedBoundarySupport] | None = None,
) -> TimelineEvidenceRef:
    return TimelineEvidenceRef(
        evidence_id=item.evidence_id,
        kind=item.kind,
        source_id=item.source_id,
        source_item_id=item.source_item_id,
        started_at=item.started_at,
        ended_at=item.ended_at,
        role=item.role,
        excerpt=item.excerpt,
        content_hash=item.content_hash,
        ephemeral=item.ephemeral,
        locator=item.locator,
        start_boundary_support=start_support or [],
        end_boundary_support=end_support or [],
        metadata=dict(getattr(item, "metadata", {}) or {}),
    )


async def _prior_rows(bundle: EvidenceBundle) -> list[TimelineEpisode]:
    episode_ids = [
        str(item.get("episode_id"))
        for item in bundle.existing_episodes
        if item.get("episode_id")
    ]
    if not episode_ids:
        return []
    return await TimelineEpisode.find({"episode_id": {"$in": episode_ids}}).to_list()


async def observed_revisions(bundle: EvidenceBundle) -> dict[str, int]:
    """The ``(episode_id → revision)`` snapshot this bundle reflects.

    ``load_reconciliation_evidence`` serializes only enough of a prior episode for the
    agent to read, so the fence's reference point is captured here, beside the load,
    rather than reconstructed at publish time when it would no longer be a snapshot.
    """

    return {row.episode_id: int(row.revision) for row in await _prior_rows(bundle)}


async def _has_newer_overlapping_evidence(
    dirty_range: DirtyEvidenceRange, bundle: EvidenceBundle
) -> bool:
    """Fence only evidence revisions whose dirty interval intersects this manifest."""

    leased = dirty_range.leased_evidence_revision
    if leased is None:
        return True
    newer = await DirtyEvidenceRange.find_one(
        {
            "user_id": dirty_range.user_id,
            "dirty_range_id": {"$ne": dirty_range.dirty_range_id},
            "evidence_revision": {"$gt": int(leased)},
            "started_at": {"$lt": _utc(bundle.manifest.ended_at)},
            "ended_at": {"$gt": _utc(bundle.manifest.started_at)},
            "state": {"$ne": "superseded"},
        }
    )
    return newer is not None


def _unchanged(prior: TimelineEpisode, episode: AgentEpisode) -> bool:
    """Whether the agent handed back the prior episode verbatim."""

    return (
        _utc(prior.started_at) == _utc(episode.started_at)
        and _utc(prior.ended_at) == _utc(episode.ended_at)
        and prior.kind == episode.kind
        and prior.title == episode.title
        and prior.summary == episode.summary
        and prior.conversational == episode.conversational
        and prior.salience == episode.salience
        and prior.activity_mode == episode.activity_mode
    )


def _rejected_retry_operations(
    *,
    dirty_range: DirtyEvidenceRange,
    action: Publish,
    interpretation_result_hash: str,
    stamp: datetime,
    start_sequence: int,
) -> list[TimelinePublicationOperation]:
    """Build deterministic, bounded retry intents for local interpretation failures."""

    hypotheses = {item.hypothesis_id: item for item in action.separation.hypotheses}
    parent_start = _utc(dirty_range.authorized_started_at or dirty_range.started_at)
    parent_end = _utc(dirty_range.authorized_ended_at or dirty_range.ended_at)
    operations: list[TimelinePublicationOperation] = []
    for rejection in sorted(
        action.interpretation.rejected, key=lambda item: item.hypothesis_id
    ):
        if rejection.reason_code == "redundant_activity":
            # Interpretation validation proves the complete new claim is already
            # covered by an accepted activity. There is no unresolved work to retry.
            continue
        hypothesis = hypotheses[rejection.hypothesis_id]
        started_at = max(parent_start, _utc(hypothesis.started_at))
        ended_at = min(parent_end, _utc(hypothesis.ended_at))
        if ended_at <= started_at:
            started_at, ended_at = parent_start, parent_end
        retry_depth = int(dirty_range.rejection_retry_depth) + 1
        exhausted = retry_depth > MAX_INTERPRETATION_REJECTION_RETRIES
        implicated = list(rejection.implicated_evidence_ids or hypothesis.evidence_ids)
        successor_id = canonical_hash(
            {
                "kind": "timeline-interpretation-rejection-retry-v1",
                "parent_dirty_range_id": dirty_range.dirty_range_id,
                "interpretation_result_hash": interpretation_result_hash,
                "hypothesis_id": rejection.hypothesis_id,
                "retry_depth": retry_depth,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "implicated_evidence_ids": implicated,
            }
        )
        request_id = canonical_hash(
            {
                "kind": "timeline-interpretation-rejection-request-v1",
                "root_reconciliation_request_id": dirty_range.reconciliation_request_id,
                "successor_dirty_range_id": successor_id,
            }
        )
        due_at = stamp if exhausted else stamp + INTERPRETATION_RETRY_DELAY
        last_error = (
            f"exhausted {MAX_INTERPRETATION_REJECTION_RETRIES} "
            f"interpretation rejection retries: {rejection.reason_code}"
            if exhausted
            else None
        )
        successor = DirtyEvidenceRange(
            dirty_range_id=successor_id,
            user_id=dirty_range.user_id,
            started_at=started_at,
            ended_at=ended_at,
            evidence_revision=int(
                dirty_range.leased_evidence_revision
                if dirty_range.leased_evidence_revision is not None
                else dirty_range.evidence_revision
            ),
            source_revisions={"interpretation_rejection": [interpretation_result_hash]},
            trigger_reasons=[
                f"interpretation_rejected:{rejection.reason_code}",
                f"hypothesis:{rejection.hypothesis_id}",
            ],
            not_before=due_at,
            force_after=due_at,
            state="failed" if exhausted else "authorized_pending",
            last_error=last_error,
            dispatch_authorized_at=dirty_range.dispatch_authorized_at,
            reconciliation_request_id=request_id,
            authorized_started_at=started_at,
            authorized_ended_at=ended_at,
            parent_dirty_range_id=dirty_range.dirty_range_id,
            rejection_retry_depth=retry_depth,
            rejection_hypothesis_id=rejection.hypothesis_id,
            rejection_reason_code=rejection.reason_code,
            rejection_evidence_ids=implicated,
            created_at=stamp,
            updated_at=stamp,
        )
        rejection_state = TimelineInterpretationRejectionState(
            hypothesis_id=rejection.hypothesis_id,
            reason_code=rejection.reason_code,
            explanation=rejection.explanation,
            implicated_evidence_ids=implicated,
            retry_depth=retry_depth,
            successor_dirty_range_id=successor_id,
            status="exhausted" if exhausted else "retry_scheduled",
            interpretation_result_hash=interpretation_result_hash,
            created_at=stamp,
        )
        operations.append(
            build_publication_operation(
                sequence=start_sequence + len(operations),
                kind="upsert_rejected_reconciliation_retry",
                payload={
                    "parent_dirty_range_id": dirty_range.dirty_range_id,
                    "rejection": rejection_state.model_dump(mode="python"),
                    "successor": successor.model_dump(mode="python", exclude={"id"}),
                },
            )
        )
    return operations


async def _build_document(
    *,
    episode: AgentEpisode,
    user_id: str,
    run_id: str,
    timezone_name: str,
    bundle: EvidenceBundle,
    episode_key: str,
    revision: int,
    predecessor_keys: list[str],
    status: SettlementStatus,
    evidence_revision: Optional[int],
    start_anchor_ids: Sequence[str] = (),
    end_anchor_ids: Sequence[str] = (),
    separation_inference_operation: str,
    separation_request_hash: str,
    separation_artifact_hash: str,
    separation_result_hash: str,
    interpretation_inference_operation: str,
    interpretation_request_hash: str,
    interpretation_artifact_hash: str,
    interpretation_result_hash: str,
) -> TimelineEpisode:
    evidence = {item.evidence_id: item for item in bundle.manifest.evidence}
    anchors = {item.anchor_id: item for item in bundle.manifest.anchors}
    start_by_evidence: dict[str, list[ResolvedBoundarySupport]] = {}
    end_by_evidence: dict[str, list[ResolvedBoundarySupport]] = {}
    for anchor_id in start_anchor_ids:
        anchor = anchors[anchor_id]
        start_by_evidence.setdefault(anchor.evidence_id, []).append(
            _resolved_support(anchor, artifact_hash=separation_artifact_hash)
        )
    for anchor_id in end_anchor_ids:
        anchor = anchors[anchor_id]
        end_by_evidence.setdefault(anchor.evidence_id, []).append(
            _resolved_support(anchor, artifact_hash=separation_artifact_hash)
        )
    refs = [
        _evidence_ref(
            evidence[evidence_id],
            start_support=start_by_evidence.get(evidence_id),
            end_support=end_by_evidence.get(evidence_id),
        )
        for evidence_id in episode.evidence_ids
        if evidence_id in evidence
    ]
    related_conversation_ids = sorted(
        set(episode.related_conversation_ids)
        | {
            str(ref.metadata["conversation_id"])
            for ref in refs
            if ref.metadata.get("conversation_id")
        }
    )
    zone = ZoneInfo(timezone_name)
    document = TimelineEpisode(
        episode_id=str(uuid.uuid4()),
        episode_key=episode_key,
        run_id=run_id,
        user_id=user_id,
        local_date=_utc(episode.started_at).astimezone(zone).date(),
        timezone=timezone_name,
        started_at=_utc(episode.started_at),
        ended_at=_utc(episode.ended_at),
        kind=episode.kind,
        title=episode.title,
        summary=episode.summary,
        conversational=episode.conversational,
        status=status,
        revision=revision,
        evidence_revision=evidence_revision,
        separation_inference_operation=separation_inference_operation,
        separation_request_hash=separation_request_hash,
        separation_artifact_hash=separation_artifact_hash,
        separation_result_hash=separation_result_hash,
        interpretation_inference_operation=interpretation_inference_operation,
        interpretation_request_hash=interpretation_request_hash,
        interpretation_artifact_hash=interpretation_artifact_hash,
        interpretation_result_hash=interpretation_result_hash,
        predecessor_keys=predecessor_keys,
        salience=episode.salience,
        confidence=episode.confidence,
        activity_mode=episode.activity_mode,
        entities=list(episode.entities),
        attributes={item.key: item.value for item in episode.attributes},
        assertions=[
            TimelineAssertion(**assertion.model_dump())
            for assertion in episode.assertions
        ],
        evidence_refs=refs,
        source_ids=sorted({ref.source_id for ref in refs if ref.source_id}),
        related_conversation_ids=related_conversation_ids,
    )
    document.audio_ranges = await build_audio_ranges(
        started_at=document.started_at,
        ended_at=document.ended_at,
        evidence_refs=document.evidence_refs,
        related_conversation_ids=document.related_conversation_ids,
    )
    return document


async def publish_reconciliation(
    user_id: str,
    dirty_range: DirtyEvidenceRange,
    bundle: EvidenceBundle,
    action: Publish,
    **kwargs: Any,
) -> PublishResult:
    """Publish one validated staged result through the crash-safe journal."""

    snapshot = await privacy.load_snapshot(user_id)
    if any(not snapshot.permits_record(item) for item in bundle.manifest.evidence):
        raise privacy.PrivacyHeld()

    return await _publish_reconciliation_locked(
        user_id, dirty_range, bundle, action, **kwargs
    )


async def _publish_reconciliation_locked(
    user_id: str,
    dirty_range: DirtyEvidenceRange,
    bundle: EvidenceBundle,
    action: Publish,
    *,
    observed: Optional[dict[str, int]] = None,
    timezone_name: Optional[str] = None,
    dispatch_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    now: Optional[datetime] = None,
) -> PublishResult:
    """Resolve explicit lineage and journal exact successor/supersession operations."""

    timezone_name = timezone_name or await _user_timezone(user_id)
    if await _has_newer_overlapping_evidence(dirty_range, bundle):
        logger.info(
            "🩹 Fenced range %s by newer evidence inside its bounded manifest",
            dirty_range.dirty_range_id,
        )
        return PublishResult(fenced=False)
    priors = await _prior_rows(bundle)
    observed = (
        observed
        if observed is not None
        else {row.episode_id: int(row.revision) for row in priors}
    )
    evidence_revision = dirty_range.leased_evidence_revision

    # (1) Compare the current rows against the snapshot the bundle reflects. A row that
    # moved on, or one written by a *newer* evidence revision, means this run is stale.
    for row in priors:
        expected = observed.get(row.episode_id)
        if expected is not None and int(row.revision) != expected:
            logger.info(
                "🩹 Fenced: episode %s moved to revision %s (bundle saw %s)",
                row.episode_id,
                row.revision,
                expected,
            )
            return PublishResult(fenced=False)
        if (
            evidence_revision is not None
            and row.evidence_revision is not None
            and int(row.evidence_revision) > int(evidence_revision)
        ):
            logger.info(
                "🩹 Fenced: episode %s carries evidence revision %s, newer than the "
                "leased snapshot %s",
                row.episode_id,
                row.evidence_revision,
                evidence_revision,
            )
            return PublishResult(fenced=False)

    prior_by_revision = {(row.episode_key, int(row.revision)): row for row in priors}
    accepted_ids = {item.hypothesis_id for item in action.interpretation.accepted}
    hypotheses = [
        item
        for item in action.separation.hypotheses
        if item.hypothesis_id in accepted_ids
    ]
    if len(hypotheses) != len(action.projection.episodes):
        raise ValueError("staged publication lost its hypothesis-to-episode join")

    run_id = f"rolling:{dirty_range.dirty_range_id}"
    separation_result_hash = canonical_hash(
        {
            "schema_version": "timeline-separation-result-v1",
            "dirty_range_id": dirty_range.dirty_range_id,
            "leased_evidence_revision": evidence_revision,
            "manifest_hash": bundle.manifest.evidence_revision,
            "result": action.separation.model_dump(mode="json"),
        }
    )
    interpretation_result_hash = canonical_hash(
        {
            "schema_version": "timeline-interpretation-result-v1",
            "dirty_range_id": dirty_range.dirty_range_id,
            "separation_result_hash": separation_result_hash,
            "result": action.interpretation.model_dump(mode="json"),
        }
    )
    stamp = _utc(now) if now else utcnow()
    rejected_retry_operations = _rejected_retry_operations(
        dirty_range=dirty_range,
        action=action,
        interpretation_result_hash=interpretation_result_hash,
        stamp=stamp,
        start_sequence=0,
    )
    policy_start = min(
        [
            bundle.manifest.started_at,
            *[
                (
                    row.started_at.replace(tzinfo=timezone.utc)
                    if row.started_at.tzinfo is None
                    else row.started_at
                )
                for row in priors
            ],
        ]
    )
    policy_end = max(
        [
            bundle.manifest.ended_at,
            *[
                (
                    row.ended_at.replace(tzinfo=timezone.utc)
                    if row.ended_at.tzinfo is None
                    else row.ended_at
                )
                for row in priors
            ],
        ]
    )
    policy_days = await activity_policy.activity_policy_days(
        user_id, policy_start, policy_end, timezone_name
    )
    policy_by_date = {day.local_date: day for day in policy_days}
    decisions = [decision for day in policy_days for decision in day.review_decisions]
    evidence_by_id = {item.evidence_id: item for item in bundle.manifest.evidence}
    documents: list[TimelineEpisode] = []
    supersession_successors: dict[tuple[str, int], list[str]] = {}
    status_updates: list[tuple[TimelineEpisode, SettlementStatus]] = []
    status_rank = {"open": 0, "provisional": 1, "settled": 2}

    for hypothesis, episode in zip(hypotheses, action.projection.episodes, strict=True):
        predecessor_refs = [
            (item.episode_key, int(item.revision))
            for item in hypothesis.lineage.predecessor_revisions
        ]
        predecessors = [prior_by_revision[item] for item in predecessor_refs]
        candidate_evidence = [
            evidence_by_id[item]
            for item in episode.evidence_ids
            if item in evidence_by_id
        ]
        human_label = any(
            {"title", "kind"} & set(prior.confirmed_fields) for prior in predecessors
        )
        if (
            not human_label
            and activity_policy.recording_only_evidence(candidate_evidence)
        ) or activity_policy.rejected_activity(
            episode.started_at, episode.ended_at, candidate_evidence, decisions
        ):
            # Bad model output must not replace a valid, unrelated predecessor.
            for prior in predecessors:
                if activity_policy.episode_is_recording_only(
                    prior
                ) or activity_policy.rejected_activity(
                    prior.started_at, prior.ended_at, prior.evidence_refs, decisions
                ):
                    supersession_successors.setdefault(
                        (prior.episode_key, int(prior.revision)), []
                    )
            continue
        if hypothesis.lineage.action == "carry":
            prior = predecessors[0]
            if _unchanged(prior, episode):
                status = await assess_settlement(episode, bundle, now, user_id=user_id)
                if status_rank.get(status, -1) > status_rank.get(prior.status, 2):
                    status_updates.append((prior, status))
                continue
            episode_key = prior.episode_key
            revision = int(prior.revision) + 1
            predecessor_keys = list(prior.predecessor_keys)
        else:
            episode_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"chronicle:{user_id}:{dirty_range.dirty_range_id}:"
                    f"{hypothesis.hypothesis_id}",
                )
            )
            revision = 1
            predecessor_keys = [item.episode_key for item in predecessors]
        status = await assess_settlement(episode, bundle, now, user_id=user_id)
        document = await _build_document(
            episode=episode,
            user_id=user_id,
            run_id=run_id,
            timezone_name=timezone_name,
            bundle=bundle,
            episode_key=episode_key,
            revision=revision,
            predecessor_keys=predecessor_keys,
            status=status,
            evidence_revision=evidence_revision,
            start_anchor_ids=hypothesis.start_anchor_ids,
            end_anchor_ids=hypothesis.end_anchor_ids,
            separation_inference_operation=action.separation_inference.operation,
            separation_request_hash=action.separation_inference.request_hash,
            separation_artifact_hash=action.separation_inference.artifact_hash,
            separation_result_hash=separation_result_hash,
            interpretation_inference_operation=action.interpretation_inference.operation,
            interpretation_request_hash=action.interpretation_inference.request_hash,
            interpretation_artifact_hash=action.interpretation_inference.artifact_hash,
            interpretation_result_hash=interpretation_result_hash,
        )
        documents.append(document)
        for predecessor in predecessors:
            supersession_successors.setdefault(
                (predecessor.episode_key, int(predecessor.revision)), []
            ).append(episode_key if episode_key != predecessor.episode_key else "")

    # Omission by the model does not preserve recorder bookkeeping as an activity.
    for prior in priors:
        if activity_policy.episode_is_recording_only(prior):
            supersession_successors.setdefault(
                (prior.episode_key, int(prior.revision)), []
            )

    for retirement in action.separation.retirements:
        key = (
            retirement.predecessor_revision.episode_key,
            int(retirement.predecessor_revision.revision),
        )
        if key not in prior_by_revision:
            raise ValueError(
                f"retirement predecessor {key!r} is not in the fenced bundle"
            )
        supersession_successors.setdefault(key, [])

    collection = TimelineEpisode.get_pymongo_collection()

    async def apply_status_updates() -> list[str]:
        transitioned: list[str] = []
        for prior, status in status_updates:
            updated = await collection.update_one(
                {
                    "episode_id": prior.episode_id,
                    "revision": int(prior.revision),
                    "status": prior.status,
                },
                {"$set": {"status": status, "revised_at": stamp}},
            )
            if updated.modified_count == 1 and status == "settled":
                transitioned.append(prior.episode_id)
        return transitioned

    await update_leased_range_fields(
        dirty_range,
        {
            "base_manifest_hash": bundle.manifest.evidence_revision,
            "separation_result_hash": separation_result_hash,
            "interpretation_result_hash": interpretation_result_hash,
            "separation_inference_operation": action.separation_inference.operation,
            "separation_request_hash": action.separation_inference.request_hash,
            "separation_artifact_hash": action.separation_inference.artifact_hash,
            "interpretation_inference_operation": action.interpretation_inference.operation,
            "interpretation_request_hash": action.interpretation_inference.request_hash,
            "interpretation_artifact_hash": action.interpretation_inference.artifact_hash,
        },
        action="recording inference provenance",
    )

    async def claim_publication() -> None:
        await claim_range_publication_fence(dirty_range)
        if await _has_newer_overlapping_evidence(dirty_range, bundle):
            raise DirtyRangeLeaseLost(
                f"dirty range {dirty_range.dirty_range_id} was invalidated before publication"
            )

    if not documents and not supersession_successors and not rejected_retry_operations:
        dispatch = dispatch_fn or dispatch_classified_episodes

        async def apply_statuses_and_dispatch() -> list[str]:
            settling_refs = [
                (prior.episode_key, int(prior.revision))
                for prior, status in status_updates
                if status == "settled"
            ]
            await mark_episode_publications_dispatch_pending(user_id, settling_refs)
            transitioned = await apply_status_updates()
            if transitioned:
                try:
                    await dispatch(user_id, transitioned)
                except Exception:
                    logger.error(
                        "❌ Settled-episode dispatch failed after lifecycle update for %s",
                        dirty_range.dirty_range_id,
                        exc_info=True,
                    )
            return transitioned

        try:
            await run_guarded_publication_action(
                user_id,
                publication_guard=claim_publication,
                action=apply_statuses_and_dispatch,
            )
        except DirtyRangeLeaseLost:
            return PublishResult(fenced=False)
        logger.debug(
            "🩹 Reconciliation of %s produced no material change",
            dirty_range.dirty_range_id,
        )
        return PublishResult(fenced=True, material_change=False)

    superseded_rows = [prior_by_revision[key] for key in supersession_successors]
    dates = sorted(
        {
            local_date
            for row in [*documents, *superseded_rows]
            for local_date in affected_local_dates(
                row.started_at, row.ended_at, timezone_name
            )
        }
    )
    operations: list[TimelinePublicationOperation] = []
    for document in documents:
        operations.append(
            build_publication_operation(
                sequence=len(operations),
                kind="insert_episode_revision",
                expected_revision=int(document.revision),
                payload=document.model_dump(mode="python", exclude={"id"}),
            )
        )
    for key, raw_successors in supersession_successors.items():
        prior = prior_by_revision[key]
        successors = sorted({item for item in raw_successors if item})
        operations.append(
            build_publication_operation(
                sequence=len(operations),
                kind="supersede_episode_revision",
                expected_revision=int(prior.revision),
                payload={
                    "episode_id": prior.episode_id,
                    "episode_key": prior.episode_key,
                    "revision": int(prior.revision),
                    "successor_keys": successors,
                    "revised_at": stamp,
                },
            )
        )
    operations.extend(
        operation.model_copy(update={"sequence": len(operations) + index})
        for index, operation in enumerate(rejected_retry_operations)
    )
    operations = [
        build_publication_operation(
            sequence=index,
            kind=operation.kind,
            payload=operation.payload,
            expected_revision=operation.expected_revision,
        )
        for index, operation in enumerate(operations)
    ]

    day_plans: list[TimelinePublicationDayPlan] = []
    superseded_ids = {item.episode_id for item in superseded_rows}
    for local_date in dates:
        current = await active_day_episodes(user_id, local_date, timezone_name)
        prospective = [
            item for item in current if item.episode_id not in superseded_ids
        ]
        for document in documents:
            if local_date in affected_local_dates(
                document.started_at, document.ended_at, timezone_name
            ):
                prospective.append(document)
        # Reuse the snapshot read with the rejection decisions. A concurrent human
        # decision changes this snapshot and the publication CAS rejects the run.
        day = policy_by_date.get(local_date)
        group_refs = (
            list(day.current_snapshot.semantic_group_revisions)
            if day is not None and day.current_snapshot is not None
            else []
        )
        snapshot = build_day_snapshot(
            user_id=user_id,
            local_date=local_date,
            timezone_name=timezone_name,
            evidence_state_hash=evidence_state_hash_for_episodes(
                prospective,
                authorized_range_revisions={
                    dirty_range.dirty_range_id: evidence_revision or 0
                },
            ),
            episode_revisions=[
                EpisodeRevisionRef(
                    episode_key=item.episode_key, revision=int(item.revision)
                )
                for item in prospective
            ],
            semantic_group_revisions=group_refs,
        )
        day_plans.append(
            TimelinePublicationDayPlan(
                local_date=local_date,
                timezone=timezone_name,
                base_snapshot_id=day.current_snapshot_id if day else None,
                resulting_snapshot=snapshot,
            )
        )

    async def apply_operation(operation: TimelinePublicationOperation) -> str:
        payload = operation.payload
        if operation.kind == "upsert_rejected_reconciliation_retry":
            return await apply_rejected_retry_operation(user_id, operation)
        if operation.kind == "insert_episode_revision":
            existing = await TimelineEpisode.find_one(
                TimelineEpisode.episode_id == payload["episode_id"]
            )
            if existing is not None:
                return (
                    "already_applied"
                    if existing.episode_key == payload["episode_key"]
                    and int(existing.revision) == int(payload["revision"])
                    else "conflict"
                )
            await TimelineEpisode.model_validate(payload).insert()
            return "applied"
        if operation.kind != "supersede_episode_revision":
            return "conflict"
        existing = await TimelineEpisode.find_one(
            TimelineEpisode.episode_id == payload["episode_id"]
        )
        if existing is None or int(existing.revision) != int(payload["revision"]):
            return "conflict"
        if existing.status == "superseded":
            return (
                "already_applied"
                if sorted(existing.successor_keys) == sorted(payload["successor_keys"])
                else "conflict"
            )
        updated = await collection.update_one(
            {
                "episode_id": payload["episode_id"],
                "revision": int(payload["revision"]),
                "status": existing.status,
            },
            {
                "$set": {
                    "status": "superseded",
                    "successor_keys": payload["successor_keys"],
                    "revised_at": datetime.fromisoformat(payload["revised_at"]),
                }
            },
        )
        return "applied" if updated.modified_count == 1 else "conflict"

    if dirty_range.lease_owner is None or dirty_range.leased_evidence_revision is None:
        raise DirtyRangeLeaseLost(
            f"dirty range {dirty_range.dirty_range_id} has no publication lease"
        )
    evidence_fence = TimelinePublicationEvidenceFence(
        dirty_range_id=dirty_range.dirty_range_id,
        lease_owner=dirty_range.lease_owner,
        lease_attempt=int(dirty_range.attempts),
        leased_evidence_revision=int(dirty_range.leased_evidence_revision),
        started_at=dirty_range.started_at,
        ended_at=dirty_range.ended_at,
    )
    try:
        await publish_timeline_revision(
            user_id=user_id,
            operation_source="agent",
            affected_days=day_plans,
            operations=operations,
            apply_operation=apply_operation,
            publication_guard=claim_publication,
            evidence_fence=evidence_fence,
        )
    except (DirtyRangeLeaseLost, PublicationConflict):
        return PublishResult(fenced=False)
    await update_leased_range_fields(
        dirty_range,
        {
            "published_snapshot_ids": [
                item.resulting_snapshot.snapshot_id for item in day_plans
            ]
        },
        action="recording published snapshots",
    )
    dispatch = dispatch_fn or dispatch_classified_episodes

    async def apply_statuses_and_dispatch() -> list[str]:
        transitioned = await apply_status_updates()
        try:
            await dispatch(
                user_id,
                transitioned + [document.episode_id for document in documents],
            )
        except Exception:
            logger.error(
                "❌ Settled-episode dispatch failed after publishing %s",
                dirty_range.dirty_range_id,
                exc_info=True,
            )
        return transitioned

    try:
        await run_guarded_publication_action(
            user_id,
            publication_guard=claim_publication,
            action=apply_statuses_and_dispatch,
        )
    except DirtyRangeLeaseLost:
        # The graph revision committed while its fence was valid. A later evidence
        # mutation owns the next reconciliation, but must not let this stale attempt
        # advance or dispatch lifecycle state after the publication lock was released.
        pass

    return PublishResult(
        episode_ids=[document.episode_id for document in documents],
        episode_keys=[document.episode_key for document in documents],
        superseded_episode_ids=[row.episode_id for row in superseded_rows],
        affected_local_dates=dates,
        fenced=True,
        material_change=bool(documents or supersession_successors),
    )


# ── The run ──────────────────────────────────────────────────────────────────


async def reconcile_range(
    dirty_range: DirtyEvidenceRange,
    *,
    executor: Any = None,
    load_evidence: Optional[LoadEvidence] = None,
    now: Optional[datetime] = None,
    accounting: Optional[list[dict[str, Any]]] = None,
) -> Optional[PublishResult]:
    """Reconcile one leased dirty range. ``None`` means the range was parked.

    ``accounting`` is an optional caller-supplied sink for the exact inspected-evidence
    record — one entry per iteration with its bounds and evidence count. The same record
    is logged; the sink exists so a caller (and a test) can assert on it without
    scraping logs.
    """

    load = load_evidence or load_reconciliation_evidence
    runner = executor if executor is not None else build_range_executor()
    timezone_name = await _user_timezone(dirty_range.user_id)
    log: list[dict[str, Any]] = accounting if accounting is not None else []

    bounds = (
        _utc(dirty_range.started_at) - CONTEXT_PADDING,
        _utc(dirty_range.ended_at) + CONTEXT_PADDING,
    )
    bundle: Optional[EvidenceBundle] = None
    snapshot: dict[str, int] = {}

    while True:
        if bundle is None:
            bundle = await load(
                dirty_range.user_id,
                bounds[0],
                bounds[1],
                timezone_name=timezone_name,
                evidence_revision=dirty_range.leased_evidence_revision or 0,
            )
            snapshot = await observed_revisions(bundle)
            immich = summarize_immich_evidence(bundle.manifest)
            log.append(
                {
                    "iteration": len(log),
                    "started_at": bounds[0].isoformat(),
                    "ended_at": bounds[1].isoformat(),
                    "evidence_count": len(bundle.manifest.evidence),
                    "immich_evidence": immich.model_dump(mode="json"),
                }
            )

        action: ReconcileAction = await runner.reconcile(
            bundle, validation_feedback=None
        )

        if isinstance(action, WaitForFutureEvidence):
            logger.info(
                "🩹 Range %s waits for future evidence: %s",
                dirty_range.dirty_range_id,
                action.reason,
            )
            await park_waiting(dirty_range, action.reason)
            return None

        if isinstance(action, RequestMoreContext):
            request = bind_context_request(dirty_range, action.request)
            if request.base_manifest_hash != bundle.manifest.evidence_revision:
                raise ValueError("context request does not match the bounded manifest")
            if request.leased_evidence_revision != dirty_range.leased_evidence_revision:
                raise ValueError(
                    "context request does not match the leased evidence fence"
                )
            logger.info(
                "🩹 Range %s awaiting bounded context %s: %s",
                dirty_range.dirty_range_id,
                request.context_request_id,
                request.reason,
            )
            await park_for_context(dirty_range, request)
            return None

        assert isinstance(action, Publish)
        fresh_bundle = await load(
            dirty_range.user_id,
            bounds[0],
            bounds[1],
            timezone_name=timezone_name,
            evidence_revision=dirty_range.leased_evidence_revision or 0,
        )
        if fresh_bundle.manifest.evidence_revision != bundle.manifest.evidence_revision:
            logger.info(
                "🩹 Range %s manifest changed before publish (%s -> %s)",
                dirty_range.dirty_range_id,
                bundle.manifest.evidence_revision,
                fresh_bundle.manifest.evidence_revision,
            )
            return PublishResult(fenced=False)
        logger.info(
            "🩹 Range %s publishing %d episode(s): %s",
            dirty_range.dirty_range_id,
            len(action.projection.episodes),
            log,
        )
        return await publish_reconciliation(
            dirty_range.user_id,
            dirty_range,
            bundle,
            action,
            observed=snapshot,
            timezone_name=timezone_name,
            now=now,
        )


async def finish_range(
    dirty_range: DirtyEvidenceRange, outcome: Optional[PublishResult]
) -> str:
    """Terminate a leased range from a reconciliation outcome.

    ``None`` means the run already parked the range. A fenced publish is not a failure:
    the trigger that raced this run left a fresh pending range, so the interval is
    reconciled again with the newer snapshot.
    """

    if outcome is None:
        return dirty_range.state
    if not outcome.fenced:
        await release_range_for_retry(
            dirty_range, "fenced by newer overlapping evidence revision"
        )
        return "authorized_pending"
    await complete_range(dirty_range)
    return "completed"
