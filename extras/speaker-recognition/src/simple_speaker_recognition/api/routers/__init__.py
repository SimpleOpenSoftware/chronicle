"""API routers for different functional areas."""

from .deepgram_wrapper import router as deepgram_router
from .enrollment import router as enrollment_router
from .enrollment_audit import router as enrollment_audit_router
from .enrollment_operations import router as enrollment_operations_router
from .identification import router as identification_router
from .speakers import router as speakers_router
from .users import router as users_router
from .websocket_wrapper import router as websocket_router

__all__ = [
    "users_router",
    "speakers_router",
    "enrollment_router",
    "enrollment_audit_router",
    "enrollment_operations_router",
    "identification_router",
    "deepgram_router",
    "websocket_router",
]
