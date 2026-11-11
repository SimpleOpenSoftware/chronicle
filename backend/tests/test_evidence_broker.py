"""Range-broker tests for ``load_reconciliation_evidence``.

The broker is the range core extracted from ``assemble_day_evidence``; these tests pin
the two entry points to identical output over identical bounds, and prove every Mongo
query is bounded by the requested range. Beanie class-level expression fields require an
initialized database, so the document models the module touches are replaced with tiny
fakes that record their queries and filter fixture rows through them.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.models.timeline import EvidenceLocator
from backend.services.timeline import evidence

DAY = date(2026, 8, 6)
TZ = "Asia/Kolkata"
DAY_START = datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
DAY_END = DAY_START + timedelta(days=1)


class _Field:
    """A queryable field whose comparisons yield Mongo fragments, like Beanie's."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _op(self, operator: str, value):
        return {self.name: {operator: value}}

    def __lt__(self, value):
        return self._op("$lt", value)

    def __le__(self, value):
        return self._op("$lte", value)

    def __gt__(self, value):
        return self._op("$gt", value)

    def __ge__(self, value):
        return self._op("$gte", value)

    def __eq__(self, value):  # type: ignore[override]
        return {self.name: value}


def _merge(args) -> dict:
    """AND the arguments the way Beanie does, keeping both bounds on one field."""

    merged: dict = {}
    for argument in args:
        for key, value in dict(argument).items():
            existing = merged.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                existing.update(value)
            else:
                merged[key] = dict(value) if isinstance(value, dict) else value
    return merged


def _bounds(query: dict, start_field: str, end_field: str):
    """Extract the ``[low, high)`` a query restricts itself to, or fail loudly."""

    start = query.get(start_field) or {}
    end = query.get(end_field) or {}
    high = start.get("$lt") if isinstance(start, dict) else None
    low = end.get("$gte") or end.get("$gt") if isinstance(end, dict) else None
    assert high is not None, f"unbounded query on {start_field}: {query}"
    assert low is not None, f"unbounded query on {end_field}: {query}"
    return low, high


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return list(self._rows)


def _fake_model(name: str, rows, queries, *, start_field: str, end_field: str):
    """A stand-in document class that range-filters ``rows`` using its own query."""

    class Fake:
        user_id = _Field("user_id")
        started_at = _Field("started_at")
        ended_at = _Field("ended_at")
        shared_at = _Field("shared_at")

        @classmethod
        def find(cls, *args):
            query = _merge(args)
            queries.append((name, query))
            if start_field is None:
                # Conversations are bounded by the id set that the range-bounded
                # audio-bounds lookup produced, not by their own timestamps.
                assert "$in" in query.get("conversation_id", {}), query
                return _Cursor([])
            low, high = _bounds(query, start_field, end_field)
            selected = [
                row
                for row in rows
                if getattr(row, start_field) < high
                and getattr(row, end_field, getattr(row, start_field)) >= low
                and _matches(row, query)
            ]
            return _Cursor(selected)

    return Fake


def _matches(row, query: dict) -> bool:
    for key, condition in query.items():
        if key in {"started_at", "ended_at", "shared_at", "user_id", "$or"}:
            continue
        value = getattr(row, key, None)
        if isinstance(condition, dict):
            if "$ne" in condition and value == condition["$ne"]:
                return False
        elif value != condition:
            return False
    if "$or" in query:
        if not any(
            all(getattr(row, key, None) == value for key, value in clause.items())
            for clause in query["$or"]
        ):
            return False
    return True


def _span(index: int, started_at: datetime, *, direction: str = "input"):
    return SimpleNamespace(
        id=f"span-{index}",
        user_id="user-1",
        source_id="capture-node",
        locator=EvidenceLocator(
            capture_source_id="capture-node",
            modality="audio",
            track_id=f"{direction}:device-{index}",
        ),
        first_source_item_id=f"item-{index}",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=10),
        direction=direction,
        source_range_hash=f"hash-{index}",
        meeting_id=None,
        state="transcribed" if direction == "output" else "closed",
        covered_seconds=600.0,
        missing_seconds=0.0,
        bucket_seconds=10.0,
        coverage_fraction=1.0,
        speech_fraction=0.5,
        acoustic_active_fraction=0.6,
        rms_dbfs=-30.0,
        peak_dbfs=-10.0,
        speech_seconds=300.0,
        acoustic_active_seconds=360.0,
        acoustic_quiet_seconds=240.0,
        longest_no_speech_seconds=5.0,
        conversation_id=None,
    )


