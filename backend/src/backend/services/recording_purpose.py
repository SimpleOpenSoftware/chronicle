"""Product visibility is independent of memory preference and corpus access.

Annotation imports remain available to dataset tools. They are never personal
activities, even when their transcript, title or memory preference changes.
"""

from collections.abc import Mapping


def personal_recording_filter() -> dict:
    return {"data_purpose": {"$ne": "annotation"}}


def is_personal_recording(record) -> bool:
    purpose = (
        record.get("data_purpose")
        if isinstance(record, Mapping)
        else getattr(record, "data_purpose", None)
    )
    return purpose != "annotation"
