"""Prompt and output schema for the semantic episode agent."""

import json

# The two range stages have independent cache identities. Changing an interpretation
# instruction must not invalidate a structurally identical separation (or vice versa).
SEPARATION_PROMPT_VERSION = "timeline-separation-pi-v1"
INTERPRETATION_PROMPT_VERSION = "timeline-interpretation-pi-v1"


_STAGED_SCHEMA_COMPONENTS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "episodes": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "started_at": {"type": "string", "format": "date-time"},
                    "ended_at": {"type": "string", "format": "date-time"},
                    "conversational": {"type": "boolean"},
                    "salience": {
                        "type": "string",
                        "enum": ["background", "routine", "notable", "highlight"],
                    },
                    "activity_mode": {
                        "type": "string",
                        "enum": ["foreground", "background", "ambient", "idle"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["key", "value"],
                        },
                    },
                    "assertions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "claim": {"type": "string"},
                                "role": {
                                    "type": "string",
                                    "enum": [
                                        "user_action",
                                        "user_statement",
                                        "third_party",
                                        "application_state",
                                        "media_content",
                                        "assistant_generated",
                                        "ambient",
                                        "uncertain",
                                    ],
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "evidence_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                            },
                            "required": ["claim", "role", "confidence", "evidence_ids"],
                        },
                    },
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "related_conversation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "parent_episode_index": {"type": ["integer", "null"]},
                    "representative_evidence_id": {"type": ["string", "null"]},
                },
                "required": [
                    "kind",
                    "title",
                    "summary",
                    "started_at",
                    "ended_at",
                    "conversational",
                    "salience",
                    "activity_mode",
                    "confidence",
                    "entities",
                    "attributes",
                    "assertions",
                    "evidence_ids",
                    "related_conversation_ids",
                    "parent_episode_index",
                    "representative_evidence_id",
                ],
            },
        },
        "unassigned_intervals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "started_at": {"type": "string", "format": "date-time"},
                    "ended_at": {"type": "string", "format": "date-time"},
                    "reason": {"type": "string"},
                },
                "required": ["started_at", "ended_at", "reason"],
            },
        },
    },
    "required": ["episodes", "unassigned_intervals"],
}


_EPISODE_REVISION_REF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "episode_key": {"type": "string"},
        "revision": {"type": "integer", "minimum": 1},
    },
    "required": ["episode_key", "revision"],
}

_LINEAGE_SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": actions},
                "predecessor_revisions": {
                    "type": "array",
                    "items": _EPISODE_REVISION_REF_SCHEMA,
                    "minItems": minimum,
                    **({"maxItems": maximum} if maximum is not None else {}),
                },
            },
            "required": ["action", "predecessor_revisions"],
        }
        for actions, minimum, maximum in [
            (["new"], 0, 0),
            (["carry", "split"], 1, 1),
            (["merge"], 2, None),
        ]
    ]
}


_CONTEXT_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        # Chronicle replaces this placeholder with the canonical request hash before
        # persistence; keeping it in the typed stage result makes the handoff explicit.
        "context_request_id": {"type": "string"},
        "hypothesis_id": {"type": ["string", "null"]},
        "stage": {"type": "string", "enum": ["separation", "interpretation"]},
        "locator": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "capture_source_id": {"type": "string"},
                "modality": {
                    "type": "string",
                    "enum": ["screen", "audio", "transcript", "photo", "context"],
                },
                "track_id": {"type": ["string", "null"]},
            },
            "required": ["capture_source_id", "modality", "track_id"],
        },
        "started_at": {"type": "string", "format": "date-time"},
        "ended_at": {"type": "string", "format": "date-time"},
        "base_manifest_hash": {"type": "string"},
        "leased_evidence_revision": {"type": "integer", "minimum": 0},
        "target_resolution": {"type": "string"},
        "max_items": {"type": "integer", "minimum": 1, "maximum": 100},
        "reason": {"type": "string", "maxLength": 500},
    },
    "required": [
        "context_request_id",
        "hypothesis_id",
        "stage",
        "locator",
        "started_at",
        "ended_at",
        "base_manifest_hash",
        "leased_evidence_revision",
        "target_resolution",
        "max_items",
        "reason",
    ],
}


SEPARATION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "started_at": {"type": "string", "format": "date-time"},
                    "ended_at": {"type": "string", "format": "date-time"},
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "start_anchor_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "end_anchor_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "lineage": _LINEAGE_SCHEMA,
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "review_reasons": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "hypothesis_id",
                    "started_at",
                    "ended_at",
                    "evidence_ids",
                    "start_anchor_ids",
                    "end_anchor_ids",
                    "lineage",
                    "confidence",
                    "review_reasons",
                ],
            },
        },
        "retirements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "predecessor_revision": _EPISODE_REVISION_REF_SCHEMA,
                    "reason": {"type": "string"},
                },
                "required": ["predecessor_revision", "reason"],
            },
        },
        "unassigned_evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "unresolved_intervals": _STAGED_SCHEMA_COMPONENTS["properties"][
            "unassigned_intervals"
        ],
        "context_requests": {
            "type": "array",
            "maxItems": 1,
            "items": _CONTEXT_REQUEST_SCHEMA,
        },
    },
    "required": [
        "hypotheses",
        "retirements",
        "unassigned_evidence_ids",
        "unresolved_intervals",
        "context_requests",
    ],
}


