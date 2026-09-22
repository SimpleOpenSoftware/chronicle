"""
Memory controller for handling memory-related business logic.
"""

import asyncio
import difflib
import logging
from typing import Optional

from beanie import PydanticObjectId
from fastapi.responses import JSONResponse

import backend.services.privacy as privacy
from backend.controllers.conversation_controller import _memory_audit_to_dict
from backend.models.conversation import Conversation
from backend.models.memory_audit import MemoryAuditEntry
from backend.services.memory import get_memory_service
from backend.services.memory.base import MemoryEntry
from backend.users import User

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")


def _resolve_target_user(user: User, user_id: Optional[str] = None) -> str:
    """Return the effective user ID: admins may override with user_id param."""
    if user.is_superuser and user_id:
        return user_id
    return user.user_id


async def get_memory_audit(
    user: User,
    limit: int,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
):
    """Return the memory vault change ledger for a user (newest first).

    Records which notes were created/updated/deleted, when, and what triggered
    each change (memory extraction, speaker reprocess, or an inbound Obsidian edit).
    """
    try:
        target_user_id = _resolve_target_user(user, user_id)

        query = MemoryAuditEntry.find(MemoryAuditEntry.user_id == target_user_id)
        query = query.find(MemoryAuditEntry.memory_space_id == None)  # noqa: E711
        if conversation_id:
            query = query.find(MemoryAuditEntry.conversation_id == conversation_id)

        entries = await query.sort(-MemoryAuditEntry.created_at).limit(limit).to_list()

        private_paths = {
            path.casefold()
            for path in await privacy.quarantined_vault_paths(target_user_id)
        }
        entries = [
            entry
            for entry in entries
            if not entry.note_path or entry.note_path.casefold() not in private_paths
        ]

        return {
            "user_id": target_user_id,
            "count": len(entries),
            "entries": [_memory_audit_to_dict(e) for e in entries],
        }

    except Exception as e:
        logger.error(f"Error fetching memory audit: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"message": f"Error fetching memory audit: {str(e)}"},
        )


async def get_memory_audit_diff(user: User, entry_id: str):
    """Return the before→after diff for a single memory-audit entry.

    The ledger stores the post-change note content (``after_text``); the "before"
    is reconstructed from the most recent prior recorded change to the same note.
    The diff therefore reflects the net change since the last recorded state,
    whether the previous writer was the AI or an inbound Obsidian (human) edit.
    """
    try:
        try:
            oid = PydanticObjectId(entry_id)
        except Exception:
            return JSONResponse(
                status_code=400, content={"message": "Invalid audit entry id"}
            )

        entry = await MemoryAuditEntry.get(oid)
        if entry is None:
            return JSONResponse(
                status_code=404, content={"message": "Audit entry not found"}
            )
        if entry.memory_space_id is not None:
            return JSONResponse(
                status_code=404, content={"message": "Audit entry not found"}
            )

        # Ownership: users may only diff their own ledger; admins may diff any.
        if entry.user_id != user.user_id and not user.is_superuser:
            return JSONResponse(
                status_code=403, content={"message": "Not authorized for this entry"}
            )

        if entry.note_path and entry.note_path.casefold() in {
            path.casefold()
            for path in await privacy.quarantined_vault_paths(entry.user_id)
        }:
            return JSONResponse(
                status_code=423,
                content={"message": "This note is held by privacy settings"},
            )

        after_text = entry.after_text

        # Reconstruct the prior state from the previous recorded change to this note.
        before_text: Optional[str] = None
        if entry.note_path:
            prior = (
                await MemoryAuditEntry.find(
                    MemoryAuditEntry.user_id == entry.user_id,
                    MemoryAuditEntry.memory_space_id == None,  # noqa: E711
                    MemoryAuditEntry.note_path == entry.note_path,
                    MemoryAuditEntry.created_at < entry.created_at,
                )
                .sort(-MemoryAuditEntry.created_at)
                .limit(1)
                .to_list()
            )
            if prior:
                before_text = prior[0].after_text

        base = {
            "id": str(entry.id),
            "note_path": entry.note_path,
            "operation": entry.operation,
            "cause": entry.cause,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }

        # Nothing recorded on either side (legacy entry or note-less delete_all).
        if after_text is None and before_text is None:
            return {
                **base,
                "before_text": None,
                "after_text": None,
                "diff": "",
                "diff_available": False,
                "reason": "No note content was recorded for this change.",
            }

        diff = "\n".join(
            difflib.unified_diff(
                (before_text or "").splitlines(),
                (after_text or "").splitlines(),
                fromfile=f"a/{entry.note_path or 'note'}",
                tofile=f"b/{entry.note_path or 'note'}",
                lineterm="",
            )
        )

        return {
            **base,
            "before_text": before_text,
            "after_text": after_text,
            "diff": diff,
            "diff_available": True,
        }

    except Exception as e:
        logger.error(f"Error building memory audit diff: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"message": f"Error building memory audit diff: {str(e)}"},
        )


