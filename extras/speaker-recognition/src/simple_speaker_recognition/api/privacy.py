"""Content-free HTTP boundary for durable speaker-gallery privacy holds."""

from fastapi.responses import JSONResponse


async def gallery_privacy_hold(request, exc):
    return JSONResponse(
        status_code=423,
        content={"detail": "Speaker enrollment held for privacy review"},
    )
