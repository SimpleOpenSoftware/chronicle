import numpy as np
import pytest

from backend.constants import is_non_enrollable_speaker
from backend.controllers.background_bucket_controller import (
    SURFACE_PROFILES,
    _background_likelihood,
    _cluster_rows,
    _content_signature,
    _foreground_matches,
    _gap_windows,
    _harvest_groups,
    _is_known_foreground,
    _max_similarity,
    _novelty_groups,
    _queue_summary,
    _reference_scores,
    _representatives,
    _review_samples,
)
from backend.models.conversation import Conversation
from backend.routers.modules.annotation_routes import _apply_diarization_label
from backend.workers import background_index_jobs, speaker_jobs
from backend.workers.background_benchmark import evaluate_reviews
from backend.workers.background_cleanup_jobs import _score_rows


def test_gap_windows_sample_audio_without_transcript_segments():
    segments = [
        {"start": 3.0, "end": 6.0},
        {"start": 10.0, "end": 12.0},
    ]

    assert _gap_windows(segments, 16.0) == [
        (0.0, 3.0),
        (6.0, 10.0),
        (12.0, 16.0),
    ]


def test_gap_windows_split_long_undeciphered_regions_into_listenable_samples():
    assert _gap_windows([], 18.0) == [(0.0, 8.0), (8.0, 16.0), (16.0, 18.0)]


def test_noise_and_background_speech_are_not_enrollable_people():
    assert is_non_enrollable_speaker("Noise")
    assert is_non_enrollable_speaker("Background Speech")


def test_background_speech_stays_speech_while_noise_becomes_an_event():
    background_speech = Conversation.SpeakerSegment(
        start=0, end=2, text="News from the television", speaker="Speaker 1"
    )
    noise = Conversation.SpeakerSegment(
        start=2, end=4, text="[fan noise]", speaker="Speaker 2"
    )

    _apply_diarization_label(background_speech, "Background Speech")
    _apply_diarization_label(noise, "Noise")

    assert background_speech.segment_type == "speech"
    assert background_speech.speaker == "Background Speech"
    assert noise.segment_type == "event"
    assert noise.speaker == "Noise"


def test_each_bucket_uses_nearest_exemplar_not_a_single_centroid():
    candidates = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    bucket = np.asarray([[1.0, 0.0], [-1.0, 0.0]], dtype=np.float32)

    similarities, indices = _max_similarity(candidates, bucket)

    assert similarities.tolist() == [1.0, 0.0]
    assert indices.tolist() == [0, 0]


def test_low_snr_strengthens_background_likelihood():
    assert _background_likelihood(0.5, 0.0) > _background_likelihood(0.5, 30.0)


def test_similar_clips_form_a_review_cluster_while_distinct_audio_does_not():
    rows = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [0.98, 0.02]},
        {"embedding": [0.96, 0.04]},
        {"embedding": [0.0, 1.0]},
    ]

    assert _cluster_rows(rows) == [[0, 1, 2], [3]]


def test_cluster_review_returns_at_most_five_central_samples():
    rows = [{"embedding": [1.0, index / 100]} for index in range(8)]

    samples = _representatives(rows, list(range(8)), 5)

    assert len(samples) == 5
    assert rows[3] in samples or rows[4] in samples


def test_cluster_review_includes_typical_and_edge_cases():
    rows = [
        {"embedding": [1.0, 0.00]},
        {"embedding": [1.0, 0.02]},
        {"embedding": [1.0, 0.04]},
        {"embedding": [0.9, 0.35]},
        {"embedding": [0.8, 0.55]},
    ]

    samples = _review_samples(rows, list(range(5)), 5)

    assert [role for _, role in samples].count("typical") == 3
    assert [role for _, role in samples].count("edge") == 2
    assert rows[4] in [row for row, role in samples if role == "edge"]


def test_labelled_foreground_references_rank_matching_speech_as_familiar():
    rows = [
        {"clip_key": "familiar", "embedding": [1.0, 0.0]},
        {"clip_key": "novel", "embedding": [0.0, 1.0]},
    ]
    references = [{"embedding": [0.99, 0.01]}]

    scores = _reference_scores(rows, references)

    assert scores["familiar"] > 0.99
    assert scores["novel"] < 0.02


