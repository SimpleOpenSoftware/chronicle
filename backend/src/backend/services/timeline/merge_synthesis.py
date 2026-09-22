"""Regenerate the semantic account after a human merges Timeline episodes."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from pydantic import BaseModel, Field

import backend.services.timeline.memory_sources as memory_sources
import backend.services.timeline.pi_tasks as pi_tasks

OPERATION = "timeline_episode_merge"
PROMPT_VERSION = "timeline-merge-v2"


class MergedEpisodeAccount(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    # Must match TimelineEpisode.summary so validation happens before any DB write.
    summary: str = Field(min_length=10, max_length=1200)


def _episode_source(episode: Any) -> dict[str, Any]:
    return {
        "started_at": episode.started_at,
        "ended_at": episode.ended_at,
        "kind": episode.kind,
        "title": episode.title,
        "summary": episode.summary,
        "entities": list(episode.entities),
        "claims": [
            (
                assertion.get("claim", "")
                if isinstance(assertion, dict)
                else assertion.claim
            )
            for assertion in episode.assertions
        ],
    }


def _json_object(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("merged episode synthesis must return a JSON object")
    return value


async def synthesize_merged_episode_account(
    episodes: Iterable[Any],
    *,
    force: bool = False,
) -> MergedEpisodeAccount:
    """Write one coherent title and summary from the selected episode accounts.

    This runs before any episode is superseded, so an inference failure leaves the
    existing Timeline untouched. Identical inputs reuse a durable inference artifact.
    """

    episodes = list(episodes)
    source = [_episode_source(episode) for episode in episodes]
    # Widening repeated slices with the same semantic account does not make that
    # account stale. This also keeps purely structural merges off the inference path.
    titles = {item["title"].strip() for item in source}
    summaries = {item["summary"].strip() for item in source}
    if not force and len(titles) == 1 and len(summaries) == 1:
        title = next(iter(titles))
        summary = next(iter(summaries))
        return MergedEpisodeAccount(
            title=title,
            summary=summary or f"Merged episode: {title}.",
        )

    outcome = await pi_tasks.run_task(
        stage="episode_merge",
        instruction="The user grouped these episode accounts as one event. Produce a coherent title and summary grounded in their evidence and relevant accepted knowledge.",
        payload={"episodes": source},
        result_type=MergedEpisodeAccount,
        sources=memory_sources.evidence_sources(episodes),
        user_id=getattr(episodes[0], "user_id", None),
    )
    return outcome.result
