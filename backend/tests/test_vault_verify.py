"""Post-write vault verification: what it catches, and what it must not."""

import subprocess

import pytest

from backend.services.memory.agent.vault_tools import VaultToolError, VaultTools
from backend.services.memory.vault_verify import (
    render_findings,
    verify_day_episode_ranges,
    verify_vault_changes,
)

PERSON_NOTE = """---
categories: ["[[People]]"]
---
## About
- Works on Chronicle.

## Conversations
![[Conversations.base#Person]]

## Mentions
- 2026-08-06 — mentioned the migration.
"""

AGENT_CONTROL_TOPIC = """---
categories: ["[[Topics]]"]
---
## About
- Load testing and end-to-end validation for agent control.
- Policy store extracts policies and skills from requirement docs with org-level and use-case policies, journeys, and soft/hard flags.
- Policy expressions create journey metrics from code-based evidence, with questions about text policy support in agent control.
- Policy output from the LLM can replace the policy-store layer in agent control.
- Galileo sends a trace and project to the policy-store API to compute metrics.
- The agent-flow UI fetches dynamic policy lists per product.

## Conversations
![[Conversations.base#Topic]]
"""

POLICY_STORE_TOPIC = """---
categories: ["[[Topics]]"]
---
## About
- Policy store extracts policies and skills from requirement docs with org-level and use-case policies, journeys, and soft/hard flags.
- Policy expressions create journey metrics from code-based evidence.
- Galileo sends a trace and project to the policy-store API to compute metrics.
- The policy-store layer may be replaced by LLM policy output in agent control.

## Conversations
![[Conversations.base#Topic]]
"""

INVALID_MULTI_AUTHOR_NOTE = """---
categories:
  - "[[Books]]"
author: "[[Heather Cocks]]", "[[Jessica Morgan]]"
created: 2023-12-08
updated: 2023-12-08
---
## About
- A jointly authored novel.
"""


def _snapshot(root):
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in root.rglob("*.md")
    }


def _write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_a_well_formed_new_note_produces_no_findings(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    assert verify_vault_changes(tmp_path, before) == []


def test_write_note_rejects_invalid_yaml_frontmatter_before_touching_vault(tmp_path):
    tools = VaultTools(tmp_path)

    with pytest.raises(VaultToolError, match="invalid YAML frontmatter"):
        tools.write_note("Books/The Royal We.md", INVALID_MULTI_AUTHOR_NOTE)

    assert not (tmp_path / "Books" / "The Royal We.md").exists()
    assert tools.touched == set()


def test_verify_vault_changes_reports_invalid_yaml_frontmatter(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "Books/The Royal We.md", INVALID_MULTI_AUTHOR_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    assert [finding.rule for finding in findings] == ["invalid_frontmatter"]
    assert "author" in findings[0].detail


def test_topic_note_at_vault_root_is_reported(tmp_path):
    """Root Markdown files are category hubs, never ordinary topic notes."""

    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "Fundraising.md",
        "## About\n- Seed round planning.\n\n"
        "## Conversations\n![[Conversations.base#Topic]]\n",
    )

    findings = verify_vault_changes(tmp_path, before)

    assert [finding.rule for finding in findings] == ["root_note_role"]
    assert "Topics/Fundraising.md" in findings[0].detail


def test_new_organic_category_hub_bundle_is_accepted(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "Templates/Projects Template.md", "---\ncategories: []\n---\n")
    base = tmp_path / "Templates" / "Bases" / "Projects.base"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("views: []\n", encoding="utf-8")
    _write(
        tmp_path,
        "Projects.md",
        "---\ntags:\n  - categories\n---\n# Projects\n\n"
        "Everything categorised under Projects.\n\n![[Projects.base]]\n",
    )

    assert verify_vault_changes(tmp_path, before) == []


def test_day_verifier_rejects_new_organic_category_bundle(tmp_path):
    before = _snapshot(tmp_path)
    VaultTools(tmp_path).create_category("Companies", ["org", "type"])

    findings = verify_vault_changes(
        tmp_path,
        before,
        forbid_new_categories=True,
    )

    category_findings = [
        finding for finding in findings if finding.rule == "new_category"
    ]
    assert len(category_findings) == 1
    assert category_findings[0].path == "Companies.md"
    assert "cannot invent" in category_findings[0].detail


def test_day_tool_rejects_category_creation_before_writing_any_bundle_file(tmp_path):
    tools = VaultTools(tmp_path, allow_new_categories=False)

    with pytest.raises(VaultToolError, match="Refusing to create category 'Companies'"):
        tools.create_category("Companies", ["org", "type"])

    assert not (tmp_path / "Companies.md").exists()
    assert not (tmp_path / "Templates" / "Companies Template.md").exists()
    assert not (tmp_path / "Templates" / "Bases" / "Companies.base").exists()


def test_day_tool_may_reuse_an_already_complete_category(tmp_path):
    VaultTools(tmp_path).create_category("Companies", ["org", "type"])
    tools = VaultTools(tmp_path, allow_new_categories=False)

    assert "already exists" in tools.create_category("Companies", ["org", "type"])


def test_new_person_note_missing_the_aggregation_embed_is_reported(tmp_path):
    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "People/Vatsal.md",
        PERSON_NOTE.replace("![[Conversations.base#Person]]", ""),
    )

    findings = verify_vault_changes(tmp_path, before)

    assert [f.rule for f in findings] == ["note_schema"]
    # The finding must tell the model how to fix it, not just what is wrong.
    assert "Person Template.md" in findings[0].detail