def test_duplicate_imports_share_a_content_signature():
    first = {
        "conversation_id": "one",
        "candidate_type": "background_speech",
        "start": 10.001,
        "end": 14.999,
        "text": "  The SAME transcript  ",
    }
    duplicate = {**first, "conversation_id": "two", "text": "the same transcript"}

    assert _content_signature(first) == _content_signature(duplicate)


def test_known_people_are_excluded_from_bulk_background_review():
    assert _is_known_foreground({"current_label": "Alex"})
    assert not _is_known_foreground({"current_label": "Speaker 3"})
    assert not _is_known_foreground({"current_label": "Unknown Speaker 5"})
    assert not _is_known_foreground({"current_label": None})


def test_not_background_examples_suppress_similar_speech_but_not_noise():
    rows = [
        {
            "clip_key": "same-voice",
            "candidate_type": "background_speech",
            "embedding": [1.0, 0.0],
        },
        {
            "clip_key": "different-voice",
            "candidate_type": "background_speech",
            "embedding": [0.0, 1.0],
        },
        {"clip_key": "noise", "candidate_type": "noise", "embedding": [1.0, 0.0]},
    ]

    matches = _foreground_matches(rows, [{"embedding": [0.99, 0.01]}])

    assert matches == {"same-voice"}


def test_confirmed_background_mines_similar_clips_for_batch_harvest():
    rows = [
        {
            "clip_key": "a",
            "conversation_id": "one",
            "candidate_type": "background_speech",
        },
        {
            "clip_key": "b",
            "conversation_id": "one",
            "candidate_type": "background_speech",
        },
        {
            "clip_key": "c",
            "conversation_id": "two",
            "candidate_type": "background_speech",
        },
        {"clip_key": "d", "conversation_id": "one", "candidate_type": "noise"},
    ]
    background = {"a": 0.9, "b": 0.6, "c": 0.4, "d": 0.9}
    foreground = {"a": 0.2, "b": 0.2, "c": 0.2, "d": 0.2}

    groups = _harvest_groups(rows, background, foreground)

    # "c" is below HARVEST_SIMILARITY and "d" is noise, not speech
    assert [[row["clip_key"] for row in group] for group in groups] == [["a", "b"]]


def test_harvest_requires_background_to_beat_foreground_by_margin():
    rows = [
        {
            "clip_key": "a",
            "conversation_id": "one",
            "candidate_type": "background_speech",
        },
    ]

    # familiar foreground voice that also happens to resemble the bucket
    assert _harvest_groups(rows, {"a": 0.7}, {"a": 0.6}) == []


def test_queue_summary_splits_sign_offs_from_genuine_unknowns():
    clusters = [
        # harvest lane: quick confirm regardless of mean scores
        {
            "mined": "harvest",
            "mean_background_similarity": 0.5,
            "mean_foreground_similarity": 0.4,
        },
        # confident zone (>=0.45 and margin >=0.20): quick confirm
        {
            "mined": None,
            "mean_background_similarity": 0.6,
            "mean_foreground_similarity": 0.3,
        },
        # unsure band: genuinely uncertain
        {
            "mined": None,
            "mean_background_similarity": 0.45,
            "mean_foreground_similarity": 0.35,
        },
        # novelty (low similarity to everything): genuinely uncertain
        {
            "mined": "novel",
            "mean_background_similarity": 0.1,
            "mean_foreground_similarity": 0.1,
        },
    ]

    assert _queue_summary(clusters) == {
        "unreviewed": 4,
        "quick_confirms": 2,
        "uncertain": 2,
    }


def test_surface_dial_widens_and_narrows_the_harvest_lane():
    rows = [
        {
            "clip_key": "a",
            "conversation_id": "one",
            "candidate_type": "background_speech",
        },
    ]
    # borderline clip: below the default 0.55 floor, above the "more" 0.45 one
    background = {"a": 0.50}
    foreground = {"a": 0.30}

    assert _harvest_groups(rows, background, foreground) == []
    assert _harvest_groups(rows, background, foreground, SURFACE_PROFILES["more"]) == [
        [rows[0]]
    ]
    # "less" tightens past a clip the default would accept
    assert (
        _harvest_groups(rows, {"a": 0.58}, {"a": 0.30}, SURFACE_PROFILES["less"]) == []
    )