def _memory(index: int, shared_at: datetime):
    return SimpleNamespace(
        user_id="user-1",
        memory_id=f"memory-{index}",
        shared_at=shared_at,
        note=f"note {index}",
        attachments=[],
        memory_at=shared_at,
        source={"application": "obsidian"},
    )


def _episode(
    key: str,
    started_at: datetime,
    *,
    status: str = "settled",
    pinned: bool = False,
):
    episode = SimpleNamespace(
        episode_id=f"id-{key}",
        episode_key=key,
        revision=1,
        user_id="user-1",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=30),
        kind="focus",
        title=f"title {key}",
        summary=f"summary {key}",
        status=status,
        pinned=pinned,
        confirmed_fields=set(),
        evidence_refs=[],
    )
    episode.model_dump = lambda *, mode, include: {
        field: getattr(episode, field) for field in include
    }
    return episode


@pytest.fixture
def fixture_day(monkeypatch):
    """Mixed evidence across one local day, with every query recorded."""

    queries: list[tuple[str, dict]] = []
    spans = [
        _span(0, DAY_START + timedelta(hours=2)),
        _span(1, DAY_START + timedelta(hours=9), direction="output"),
    ]
    memories = [
        _memory(0, DAY_START + timedelta(hours=3)),
        _memory(1, DAY_START + timedelta(hours=10)),
    ]
    episodes = [
        _episode("rolling-early", DAY_START + timedelta(hours=2)),
        _episode("rolling-late", DAY_START + timedelta(hours=9)),
        _episode("dropped", DAY_START + timedelta(hours=2), status="superseded"),
        _episode("rolling-pinned", DAY_START + timedelta(hours=9), pinned=True),
    ]
    device_calls: list[tuple[datetime, datetime]] = []
    conversation_calls: list[tuple[datetime, datetime]] = []

    async def fake_device_rows(user_id, day_start, range_end):
        device_calls.append((day_start, range_end))
        return []

    async def fake_bounds(user_id, day_start, range_end):
        conversation_calls.append((day_start, range_end))
        return {}

    async def fake_activity_policy_days(user_id, day_start, range_end, timezone_name):
        return []

    monkeypatch.setattr(
        evidence,
        "AudioEvidenceSpan",
        _fake_model(
            "audio", spans, queries, start_field="started_at", end_field="ended_at"
        ),
    )
    monkeypatch.setattr(
        evidence,
        "ManualMemory",
        _fake_model(
            "manual", memories, queries, start_field="shared_at", end_field="shared_at"
        ),
    )
    monkeypatch.setattr(
        evidence,
        "TimelineEpisode",
        _fake_model(
            "episode", episodes, queries, start_field="started_at", end_field="ended_at"
        ),
    )
    monkeypatch.setattr(evidence, "_device_input_rows", fake_device_rows)
    monkeypatch.setattr(evidence, "_conversation_audio_bounds", fake_bounds)
    monkeypatch.setattr(
        "backend.services.timeline.activity_policy.activity_policy_days",
        fake_activity_policy_days,
    )
    monkeypatch.setattr(
        evidence,
        "Conversation",
        _fake_model("conversation", [], queries, start_field=None, end_field=None),
    )
    return SimpleNamespace(
        queries=queries,
        device_calls=device_calls,
        conversation_calls=conversation_calls,
    )


@pytest.mark.asyncio
async def test_day_assembly_and_range_broker_agree_over_the_same_bounds(fixture_day):
    manifest, _images = await evidence.assemble_day_evidence(
        "user-1", DAY, TZ, now=DAY_END + timedelta(hours=1)
    )
    bundle = await evidence.load_reconciliation_evidence(
        "user-1", DAY_START, DAY_END, timezone_name=TZ
    )

    assert bundle.manifest.evidence_revision == manifest.evidence_revision
    assert [item.evidence_id for item in bundle.manifest.evidence] == [
        item.evidence_id for item in manifest.evidence
    ]
    assert [window.window_id for window in bundle.manifest.windows] == [
        window.window_id for window in manifest.windows
    ]
    assert bundle.manifest.local_date == manifest.local_date == DAY
    assert bundle.manifest.model_dump() == manifest.model_dump()


