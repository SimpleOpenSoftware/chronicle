"""
Background jobs for annotation-based AI suggestions.

These jobs run periodically via the cron scheduler to:
1. Surface potential errors in transcripts and memories for user review
2. Fine-tune error detection models using accepted/rejected annotations
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from backend.constants import TITLE_NOT_GENERATED
from backend.llm_client import async_generate
from backend.models.annotation import (
    Annotation,
    AnnotationSource,
    AnnotationStatus,
    AnnotationType,
)
from backend.models.conversation import Conversation
from backend.models.user import User
from backend.prompt_registry import get_prompt_registry
from backend.services import privacy

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 7
MAX_SEGMENTS_PER_PROMPT = 30
MAX_SUGGESTIONS_PER_RUN = 50

PROMPT_ID = "annotation.transcript_error_detection"


async def surface_error_suggestions():
    """
    Generate AI suggestions for potential transcript errors.

    Runs daily via cron. For each user, queries recent conversations
    and uses the LLM to identify potential transcription errors.
    Creates PENDING annotations with MODEL_SUGGESTION source for
    user review in the swipe UI.
    """
    logger.info("Checking for annotation suggestions...")
    total_created = 0
    privacy_held = 0

    try:
        users = await User.find_all().to_list()
        logger.info(f"Found {len(users)} users to analyze")

        for user in users:
            user_id = str(user.id)
            cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

            recent_conversations = await Conversation.find(
                Conversation.user_id == user_id,
                Conversation.created_at >= cutoff,
                Conversation.deleted != True,
            ).to_list()

            if not recent_conversations:
                logger.info(
                    f"User {user.email or user_id}: no recent conversations, skipping"
                )
                continue

            logger.info(
                f"User {user.email or user_id}: {len(recent_conversations)} conversations in last {LOOKBACK_DAYS} days"
            )

            # Get conversation IDs that already have pending model suggestions
            existing = await Annotation.find(
                Annotation.user_id == user_id,
                Annotation.source == AnnotationSource.MODEL_SUGGESTION,
                Annotation.status == AnnotationStatus.PENDING,
            ).to_list()
            skip_conversation_ids = {
                a.conversation_id for a in existing if a.conversation_id
            }
            if skip_conversation_ids:
                logger.info(
                    f"  Skipping {len(skip_conversation_ids)} conversations with existing pending suggestions"
                )

            created_for_user = 0
            for conversation in recent_conversations:
                if total_created >= MAX_SUGGESTIONS_PER_RUN:
                    logger.info(
                        f"  Reached max suggestions per run ({MAX_SUGGESTIONS_PER_RUN}), stopping"
                    )
                    break
                if conversation.conversation_id in skip_conversation_ids:
                    continue

                try:
                    snapshot = await privacy.require_record(conversation)
                except privacy.PrivacyHeld:
                    privacy_held += 1
                    continue

                active_transcript = conversation.active_transcript
                if not active_transcript or not active_transcript.segments:
                    logger.debug("No transcript segments; skipping annotation analysis")
                    continue

                seg_count = len(active_transcript.segments)
                logger.info("Analyzing transcript (%d segments)", seg_count)

                try:
                    suggestions = await _analyze_transcript(
                        conversation, active_transcript, snapshot
                    )
                    await privacy.assert_current(user_id, snapshot)
                except privacy.PrivacyHeld:
                    privacy_held += 1
                    continue

                if not suggestions:
                    logger.info(f"    No issues found")
                else:
                    logger.info(f"    LLM found {len(suggestions)} potential issues")

                for suggestion in suggestions:
                    if total_created >= MAX_SUGGESTIONS_PER_RUN:
                        break

                    seg_idx = suggestion.get("segment_index")
                    if seg_idx is None or seg_idx >= len(active_transcript.segments):
                        logger.debug(f"    Skipping invalid segment_index={seg_idx}")
                        continue

                    annotation = Annotation(
                        annotation_type=AnnotationType.TRANSCRIPT,
                        user_id=user_id,
                        conversation_id=conversation.conversation_id,
                        segment_index=seg_idx,
                        original_text=suggestion.get("original_text", ""),
                        corrected_text=suggestion.get("corrected_text", ""),
                        source=AnnotationSource.MODEL_SUGGESTION,
                        status=AnnotationStatus.PENDING,
                    )
                    try:
                        await privacy.assert_current(user_id, snapshot)
                    except privacy.PrivacyHeld:
                        privacy_held += 1
                        break
                    await annotation.save()
                    total_created += 1
                    created_for_user += 1
                    logger.info("Created a transcript correction suggestion")

            logger.info(
                f"User {user.email or user_id}: {created_for_user} suggestions created"
            )

        logger.info(f"Suggestion check complete: {total_created} annotations created")
        return {"created": total_created, "privacy_held": privacy_held}

    except Exception as e:
        logger.error(f"Error in surface_error_suggestions: {e}", exc_info=True)
        raise


async def _analyze_transcript(conversation, transcript, snapshot) -> list[dict]:
    """Use LLM to analyze a transcript for potential errors."""
    segments = transcript.segments[:MAX_SEGMENTS_PER_PROMPT]
    segments_text = "\n".join(
        f"{i}: {seg.speaker} - {seg.text}"
        for i, seg in enumerate(segments)
        if seg.text.strip()
    )

    if not segments_text:
        logger.debug(f"    No non-empty segments to analyze")
        return []

    registry = get_prompt_registry()
    prompt = await registry.get_prompt(
        PROMPT_ID,
        title=conversation.title or TITLE_NOT_GENERATED,
        segments_text=segments_text,
    )

    try:
        logger.debug(f"    Sending {len(segments)} segments to LLM for analysis...")
        await privacy.assert_current(str(conversation.user_id), snapshot)
        response = await async_generate(prompt)
        await privacy.assert_current(str(conversation.user_id), snapshot)
        logger.debug(f"    LLM response length: {len(response)} chars")
        # Parse JSON from response, handling markdown code blocks
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        suggestions = json.loads(text)
        if not isinstance(suggestions, list):
            logger.warning(f"    LLM returned non-list response, ignoring")
            return []
        return suggestions
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in annotation analysis response")
        return []
    except privacy.PrivacyHeld:
        raise
    except Exception as e:
        logger.warning("Annotation analysis failed (%s)", type(e).__name__)
        return []


async def finetune_hallucination_model():
    """
    Fine-tune error detection model using accepted/rejected annotations.
    Runs weekly, improves suggestion accuracy over time.

    This is a PLACEHOLDER implementation. To fully implement:
    1. Fetch all accepted annotations (ground truth corrections)
       - These show real errors that users confirmed
    2. Fetch all rejected annotations (false positives)
       - These show suggestions users disagreed with
    3. Build training dataset:
       - Positive examples: accepted annotations (real errors)
       - Negative examples: rejected annotations (false alarms)
    4. Fine-tune LLM or update prompt engineering:
       - Use accepted examples as few-shot learning
       - Adjust model to reduce false positives
    5. Log metrics:
       - Acceptance rate, rejection rate
       - Most common error types
       - Model accuracy improvement

    TODO: Implement model training logic.
    """
    logger.info("🎓 Checking for model training opportunities (placeholder)...")

    try:
        # Fetch annotation statistics
        total_annotations = await Annotation.find().count()
        accepted_count = await Annotation.find(
            Annotation.status == AnnotationStatus.ACCEPTED,
            Annotation.source == AnnotationSource.MODEL_SUGGESTION,
        ).count()
        rejected_count = await Annotation.find(
            Annotation.status == AnnotationStatus.REJECTED,
            Annotation.source == AnnotationSource.MODEL_SUGGESTION,
        ).count()

        logger.info(f"   Total annotations: {total_annotations}")
        logger.info(f"   Accepted suggestions: {accepted_count}")
        logger.info(f"   Rejected suggestions: {rejected_count}")

        if accepted_count + rejected_count == 0:
            logger.info("   ℹ️  No user feedback yet, skipping training")
            return

        # TODO: Fetch accepted annotations (ground truth)
        # accepted_annotations = await Annotation.find(
        #     Annotation.status == AnnotationStatus.ACCEPTED,
        #     Annotation.source == AnnotationSource.MODEL_SUGGESTION
        # ).to_list()

        # TODO: Fetch rejected annotations (false positives)
        # rejected_annotations = await Annotation.find(
        #     Annotation.status == AnnotationStatus.REJECTED,
        #     Annotation.source == AnnotationSource.MODEL_SUGGESTION
        # ).to_list()

        # TODO: Build training dataset
        # training_data = []
        # for annotation in accepted_annotations:
        #     training_data.append({
        #         "input": annotation.original_text,
        #         "output": annotation.corrected_text,
        #         "label": "error"
        #     })
        #
        # for annotation in rejected_annotations:
        #     training_data.append({
        #         "input": annotation.original_text,
        #         "output": annotation.original_text,  # No change needed
        #         "label": "correct"
        #     })

        # TODO: Fine-tune model or update prompt examples
        # if len(training_data) >= MIN_TRAINING_SAMPLES:
        #     await llm_provider.fine_tune_error_detection(
        #         training_data=training_data,
        #         validation_split=0.2
        #     )
        #     logger.info("✅ Model fine-tuning complete")
        # else:
        #     logger.info(f"   ℹ️  Not enough samples for training (need {MIN_TRAINING_SAMPLES})")

        # Calculate acceptance rate
        if accepted_count + rejected_count > 0:
            acceptance_rate = (accepted_count / (accepted_count + rejected_count)) * 100
            logger.info(f"   Suggestion acceptance rate: {acceptance_rate:.1f}%")

        logger.info("✅ Training check complete (placeholder implementation)")
        logger.info("   ℹ️  TODO: Implement model fine-tuning using user feedback data")

    except Exception as e:
        logger.error(f"❌ Error in finetune_hallucination_model: {e}", exc_info=True)
        raise


# Additional helper functions for future implementation


async def analyze_common_error_patterns() -> List[dict]:
    """
    Analyze accepted annotations to identify common error patterns.
    Returns list of patterns for prompt engineering or model training.

    TODO: Implement pattern analysis.
    """
    # TODO: Group annotations by error type
    # TODO: Find frequent patterns (e.g., "their" → "there")
    # TODO: Return structured patterns for model improvement
    return []


async def calculate_suggestion_metrics() -> dict:
    """
    Calculate metrics about suggestion quality and user engagement.

    Returns:
        dict: Metrics including acceptance rate, response time, etc.

    TODO: Implement metrics calculation.
    """
    # TODO: Calculate acceptance/rejection rates
    # TODO: Measure time to user response
    # TODO: Identify high-confidence vs low-confidence suggestions
    # TODO: Track improvement over time
    return {
        "total_suggestions": 0,
        "acceptance_rate": 0.0,
        "avg_response_time_hours": 0.0,
    }