def test_empty_new_person_scaffold_is_not_accepted_as_memory(tmp_path):
    before = _snapshot(tmp_path)
    note = PERSON_NOTE.replace("- Works on Chronicle.", "-").replace(
        "- 2026-08-06 — mentioned the migration.", "-"
    )
    _write(tmp_path, "People/Aryan Neosapiens.md", note)

    findings = verify_vault_changes(tmp_path, before)

    assert [finding.rule for finding in findings] == ["empty_semantic_note"]
    assert "unresolved wikilink" in findings[0].detail


def test_empty_new_topic_scaffold_is_not_accepted_as_memory(tmp_path):
    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "Topics/Thin Topic.md",
        '---\ncategories: ["[[Topics]]"]\n---\n'
        "## About\n-\n\n## Conversations\n![[Conversations.base#Topic]]\n",
    )

    findings = verify_vault_changes(tmp_path, before)

    assert [finding.rule for finding in findings] == ["empty_semantic_note"]


def test_newly_duplicated_section_is_reported(tmp_path):
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE + "\n## About\n- pasted again\n")

    findings = verify_vault_changes(tmp_path, before)

    assert [f.rule for f in findings] == ["duplicate_section"]
    assert "'## about'" in findings[0].detail


def test_preexisting_duplication_is_not_blamed_on_this_run(tmp_path):
    """Otherwise a note that is already broken can never be repaired."""

    broken = PERSON_NOTE + "\n## About\n- duplicated before this run\n"
    _write(tmp_path, "People/Vatsal.md", broken)
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", broken + "- one genuinely new fact\n")

    assert verify_vault_changes(tmp_path, before) == []