async def get_memories(user: User, limit: int, user_id: Optional[str] = None):
    """Get memories. Users see only their own memories, admins can see all or filter by user."""
    try:
        memory_service = get_memory_service()

        target_user_id = _resolve_target_user(user, user_id)

        # Execute memory retrieval directly (now async)
        memories = await memory_service.get_all_memories(target_user_id, limit)

        # Get total count (service returns None on failure)
        total_count = await memory_service.count_memories(target_user_id)

        # Convert MemoryEntry objects to dicts for JSON serialization
        memories_dicts = [mem.to_dict() for mem in memories]

        return {
            "memories": memories_dicts,
            "count": len(memories),
            "total_count": total_count,
            "user_id": target_user_id,
        }

    except Exception as e:
        audio_logger.error(f"Error fetching memories: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"message": f"Error fetching memories: {str(e)}"}
        )


async def get_memories_with_transcripts(
    user: User, limit: int, user_id: Optional[str] = None
):
    """Get memories with their source transcripts. Users see only their own memories, admins can see all or filter by user."""
    try:
        memory_service = get_memory_service()

        target_user_id = _resolve_target_user(user, user_id)

        # Execute memory retrieval directly (now async)
        memories_with_transcripts = await memory_service.get_memories_with_transcripts(
            target_user_id, limit
        )

        return {
            "memories": memories_with_transcripts,  # Streamlit expects 'memories' key
            "count": len(memories_with_transcripts),
            "user_id": target_user_id,
        }

    except Exception as e:
        audio_logger.error(
            f"Error fetching memories with transcripts: {e}", exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={"message": f"Error fetching memories with transcripts: {str(e)}"},
        )


async def search_memories(
    query: str,
    user: User,
    limit: int,
    score_threshold: float = 0.0,
    user_id: Optional[str] = None,
):
    """Search memories by text query. Users can only search their own memories, admins can search all or filter by user."""
    try:
        memory_service = get_memory_service()

        target_user_id = _resolve_target_user(user, user_id)

        # Execute search directly (now async)
        search_results = await memory_service.search_memories(
            query, target_user_id, limit, score_threshold
        )

        # Convert MemoryEntry objects to dicts for JSON serialization
        results_dicts = [result.to_dict() for result in search_results]

        return {
            "query": query,
            "results": results_dicts,
            "count": len(search_results),
            "user_id": target_user_id,
        }

    except Exception as e:
        audio_logger.error(f"Error searching memories: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"message": f"Error searching memories: {str(e)}"}
        )


async def delete_memory(memory_id: str, user: User):
    """Delete a memory by ID. Users can only delete their own memories, admins can delete any."""
    try:
        memory_service = get_memory_service()

        # For non-admin users, verify memory ownership before deletion
        if not user.is_superuser:
            # Check if memory belongs to current user
            user_memories = await memory_service.get_all_memories(user.user_id, 1000)

            # MemoryEntry is a dataclass, access id attribute directly
            memory_ids = [str(mem.id) for mem in user_memories]
            if memory_id not in memory_ids:
                return JSONResponse(
                    status_code=404, content={"message": "Memory not found"}
                )

        # Delete the memory
        audio_logger.info(
            f"Deleting memory {memory_id} for user_id={user.user_id}, email={user.email}"
        )
        success = await memory_service.delete_memory(
            memory_id, user_id=user.user_id, user_email=user.email
        )

        if success:
            return JSONResponse(
                content={"message": f"Memory {memory_id} deleted successfully"}
            )
        else:
            return JSONResponse(
                status_code=404, content={"message": "Memory not found"}
            )

    except Exception as e:
        audio_logger.error(f"Error deleting memory: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"message": f"Error deleting memory: {str(e)}"}
        )


