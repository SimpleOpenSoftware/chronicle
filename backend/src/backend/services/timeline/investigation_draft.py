"""Editable, unpublished task results. Every revision still passes full validation."""

from copy import deepcopy


def read_pointer(document, pointer):
    """Resolve an inspected JSON value without interpreting it as code or a path."""
    if pointer == "":
        return deepcopy(document)
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Use an absolute JSON Pointer within the inspected result")
    value = document
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not part.isdecimal() or int(part) >= len(value):
                raise ValueError("Inspected result array index outside range")
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError("Inspected result pointer has no child value")
    return deepcopy(value)


def revise_draft(draft, edits):
    """Apply a bounded JSON Pointer edit set atomically to a private candidate.

    Paths address the result itself, not its envelope, files or source stores. Invalid edits
    leave the saved candidate intact; validation/publication belongs to the caller.
    """
    if draft is None:
        raise ValueError("No submitted draft to revise")
    if not isinstance(edits, list) or not 0 <= len(edits) <= 64:
        raise ValueError("Supply at most 64 draft edits")
    candidate = deepcopy(draft)
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("Each draft edit must be an object")
        op, path = edit.get("op"), edit.get("path")
        if op not in {"add", "replace", "remove"}:
            raise ValueError("Draft edits support add, replace and remove")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Use an absolute JSON Pointer within the draft")
        parts = [
            part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")
        ]
        if op != "remove" and "value" not in edit:
            raise ValueError("An add or replace edit requires value")
        parent = candidate
        for part in parts[:-1]:
            if isinstance(parent, list) and (
                not part.isdecimal() or int(part) >= len(parent)
            ):
                raise ValueError(f"Draft array index outside range: {path}")
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = parts[-1]
        if isinstance(parent, list):
            index = len(parent) if key == "-" and op == "add" else int(key)
            # Adding may append after the last item; other edits need an item.
            last_allowed_index = len(parent) if op == "add" else len(parent) - 1
            if index < 0 or index > last_allowed_index:
                raise ValueError(f"Draft array index outside range: {path}")
            if op == "add":
                parent.insert(index, deepcopy(edit["value"]))
            elif op == "remove":
                parent.pop(index)
            else:
                parent[index] = deepcopy(edit["value"])
        elif isinstance(parent, dict):
            if op != "add" and key not in parent:
                raise ValueError(f"Draft path does not exist: {path}")
            if op == "remove":
                del parent[key]
            else:
                parent[key] = deepcopy(edit["value"])
        else:
            raise ValueError(f"Draft path has no editable parent: {path}")
    return candidate
