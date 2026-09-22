"""Bounded, attributed session accounts. Exact model exchanges are retained."""

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, Field

VERSION = "session-account-pi-v4"
SOURCE_BUDGET = 48000
ACCOUNT_TEXT_BUDGET = 18000
REVIEW_TEXT_BUDGET = 24000
_work_deadline = ContextVar("session_account_deadline", default=None)


class CitationMismatch(ValueError):
    """A generated citation did not occur contiguously in its source."""

    def __init__(self, message, source_fields=()):
        super().__init__(message)
        self.source_fields = list(source_fields)


class AccountWorkPending(Exception):
    """Completed source work is durable; continue it in a subsequent queued job."""


@contextmanager
def account_work_budget(seconds=300):
    token = _work_deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _work_deadline.reset(token)


def check_work_budget():
    deadline = _work_deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise AccountWorkPending()


def remaining_work_seconds():
    deadline = _work_deadline.get()
    return max(0, deadline - time.monotonic()) if deadline is not None else None


class SourceQuote(BaseModel):
    source_key: str = Field(
        description="Exact key of the supporting source in the evidence inventory"
    )
    quote: str = Field(
        min_length=1,
        description="A contiguous verbatim passage from the assigned evidence; no paraphrases or ellipses",
    )


class AccountClaim(BaseModel):
    text: str = Field(min_length=1, description="A concise candidate memory fact")
    source_keys: list[str] = Field(
        default_factory=list,
        description="Assigned evidence keys only; accepted vault notes are context, not new evidence",
    )
    personal: bool = Field(
        description="Whether this asserts a fact about a person or their activity. False does not make uncertain evidence eligible."
    )
    citations: list[SourceQuote] = Field(default_factory=list, max_length=6)


class SourceRelationship(BaseModel):
    source_keys: list[str] = Field(min_length=2, max_length=8)
    relationship: Literal[
        "supporting_capture",
        "duplicate",
        "complementary_speakers",
        "unrelated_activity",
        "unresolved",
    ]
    reason: str = Field(min_length=1, description="Concise source-backed explanation")


class SessionAccount(BaseModel):
    """A concise account with at most 18000 characters of narrative, questions,
    relationship reasons and quoted evidence in total. Source identifiers do not
    count toward this content budget.
    """

    title: str = Field(min_length=1, description="A short, descriptive session title")
    summary: str = Field(
        description="Concise account, including relevant evidence limitations or reasons for excluding background material",
    )
    claims: list[AccountClaim] = Field(
        max_length=30,
        description="Only useful candidate memory facts with eligible supporting quotations. Put evidence-quality analysis and exclusion reasons in summary, not claims. Uncertain sources cannot support claims, even when personal is false.",
    )
    questions: list[str] = Field(max_length=8)
    useful: bool
    relationships: list[SourceRelationship] = Field(default_factory=list, max_length=20)


