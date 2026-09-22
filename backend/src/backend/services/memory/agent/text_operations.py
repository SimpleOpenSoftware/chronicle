"""Text computation contract; filesystem access and vault policy live in VaultTools."""

from typing import Protocol

from .edit_engine import Edit

MAX_READ_CHARS = 8000
DEFAULT_READ_LINES = 200
MAX_READ_LINES = 2000
DEFAULT_SLICE_CHARS = 2000


class TextOperations(Protocol):
    def read(self, content: str, path: str, offset: int, limit: int) -> str: ...

    def edit(self, content: str, edits: list[Edit], path: str) -> str: ...