def test_novelty_lane_surfaces_clips_unlike_any_labelled_reference():
    rows = [
        {
            "clip_key": f"tv{i}",
            "conversation_id": "tv-conv",
            "candidate_type": "background_speech",
        }
        for i in range(5)
    ] + [
        {
            "clip_key": "familiar",
            "conversation_id": "tv-conv",
            "candidate_type": "background_speech",
        },
        {
            "clip_key": "lone",
            "conversation_id": "small-conv",
            "candidate_type": "background_speech",
        },
    ]
    background = {
        key: 0.1 for key in ("tv0", "tv1", "tv2", "tv3", "tv4", "familiar", "lone")
    }
    foreground = {key: 0.1 for key in background} | {"familiar": 0.8}

    groups = _novelty_groups(rows, background, foreground, consumed={"tv4"})

    # tv4 was already clustered elsewhere; "familiar" matches a labelled voice;
    # "small-conv" has too few clips to be worth a review round
    assert [[row["clip_key"] for row in group] for group in groups] == [
        ["tv0", "tv1", "tv2", "tv3"]
    ]


def test_cleanup_only_proposes_background_when_it_beats_foreground_by_margin():
    rows = [
        {
            "clip_key": "background",
            "conversation_id": "one",
            "start": 0,
            "end": 3,
            "embedding": [1.0, 0.0],
            "candidate_type": "background_speech",
            "stored_confidence": 0.2,
        },
        {
            "clip_key": "foreground",
            "conversation_id": "one",
            "start": 3,
            "end": 6,
            "embedding": [0.0, 1.0],
            "candidate_type": "background_speech",
            "stored_confidence": 0.9,
        },
    ]
    buckets = {
        "noise": [],
        "background_speech": [{"embedding": [1.0, 0.0]}],
    }

    scored = _score_rows(rows, buckets, [{"embedding": [0.0, 1.0]}])

    assert [(item["clip_key"], item["tier"]) for item in scored] == [
        ("background", "high")
    ]


def test_noise_references_never_relabel_transcribed_speech():
    rows = [
        {
            "clip_key": "speech",
            "conversation_id": "one",
            "start": 0,
            "end": 3,
            "embedding": [1.0, 0.0],
            "candidate_type": "background_speech",
        }
    ]

    scored = _score_rows(
        rows,
        {"noise": [{"embedding": [1.0, 0.0]}], "background_speech": []},
        [],
    )

    assert scored == []


def test_fg_bg_benchmark_reports_f1_improvement_without_training_on_test_cluster():
    reviews = []
    for cluster_id, is_background, embedding in (
        ("bg-1", True, [1.0, 0.0]),
        ("bg-2", True, [0.99, 0.01]),
        ("fg-1", False, [0.0, 1.0]),
        ("fg-2", False, [0.01, 0.99]),
    ):
        reviews.append(
            {
                "cluster_id": cluster_id,
                "examples": [
                    {
                        "clip_key": cluster_id,
                        "candidate_type": "background_speech",
                        "embedding": embedding,
                        "content_signature": cluster_id,
                        "is_background": is_background,
                        "decision": (
                            "background_speech" if is_background else "not_background"
                        ),
                    }
                ],
            }
        )

    report = evaluate_reviews(reviews)

    assert report["baseline"]["f1"] == 0.0
    assert report["adapted"]["f1"] == 1.0
    assert report["f1_change"] == 1.0


@pytest.mark.asyncio
async def test_corpus_index_waits_for_speaker_service_to_become_ready(monkeypatch):
    responses = iter(
        [
            {"error": "connection_failed"},
            {"embedding_model": "wespeaker-test"},
        ]
    )

    class Client:
        async def get_embedding_info(self):
            return next(responses)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(background_index_jobs.asyncio, "sleep", no_wait)

    assert (
        await background_index_jobs._wait_for_embedding_model(Client())
        == "wespeaker-test"
    )