def source_batches(sources):
    """Split large excerpts without dropping bytes or their source attribution."""
    # Defer this dependency to break the import cycle through
    # backend.services.timeline.pi_tasks -> backend.services.timeline.session_accounts.
    from .pi_tasks import source_header

    batch, size = [], 0
    for source in sources:
        text = source["excerpt"]
        header = source_header(source)
        capacity = max(
            1, SOURCE_BUDGET - len(json.dumps(header, ensure_ascii=False)) - 200
        )
        pieces, offset = [], 0
        while offset < len(text):
            end = min(len(text), offset + capacity)
            if end < len(text):
                boundary = text.rfind("\n", offset + capacity // 2, end)
                if boundary >= 0:
                    end = boundary + 1
            pieces.append((offset, text[offset:end]))
            offset = end
        for index, (offset, piece) in enumerate(pieces or [(0, "")]):
            item = dict(header)
            item.update(
                excerpt=piece,
                excerpt_part=index + 1,
                excerpt_parts=len(pieces),
                excerpt_offset=offset,
            )
            cost = len(json.dumps(item, ensure_ascii=False))
            if batch and size + cost > SOURCE_BUDGET:
                yield batch
                batch, size = [], 0
            batch.append(item)
            size += cost
    if batch:
        yield batch


async def prepare_source_account(passages, sources, *, record, accepted_context=None):
    """Investigate one assigned group; other sources remain inspection context."""
    return await run_account_task(
        instruction="Prepare a grounded, concise account of the assigned source passages for personal memory review. Assess useful information and unresolved questions using relevant accepted knowledge. Claims must be supported within claim_passages. Other authorized evidence is context; it need not be investigated exhaustively. Partial accounts will be combined into one session account.",
        payload={**claim_scope_payload(passages), "sources": passages},
        briefing_details={},
        sources=sources,
        claim_sources=passages,
        record=record,
        accepted_context=accepted_context,
    )


async def combine_partial_accounts(accounts, sources, *, record, accepted_context=None):
    """Combine a bounded group of accounts, preserving access to their sources."""
    payload = {"partial_accounts": [account.model_dump() for account in accounts]}
    return await run_account_task(
        instruction="Combine the supplied partial_accounts into one concise session account for memory review. Preserve useful supported information, reconcile overlapping claims, and resolve consequential questions using original evidence and relevant accepted knowledge. Inspect source details as needed to check the combined account.",
        payload={**payload, **claim_scope_payload(sources)},
        briefing_details=payload,
        sources=sources,
        claim_sources=sources,
        record=record,
        accepted_context=accepted_context,
    )


async def revise_account(account, findings, sources, *, record, accepted_context=None):
    """Start from a saved candidate, then apply feedback through editable draft tools."""
    candidate = account.model_dump()
    payload = {"candidate": candidate, "review_findings": findings}
    return await run_account_task(
        instruction="Revise the supplied session account using the independent review findings, original evidence and relevant accepted knowledge. Preserve useful grounded claims and consequential unresolved questions.",
        payload={**claim_scope_payload(sources), **payload},
        briefing_details=payload,
        initial_result=candidate,
        sources=sources,
        claim_sources=sources,
        record=record,
        accepted_context=accepted_context,
    )


def claim_scope_payload(passages):
    """Expose the permitted claim sources and their exact assigned text offsets."""
    return {
        "claim_source_keys": [
            passage["key"]
            for passage in passages
            if passage["participation"] == "supporting"
        ],
        "claim_passages": [
            {
                "key": passage["key"],
                "offset": passage.get("excerpt_offset", 0),
                "length": len(passage["excerpt"]),
            }
            for passage in passages
        ],
    }


async def run_account_task(
    *,
    instruction,
    payload,
    briefing_details,
    sources,
    claim_sources,
    record,
    accepted_context=None,
    initial_result=None,
):
    """Run the shared account contract; the caller explicitly selects the operation."""
    # Defer this dependency to break the import cycle through
    # backend.services.timeline.pi_tasks -> backend.services.timeline.session_accounts.
    from .pi_tasks import run_task

    check_work_budget()
    source_scope = [
        {
            "key": source["key"],
            "offset": source.get("excerpt_offset", 0),
            "length": len(source["excerpt"]),
            "role": source["role"],
            "participation": source["participation"],
            "claim_use": claim_use(source),
        }
        for source in claim_sources
    ]
    briefing = {
        "perspective": "Account for the owner of this personal archive. Resolve references using accepted knowledge; a speaker label alone does not establish that the speaker is the owner.",
        "source_scope": source_scope,
        **briefing_details,
    }
    outcome = await run_task(
        stage="session_account",
        operation="timeline_session_account",
        instruction=instruction,
        payload=payload,
        sources=sources,
        briefing=briefing,
        initial_result=initial_result,
        result_type=SessionAccount,
        accepted_context=accepted_context,
        record=record,
        validate=lambda account: validate_account(
            account, sources, claim_sources=claim_sources
        ),
    )
    if accepted_context is not None:
        accepted_context.update(outcome.context)
    # The harness callback validates; its return value is not the task result.
    # Normalize explicitly so the reviewer and writer receive original source spans.
    return normalized_account(outcome.result, sources, claim_sources=claim_sources)


PERSONAL_ROLES = {"user_statement", "user_action", "third_party", "application_state"}


def claim_use(source):
    """Expose the validator's eligibility contract before a model drafts claims."""
    if source["participation"] == "uncertain":
        return "context_only: attribution unresolved"
    if source["role"] in PERSONAL_ROLES:
        return "candidate_claim: preserve the source's attribution and limits"
    return "nonpersonal_only: cannot establish a person's activity"


def normalized_account(account, sources, *, claim_sources=None):
    """Return an independent validated account with canonical source quotations."""
    return SessionAccount.model_validate(
        validate_account(account, sources, claim_sources=claim_sources)
    )


def validate_account(account, sources, *, claim_sources=None):
    """Validate a copy and return normalized data, leaving the candidate untouched."""
    account = account.model_copy(deep=True)
    by_key = {s["key"]: s for s in sources}
    owned = sources if claim_sources is None else claim_sources
    owned_keys = {s["key"] for s in owned}
    errors, citation_errors, source_fields = [], [], []
    for index, relationship in enumerate(account.relationships, 1):
        if not set(relationship.source_keys) <= by_key.keys():
            errors.append(
                f"Relationship {index}: Source relationship cites unknown evidence"
            )
    for index, claim in enumerate(account.claims, 1):
        prefix = f"Claim {index}: "
        if claim.citations:
            claim.source_keys = list(
                dict.fromkeys(c.source_key for c in claim.citations)
            )
        if not set(claim.source_keys) <= by_key.keys():
            errors.append(
                prefix
                + f"Session account cites unknown sources: {sorted(set(claim.source_keys) - by_key.keys())}"
            )
            continue
        if not set(claim.source_keys) <= owned_keys:
            errors.append(
                prefix
                + f"Claim cites context outside this task's assigned passages. Eligible claim sources: {sorted(owned_keys)}"
            )
            continue
        unresolved = [
            k for k in claim.source_keys if by_key[k]["participation"] == "uncertain"
        ]
        if unresolved:
            errors.append(
                prefix
                + f"Unresolved attribution cannot support this claim; sources {unresolved}. Use resolved supporting evidence or leave the claim out."
            )
        if claim.personal and not any(
            by_key[k]["role"] in PERSONAL_ROLES for k in claim.source_keys
        ):
            errors.append(
                prefix
                + "Personal claim has only media, ambient, assistant or uncertain evidence"
            )
        if not claim.citations:
            errors.append(prefix + "Claim does not quote each supporting source")
        for citation_index, citation in enumerate(claim.citations):
            exact = supporting_quote(citation, owned, by_key)
            if exact is None:
                citation_errors.append(
                    prefix + f"Quote for source {citation.source_key} is not verbatim."
                )
                source_fields.append(
                    {
                        "path": f"/claims/{index - 1}/citations/{citation_index}/quote",
                        "source_key": citation.source_key,
                    }
                )
            else:
                citation.quote = exact
    if account.useful and not account.claims:
        errors.append("Useful account has no grounded claims")
    text_fields = {"/title": len(account.title), "/summary": len(account.summary)}
    text_fields.update(
        {f"/questions/{i}": len(q) for i, q in enumerate(account.questions)}
    )
    text_fields.update(
        {
            f"/relationships/{i}/reason": len(r.reason)
            for i, r in enumerate(account.relationships)
        }
    )
    for i, claim in enumerate(account.claims):
        text_fields[f"/claims/{i}/text"] = len(claim.text)
        text_fields.update(
            {
                f"/claims/{i}/citations/{j}/quote": len(c.quote)
                for j, c in enumerate(claim.citations)
            }
        )
    text_size = sum(text_fields.values())
    if text_size > ACCOUNT_TEXT_BUDGET:
        summary_room = ACCOUNT_TEXT_BUDGET - (text_size - len(account.summary))
        summary_repair = (
            f" Replacing /summary with at most {summary_room} characters is sufficient; "
            "the claims and quotations can remain unchanged."
            if summary_room >= 0
            else ""
        )
        errors.append(
            f"Session account contains {text_size} text characters; maximum {ACCOUNT_TEXT_BUDGET}. Reduce it by at least {text_size - ACCOUNT_TEXT_BUDGET} characters. "
            "Largest editable fields (JSON Pointer: current characters): "
            + json.dumps(
                dict(
                    sorted(text_fields.items(), key=lambda item: item[1], reverse=True)[
                        :5
                    ]
                )
            )
            + ". Compact the account while preserving useful claims and exact supporting quotations. The summary need not repeat individual claims. Source identifiers are not counted."
            + summary_repair
        )
    if errors or citation_errors:
        guidance = (
            "\nUse contiguous verbatim source spans, preserving intervening characters; shorter exact quotations are valid."
            if citation_errors
            else ""
        )
        message = "\n".join([*errors, *citation_errors]) + guidance
        if citation_errors:
            raise CitationMismatch(message, source_fields)
        raise ValueError(message)
    return account.model_dump()


def supporting_quote(citation, passages, sources_by_key):
    """Find a verbatim quotation inside an assigned passage of its source."""
    for passage in passages:
        if passage["key"] != citation.source_key:
            continue
        source = {**sources_by_key[passage["key"]], "excerpt": passage["excerpt"]}
        quote = canonical_quote(source, citation.quote)
        if quote is not None:
            return quote
    return None


def canonical_quote(source, quote):
    """Return the raw supporting span, allowing only same-speaker line wrapping.

    Whitespace wrapping is normalized; transcript casing and repeated labels from
    the same speaker may also differ. No words, punctuation or speaker changes
    are tolerated. The raw model
    exchange stays immutable; the validated citation contains the original span.
    """
    text = source["excerpt"]
    if quote in text:
        return quote
    transcript = source["kind"] == "transcript"
    speakers = source.get("metadata", {}).get("speakers", [])
    removed, previous = set(), None
    for match in re.finditer(r"(?m)^([^:\n]{1,100}):\s*", text):
        speaker = match.group(1)
        if not transcript or speaker not in speakers:
            previous = None
            continue
        if speaker == previous:
            removed.update(range(match.start(), match.end()))
        previous = speaker
    chars, positions = [], []
    for index, char in enumerate(text):
        if index in removed:
            continue
        if char.isspace():
            if chars and chars[-1] == " ":
                continue
            char = " "
        normalized = char.lower() if transcript else char
        chars.extend(normalized)
        positions.extend([index] * len(normalized))
    needle = " ".join((quote.lower() if transcript else quote).split())

    # OCR commonly puts parentheses and their contents on separate visual lines.
    # Ignore that layout space only, never spaces between words or punctuation.
    def bracket_layout(value):
        return re.sub(r"(?<=[(\[]) +| +(?=[)\]])", "", value)

    normalized_text = "".join(chars)
    retained = [
        i
        for i in range(len(chars))
        if not (
            chars[i] == " "
            and (
                (i > 0 and chars[i - 1] in "([")
                or (i + 1 < len(chars) and chars[i + 1] in ")]")
            )
        )
    ]
    positions = [positions[i] for i in retained]
    normalized_text = "".join(normalized_text[i] for i in retained)
    needle = bracket_layout(needle)
    start = normalized_text.find(needle)
    if start < 0:
        return None
    return text[positions[start] : positions[start + len(needle) - 1] + 1]


class AccountReviewCheck(BaseModel):
    target: Literal["claim", "question"]
    index: int = Field(
        ge=0, description="Zero-based index in the candidate's claims or questions"
    )
    action: Literal["keep", "revise", "remove"]
    reason: str = Field(
        min_length=1,
        description="For a claim, assess whether the sources support all asserted details. For a question, explain what its answer changes in the memory of the captured event.",
    )


class AccountReview(BaseModel):
    checks: list[AccountReviewCheck] = Field(
        max_length=38,
        description="Assess every claim and every question exactly once. Unsupported details cannot be kept as stated, regardless of their severity.",
    )
    account_issues: list[str] = Field(
        max_length=8,
        description="Material problems with the title, summary or source relationships that change grounding, attribution or meaning. Optional wording improvements belong in advisories.",
    )
    advisories: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Optional presentation or wording suggestions that do not change supported meaning and do not prevent memory drafting.",
    )
    reason: str = Field(
        min_length=1,
        description="Concise overall assessment; individual corrections belong in checks or account_issues. All review explanations, issues and advisories together have a 24000-character text budget.",
    )


