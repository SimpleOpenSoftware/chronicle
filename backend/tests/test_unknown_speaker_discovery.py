import pytest

from backend.models.conversation import Conversation
from backend.workers.unknown_speaker_jobs import (
    _unknown_identities,
    cluster_local_identities,
    discover_unknown_speakers_job,
)


def _identity(key, conversation, vector):
    return {
        "identity_key": key,
        "conversation_id": conversation,
        "centroid": vector,
    }


def test_local_unknown_numbers_do_not_define_cross_conversation_identity():
    identities = [
        _identity("a:Unknown Speaker 1", "a", [1.0, 0.0]),
        _identity("b:Unknown Speaker 9", "b", [0.99, 0.1]),
        _identity("c:Unknown Speaker 1", "c", [0.0, 1.0]),
    ]

    clusters, outliers = cluster_local_identities(identities, threshold=0.9)

    assert [[item["conversation_id"] for item in cluster] for cluster in clusters] == [
        ["a", "b"]
    ]
    assert [item["conversation_id"] for item in outliers] == ["c"]


def test_distinct_local_identities_in_one_conversation_never_merge():
    identities = [
        _identity("a:Unknown Speaker 1", "a", [1.0, 0.0]),
        _identity("a:Unknown Speaker 2", "a", [1.0, 0.0]),
    ]

    clusters, outliers = cluster_local_identities(identities, threshold=0.5)

    assert clusters == []
    assert len(outliers) == 2


def test_clustering_is_deterministic_for_input_order():
    identities = [
        _identity("c:x", "c", [0.0, 1.0]),
        _identity("b:x", "b", [1.0, 0.0]),
        _identity("a:x", "a", [1.0, 0.0]),
    ]

    first = cluster_local_identities(identities, threshold=0.9)
    second = cluster_local_identities(list(reversed(identities)), threshold=0.9)

    assert first == second


def test_discovery_job_is_rq_importable():
    assert discover_unknown_speakers_job.__module__ == (
        "backend.workers.unknown_speaker_jobs"
    )


class _AsyncCursor:
    def __init__(self, documents):
        self.documents = documents

    def __aiter__(self):
        self.iterator = iter(self.documents)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error


class _Collection:
    def __init__(self, documents):
        self.documents = documents
        self.query = None

    def find(self, query, _projection):
        self.query = query
        return _AsyncCursor(self.documents)


@pytest.mark.asyncio
async def test_unknown_scan_is_user_scoped_and_includes_active_annotation_datasets(
    monkeypatch,
    isolated_privacy_database,
):
    await isolated_privacy_database.conversations.insert_one(
        {"conversation_id": "dataset-clip", "user_id": "user-1"}
    )
    collection = _Collection(
        [
            {
                "conversation_id": "dataset-clip",
                "external_source_type": "annotation_dataset",
                "audio_total_duration": 10.0,
                "active_transcript_version": "active",
                "transcript_versions": [
                    {
                        "version_id": "active",
                        "segments": [
                            {
                                "speaker": "Unknown Speaker 4",
                                "start": 0.0,
                                "end": 4.0,
                                "text": "hello",
                            }
                        ],
                    }
                ],
            }
        ]
    )
    monkeypatch.setattr(Conversation, "get_pymongo_collection", lambda: collection)

    identities = await _unknown_identities("user-1")

    assert collection.query == {
        "user_id": "user-1",
        "deleted": {"$ne": True},
        "audio_archived": {"$ne": True},
        "audio_chunks_count": {"$gt": 0},
    }
    assert identities[0]["identity_key"] == "dataset-clip:Unknown Speaker 4"