@pytest.mark.asyncio
class _StubUser:
    user_id = "test-user"


async def _stub_suppression_ledger(monkeypatch):
    """Allowed synthetic target with a stand-in ledger; capture written records."""
    await speaker_jobs.privacy.database().conversations.insert_one(
        {
            "conversation_id": "conversation",
            "user_id": "test-user",
            "client_id": "synthetic-control",
        }
    )
    captured: list[dict] = []

    async def no_override(_user_id, _conversation_id):
        return None

    async def no_sticky(_user_id, _conversation_id):
        return {}

    async def record(
        _conversation_id, _user_id, records, source, prune=True, *, privacy_visibility
    ):
        await privacy_visibility.assert_current()
        captured.extend({**r, "source": source} for r in records)
        return len(records)

    suppression = speaker_jobs.background_suppression
    monkeypatch.setattr(suppression, "get_subject_override", no_override)
    monkeypatch.setattr(suppression, "load_sticky_segments", no_sticky)
    monkeypatch.setattr(suppression, "record_conversation_suppressions", record)
    return captured


async def test_background_reference_overrides_weaker_foreground_match(monkeypatch):
    # A solid identification (0.55 is a strong wespeaker match) is never
    # silently overridden: strong background similarity sends it to the unsure
    # queue for review instead. Only a weak identification loses outright.
    segments = [
        {
            "start": 0.0,
            "end": 3.0,
            "identified_as": "Alex",
            "confidence": 0.55,
        },
        {
            "start": 4.0,
            "end": 7.0,
            "identified_as": "Alex",
            "confidence": 0.30,
        },
    ]

    resolved_calls = []

    async def resolve(conversation_id):
        resolved_calls.append(conversation_id)
        return ["resolved-claim"]

    async def reconstruct(resolved, ranges, **_kwargs):
        assert resolved == ["resolved-claim"]
        assert ranges == [(0.0, 3.0), (4.0, 7.0)]
        return [b"wav-1", b"wav-2"]

    class SpeakerClient:
        async def extract_speaker_embedding(self, _wav):
            return {"embedding": [1.0, 0.0], "embedding_model": "test-model"}

    async def match(_user, _embeddings, bucket_type, _model, *, visibility):
        similarity = 0.72 if bucket_type == "background_speech" else 0.2
        return {"results": [{"bucket_similarity": similarity}]}

    monkeypatch.setattr(speaker_jobs, "resolve_conversation_audio", resolve)
    monkeypatch.setattr(speaker_jobs, "reconstruct_resolved_audio_ranges", reconstruct)
    monkeypatch.setattr(
        speaker_jobs.background_bucket_controller, "match_embeddings", match
    )
    ledger = await _stub_suppression_ledger(monkeypatch)

    await speaker_jobs._apply_background_references(
        "conversation", segments, _StubUser(), SpeakerClient()
    )

    # Strong identification survives; the conflict is queued for review.
    assert segments[0]["identified_as"] == "Alex"
    assert "status" not in segments[0]
    # Weak identification is overridden outright.
    assert segments[1]["identified_as"] == "Background Speech"
    assert segments[1]["status"] == "background_reference"
    # Both verdicts are disclosed, remembering the identification each replaced
    # (or challenged) so a restore can put it back.
    records = sorted(ledger, key=lambda r: r["segment_start"])
    assert [r["zone"] for r in records] == ["unsure", "confident_background"]
    assert all(r["previous_identified_as"] == "Alex" for r in records)
    assert resolved_calls == ["conversation"]