def validate_account_review(review, account):
    text_length = sum(
        len(text)
        for text in [
            review.reason,
            *(check.reason for check in review.checks),
            *review.account_issues,
            *review.advisories,
        ]
    )
    if text_length > REVIEW_TEXT_BUDGET:
        raise ValueError(
            f"Total review text budget exceeded: {text_length}/{REVIEW_TEXT_BUDGET} characters. "
            "Compact the explanations while retaining every claim and question check."
        )
    expected = {
        (target, index)
        for target, rows in (("claim", account.claims), ("question", account.questions))
        for index in range(len(rows))
    }
    actual = [(check.target, check.index) for check in review.checks]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError(
            "Review each candidate claim and question exactly once, using its zero-based index. "
            + json.dumps(
                {
                    "missing": sorted(expected - set(actual)),
                    "unexpected": sorted(set(actual) - expected),
                    "duplicates": sorted(
                        {item for item in actual if actual.count(item) > 1}
                    ),
                }
            )
        )
    return not review.account_issues and all(
        check.action == "keep"
        or (check.target == "question" and check.action == "remove")
        for check in review.checks
    )


def account_groups(accounts):
    """Bound each combining pass while ensuring that every pass makes progress.

    A group may exceed the preferred budget for its first two accounts. Without
    this minimum, oversized single accounts could repeat forever without combining.
    """
    group, size = [], 0
    for account in accounts:
        cost = len(json.dumps(account.model_dump(), ensure_ascii=False))
        if len(group) >= 2 and size + cost > SOURCE_BUDGET:
            yield group
            group, size = [], 0
        group.append(account)
        size += cost
    if group:
        yield group


