"""Revocable, restart-safe browser sessions; API clients retain JWT/API-key auth.

The opaque cookie is only accepted at these endpoints, never as API authority.
Its absolute 30-day lifetime is not extended by renewal. A stable random secret
makes parallel tabs and retries after a lost response safe; only its hash is stored.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from beanie import Document, PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users.authentication import JWTStrategy
from fastapi_users.exceptions import UserNotExists
from pymongo import IndexModel

from .auth import SECRET_KEY, UserManager, get_jwt_strategy, get_user_manager
from .models.api_key import TOKEN_SCHEME

COOKIE_NAME = "chronicle_session"
SESSION_SECONDS = 30 * 24 * 60 * 60


class BrowserSession(Document):
    token_hash: str
    user_id: PydanticObjectId
    credential_hash: str
    expires_at: datetime

    class Settings:
        name = "browser_sessions"
        indexes = [
            IndexModel("token_hash", unique=True),
            IndexModel("expires_at", expireAfterSeconds=0),
        ]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def credential_hash(user) -> str:
    # Password changes and signing-key rotation both invalidate refresh sessions.
    return hmac.new(
        SECRET_KEY.encode(), user.hashed_password.encode(), "sha256"
    ).hexdigest()


def require_browser_request(request: Request):
    # A cross-origin HTML form cannot set this header. CORS controls which
    # origins can make credentialed requests with it; SameSite adds protection.
    if request.headers.get("X-Chronicle-Session") != "1":
        raise HTTPException(403, "Browser session header required")
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        local = request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.hostname != request.url.hostname
            or (not local and parsed.netloc != request.url.netloc)
            or parsed.scheme not in ({"http", "https"} if local else {"https"})
        ):
            raise HTTPException(403, "Session origin rejected")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise HTTPException(403, "Cross-site session request rejected")


router = APIRouter(
    prefix="/auth/session",
    tags=["auth"],
    dependencies=[Depends(require_browser_request)],
)


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


async def revoke_cookie(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = await BrowserSession.find_one({"token_hash": digest(token)})
        if session:
            await session.delete()


async def access_token(user, response: Response):
    private_response(response)
    return {
        "access_token": await get_jwt_strategy().write_token(user),
        "token_type": "bearer",
    }


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    credentials: OAuth2PasswordRequestForm = Depends(),
    manager: UserManager = Depends(get_user_manager),
):
    user = await manager.authenticate(credentials)
    if user is None or not user.is_active:
        raise HTTPException(400, "LOGIN_BAD_CREDENTIALS")
    return await start_session(user, request, response, manager)


@router.post("/exchange")
async def exchange(
    request: Request,
    response: Response,
    manager: UserManager = Depends(get_user_manager),
):
    """Trade a bearer JWT for a browser session (single sign-on handoff).

    A host such as Ushadow mints a JWT for the user with the shared signing key
    and opens the WebUI with it; the WebUI calls this once so renewal works like
    a password sign-in. API keys are refused: they are long-lived machine
    credentials, not an interactive sign-in.
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or token.startswith(f"{TOKEN_SCHEME}_"):
        raise HTTPException(401, "Bearer JWT required")
    user = await JWTStrategy.read_token(get_jwt_strategy(), token, manager)
    if user is None or not user.is_active:
        raise HTTPException(401, "Invalid or expired token")
    return await start_session(user, request, response, manager)


async def start_session(
    user, request: Request, response: Response, manager: UserManager
):
    """Create a refresh session and cookie for an authenticated user."""
    token = secrets.token_urlsafe(32)
    await revoke_cookie(request)
    await BrowserSession(
        token_hash=digest(token),
        user_id=user.id,
        credential_hash=credential_hash(user),
        expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_SECONDS),
    ).insert()
    # Omitting Path lets the browser scope it to the externally visible
    # /[deployment-prefix/]auth/session directory, including stripped proxies.
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        path=None,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https"
        or request.url.hostname not in {"localhost", "127.0.0.1", "::1"},
    )
    # Remove the old API cookie; API calls now use the renewed bearer token.
    response.delete_cookie("fastapiusersauth")
    await manager.on_after_login(user, request, response)
    return await access_token(user, response)


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    manager: UserManager = Depends(get_user_manager),
):
    token = request.cookies.get(COOKIE_NAME)
    session = None
    if token:
        session = await BrowserSession.find_one(
            {
                "token_hash": digest(token),
                "expires_at": {"$gt": datetime.now(UTC)},
            }
        )
    if session is None:
        raise HTTPException(401, "Browser session expired")
    try:
        user = await manager.get(session.user_id)
    except UserNotExists:
        user = None
    if (
        user is None
        or not user.is_active
        or not hmac.compare_digest(session.credential_hash, credential_hash(user))
    ):
        await session.delete()
        raise HTTPException(401, "Browser session revoked")
    return await access_token(user, response)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    await revoke_cookie(request)
    response.delete_cookie(COOKIE_NAME, path=None, httponly=True, samesite="strict")
    response.delete_cookie("fastapiusersauth")
    private_response(response)
