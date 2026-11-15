"""Exercise registered browser-auth routes with database/user boundaries faked."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from fastapi_users.exceptions import UserNotExists
from httpx import ASGITransport, AsyncClient

from backend import browser_sessions as sessions


@pytest.fixture
async def browser(monkeypatch):
    rows = {}

    class StoredSession:
        def __init__(self, **values):
            self.__dict__.update(values)

        async def insert(self):
            rows[self.token_hash] = self

        async def delete(self):
            rows.pop(self.token_hash, None)

        @classmethod
        async def find_one(cls, query):
            row = rows.get(query["token_hash"])
            if (
                row
                and "expires_at" in query
                and row.expires_at <= query["expires_at"]["$gt"]
            ):
                return None
            return row

    monkeypatch.setattr(sessions, "BrowserSession", StoredSession)
    user = SimpleNamespace(
        id=PydanticObjectId(), hashed_password="password-hash", is_active=True
    )
    manager = SimpleNamespace(
        authenticate=AsyncMock(return_value=user),
        get=AsyncMock(return_value=user),
        on_after_login=AsyncMock(),
        parse_id=PydanticObjectId,
    )

    def make_app():
        app = FastAPI()
        app.include_router(sessions.router)
        app.dependency_overrides[sessions.get_user_manager] = lambda: manager
        return app

    async with AsyncClient(
        transport=ASGITransport(app=make_app()),
        base_url="https://chronicle.test",
        headers={"X-Chronicle-Session": "1"},
    ) as client:
        yield client, rows, manager, make_app


async def sign_in(client):
    result = await client.post(
        "/auth/session/login",
        data={"username": "user@example.com", "password": "password"},
    )
    assert result.status_code == 200
    return result


@pytest.mark.asyncio
async def test_login_cookie_and_restart_renewal(browser):
    client, rows, manager, make_app = browser
    result = await sign_in(client)
    cookie = next(
        v
        for v in result.headers.get_list("set-cookie")
        if v.startswith(sessions.COOKIE_NAME + "=")
    )
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert f"Max-Age={sessions.SESSION_SECONDS}" in cookie
    assert "Path=" not in cookie
    assert result.headers["cache-control"] == "no-store"
    token = client.cookies.get(sessions.COOKIE_NAME)
    assert token not in repr(rows)
    assert sessions.digest(token) in rows
    async with AsyncClient(
        transport=ASGITransport(app=make_app()),
        base_url="https://chronicle.test",
        cookies=client.cookies,
        headers={"X-Chronicle-Session": "1"},
    ) as restarted:
        renewed = await restarted.post("/auth/session/refresh")
    assert renewed.status_code == 200
    payload = jwt.decode(
        renewed.json()["access_token"],
        sessions.SECRET_KEY,
        algorithms=["HS256"],
        audience="fastapi-users:auth",
    )
    assert payload["sub"] == str(manager.get.return_value.id)
    assert "set-cookie" not in renewed.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["expired", "password_changed", "disabled", "deleted", "key_rotated"]
)
async def test_refresh_rejects_invalid_sessions(browser, monkeypatch, failure):
    client, rows, manager, _ = browser
    await sign_in(client)
    row = next(iter(rows.values()))
    if failure == "expired":
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if failure == "password_changed":
        manager.get.return_value.hashed_password = "new-password-hash"
    if failure == "disabled":
        manager.get.return_value.is_active = False
    if failure == "deleted":
        manager.get.side_effect = UserNotExists()
    if failure == "key_rotated":
        monkeypatch.setattr(sessions, "SECRET_KEY", "different-signing-key")
    assert (await client.post("/auth/session/refresh")).status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_stolen_cookie_and_is_idempotent(browser):
    client, rows, _, _ = browser
    await sign_in(client)
    cookie = client.cookies.get(sessions.COOKIE_NAME)
    assert (await client.post("/auth/session/logout")).status_code == 204
    assert not rows
    assert (
        await client.post(
            "/auth/session/refresh",
            headers={"Cookie": f"{sessions.COOKIE_NAME}={cookie}"},
        )
    ).status_code == 401
    assert (await client.post("/auth/session/logout")).status_code == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["login", "refresh", "logout", "exchange"])
async def test_csrf_requests_rejected(browser, endpoint):
    client, rows, _, _ = browser
    await sign_in(client)
    for headers in [
        {"X-Chronicle-Session": ""},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://other.chronicle.test", "Sec-Fetch-Site": "same-site"},
    ]:
        result = await client.post(
            f"/auth/session/{endpoint}",
            headers=headers,
            data={"username": "x", "password": "x"},
        )
        assert result.status_code == 403
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_database_outage_is_not_reported_as_expired(browser, monkeypatch):
    client, _, _, _ = browser
    await sign_in(client)
    monkeypatch.setattr(
        sessions.BrowserSession,
        "find_one",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await client.post("/auth/session/refresh")
    assert client.cookies.get(sessions.COOKIE_NAME)


@pytest.mark.asyncio
async def test_bad_login_does_not_create_session(browser):
    client, rows, manager, _ = browser
    manager.authenticate.return_value = None
    result = await client.post(
        "/auth/session/login", data={"username": "x", "password": "wrong"}
    )
    assert result.status_code == 400
    assert not rows


def host_token(user_id, *, key=None, expires_in=300, audience=("ushadow", "chronicle")):
    """A JWT as a host app (Ushadow) mints it with the shared signing key."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iss": "ushadow",
            "aud": list(audience),
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        },
        key or sessions.SECRET_KEY,
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_exchange_turns_host_token_into_renewable_session(browser):
    client, rows, manager, _ = browser
    user = manager.get.return_value
    result = await client.post(
        "/auth/session/exchange",
        headers={"Authorization": f"Bearer {host_token(user.id)}"},
    )
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert sessions.digest(client.cookies.get(sessions.COOKIE_NAME)) in rows
    payload = jwt.decode(
        result.json()["access_token"],
        sessions.SECRET_KEY,
        algorithms=["HS256"],
        audience="fastapi-users:auth",
    )
    assert payload["sub"] == str(user.id)
    assert (await client.post("/auth/session/refresh")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["missing", "api_key", "wrong_key", "expired", "wrong_audience", "disabled"],
)
async def test_exchange_rejects_unusable_tokens(browser, failure):
    client, rows, manager, _ = browser
    user = manager.get.return_value
    token = {
        "missing": "",
        "api_key": "chrn_abc123abc123_secret",
        "wrong_key": host_token(user.id, key="not-the-shared-key"),
        "expired": host_token(user.id, expires_in=-60),
        "wrong_audience": host_token(user.id, audience=("someone-else",)),
        "disabled": host_token(user.id),
    }[failure]
    if failure == "disabled":
        user.is_active = False
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    result = await client.post("/auth/session/exchange", headers=headers)
    assert result.status_code == 401
    assert not rows