async def prepare_account(sources, *, record, progress, stage, accepted_context):
    """Read bounded source groups, then reduce their accounts to one session."""
    batches = list(source_batches(sources))
    accounts = []
    await progress(0, len(batches))
    for index, batch in enumerate(batches):
        accounts.append(
            await prepare_source_account(
                batch,
                sources,
                record=record,
                accepted_context=accepted_context,
            )
        )
        await progress(index + 1, len(batches))
    if not accounts:
        return SessionAccount(
            title="Reference evidence",
            summary="",
            claims=[],
            questions=[],
            useful=False,
        )
    if stage and len(accounts) > 1:
        await stage("combining")
    while len(accounts) > 1:
        combined = []
        for group in account_groups(accounts):
            if len(group) == 1:
                combined.append(group[0])
            else:
                combined.append(
                    await combine_partial_accounts(
                        group,
                        sources,
                        record=record,
                        accepted_context=accepted_context,
                    )
                )
        accounts = combined
    return accounts[0]


async def build_session_account(
    sources,
    *,
    record,
    progress,
    stage=None,
    accepted_context=None,
    prior_account=None,
    revision_feedback=None,
):
    """Prepare or reuse an account, apply requested feedback, then independently review."""
    context = accepted_context if accepted_context is not None else {}
    if prior_account is not None and revision_feedback:
        # Only the owner-aware worker may supply a scope-matched prior account.
        account = normalized_account(
            SessionAccount.model_validate(prior_account), sources
        )
        await progress(0, 0)
    else:
        account = await prepare_account(
            sources,
            record=record,
            progress=progress,
            stage=stage,
            accepted_context=context,
        )
        if not sources:
            return account
    if revision_feedback:
        if stage:
            await stage("revising")
        # Feedback guides revision; it is never newly captured personal evidence.
        account = await revise_account(
            account,
            {"origin": "draft_review", "feedback": revision_feedback},
            sources,
            record=record,
            accepted_context=context,
        )
    if stage:
        await stage("checking_claims")
    return await verify_session_claims(
        account, sources, record=record, accepted_context=context
    )