def test_case_only_variant_of_an_existing_note_is_reported(tmp_path):
    _write(tmp_path, "People/hermes-labs.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Hermes-Labs.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    rules = [f.rule for f in findings]
    assert "case_collision" in rules
    detail = next(f.detail for f in findings if f.rule == "case_collision")
    assert "People/Hermes-Labs.md" in detail and "People/hermes-labs.md" in detail


def test_untouched_case_collision_is_not_reported(tmp_path):
    """Only problems this run introduced — an old pair is not this agent's to fix."""

    _write(tmp_path, "People/hermes-labs.md", PERSON_NOTE)
    _write(tmp_path, "People/Hermes-Labs.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "Topics/Chronicle.md",
        "## About\n- unrelated\n\n## Conversations\n![[Conversations.base#Topic]]\n",
    )

    assert verify_vault_changes(tmp_path, before) == []


def test_diarization_placeholder_person_note_is_reported(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Unknown Speaker 2.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    assert "not_a_person" in [f.rule for f in findings]


def test_hermes_is_a_topic_not_a_person(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Hermes.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before)

    detail = next(f.detail for f in findings if f.rule == "not_a_person")
    assert "Topics/Hermes.md" in detail


def test_required_note_never_written_is_reported(tmp_path):
    """DeepSeek V4 Pro updated two People notes for a day and skipped the day note.

    It stopped after ten rounds with no error, no truncation and no stall — it simply
    believed it was finished, which is exactly what a self-reported checklist cannot
    catch and a filesystem check can.
    """

    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before, required=["Daily/2026-08-06.md"])

    assert [f.rule for f in findings] == ["record_missing"]
    assert findings[0].path == "Daily/2026-08-06.md"


def test_required_note_written_this_run_is_accepted(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "Daily/2026-08-06.md", "## 11:41 Standup\n- shipped it\n")

    assert (
        verify_vault_changes(tmp_path, before, required=["Daily/2026-08-06.md"]) == []
    )


def test_required_note_left_untouched_from_a_previous_run_is_reported(tmp_path):
    """A day note already on disk is not evidence that *this* run recorded the day."""

    _write(tmp_path, "Daily/2026-08-06.md", "## 09:00 Yesterday's write\n- old\n")
    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    findings = verify_vault_changes(tmp_path, before, required=["Daily/2026-08-06.md"])

    assert [f.rule for f in findings] == ["record_missing"]


def test_day_episode_ranges_must_exactly_match_the_current_digest(tmp_path):
    """A successful agent edit cannot leave stale bounds from an older analysis."""

    note = _write(
        tmp_path,
        "Daily/2026-08-10.md",
        """# 2026-08-10

## Episodes

- 06:10–06:10 · meeting — stale short meeting
- 21:46–22:09 · application_state — new late episode
""",
    )
    digest = """Local day 2026-08-10 (Etc/UTC), 2 episode(s).

### 06:10–06:52 · meeting · highlight
title: ADS Weekly Planning Sync

### 21:46–22:09 · application_state · background
title: Late Zed review
"""

    findings = verify_day_episode_ranges(note, digest)

    assert [finding.rule for finding in findings] == ["episode_ranges"]
    assert "06:10–06:52" in findings[0].detail
    assert "06:10–06:10" in findings[0].detail


def test_day_episode_ranges_accept_exactly_one_bullet_per_digest_episode(tmp_path):
    note = _write(
        tmp_path,
        "Daily/2026-08-10.md",
        """# 2026-08-10

## Episodes

- 06:10–06:52 · meeting · highlight — ADS Weekly Planning Sync
- 21:46–22:09 · application_state · background — Late Zed review
""",
    )
    digest = """Local day 2026-08-10 (Etc/UTC), 2 episode(s).

### 06:10–06:52 · meeting · highlight
title: ADS Weekly Planning Sync

### 21:46–22:09 · application_state · background
title: Late Zed review
"""

    assert verify_day_episode_ranges(note, digest) == []


def test_verify_vault_does_not_demand_the_record_note_from_a_run_that_touched_nothing(
    tmp_path,
):
    """An agent that judged the day already covered made a legitimate no-op.

    Demanding the record note there would turn a correct decision into a redundant
    write, which is the opposite of what the check is for.
    """

    tools = VaultTools(tmp_path, required_notes=["Daily/2026-08-06.md"])
    tools.baseline()

    assert "passed" in tools.verify_vault()

    tools.write_note("People/Vatsal.md", PERSON_NOTE)

    assert "Daily/2026-08-06.md" in tools.verify_vault()


def test_day_write_minting_a_conversation_note_is_reported(tmp_path):
    """Qwen3.6 wrote Conversations/ads-standup-2026-08-06.md from a day episode.

    Conversations/ is one note per real conversation, keyed by conversation_id. A day
    write has none, so anything it puts there is invented and shadows the note the
    conversation path would write.
    """

    before = _snapshot(tmp_path)
    _write(
        tmp_path, "Conversations/ads-standup-2026-08-06.md", "## Summary\n- standup\n"
    )

    findings = verify_vault_changes(
        tmp_path, before, forbidden_folders=["Conversations"]
    )

    assert [f.rule for f in findings] == ["forbidden_folder"]
    assert "Conversations/" in findings[0].detail


def test_a_conversation_note_is_fine_when_no_folder_is_forbidden(tmp_path):
    """The conversation write path must keep writing exactly this note."""

    before = _snapshot(tmp_path)
    _write(tmp_path, "Conversations/69d2574e.md", "## Summary\n- a real conversation\n")

    assert verify_vault_changes(tmp_path, before) == []


def test_day_write_changing_people_mentions_is_reported(tmp_path):
    """Daily/Timeline owns chronology; a native agent cannot bypass that contract."""

    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)
    before = _snapshot(tmp_path)
    _write(
        tmp_path,
        "People/Vatsal.md",
        PERSON_NOTE.replace(
            "- 2026-08-06 — mentioned the migration.",
            "- 2026-08-06 — mentioned the migration.\n"
            "- 2026-08-07 — worked on the migration all day.",
        ),
    )

    findings = verify_vault_changes(
        tmp_path,
        before,
        immutable_sections=[("People", "Mentions")],
    )

    assert [finding.rule for finding in findings] == ["immutable_section"]
    assert "Daily/Timeline" in findings[0].detail