@pytest.mark.asyncio
async def test_narrow_range_excludes_evidence_outside_it(fixture_day):
    narrow_start = DAY_START + timedelta(hours=1)
    narrow_end = DAY_START + timedelta(hours=4)

    bundle = await evidence.load_reconciliation_evidence(
        "user-1", narrow_start, narrow_end, timezone_name=TZ
    )

    ids = {item.evidence_id for item in bundle.manifest.evidence}
    assert any(identifier.startswith("audio_span:span-0") for identifier in ids)
    assert not any(identifier.startswith("audio_span:span-1") for identifier in ids)
    assert any("memory-0" in identifier for identifier in ids)
    assert not any("memory-1" in identifier for identifier in ids)
    assert bundle.manifest.started_at == narrow_start
    assert bundle.manifest.ended_at == narrow_end


@pytest.mark.asyncio
async def test_every_query_is_bounded_by_the_requested_range(fixture_day):
    narrow_start = DAY_START + timedelta(hours=1)
    narrow_end = DAY_START + timedelta(hours=4)

    await evidence.load_reconciliation_evidence(
        "user-1", narrow_start, narrow_end, timezone_name=TZ
    )

    assert fixture_day.queries, "no query was recorded"
    for name, query in fixture_day.queries:
        if name == "conversation":
            assert "$in" in query["conversation_id"]
            continue
        start_field = "shared_at" if name == "manual" else "started_at"
        end_field = "shared_at" if name == "manual" else "ended_at"
        low, high = _bounds(query, start_field, end_field)
        assert high == narrow_end, name
        assert low == narrow_start, name
    assert fixture_day.device_calls == [(narrow_start, narrow_end)]
    assert fixture_day.conversation_calls == [(narrow_start, narrow_end)]


@pytest.mark.asyncio
async def test_existing_episodes_are_active_rolling_rows_only(fixture_day):
    bundle = await evidence.load_reconciliation_evidence(
        "user-1", DAY_START, DAY_END, timezone_name=TZ
    )

    assert [row["episode_id"] for row in bundle.existing_episodes] == [
        "id-rolling-early",
        "id-rolling-late",
        "id-rolling-pinned",
    ]


@pytest.mark.asyncio
async def test_pinned_episodes_are_carried_into_reconciliation(fixture_day):
    bundle = await evidence.load_reconciliation_evidence(
        "user-1", DAY_START, DAY_END, timezone_name=TZ
    )

    assert {row["episode_key"] for row in bundle.pinned_episodes} == {"rolling-pinned"}
    assert set(bundle.pinned_episodes[0]) == {
        "episode_id",
        "episode_key",
        "revision",
        "started_at",
        "ended_at",
        "kind",
        "title",
        "summary",
        "confirmed_fields",
        "evidence_ids",
    }


@pytest.mark.asyncio
async def test_dirty_range_counter_is_carried_beside_the_content_hash(fixture_day):
    bundle = await evidence.load_reconciliation_evidence(
        "user-1", DAY_START, DAY_END, timezone_name=TZ, evidence_revision=17
    )

    assert bundle.evidence_revision == 17
    assert isinstance(bundle.manifest.evidence_revision, str)
    assert len(bundle.manifest.evidence_revision) == 64


@pytest.mark.asyncio
async def test_audio_items_carry_capture_direction(fixture_day):
    bundle = await evidence.load_reconciliation_evidence(
        "user-1", DAY_START, DAY_END, timezone_name=TZ
    )

    directions = {
        item.metadata["direction"]
        for item in bundle.manifest.evidence
        if item.kind == "audio_span"
    }
    assert directions == {"input", "output"}
    roles = {
        item.metadata["direction"]: item.role
        for item in bundle.manifest.evidence
        if item.kind == "audio_span"
    }
    assert roles["output"] == "media_content"
    assert roles["input"] == "uncertain"
