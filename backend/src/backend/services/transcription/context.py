"""
ASR context builder for transcription.

Combines static hot words from the prompt registry with per-user dynamic
jargon cached in Redis by the ``asr_jargon_extraction`` cron job.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from backend.prompt_registry import get_prompt_registry
from backend.redis_factory import create_async_redis
from backend.services import privacy

logger = logging.getLogger(__name__)


def jargon_cache_key(user_id, snapshot):
    revision = hashlib.sha256(
        json.dumps(snapshot.revisions, sort_keys=True).encode()
    ).hexdigest()
    return f"asr:jargon:references-v1:{user_id}:{revision}"


async def cached_jargon(user_id, redis_client):
    snapshot = await privacy.load_snapshot(user_id)
    await privacy.assert_current(user_id, snapshot)
    cached = await redis_client.get(jargon_cache_key(user_id, snapshot))
    text, receipt = "", []
    if cached:
        try:
            data = json.loads(cached)
            text, receipt = data["text"], data["privacy_reference_receipt"]
            if not isinstance(text, str) or not isinstance(receipt, list):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise privacy.PrivacyHeld() from None
        if not snapshot.permits_record({"privacy_reference_receipt": receipt}):
            raise privacy.PrivacyHeld()
    snapshot.watch_all_sources()
    await privacy.assert_current(user_id, snapshot)
    return text, snapshot, receipt


@dataclass
class TranscriptionContext:
    """Structured context gathered before transcription.

    Holds the individual context components so callers can inspect them
    and Langfuse spans can log them as structured metadata.
    """

    hot_words: str = ""
    user_jargon: str = ""
    user_id: Optional[str] = None
    privacy_checks: list = field(default_factory=list, repr=False)
    reference_receipt: list[str] = field(default_factory=list, repr=False)

    @property
    def combined(self) -> str:
        """Newline-separated context string for ASR providers.

        VibeVoice training data uses newline-separated ``customized_context``
        items (proper nouns, domain terms, full contextual sentences).
        Each comma-separated hot word / jargon term becomes its own line.
        """
        items: list[str] = []
        for source in [self.hot_words, self.user_jargon]:
            if not source or not source.strip():
                continue
            for token in source.split(","):
                token = token.strip()
                if token:
                    items.append(token)
        return "\n".join(items)

    def to_metadata(self) -> dict:
        """Return a dict suitable for Langfuse span metadata."""
        return {
            "hot_words_length": len(self.hot_words),
            "user_jargon_length": len(self.user_jargon),
            "user_id": self.user_id,
            "combined_length": len(self.combined),
        }


async def gather_transcription_context(
    user_id: Optional[str] = None,
) -> TranscriptionContext:
    """Build structured transcription context: static hot words + cached user jargon.

    Args:
        user_id: If provided, also look up per-user jargon from Redis.

    Returns:
        TranscriptionContext with individual components.
    """
    registry = get_prompt_registry()
    try:
        hot_words = await registry.get_prompt("asr.hot_words")
    except Exception:
        logger.debug("Failed to fetch asr.hot_words prompt, using empty default")
        hot_words = ""

    user_jargon = ""
    checks = []
    receipt = []
    if user_id:
        try:
            redis_client = create_async_redis(decode_responses=True)
            try:
                user_jargon, snapshot, receipt = await cached_jargon(
                    user_id, redis_client
                )
                if user_jargon:
                    checks.append((str(user_id), snapshot))
            finally:
                await redis_client.aclose()
        except Exception:
            user_jargon, checks, receipt = "", [], []

    return TranscriptionContext(
        hot_words=hot_words or "",
        user_jargon=user_jargon,
        user_id=user_id,
        privacy_checks=checks,
        reference_receipt=receipt,
    )