def test_day_write_may_create_person_with_empty_mentions_placeholder(tmp_path):
    """A canonical new Person template still carries the empty Mentions section."""

    before = _snapshot(tmp_path)
    note = PERSON_NOTE.replace(
        "- 2026-08-06 — mentioned the migration.",
        "-",
    )
    _write(tmp_path, "People/Vatsal.md", note)

    assert (
        verify_vault_changes(
            tmp_path,
            before,
            immutable_sections=[("People", "Mentions")],
        )
        == []
    )


def test_day_tool_rejects_mentions_edit_without_mutating_note(tmp_path):
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)
    tools = VaultTools(
        tmp_path,
        immutable_sections=[("People", "Mentions")],
    )

    with pytest.raises(VaultToolError, match="immutable.*Mentions"):
        tools.edit_section(
            "People/Vatsal.md",
            "Mentions",
            "- 2026-08-07 — worked on the migration all day.",
        )

    assert (tmp_path / "People" / "Vatsal.md").read_text(
        encoding="utf-8"
    ) == PERSON_NOTE


def test_day_tool_allows_durable_about_edit(tmp_path):
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)
    tools = VaultTools(
        tmp_path,
        immutable_sections=[("People", "Mentions")],
    )

    tools.edit_section(
        "People/Vatsal.md",
        "About",
        "- Maintains the capture pipeline.",
    )

    written = (tmp_path / "People" / "Vatsal.md").read_text(encoding="utf-8")
    assert "- Maintains the capture pipeline." in written
    assert "- 2026-08-06 — mentioned the migration." in written


