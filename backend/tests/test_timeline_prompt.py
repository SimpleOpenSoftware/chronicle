from backend.services.timeline.prompt import (
    INTERPRETATION_OUTPUT_SCHEMA,
    INTERPRETATION_PROMPT_VERSION,
    SEPARATION_OUTPUT_SCHEMA,
    SEPARATION_PROMPT_VERSION,
    build_interpretation_prompt,
    build_separation_prompt,
)


def _assert_strict_objects(schema: object) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
        for value in schema.values():
            _assert_strict_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_strict_objects(value)


def test_timeline_output_schema_has_no_open_objects():
    _assert_strict_objects(SEPARATION_OUTPUT_SCHEMA)
    _assert_strict_objects(INTERPRETATION_OUTPUT_SCHEMA)


def test_interpretation_schema_requires_grounded_assertions():
    episode = INTERPRETATION_OUTPUT_SCHEMA["properties"]["accepted"]["items"]
    assertion = episode["properties"]["assertions"]["items"]
    assert assertion["properties"]["evidence_ids"]["minItems"] == 1


def test_prompt_version_is_pinned():
    """Changing the prompt text without bumping this leaves cached runs on the old rules.

    Asserted once, here — not inside each content test, where two of them disagreeing is
    the only thing a second copy can achieve.
    """

    assert SEPARATION_PROMPT_VERSION == "timeline-separation-pi-v1"
    assert INTERPRETATION_PROMPT_VERSION == "timeline-interpretation-pi-v1"


def test_separation_schema_contains_only_structure_and_explicit_lineage():
    episode = SEPARATION_OUTPUT_SCHEMA["properties"]["hypotheses"]["items"]

    assert "title" not in episode["properties"]
    assert "summary" not in episode["properties"]
    assert "kind" not in episode["properties"]
    assert "lineage" in episode["required"]
    assert {
        action
        for branch in episode["properties"]["lineage"]["anyOf"]
        for action in branch["properties"]["action"]["enum"]
    } == {
        "new",
        "carry",
        "split",
        "merge",
    }


def test_interpretation_schema_joins_semantics_without_structure():
    episode = INTERPRETATION_OUTPUT_SCHEMA["properties"]["accepted"]["items"]

    assert "hypothesis_id" in episode["required"]
    assert "title" in episode["required"]
    assert "started_at" not in episode["properties"]
    assert "ended_at" not in episode["properties"]
    assert "evidence_ids" not in episode["properties"]


def test_stage_tools_receive_their_distinct_result_contracts():
    # Check the interfaces, not a prescribed phrasing or investigation sequence.
    assert '"hypotheses"' in build_separation_prompt()
    assert '"retirements"' in build_separation_prompt()
    assert '"accepted"' in build_interpretation_prompt()
    assert '"rejected"' in build_interpretation_prompt()