async def verify_session_claims(account, sources, *, record, accepted_context=None):
    """An independent Pi investigation reviews the whole account, including questions."""
    # Defer this dependency to break the import cycle through
    # backend.services.timeline.pi_tasks -> backend.services.timeline.session_accounts.
    from .pi_tasks import run_task, settings

    account = normalized_account(account, sources)
    context = accepted_context if accepted_context is not None else {}
    for attempt in range(int(settings().get("max_attempts", 3))):
        check_work_budget()
        # Only scope is inherited: the reviewer chooses its own evidence and vault reads.
        reviewer_context = {
            key: context[key]
            for key in ("scope", "scope_hash", "timezone")
            if key in context
        }
        candidate = {
            **account.model_dump(),
            "claims": [
                {"review_index": index, **claim.model_dump()}
                for index, claim in enumerate(account.claims)
            ],
            "questions": [
                {"review_index": index, "text": question}
                for index, question in enumerate(account.questions)
            ],
        }
        outcome = await run_task(
            stage="session_review",
            instruction="Independently review the account using original evidence and relevant accepted knowledge. Source scope and quotation exactness have already passed code validation; assess whether the evidence supports the asserted meaning, treating candidate interpretations as hypotheses. Assess every claim and question. Keep claims only when their asserted details are grounded; narrow or remove unsupported details. Keep questions only when an answer is necessary to prepare useful memory of the captured event; absent later developments do not make a historical memory incomplete. Separate material account problems from optional wording advisories. Question removals are applied directly; changes to supported meaning require revision. Its contract is available as account-contract.json.",
            payload={"candidate": candidate},
            # Present each claim beside its code-validated supporting quotations.
            # Original sources remain independently inspectable for context.
            briefing={"candidate": candidate},
            sources=sources,
            result_type=AccountReview,
            materials_extra={
                "account-contract.json": json.dumps(SessionAccount.model_json_schema())
            },
            accepted_context=reviewer_context,
            record=record,
            validate=lambda result: validate_account_review(result, account),
        )
        review = outcome.result
        reviewer_context = outcome.context
        ready = validate_account_review(review, account)
        context["review"] = {
            **review.model_dump(),
            "verdict": "ready" if ready else "revise",
        }
        context["review_context"] = reviewer_context
        if ready:
            # The reviewer has already approved every retained item. Removing a
            # question does not rewrite the account or authorize removing claims
            # without also refreshing their summary.
            removed = {
                check.index
                for check in review.checks
                if check.target == "question" and check.action == "remove"
            }
            account = account.model_copy(
                update={
                    "questions": [
                        question
                        for index, question in enumerate(account.questions)
                        if index not in removed
                    ]
                }
            )
            return normalized_account(account, sources)
        if attempt + 1 < int(settings().get("max_attempts", 3)):
            account = await revise_account(
                account,
                review.model_dump(),
                sources,
                record=record,
                accepted_context=context,
            )
    raise ValueError(
        "Session review requires revision; no reviewed account was published"
    )
