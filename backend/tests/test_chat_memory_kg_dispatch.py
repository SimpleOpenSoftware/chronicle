"""Direct chat extraction must not bypass the review-and-save workflow."""

from unittest.mock import AsyncMock

import pytest

from backend.chat_service import ChatService


@pytest.mark.asyncio
async def test_direct_extraction_rejects_without_writing_or_dispatching(monkeypatch):
    service = ChatService()
    service.memory_service = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr("backend.chat_service.dispatch_plugin_event", dispatch)

    with pytest.raises(ValueError, match="Review and save from a new chat"):
        await service.extract_memories_from_session("session", "owner")

    service.memory_service.add_memory.assert_not_awaited()
    dispatch.assert_not_awaited()
