"""Vault filesystem confinement for model-driven memory tools."""

import contextlib

import pytest

from backend.services.memory.agent import vault_tools
from backend.services.memory.agent.vault_tools import VaultToolError, VaultTools
from backend.services.memory.vault_scaffold import VaultPathError, write_category
from backend.services.memory.vault_templates import PERSON_TEMPLATE, TOPIC_TEMPLATE


@pytest.fixture
def unlocked(monkeypatch):
    """Keep filesystem-security tests independent of the Redis-backed vault lock."""

    @contextlib.contextmanager
    def no_lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(vault_tools, "vault_note_lock", no_lock)


def _person(name: str) -> str:
    return PERSON_TEMPLATE.replace("{{date}}", "2026-08-06").replace(
        "## About\n-", f"## About\n- {name}"
    )


def test_absolute_category_cannot_create_files_outside_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside_stem = tmp_path / "escaped"

    with pytest.raises(VaultPathError, match="vault-relative path"):
        write_category(vault, str(outside_stem), [])

    assert not outside_stem.with_suffix(".md").exists()
    assert list(vault.rglob("*")) == []


@pytest.mark.parametrize(
    "category", ['Books "Private"', "Projects]]", "Topics # Internal"]
)
def test_category_rejects_yaml_and_wikilink_metacharacters(tmp_path, category):
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(VaultPathError, match="letters/numbers"):
        write_category(vault, category, [])

    assert list(vault.rglob("*")) == []


@pytest.mark.usefixtures("unlocked")
def test_category_rejects_traversal_but_preserves_unicode_and_spaces(tmp_path):
    vault = tmp_path / "vault"
    tools = VaultTools(vault)

    with pytest.raises(VaultToolError, match="Invalid category name"):
        tools.create_category("../escaped")

    tools.create_category("Research Áreas", ["status"])

    assert not (tmp_path / "escaped.md").exists()
    assert (vault / "Research Áreas.md").is_file()
    assert (vault / "Templates" / "Research Áreas Template.md").is_file()


def test_read_note_rejects_symlink_leaf(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Topics").mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("outside secret", encoding="utf-8")
    (vault / "Topics" / "Linked.md").symlink_to(outside)

    with pytest.raises(VaultToolError, match="symbolic link"):
        VaultTools(vault).read_note("Topics/Linked.md")


@pytest.mark.usefixtures("unlocked")
def test_read_existing_base_definition_through_registered_tool(tmp_path):
    vault = tmp_path / "vault"
    path = vault / "Templates" / "Bases" / "Projects.base"
    path.parent.mkdir(parents=True)
    content = "views:\n  - type: table\n    name: Projects\n"
    path.write_text(content)
    tools = VaultTools(vault)
    assert (
        tools.dispatch("read_note", {"path": "Templates/Bases/Projects.base"})
        == content
    )
    assert "views:" in tools.read_note("Templates/Bases/Projects.base", limit=1)
    with pytest.raises(VaultToolError):
        tools.write_note("Templates/Bases/Projects.base", "changed", overwrite=True)
    assert path.read_text() == content
    assert not tools.touched


@pytest.mark.parametrize(
    "unsafe", ["../outside.base", "/tmp/outside.base", "Templates/Linked.base"]
)
def test_base_definition_reads_preserve_confinement(tmp_path, unsafe):
    vault = tmp_path / "vault"
    (vault / "Templates").mkdir(parents=True)
    outside = tmp_path / "outside.base"
    outside.write_text("private")
    (vault / "Templates" / "Linked.base").symlink_to(outside)
    with pytest.raises(VaultToolError):
        VaultTools(vault).dispatch("read_note", {"path": unsafe})


@pytest.mark.usefixtures("unlocked")
def test_edit_and_overwrite_reject_symlink_leaf(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Topics").mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("outside secret", encoding="utf-8")
    (vault / "Topics" / "Linked.md").symlink_to(outside)
    tools = VaultTools(vault)

    with pytest.raises(VaultToolError, match="symbolic link"):
        tools.edit_note(
            "Topics/Linked.md",
            [{"old_text": "outside", "new_text": "changed"}],
        )
    with pytest.raises(VaultToolError, match="symbolic link"):
        tools.write_note("Topics/Linked.md", "changed", overwrite=True)

    assert outside.read_text(encoding="utf-8") == "outside secret"


@pytest.mark.usefixtures("unlocked")
def test_write_rejects_symlink_parent_directory(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "Topics").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VaultToolError, match="symbolic link"):
        VaultTools(vault).write_note("Topics/New.md", TOPIC_TEMPLATE)

    assert not (outside / "New.md").exists()


@pytest.mark.usefixtures("unlocked")
@pytest.mark.parametrize("linked_side", ["source", "target"])
def test_rename_rejects_symlinked_source_or_target(tmp_path, linked_side):
    vault = tmp_path / "vault"
    people = vault / "People"
    people.mkdir(parents=True)
    outside = tmp_path / "outside-person.md"
    outside.write_text(_person("Outside"), encoding="utf-8")
    source = people / "Alice.md"
    target = people / "Alicia.md"
    if linked_side == "source":
        source.symlink_to(outside)
    else:
        source.write_text(_person("Alice"), encoding="utf-8")
        target.symlink_to(outside)

    with pytest.raises(VaultToolError, match="symbolic link"):
        VaultTools(vault).rename_person("Alice", "Alicia")

    assert outside.read_text(encoding="utf-8") == _person("Outside")


@pytest.mark.usefixtures("unlocked")
def test_rename_rejects_symlink_in_backlink_scan(tmp_path):
    vault = tmp_path / "vault"
    people = vault / "People"
    people.mkdir(parents=True)
    (people / "Alice.md").write_text(_person("Alice"), encoding="utf-8")
    conversations = vault / "Conversations"
    conversations.mkdir()
    outside = tmp_path / "outside-conversation.md"
    outside.write_text("A link to [[Alice]].", encoding="utf-8")
    (conversations / "Linked.md").symlink_to(outside)

    with pytest.raises(VaultToolError, match="symbolic link"):
        VaultTools(vault).rename_person("Alice", "Alicia")

    assert (people / "Alice.md").is_file()
    assert not (people / "Alicia.md").exists()
    assert outside.read_text(encoding="utf-8") == "A link to [[Alice]]."


@pytest.mark.usefixtures("unlocked")
def test_valid_unicode_note_and_safe_audit_paths_are_preserved(tmp_path):
    tools = VaultTools(tmp_path / "vault")

    tools.write_note(
        "Topics/Café au lait.md",
        TOPIC_TEMPLATE.replace("{{date}}", "2026-08-06"),
    )

    assert tools.touched == {"Topics/Café au lait.md"}
    with pytest.raises(VaultToolError, match="Unsafe vault audit path"):
        tools._mark_touched("../outside.md")
    with pytest.raises(VaultToolError, match="Unsafe vault audit path"):
        tools._record_removal("../outside.md", "Topics/Café au lait.md", "before")
