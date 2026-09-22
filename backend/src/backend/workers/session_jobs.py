"""RQ entry points own session preparation and memory-generation lifecycles."""

from bson import ObjectId

import backend.services.timeline.review as review
import backend.services.timeline.sessions as sessions
from backend.models.job import async_job
from backend.models.session_memory import SessionPreparation
from backend.models.timeline import MemoryReviewProposal, utcnow
from backend.services.redis_lock import distributed_lock


@async_job(redis=True, beanie=True)
async def prepare_sessions_job(identifier, *, redis_client=None):

    async with distributed_lock(
        f"session-preparation:{identifier}", timeout=60, blocking_timeout=1, renew=True
    ):
        item = await SessionPreparation.get(ObjectId(identifier))
        if item is None or item.state in {"complete", "stale"} or item.attempts >= 3:
            return
        item.state = "running"
        item.attempts += 1
        item.updated_at = utcnow()
        await sessions.persist_preparation(item)
        try:
            await sessions.prepare_sessions(item)
        except Exception as exc:
            item.state = "failed"
            item.error = str(exc)[:2000]
            await sessions.persist_preparation(item)
            raise
        finally:
            # A source correction arriving during this attempt creates durable
            # subsequent work even when the old attempt fails or is finishing.
            await SessionPreparation.get_pymongo_collection().update_one(
                {
                    "_id": item.id,
                    "requested_revision": {"$gt": item.requested_revision},
                },
                {"$set": {"state": "queued", "attempts": 0, "error": None}},
            )


@async_job(redis=True, beanie=True)
async def generate_session_memory_job(identifier, *, redis_client=None):

    proposal = await MemoryReviewProposal.find_one({"proposal_id": identifier})
    if proposal is None or proposal.state != "queued" or proposal.attempts >= 3:
        return
    result = await review.generate_memory_review(proposal)

    current = await MemoryReviewProposal.find_one({"proposal_id": identifier})
    await sessions.publish_progress(current)
    if result == "failed":
        raise RuntimeError(current.error or "Session memory generation failed")
    return result


@async_job(redis=True, beanie=True)
async def apply_session_memory_job(identifier, *, redis_client=None):

    proposal = await MemoryReviewProposal.find_one({"proposal_id": identifier})
    if proposal is None or proposal.state not in {
        "checking",
        "applying",
        "regenerating",
    }:
        return
    result = await review.process_memory_review_decision(proposal)
    await sessions.publish_progress(
        await MemoryReviewProposal.find_one({"proposal_id": identifier})
    )
    return result
