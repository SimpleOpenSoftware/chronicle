"""Shared collection of successful note observations for retrieval and citations."""

import json

from .text_operations import DEFAULT_READ_LINES, DEFAULT_SLICE_CHARS

INSPECTION_TOOLS = frozenset({"read_note", "read_slice"})
_MAX_NOTE_CHARS = 64000


def inspection_window(name: str, arguments: dict) -> str:
    keys = (
        {"offset": 0, "limit": DEFAULT_READ_LINES}
        if name == "read_note"
        else {
            "char_offset": 0,
            "max_chars": DEFAULT_SLICE_CHARS,
        }
    )
    return (
        name
        + " "
        + json.dumps(
            {key: arguments.get(key, default) for key, default in keys.items()},
            sort_keys=True,
        )
    )


class InspectionEvidence:
    """Keep distinct windows, replacing rereads and bounding retained note content.

    Latest windows appear first so a focused follow-up remains visible when the
    final synthesis applies its stricter total byte budget. Raw tool traces retain
    every observation. .base files are presentation references, not memory evidence.
    The caller serializes updates if dispatching across threads.
    """

    def __init__(self):
        self.notes: dict[str, str] = {}
        self._windows: dict[str, dict[str, str]] = {}

    def record(self, name: str, arguments: dict, result: str, *, path: str) -> None:
        if (
            name not in INSPECTION_TOOLS
            or path.lower().endswith(".base")
            or result.startswith("Error:")
        ):
            return
        key = inspection_window(name, arguments)
        windows = self._windows.setdefault(path, {})
        windows.pop(key, None)
        windows[key] = result
        while len(windows) > 1 and sum(map(len, windows.values())) > _MAX_NOTE_CHARS:
            windows.pop(next(iter(windows)))
        if len(windows) == 1:
            self.notes[path] = result[:_MAX_NOTE_CHARS]
        else:
            self.notes[path] = "\n\n".join(
                f"[Observed {window}]\n{text}"
                for window, text in reversed(windows.items())
            )[:_MAX_NOTE_CHARS]