async def add_memory(content: str, user: User, source_id: Optional[str] = None):
    """Add a memory directly from content text. Extracts structured memories from the provided content."""
    try:
        memory_service = get_memory_service()

        # Use source_id or generate a unique one
        memory_source_id = (
            source_id or f"manual_{user.user_id}_{int(asyncio.get_event_loop().time())}"
        )

        # Extract memories from content
        success, memory_ids = await memory_service.add_memory(
            transcript=content,
            client_id=f"{user.user_id[:8]}-manual",
            source_id=memory_source_id,
            user_id=user.user_id,
            user_email=user.email,
            allow_update=False,
            db_helper=None,
        )

        if success:
            return {
                "success": True,
                "memory_ids": memory_ids,
                "count": len(memory_ids),
                "source_id": memory_source_id,
                "message": f"Successfully created {len(memory_ids)} memory/memories",
            }
        else:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Failed to create memories"},
            )

    except Exception as e:
        audio_logger.error(f"Error adding memory: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Error adding memory: {str(e)}"},
        )


async def get_all_memories_admin(user: User, limit: int):
    """Get all memories across all users for admin review. Admin only."""
    try:
        memory_service = get_memory_service()

        # Get all memories without user filtering
        all_memories = await memory_service.get_all_memories_debug(limit)

        # Group by user for easier admin review
        user_memories = {}
        users_with_memories = set()
        client_ids_with_memories = set()

        for memory in all_memories:
            user_id = memory.get("user_id", "unknown")
            client_id = memory.get("client_id", "unknown")

            if user_id not in user_memories:
                user_memories[user_id] = []
            user_memories[user_id].append(memory)

            # Track users and clients for debug info
            users_with_memories.add(user_id)
            client_ids_with_memories.add(client_id)

        # Enhanced stats combining both admin and debug information
        stats = {
            "total_memories": len(all_memories),
            "total_users": len(user_memories),
            "users_with_memories": sorted(list(users_with_memories)),
            "client_ids_with_memories": sorted(list(client_ids_with_memories)),
        }

        return {
            "memories": all_memories,  # Flat list for compatibility
            "user_memories": user_memories,  # Grouped by user
            "stats": stats,
            "total_users": len(user_memories),
            "total_memories": len(all_memories),
            "limit": limit,
        }

    except Exception as e:
        audio_logger.error(f"Error fetching admin memories: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"message": f"Error fetching admin memories: {str(e)}"},
        )


async def get_memory_by_id(memory_id: str, user: User, user_id: Optional[str] = None):
    """Get a single memory by ID. Users can only access their own memories, admins can access any."""
    try:
        memory_service = get_memory_service()

        target_user_id = _resolve_target_user(user, user_id)

        # Get the specific memory
        memory = await memory_service.get_memory(memory_id, target_user_id)

        if memory:
            # Convert MemoryEntry to dict for JSON serialization
            memory_dict = memory.to_dict()

            # Enrich with source conversation info if source_id exists in metadata
            source_id = memory.metadata.get("source_id")
            if source_id:
                try:
                    conversation = await Conversation.find_one(
                        Conversation.conversation_id == source_id
                    )
                    if conversation:
                        memory_dict["source_conversation"] = {
                            "conversation_id": conversation.conversation_id,
                            "title": conversation.title,
                            "summary": conversation.summary,
                            "created_at": (
                                conversation.created_at.isoformat()
                                if conversation.created_at
                                else None
                            ),
                        }
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch source conversation {source_id}: {e}"
                    )

            return {"memory": memory_dict}
        else:
            return JSONResponse(
                status_code=404, content={"message": "Memory not found"}
            )

    except Exception as e:
        audio_logger.error(f"Error fetching memory {memory_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"message": f"Error fetching memory: {str(e)}"}
        )
