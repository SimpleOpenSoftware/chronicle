"""Refresh suggestions must stay inside the page's recording/user/space scope."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.models.timeline import MemoryReviewProposal
from backend.routers.modules import source_search_routes as routes


@pytest.mark.asyncio
async def test_recording_scope_applies_before_suggestion_limit(monkeypatch):
    queries = []

    class Results:
        def sort(self, _):
            return self

        def limit(self, value):
            assert value == 100
            return self

        async def to_list(self):
            return []

    def find(query):
        queries.append(query)
        return Results()

    check_space = AsyncMock()
    monkeypatch.setattr(routes, "check_space", check_space)
    monkeypatch.setattr(MemoryReviewProposal, "find", find)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.current_active_user] = lambda: SimpleNamespace(
        id="owner"
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/context-refreshes",
            params={
                "recording_id": "recording-a",
                "memory_space_id": "space-a",
            },
        )
        assert response.status_code == 200
        assert queries[-1]["recording_id"] == "recording-a"
        assert queries[-1]["user_id"] == "owner"
        assert queries[-1]["memory_space_id"] == "space-a"
        check_space.assert_awaited_with("owner", "space-a")

        response = await client.get(
            "/api/context-refreshes", params={"local_date": "2026-09-04"}
        )
        assert response.status_code == 200
        assert "recording_id" not in queries[-1]
        assert queries[-1]["local_date"] == date(2026, 9, 4)
        assert queries[-1]["memory_space_id"] is None

        response = await client.get(
            "/api/context-refreshes", params={"local_date": "not-a-date"}
        )
        assert response.status_code == 422