def test_new_topic_whose_scope_repeats_another_new_topic_is_reported(tmp_path):
    """The exact Agent Control/Policy Store failure from the partial corpus run."""

    before = _snapshot(tmp_path)
    _write(tmp_path, "Topics/Agent Control.md", AGENT_CONTROL_TOPIC)
    _write(tmp_path, "Topics/Policy Store.md", POLICY_STORE_TOPIC)

    findings = verify_vault_changes(tmp_path, before)

    overlaps = [
        finding for finding in findings if finding.rule == "topic_scope_overlap"
    ]
    assert len(overlaps) == 1
    assert overlaps[0].path == "Topics/Policy Store.md"
    assert "Topics/Agent Control.md" in overlaps[0].detail


def test_topic_tool_rejects_second_overlapping_note_before_writing(tmp_path):
    tools = VaultTools(tmp_path)
    tools.write_note("Topics/Agent Control.md", AGENT_CONTROL_TOPIC)

    with pytest.raises(VaultToolError, match="overlap.*Agent Control"):
        tools.write_note("Topics/Policy Store.md", POLICY_STORE_TOPIC)

    assert not (tmp_path / "Topics" / "Policy Store.md").exists()


def test_related_topic_with_mostly_unique_facts_is_not_collapsed(tmp_path):
    before = _snapshot(tmp_path)
    _write(tmp_path, "Topics/Agent Control.md", AGENT_CONTROL_TOPIC)
    _write(
        tmp_path,
        "Topics/Load Testing.md",
        """---
categories: ["[[Topics]]"]
---
## About
- Load testing and end-to-end validation for agent control.
- Soak tests hold a fixed request rate for six hours.
- The harness records first-token latency and saturation throughput.
- Canary runs compare one worker against bounded two-window concurrency.

## Conversations
![[Conversations.base#Topic]]
""",
    )

    findings = verify_vault_changes(tmp_path, before)

    assert "topic_scope_overlap" not in [finding.rule for finding in findings]


def test_render_findings_is_actionable_and_says_so_when_clean(tmp_path):
    assert "passed" in render_findings([])

    before = _snapshot(tmp_path)
    _write(tmp_path, "People/Unknown Speaker 1.md", PERSON_NOTE)
    text = render_findings(verify_vault_changes(tmp_path, before))

    assert "verify_vault again" in text
    assert "People/Unknown Speaker 1.md" in text


def test_read_note_windows_a_long_note_instead_of_returning_all_of_it(tmp_path):
    """An unbounded read is what pushed a 215 KB note through a 65k context."""

    _write(tmp_path, "Daily/2026-08-06.md", "\n".join(f"line {i}" for i in range(5000)))
    tools = VaultTools(tmp_path)

    first = tools.read_note("Daily/2026-08-06.md")

    assert "line 0" in first and "line 4999" not in first
    assert "of 5000" in first
    assert len(first) < 8_000 + 500

    later = tools.read_note("Daily/2026-08-06.md", offset=4990)
    assert "line 4999" in later


def test_read_note_returns_a_short_note_whole_and_unadorned(tmp_path):
    _write(tmp_path, "People/Vatsal.md", PERSON_NOTE)

    assert VaultTools(tmp_path).read_note("People/Vatsal.md") == PERSON_NOTE


def test_read_note_can_page_through_one_very_long_line(tmp_path):
    _write(tmp_path, "Topics/Long Line.md", "x" * 20_000)
    tools = VaultTools(tmp_path)

    first = tools.read_note("Topics/Long Line.md")
    second = tools.read_slice("Topics/Long Line.md", char_offset=8_000, max_chars=8_000)
    final = tools.read_slice("Topics/Long Line.md", char_offset=16_000, max_chars=8_000)

    assert "char_offset=8000" in first
    assert "char_offset=16000" in second
    assert first.startswith("x" * 8_000)
    assert second.startswith("x" * 8_000)
    assert final.startswith("x" * 4_000)
    assert "truncated at 8000" not in final


def test_grep_content_is_bounded_by_characters_not_only_lines(tmp_path):
    _write(tmp_path, "Topics/Large.md", "match " + "x" * 20_000 + "\n")

    result = VaultTools(tmp_path).grep("match", output_mode="content")

    assert len(result) < 8_200
    assert "search output truncated at 8000 characters" in result