_SEMANTIC_PROPERTIES = {
    key: value
    for key, value in _STAGED_SCHEMA_COMPONENTS["properties"]["episodes"]["items"][
        "properties"
    ].items()
    if key not in {"started_at", "ended_at", "evidence_ids"}
}


INTERPRETATION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accepted": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    **_SEMANTIC_PROPERTIES,
                },
                "required": [
                    "hypothesis_id",
                    *[
                        field
                        for field in _STAGED_SCHEMA_COMPONENTS["properties"][
                            "episodes"
                        ]["items"]["required"]
                        if field not in {"started_at", "ended_at", "evidence_ids"}
                    ],
                ],
            },
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "reason_code": {
                        "type": "string",
                        "enum": [
                            "incoherent",
                            "mixed_activities",
                            "redundant_activity",
                            "insufficient_context",
                        ],
                    },
                    "explanation": {"type": "string"},
                    "implicated_evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "hypothesis_id",
                    "reason_code",
                    "explanation",
                    "implicated_evidence_ids",
                ],
            },
        },
        "context_requests": {
            "type": "array",
            "maxItems": 1,
            "items": _CONTEXT_REQUEST_SCHEMA,
        },
    },
    "required": ["accepted", "rejected", "context_requests"],
}


SEPARATION_PREAMBLE = """Organize evidence within the authorized time range into grounded activity hypotheses.
Inspect original sources and relevant accepted knowledge as needed. Preserve uncertainty,
source identity and independently supported activities. Separate activity evidence from
recording coverage. Prior accounts are revisable interpretations; honor explicit user decisions.

Cite supplied evidence and boundary anchors. Account for the range through hypotheses,
unassigned evidence and unresolved intervals. Use exact predecessor revisions for lineage:
new has none, carry has one, split shares one across outputs, merge has multiple.
Omitted predecessors remain active; retirements explicitly remove a predecessor and cannot
also consume it in lineage. Preserve pinned fields. Out-of-range evidence supplies context,
not authorization to change it. Request bounded additional context when needed using the
supplied revision fences. Submit the structured result through finish_task."""

INTERPRETATION_PREAMBLE = """Interpret the supplied validated activity hypotheses using their original evidence
and relevant accepted knowledge. Assess meaning, attribution and uncertainty without
changing hypothesis bounds, membership or lineage. Join results by hypothesis_id.
Cite assertions only to evidence belonging to that hypothesis. Accept grounded accounts;
reject unsupported or incoherent hypotheses with a reason and implicated evidence.
Request bounded additional context when needed using the supplied revision fences.
Submit the structured result through finish_task."""


def _stage_prompt(
    preamble: str,
    schema: dict,
    *,
    stage: str,
    evidence_guide: str | None,
) -> str:
    guide = evidence_guide or (
        "Read README.md and windows/index.json, then process every numbered window "
        "JSON in order."
    )
    return (
        preamble
        + "\n\n"
        + guide
        + f"\nReturn only one schema-valid {stage} JSON object, with no Markdown "
        "fence or commentary:\n\n" + json.dumps(schema, indent=2)
    )


def build_separation_prompt(*, evidence_guide: str | None = None) -> str:
    return _stage_prompt(
        SEPARATION_PREAMBLE,
        SEPARATION_OUTPUT_SCHEMA,
        stage="separation",
        evidence_guide=evidence_guide,
    )


def build_interpretation_prompt(*, evidence_guide: str | None = None) -> str:
    return _stage_prompt(
        INTERPRETATION_PREAMBLE,
        INTERPRETATION_OUTPUT_SCHEMA,
        stage="interpretation",
        evidence_guide=evidence_guide,
    )


PHOTO_HISTORY_RULES = """Photos are independent event evidence and need no conversation overlap.
Use capture timestamps as anchors, not server arrival/processing timestamps. Only visually
inspected assets support pixel claims; unsampled metadata does not prove scene content.
Immich named people are provider associations, not identities inferred from pixels. Do not
assume the user attended, photographed, owned, or participated merely because an asset is
in their library. Preserve uncertainty and provenance. Photo gaps are not event duration.
"""

SEPARATION_PREAMBLE += "\n" + PHOTO_HISTORY_RULES
INTERPRETATION_PREAMBLE += "\n" + PHOTO_HISTORY_RULES
