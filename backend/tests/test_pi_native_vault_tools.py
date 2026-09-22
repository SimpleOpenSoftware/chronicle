"""Production vault dispatch with real Pi factories; no model or live vault needed.

Run with the image's pinned Pi 0.85.1 on PATH, or set PI_BINARY to that installation.
"""

import contextlib
import os
import subprocess

import pytest
from test_pi_executor import _fake_spawn, _runtime_config, _successful_events

from backend.services.memory.agent import pi_agent, pi_native_tools, vault_tools


@pytest.fixture
def native_binary():
    binary = os.environ.get("PI_BINARY", "pi")
    pi_native_tools._pi_entrypoint(
        binary
    )  # A required runtime dependency, never a skip.
    return binary


@pytest.fixture
def gateway(tmp_path, monkeypatch, native_binary):
    @contextlib.contextmanager
    def lock(_user):
        yield

    monkeypatch.setattr(vault_tools, "vault_note_lock", lock)
    with pi_agent._VaultToolGateway(
        tmp_path,
        vault_tools.VAULT_TOOL_SCHEMAS,
        native_binary=native_binary,
    ) as gateway:
        yield gateway
    assert gateway._native_text_tools._process is None


def note(root, content, name="People/Alice.md"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_native_edit_preserves_neighbors_and_whole_transaction(gateway, monkeypatch):
    before = "Prefix “untouched” — says “yes”.\n"
    target = note(gateway.tools.root, before)
    held = False
    entered = 0

    @contextlib.contextmanager
    def lock(_user):
        nonlocal held, entered
        held = True
        entered += 1
        try:
            yield
        finally:
            held = False

    original = gateway._native_text_tools.edit

    def checked_edit(*args):
        assert held
        assert target.read_text() == before
        return original(*args)

    monkeypatch.setattr(vault_tools, "vault_note_lock", lock)
    monkeypatch.setattr(gateway._native_text_tools, "edit", checked_edit)
    args = {
        "path": "People/Alice.md",
        "edits": [{"old_text": 'says "yes".', "new_text": 'says "no".'}],
    }
    assert "exactly once" in gateway.dispatch("edit_note", args)
    assert target.read_text() == before
    assert not gateway.tools.touched
    args["edits"][0]["old_text"] = "says “yes”."
    assert gateway.dispatch("edit_note", args).startswith("Edited")
    assert target.read_text() == 'Prefix “untouched” — says "no".\n'
    assert entered == 2
    assert gateway.tools.touched == {"People/Alice.md"}


@pytest.mark.parametrize(
    "edits",
    [
        [{"old_text": "", "new_text": "new"}],
        [{"old_text": "tea", "new_text": "coffee"}],  # ambiguous
        [{"old_text": "Alice", "new_text": "Alice"}],  # no-op
        [
            {"old_text": "Alice likes", "new_text": "Bob"},
            {"old_text": "likes tea", "new_text": "coffee"},
        ],
        [
            {"old_text": "Alice", "new_text": "Bob"},
            {"old_text": "missing", "new_text": "coffee"},
        ],
    ],
)
def test_invalid_native_edit_batch_never_writes(gateway, edits):
    before = "Alice likes tea. Bob likes tea.\n"
    target = note(gateway.tools.root, before)
    result = gateway.dispatch("edit_note", {"path": "People/Alice.md", "edits": edits})
    assert result.startswith("Error:"), result
    assert target.read_text() == before
    assert not gateway.tools.touched


def test_native_validation_and_source_attribution(gateway):
    before = (
        '---\ncategories: ["[[People]]"]\n---\n## About\n- Likes tea.\n## Mentions\n'
    )
    target = note(gateway.tools.root, before)
    gateway.tools.source_claims = {"claim1": ["evidence1"]}
    gateway.tools.allowed_source_episode_keys = {"episode1"}
    gateway.tools.require_source_episode_keys = True
    args = {
        "path": "People/Alice.md",
        "source_claim_ids": ["claim1"],
        "source_episode_keys": ["episode1"],
    }
    args["edits"] = [{"old_text": '["[[People]]"]', "new_text": "[invalid"}]
    assert gateway.dispatch("edit_note", args).startswith("Error:")
    assert target.read_text() == before
    assert not gateway.tools.source_evidence_keys_by_path
    args["edits"] = [{"old_text": "Likes tea.", "new_text": "Likes coffee."}]
    assert gateway.dispatch("edit_note", args).startswith("Edited")
    assert gateway.tools.source_evidence_keys_by_path == {
        "People/Alice.md": {"evidence1"}
    }
    assert gateway.tools.source_episode_keys_by_path == {
        "People/Alice.md": {"episode1"}
    }


def test_native_long_line_continues_with_unicode_range_evidence(gateway):
    content = "heading\n" + "🙂" * 17000 + "NEEDLE" + "z" * 100
    note(gateway.tools.root, content)
    first = gateway.dispatch("read_note", {"path": "People/Alice.md", "offset": 1})
    assert "read_slice(path, char_offset=8)" in first
    assert "bash" not in first
    result = gateway.dispatch(
        "read_slice", {"path": "people/alice.md", "char_offset": 17000, "max_chars": 30}
    )
    assert result.startswith(content[17000:17030])
    assert "NEEDLE" in gateway.read_notes["People/Alice.md"]
    repeated = gateway.dispatch(
        "read_slice", {"path": "People/Alice.md", "char_offset": 17000, "max_chars": 30}
    )
    assert "unchanged read_slice window" in repeated
    assert "NEEDLE" in pi_agent._pi_final_search_prompt(
        "Find the marker", gateway.read_notes
    )


def test_native_reader_budget_and_zero_based_line_continuation(gateway):
    content = "".join(f"line {i} " + "x" * 100 + "\n" for i in range(3000))
    note(gateway.tools.root, content)
    first = gateway.dispatch("read_note", {"path": "People/Alice.md"})
    assert first.startswith(content[:8000])
    assert len(first) < 8300
    assert "read_slice(path, char_offset=8000)" in first
    window = gateway.dispatch(
        "read_note", {"path": "People/Alice.md", "offset": 2000, "limit": 2}
    )
    assert window.startswith("line 2000 ")
    assert "read_note(path, offset=2002)" in window
    assert "Use offset=" not in window


@pytest.mark.parametrize("tool", ["read_note", "read_slice"])
@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "People/Link.md"])
def test_inspection_rejects_escape_before_native_access(gateway, tmp_path, tool, path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.md")
    outside.write_text("HOST SECRET")
    (tmp_path / "People").mkdir(exist_ok=True)
    (tmp_path / "People/Link.md").symlink_to(outside)
    result = gateway.dispatch(tool, {"path": path})
    assert result.startswith("Error:")
    assert "HOST SECRET" not in result
    assert gateway.read_notes == {}
    assert gateway._native_text_tools._process is None


def test_inspection_retains_distinct_windows_but_excludes_base(gateway):
    note(gateway.tools.root, "first fact\nsecond fact\nthird fact\n")
    for offset in [0, 1, 2]:
        gateway.dispatch(
            "read_note", {"path": "People/Alice.md", "offset": offset, "limit": 1}
        )
    evidence = gateway.read_notes["People/Alice.md"]
    assert all(fact in evidence for fact in ["first fact", "second fact", "third fact"])
    base = note(gateway.tools.root, "views: []\n", "Templates/Bases/People.base")
    assert "views" in gateway.dispatch(
        "read_slice", {"path": str(base.relative_to(gateway.tools.root))}
    )
    assert list(gateway.read_notes) == ["People/Alice.md"]


def test_helper_death_rejects_edit_then_next_call_recovers(gateway):
    target = note(gateway.tools.root, "Alice likes tea.\n")
    gateway.dispatch("read_note", {"path": "People/Alice.md"})
    process = gateway._native_text_tools._process
    process.kill()
    process.wait()
    args = {
        "path": "People/Alice.md",
        "edits": [{"old_text": "tea", "new_text": "coffee"}],
    }
    assert gateway.dispatch("edit_note", args).startswith("Error:")
    assert target.read_text() == "Alice likes tea.\n"
    assert gateway._native_text_tools._process is None
    assert gateway.dispatch("edit_note", args).startswith("Edited")


def test_helper_timeout_kills_and_reaps_without_writing(gateway, monkeypatch):
    target = note(gateway.tools.root, "Alice likes tea.\n")
    # A real wedged child exercises the production transport deadline/cleanup.
    process = subprocess.Popen(
        ["node", "-e", "setInterval(() => {}, 1000)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    gateway._native_text_tools._process = process
    monkeypatch.setattr(pi_native_tools, "_CALL_TIMEOUT", 0.05)
    result = gateway.dispatch(
        "edit_note",
        {
            "path": "People/Alice.md",
            "edits": [{"old_text": "tea", "new_text": "coffee"}],
        },
    )
    assert "timed out" in result
    assert target.read_text() == "Alice likes tea.\n"
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed


@pytest.mark.parametrize(
    "args",
    [
        {"char_offset": -1},
        {"char_offset": True},
        {"char_offset": 1.5},
        {"max_chars": 0},
    ],
)
def test_slice_rejects_invalid_ranges(gateway, args):
    note(gateway.tools.root, "text")
    assert gateway.dispatch(
        "read_slice", {"path": "People/Alice.md", **args}
    ).startswith("Error:")


@pytest.mark.asyncio
async def test_search_entrypoint_returns_slice_evidence(
    tmp_path, monkeypatch, native_binary
):
    note(tmp_path, "x" * 60000 + "HIDDEN FACT")
    captured = {}
    monkeypatch.setenv("PI_OPERATING_MEMORY_DIR", str(tmp_path / "operating"))
    monkeypatch.setattr(
        pi_agent, "_resolve_pi_config", lambda *_a, **_k: _runtime_config()
    )
    monkeypatch.setattr(
        pi_agent, "persist_inference_run", lambda **_k: ("request", "artifact")
    )

    async def prompt(*_a):
        return "Search notes"

    monkeypatch.setattr(pi_agent, "_get_prompt", prompt)
    monkeypatch.setattr(
        pi_agent.asyncio,
        "create_subprocess_exec",
        _fake_spawn(
            captured,
            events=_successful_events(
                summary="HIDDEN FACT", tool_name="read_slice", usage={}
            ),
            tool_call=("read_slice", {"path": "People/Alice.md", "char_offset": 60000}),
        ),
    )
    result = await pi_agent.search_vault_with_pi(
        "Find the fact", tmp_path, notes_only=True
    )
    assert result.answer == "HIDDEN FACT"
    assert len(result.notes) == 1
    assert result.notes[0]["path"] == "People/Alice.md"
    assert "HIDDEN FACT" in result.notes[0]["content"]
    assert result.errors == []
