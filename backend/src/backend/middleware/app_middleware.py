"""
Middleware configuration for Chronicle backend.

Centralizes CORS configuration and global exception handlers.
"""

import json
import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure, PyMongoError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from backend.app_config import get_app_config
from backend.config_loader import ConfigSaveError
from backend.model_registry import MissingModelCredentialsError

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("api.requests")


def setup_cors_middleware(app: FastAPI) -> None:
    """Configure CORS middleware for the FastAPI application."""
    config = get_app_config()

    logger.info(f"🌐 CORS configured with origins: {config.allowed_origins}")
    logger.info(
        f"🌐 CORS also allows Tailscale IPs via regex: {config.tailscale_regex}"
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_origin_regex=config.tailscale_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log API requests and JSON responses.

    Only small ``application/json`` bodies are buffered for pretty-printing.
    Everything else (file downloads, audio, SSE, huge JSON) gets a status
    line only — buffering a large body here happens ON the event loop and
    has frozen the whole backend before (163MB zip → minutes at 100% CPU).
    Note: ``call_next`` always returns a streaming wrapper, so response
    *type* can't be used to detect file responses — only headers can.
    """

    # Paths to exclude from logging
    EXCLUDED_PATHS = {
        "/auth/jwt/login",
        "/auth/cookie/login",
        "/auth/jwt/logout",
        "/auth/cookie/logout",
        "/ws",
        "/mcp",
        "/health",
        "/auth/health",
        "/readiness",
        "/api/queue/dashboard",  # Auto-refresh endpoint, too noisy
        "/api/events/stream",  # SSE endpoint — BaseHTTPMiddleware breaks streaming
    }

    # Bodies larger than this are never buffered/logged, even JSON.
    MAX_LOGGED_BODY_BYTES = 256 * 1024

    def should_log_request(self, path: str) -> bool:
        """Determine if request should be logged."""
        # Exclude exact path matches
        if path in self.EXCLUDED_PATHS:
            return False

        # Exclude paths starting with excluded prefixes
        for excluded in self.EXCLUDED_PATHS:
            if path.startswith(excluded):
                return False

        # Exclude audio file serving
        if path.startswith("/audio/"):
            return False

        return True

    def should_log_response_body(
        self, content_type: str, content_length: Optional[str]
    ) -> bool:
        """Buffer-and-log only small JSON bodies, and only when asked for.

        Pretty-printing every response at INFO made this logger ~97% of the backend's
        entire log output — 434 requests produced 20,505 lines in two minutes, because
        ``indent=2`` turns one response into hundreds of lines. All of it is written to
        a stdout pipe, and all of it is buffered and re-serialized on the event loop.

        The status line stays at INFO; bodies are debugging material, so they need
        ``logging.getLogger("api.requests").setLevel(logging.DEBUG)``. Below that the
        buffering is skipped outright rather than done and discarded.
        """
        if not request_logger.isEnabledFor(logging.DEBUG):
            return False
        if not content_type.startswith("application/json"):
            return False
        if content_length:
            try:
                if int(content_length) > self.MAX_LOGGED_BODY_BYTES:
                    return False
            except ValueError:
                pass
        return True

    async def dispatch(self, request: Request, call_next):
        """Process request and log request/response information."""
        path = request.url.path

        # Skip logging for excluded paths
        if not self.should_log_request(path):
            return await call_next(request)

        # Start timing
        start_time = time.time()

        # Log request
        request_logger.info(f"→ {request.method} {path}")

        # Process request
        response = await call_next(request)

        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000

        should_log_body = self.should_log_response_body(
            response.headers.get("content-type", ""),
            response.headers.get("content-length"),
        )

        if should_log_body and response.status_code != 204:  # No content
            try:
                # Read response body (bytearray: linear-time accumulation)
                buffer = bytearray()
                async for chunk in response.body_iterator:
                    buffer.extend(chunk)
                    if len(buffer) > self.MAX_LOGGED_BODY_BYTES:
                        # Mis-declared content-length; stop pretty-printing but
                        # keep consuming so the response can be re-emitted.
                        async for chunk in response.body_iterator:
                            buffer.extend(chunk)
                        break
                response_body = bytes(buffer)

                if len(response_body) > self.MAX_LOGGED_BODY_BYTES:
                    request_logger.info(
                        f"← {request.method} {path} - {response.status_code} - {duration_ms:.2f}ms "
                        f"(json body too large to log: {len(response_body)} bytes)"
                    )
                else:
                    # Try to parse as JSON for pretty printing
                    try:
                        json_body = json.loads(response_body)
                        formatted_json = json.dumps(json_body, indent=2)
                        request_logger.info(
                            f"← {request.method} {path} - {response.status_code} - {duration_ms:.2f}ms\n"
                            f"Response body:\n{formatted_json}"
                        )
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        request_logger.info(
                            f"← {request.method} {path} - {response.status_code} - {duration_ms:.2f}ms "
                            f"(non-JSON response)"
                        )

                # Recreate response with the body we consumed
                return Response(
                    content=response_body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
            except Exception as e:
                # If anything goes wrong, just log basic info
                request_logger.warning(
                    f"← {request.method} {path} - {response.status_code} - {duration_ms:.2f}ms "
                    f"(error reading response: {e})"
                )
                return response
        else:
            # Status line only — body passes through untouched (streaming).
            request_logger.info(
                f"← {request.method} {path} - {response.status_code} - {duration_ms:.2f}ms"
            )
            return response


def setup_exception_handlers(app: FastAPI) -> None:
    """Configure global exception handlers for the FastAPI application."""

    @app.exception_handler(ConnectionFailure)
    @app.exception_handler(PyMongoError)
    async def database_exception_handler(request: Request, exc: Exception):
        """Handle database connection failures and return structured error response."""
        logger.error(f"Database connection error: {type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Unable to connect to server. Please check your connection and try again.",
                "error_type": "connection_failure",
                "error_category": "database",
            },
        )

    @app.exception_handler(ConnectionError)
    async def connection_exception_handler(request: Request, exc: ConnectionError):
        """Handle general connection errors and return structured error response."""
        logger.error(f"Connection error: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Unable to connect to server. Please check your connection and try again.",
                "error_type": "connection_failure",
                "error_category": "network",
            },
        )

    @app.exception_handler(ConfigSaveError)
    async def config_save_exception_handler(request: Request, exc: ConfigSaveError):
        """A settings write failed; tell the caller why instead of a bare 500."""
        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc),
                "error_type": "config_save_failure",
                "error_category": "configuration",
            },
        )

    @app.exception_handler(MissingModelCredentialsError)
    async def missing_credentials_exception_handler(
        request: Request, exc: MissingModelCredentialsError
    ):
        """An LLM call was attempted against a hosted model with no API key."""
        return JSONResponse(
            status_code=503,
            content={
                "detail": str(exc),
                "error_type": "missing_model_credentials",
                "error_category": "configuration",
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """Handle HTTP exceptions with structured error response."""
        # For authentication failures (401), add error_type
        if exc.status_code == 401:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "detail": exc.detail,
                    "error_type": "authentication_failure",
                    "error_category": "security",
                },
            )

        # For other HTTP exceptions, return as normal
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def setup_middleware(app: FastAPI, *, disable_request_logging: bool = False) -> None:
    """Set up all middleware for the FastAPI application."""
    # Add request logging middleware
    if not disable_request_logging:
        app.add_middleware(RequestLoggingMiddleware)
        logger.info("📝 Request logging middleware enabled")
    else:
        logger.info("📝 Request logging middleware DISABLED (profiling)")

    setup_cors_middleware(app)
    setup_exception_handlers(app)
