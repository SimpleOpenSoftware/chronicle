"""Structured inputs and outputs for drafting memory from a reviewed session.

Prompt text is presentation. Source permissions and returned provenance travel
separately so formatting a prompt cannot change which evidence a writer may cite.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class WriteSourcePermissions:
    episode_keys: tuple[str, ...]
    claim_sources: dict[str, list[str]]


@dataclass(frozen=True)
class SessionWriteInput:
    session_key: str
    event_date: str | None
    source_date: str
    processing_time: str
    account: dict[str, Any]
    accepted_context: dict[str, Any]
    source_provenance: list[dict[str, Any]]
    episode_keys: tuple[str, ...]
    guidance: str = ""
    proposal_id: str = ""
    episode_ids: tuple[str, ...] = ()
    conversation_ids: tuple[str, ...] = ()

    @property
    def permissions(self) -> WriteSourcePermissions:
        return WriteSourcePermissions(
            episode_keys=self.episode_keys,
            claim_sources={
                claim["claim_id"]: list(claim["source_keys"])
                for claim in self.account["claims"]
            },
        )

    def render(self) -> str:
        brief = json.dumps(
            {
                "session_key": self.session_key,
                "event_date": self.event_date,
                "accepted_context": self.accepted_context,
                "account": self.account,
                "source_provenance": self.source_provenance,
                "source_episode_keys": list(self.episode_keys),
                "processing_time": self.processing_time,
            },
            ensure_ascii=False,
        )
        if self.guidance:
            brief += "\n\n" + self.guidance
        return brief


@dataclass
class SessionDraftResult:
    outcome: Literal["complete", "partial", "failed"]
    touched: list[str] = field(default_factory=list)
    source_episode_keys_by_path: dict[str, list[str]] = field(default_factory=dict)
    source_evidence_keys_by_path: dict[str, list[str]] = field(default_factory=dict)
    inference_artifacts: list[dict] = field(default_factory=list)

    def retain_attempt(self, attempt) -> None:
        """Keep citations and traces from primary, recovery and repair attempts."""
        self.touched = sorted(set(self.touched) | set(attempt.touched))
        self.inference_artifacts.extend(attempt.inference_artifacts)
        for target, incoming in (
            (self.source_episode_keys_by_path, attempt.source_episode_keys_by_path),
            (self.source_evidence_keys_by_path, attempt.source_evidence_keys_by_path),
        ):
            for path, keys in incoming.items():
                target[path] = sorted(set(target.get(path, [])) | set(keys))
