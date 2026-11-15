"""Manual summary reprocessing: the Recordings menu's "Reprocess Summary" action."""

import json
from types import SimpleNamespace

import pytest

from backend.controllers import conversation_controller as controller

CONVERSATION_ID = "6e37beb4-7d00-4d3d-b9b3-4cb232a6e91a"
USER = SimpleNamespace(user_id="u1", is_superuser=False)


def _conversation(transcript="hello there, this is a test", segments=None):
    return SimpleNamespace(transcript=transcript, segments=segments or [])


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def found(conversation_id, user):
        return _conversation(), None

    def enqueue(conversation_id, *, meta=None, **_):
        calls.append((conversation_id, meta))
        return {
            stage: SimpleNamespace(id=f"{stage}_{conversation_id[:12]}")
            for stage in ("title", "short_summary", "detailed_summary")
        }

    monkeypatch.setattr(controller, "_get_conversation_or_error", found)
    monkeypatch.setattr(controller, "enqueue_summary_job_bundle", enqueue)
    monkeypatch.setattr(controller, "summary_bundle_in_flight", lambda _id: False)
    return calls


async def test_queues_the_full_title_and_summary_bundle(enqueued):
    response = await controller.reprocess_summary(CONVERSATION_ID, USER)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["job_ids"]["title"] == f"title_{CONVERSATION_ID[:12]}"
    assert enqueued == [
        (
            CONVERSATION_ID,
            {"conversation_id": CONVERSATION_ID, "trigger": "summary_reprocess"},
        )
    ]


async def test_refuses_while_a_bundle_is_still_running(enqueued, monkeypatch):
    """Bundle job IDs are fixed, so a second enqueue would overwrite a held job."""

    monkeypatch.setattr(controller, "summary_bundle_in_flight", lambda _id: True)

    response = await controller.reprocess_summary(CONVERSATION_ID, USER)

    assert response.status_code == 409
    assert enqueued == []


async def test_refuses_a_recording_with_no_transcript(enqueued, monkeypatch):
    async def empty(conversation_id, user):
        return _conversation(transcript=""), None

    monkeypatch.setattr(controller, "_get_conversation_or_error", empty)

    response = await controller.reprocess_summary(CONVERSATION_ID, USER)

    assert response.status_code == 409
    assert enqueued == []