def test_grep_compacts_unchanged_result_when_only_head_limit_increases(tmp_path):
    _write(tmp_path, "Topics/One.md", "## First\n## Second\n")
    tools = VaultTools(tmp_path)

    first = tools.grep("^## ", output_mode="content", head_limit=100)
    repeated = tools.grep("^## ", output_mode="content", head_limit=200)

    assert "Topics/One.md:1:## First" in first
    assert "Topics/One.md:1:## First" not in repeated
    assert "Search result unchanged" in repeated
    assert "changing ignored arguments or head_limit adds no output" in repeated.lower()


def test_grep_compacts_unchanged_result_when_ripgrep_reorders_lines(
    tmp_path, monkeypatch
):
    tools = VaultTools(tmp_path)
    outputs = iter(
        [
            "./Topics/A.md:1:a\n./Topics/B.md:1:b\n",
            "./Topics/B.md:1:b\n./Topics/A.md:1:a\n",
        ]
    )

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    first = tools.grep("a|b", glob="Topics/*.md", output_mode="content", head_limit=20)
    repeated = tools.grep(
        "a|b", glob="Topics/*.md", output_mode="content", head_limit=20
    )

    assert "Topics/A.md:1:a" in first
    assert "Search result unchanged" in repeated
    assert len(repeated) < 300


def test_grep_cache_ignores_context_when_output_mode_cannot_use_it(tmp_path):
    _write(tmp_path, "Topics/One.md", "No matching heading\n")
    tools = VaultTools(tmp_path)

    first = tools.grep("absent", output_mode="files_with_matches", context=0)
    repeated = tools.grep("absent", output_mode="files_with_matches", context=37)

    assert first == "No matches found."
    assert "Search result unchanged" in repeated
    assert "no new vault evidence" in repeated


def test_grep_context_change_is_full_only_while_it_reveals_new_evidence(tmp_path):
    _write(tmp_path, "Topics/One.md", "before\n## Match\nafter\n")
    tools = VaultTools(tmp_path)

    narrow = tools.grep("^## ", output_mode="content", context=0)
    expanded = tools.grep("^## ", output_mode="content", context=1)
    saturated = tools.grep("^## ", output_mode="content", context=2)

    assert "before" not in narrow
    assert "before" in expanded
    assert "Search result unchanged" not in expanded
    assert "Search result unchanged" in saturated


def test_grep_does_not_compact_when_higher_limit_reveals_more_lines(tmp_path):
    _write(tmp_path, "Topics/One.md", "## First\n## Second\n")
    tools = VaultTools(tmp_path)

    first = tools.grep("^## ", output_mode="content", head_limit=1)
    expanded = tools.grep("^## ", output_mode="content", head_limit=2)

    assert "more line(s) truncated" in first
    assert "Topics/One.md:2:## Second" in expanded
    assert "Search result unchanged" not in expanded


def test_grep_cache_never_hides_changed_vault_evidence(tmp_path):
    _write(tmp_path, "Topics/One.md", "## First\n")
    tools = VaultTools(tmp_path)
    tools.grep("^## ", output_mode="content", head_limit=100)
    _write(tmp_path, "Topics/Two.md", "## New evidence\n")

    changed = tools.grep("^## ", output_mode="content", head_limit=200)

    assert "Topics/Two.md:1:## New evidence" in changed
    assert "Search result unchanged" not in changed


def test_grep_result_compaction_can_be_disabled_for_paired_benchmark(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CHRONICLE_GREP_RESULT_COMPACTION", "off")
    _write(tmp_path, "Topics/One.md", "## First\n")
    tools = VaultTools(tmp_path)

    first = tools.grep("^## ", output_mode="content")
    repeated = tools.grep("^## ", output_mode="content")

    assert repeated == first
    assert "Search result unchanged" not in repeated