@pytest.mark.asyncio
async def test_background_reference_does_not_override_stronger_foreground(monkeypatch):
    segments = [
        {
            "start": 0.0,
            "end": 3.0,
            "identified_as": "Alex",
            "confidence": 0.8,
        }
    ]

    async def resolve(_conversation_id):
        return ["resolved-claim"]

    async def reconstruct(_resolved, ranges, **_kwargs):
        assert ranges == [(0.0, 3.0)]
        return [b"wav"]

    class SpeakerClient:
        async def extract_speaker_embedding(self, _wav):
            return {"embedding": [1.0, 0.0], "embedding_model": "test-model"}

    async def match(_user, _embeddings, bucket_type, _model, *, visibility):
        similarity = 0.74 if bucket_type == "background_speech" else 0.2
        return {"results": [{"bucket_similarity": similarity}]}

    monkeypatch.setattr(speaker_jobs, "resolve_conversation_audio", resolve)
    monkeypatch.setattr(speaker_jobs, "reconstruct_resolved_audio_ranges", reconstruct)
    monkeypatch.setattr(
        speaker_jobs.background_bucket_controller, "match_embeddings", match
    )
    ledger = await _stub_suppression_ledger(monkeypatch)

    await speaker_jobs._apply_background_references(
        "conversation", segments, _StubUser(), SpeakerClient()
    )

    assert segments[0]["identified_as"] == "Alex"
    # The foreground identification wins outright — nothing recorded, not even
    # as unsure: a familiar voice beating the bucket is not a close call.
    assert ledger == []


@pytest.mark.asyncio
async def test_background_reference_reuses_identification_embedding(monkeypatch):
    segments = [
        {
            "start": 0.0,
            "end": 3.0,
            "identified_as": None,
            "confidence": 0.1,
            "_evaluation_embedding": [1.0, 0.0],
            "_embedding_model": "wespeaker-test",
        }
    ]

    async def should_not_resolve(*_args):
        raise AssertionError("identification audio claim must not be resolved twice")

    async def should_not_reconstruct(*_args, **_kwargs):
        raise AssertionError("identification audio must not be reconstructed twice")

    class SpeakerClient:
        async def extract_speaker_embedding(self, _wav):
            raise AssertionError("identification audio must not be embedded twice")

    async def match(_user, embeddings, bucket_type, model, *, visibility):
        assert embeddings == [[1.0, 0.0]]
        assert model == "wespeaker-test"
        similarity = 0.8 if bucket_type == "background_speech" else 0.2
        return {"results": [{"bucket_similarity": similarity}]}

    monkeypatch.setattr(speaker_jobs, "resolve_conversation_audio", should_not_resolve)
    monkeypatch.setattr(
        speaker_jobs, "reconstruct_resolved_audio_ranges", should_not_reconstruct
    )
    monkeypatch.setattr(
        speaker_jobs.background_bucket_controller, "match_embeddings", match
    )
    await _stub_suppression_ledger(monkeypatch)

    await speaker_jobs._apply_background_references(
        "conversation", segments, _StubUser(), SpeakerClient()
    )

    assert segments[0]["identified_as"] == "Background Speech"


@pytest.mark.asyncio
async def test_background_reference_reconstructs_large_turn_sets_in_bounded_batches(
    monkeypatch,
):
    segments = [
        {
            "start": float(index * 3),
            "end": float(index * 3 + 2),
            "identified_as": "Alex",
            "confidence": 0.9,
        }
        for index in range(205)
    ]
    resolve_calls = 0
    batch_sizes = []

    async def resolve(_conversation_id):
        nonlocal resolve_calls
        resolve_calls += 1
        return ["resolved-claim"]

    async def reconstruct(resolved, ranges, **_kwargs):
        assert resolved == ["resolved-claim"]
        batch_sizes.append(len(ranges))
        return [b"wav"] * len(ranges)

    class SpeakerClient:
        async def extract_speaker_embedding(self, _wav):
            return {"embedding": [1.0, 0.0], "embedding_model": "test-model"}

    async def match(_user, _embeddings, _bucket_type, _model, *, visibility):
        return {"results": [{"bucket_similarity": 0.0}]}

    monkeypatch.setattr(speaker_jobs, "resolve_conversation_audio", resolve)
    monkeypatch.setattr(speaker_jobs, "reconstruct_resolved_audio_ranges", reconstruct)
    monkeypatch.setattr(
        speaker_jobs.background_bucket_controller, "match_embeddings", match
    )
    await _stub_suppression_ledger(monkeypatch)

    await speaker_jobs._apply_background_references(
        "conversation", segments, _StubUser(), SpeakerClient()
    )

    assert resolve_calls == 1
    assert batch_sizes == [100, 100, 5]
