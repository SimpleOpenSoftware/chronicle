"""Keep historical screening from multiplying work for every listing row."""

from datetime import datetime, timedelta, timezone

from backend.services import privacy


async def test_canonical_listing_work_is_bounded_and_does_not_load_transcripts(
    isolated_privacy_database, monkeypatch
):
    db = isolated_privacy_database
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at = lambda seconds: start + timedelta(seconds=seconds)
    await db.capture_sources.insert_one(
        dict(
            user_id="owner",
            source_id="synthetic",
            privacy_enabled_from=start,
            privacy_revision=1,
        )
    )
    await db.privacy_display_sets.insert_one(
        dict(
            user_id="owner",
            source_id="synthetic",
            observed_at=start,
            transition_started_at=start,
            track_ids=["display"],
        )
    )
    await db.privacy_screening.insert_many(
        [
            dict(
                user_id="owner",
                source_id="synthetic",
                track_id="display",
                started_at=at(i * 10),
                ended_at=at((i + 1) * 10),
                segments=[
                    dict(
                        started_at=at(i * 10),
                        ended_at=at((i + 1) * 10),
                        state="allowed",
                    )
                ],
            )
            for i in range(2000)
        ]
    )
    rows = [
        dict(
            conversation_id=f"synthetic-{i}",
            user_id="owner",
            client_id="synthetic",
            created_at=at(i * 10),
            ended_at=at((i + 1) * 10),
            transcript="Synthetic text must not be loaded for policy decisions",
        )
        for i in range(100)
    ]
    await db.conversations.insert_many(rows)
    calls = 0
    utc = privacy.utc

    def counted(value):
        nonlocal calls
        calls += 1
        return utc(value)

    monkeypatch.setattr(privacy, "utc", counted)
    permits = privacy.PrivacySnapshot.permits_record

    def check_projection(self, row):
        assert "transcript" not in row
        return permits(self, row)

    monkeypatch.setattr(privacy.PrivacySnapshot, "permits_record", check_projection)
    visibility = privacy.ConversationPrivacyFilter()
    projected = [dict(conversation_id=row["conversation_id"]) for row in rows]
    assert await visibility.filter(projected) == projected
    await visibility.assert_current()
    assert (
        calls < 10000
    ), "Timestamp normalization must not scale as intervals times conversations"
