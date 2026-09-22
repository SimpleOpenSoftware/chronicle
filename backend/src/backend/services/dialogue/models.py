"""The language of dialogue. Persistence and provider payloads live elsewhere."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from backend.services.interaction_modes.contracts import AudioInterval


class DialogueModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Identity = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(min_length=1, max_length=32000)]


class Utterance(DialogueModel):
    id: Identity
    thread_id: Identity
    role: Literal["user", "assistant"]
    text: Text
    created_at: AwareDatetime


class DialogueThread(DialogueModel):
    id: Identity
    user_id: Identity
    memory_space_id: str | None = None


class TypedInput(DialogueModel):
    kind: Literal["typed"] = "typed"


class CapturedSpeech(DialogueModel):
    kind: Literal["speech"] = "speech"
    interval: AudioInterval | None = None
    capture_session_id: Identity | None = None

    @model_validator(mode="after")
    def captured_reference(self):
        if self.interval is None and self.capture_session_id is None:
            raise ValueError("Speech requires a capture reference")
        if (
            self.interval
            and self.capture_session_id
            and self.interval.audio_session_id != self.capture_session_id
        ):
            raise ValueError("Speech capture references disagree")
        return self


class ChoiceSelection(DialogueModel):
    kind: Literal["selection"] = "selection"
    task_id: Identity
    task_revision: int = Field(ge=0)
    choice_id: Identity


class AssistantOutput(DialogueModel):
    kind: Literal["assistant"] = "assistant"
    run_id: Identity


UtteranceSource = Annotated[
    TypedInput | CapturedSpeech | ChoiceSelection | AssistantOutput,
    Field(discriminator="kind"),
]


class UtteranceDelivery(DialogueModel):
    utterance_id: Identity
    client_id: Identity
    response_id: str | None = None
    displayed: bool = False
    rendered_samples: int = Field(default=0, ge=0)
    heard_text: str = ""
    outcome: Literal["pending", "delivered", "interrupted", "failed"] = "pending"


class ReplyChoice(DialogueModel):
    id: Identity
    label: Annotated[str, Field(min_length=1, max_length=200)]


class InputWait(DialogueModel):
    after_utterance_id: Identity
    choices: tuple[ReplyChoice, ...] = Field(default=(), max_length=20)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def unique_choices(self):
        if len({choice.id for choice in self.choices}) != len(self.choices):
            raise ValueError("reply choice IDs must be unique")
        return self


class ConversationContinuation(DialogueModel):
    kind: Literal["conversation"] = "conversation"
    request: Text


class HomeAssistantContinuation(DialogueModel):
    kind: Literal["home_assistant"] = "home_assistant"
    request: Text
    entity_ids: tuple[str, ...] = ()


class InstamartContinuation(DialogueModel):
    kind: Literal["instamart"] = "instamart"
    checkpoint_id: Identity
    phase: Identity
    review_revision: str | None = None


class HermesContinuation(DialogueModel):
    kind: Literal["hermes"] = "hermes"
    request: Text
    run_id: str | None = None
    submission_started: bool = False
    pending_request_id: str | None = None
    pending_revision: int = Field(default=0, ge=0)
    pending_kind: Literal["clarification", "approval"] | None = None
    observe_until: AwareDatetime | None = None
    cancel_requested: bool = False


TaskContinuation = Annotated[
    ConversationContinuation
    | HomeAssistantContinuation
    | InstamartContinuation
    | HermesContinuation,
    Field(discriminator="kind"),
]
TaskStatus = Literal[
    "running", "awaiting_input", "paused", "completed", "cancelled", "failed", "stale"
]
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed", "stale"})


class ActionConfirmation(DialogueModel):
    operation_id: Identity
    operation_revision: Identity
    accepted_by_utterance_id: Identity


class DialogueTask(DialogueModel):
    id: Identity
    thread_id: Identity
    title: Annotated[str, Field(min_length=1, max_length=200)]
    status: TaskStatus
    input_wait: InputWait | None = None
    revision: int = Field(ge=0)
    continuation: TaskContinuation
    return_offered: bool = False
    reply_utterance_id: str | None = None
    confirmation: ActionConfirmation | None = None

    @model_validator(mode="after")
    def valid_wait(self):
        if self.status == "awaiting_input" and self.input_wait is None:
            raise ValueError("awaiting_input requires an input wait")
        if self.input_wait is not None and self.status not in {
            "awaiting_input",
            "paused",
        }:
            raise ValueError("only waiting or paused tasks may retain an input wait")
        return self


class UtteranceInterpretation(DialogueModel):
    intent: Literal["answer", "correction", "new_request", "pause", "resume", "cancel"]
    task_id: str | None = None

    @model_validator(mode="after")
    def target_required(self):
        if self.intent != "new_request" and not self.task_id:
            raise ValueError("task interpretation requires a target")
        return self


class TaskCommand(DialogueModel):
    id: Identity
    task_id: Identity
    revision: int = Field(ge=0)
    action: Literal["reply", "pause", "resume", "cancel"]
    utterance_id: str | None = None
    choice_id: str | None = None


class InterpretedUtterance(DialogueModel):
    utterance_id: Identity
    interpretation: UtteranceInterpretation


class DialogueEffect(DialogueModel):
    id: Identity
    task_id: Identity
    task_revision: int = Field(ge=0)
    kind: Literal["resume", "cancel", "archive", "present", "return"]
    status: Literal["pending", "claimed", "uncertain"] = "pending"
    lease_token: str | None = None
    lease_until: AwareDatetime | None = None
    archived_task: DialogueTask | None = None
    not_before: AwareDatetime | None = None
    utterance_id: str | None = None
    language: Literal["en", "hi"] = "en"


class CapturePresentation(DialogueModel):
    client_id: Identity
    capture_session_id: Identity
    capture_epoch: int = Field(ge=0)
    voice_session_id: Identity
    generation: int = Field(ge=1)
    turn_id: Identity
    turn_revision: int = Field(ge=0)
    expires_at: AwareDatetime


class AudioOwner(DialogueModel):
    client_id: Identity
    engagement_id: Identity
    expires_at: AwareDatetime


class CommandReceipt(DialogueModel):
    id: Identity
    fingerprint: str


class DialogueState(DialogueModel):
    thread: DialogueThread
    revision: int = Field(default=0, ge=0)
    tasks: tuple[DialogueTask, ...] = Field(default=(), max_length=8)
    foreground_task_id: str | None = None
    effects: tuple[DialogueEffect, ...] = Field(default=(), max_length=64)
    applied_commands: tuple[str, ...] = Field(default=(), max_length=128)
    command_receipts: tuple[CommandReceipt, ...] = Field(default=(), max_length=128)
    audio_owner: AudioOwner | None = None
    capture_presentation: CapturePresentation | None = None

    @model_validator(mode="after")
    def consistent_tasks(self):
        ids = {task.id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("task IDs must be unique")
        if any(task.thread_id != self.thread.id for task in self.tasks):
            raise ValueError("task must belong to its thread")
        waits = [task for task in self.tasks if task.status == "awaiting_input"]
        if len(waits) > 1:
            raise ValueError("a thread has at most one foreground input wait")
        if self.foreground_task_id is not None and self.foreground_task_id not in ids:
            raise ValueError("foreground task must exist")
        if waits and waits[0].id != self.foreground_task_id:
            raise ValueError("input wait must be foreground")
        return self


class DialogueView(DialogueModel):
    """Public snapshot; execution leases and private outbox records stay server-side."""

    thread: DialogueThread
    revision: int
    tasks: tuple[DialogueTask, ...]
    foreground_task_id: str | None
    audio_client_id: str | None

    @classmethod
    def from_state(cls, state: DialogueState):
        return cls(
            thread=state.thread,
            revision=state.revision,
            tasks=state.tasks,
            foreground_task_id=state.foreground_task_id,
            audio_client_id=state.audio_owner.client_id if state.audio_owner else None,
        )
