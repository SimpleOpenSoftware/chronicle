"""Exercise HTTP orchestration with a real durable gallery and fake inference."""

import asyncio
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from simple_speaker_recognition.api.routers import enrollment_operations as routes
from simple_speaker_recognition.core import enrollment_operations as operations
from simple_speaker_recognition.core import unified_speaker_db
from simple_speaker_recognition.database import Base
from simple_speaker_recognition.database.models import (
    EnrollmentOperation,
    Speaker,
    SpeakerAudioSegment,
    SpeakerCatalogIdentity,
    User,
)
from simple_speaker_recognition.utils import audio_processing

OWNER = "synthetic-owner"
OP = "a" * 32


def binding(**changes):
    return {
        "user_id": OWNER,
        "speaker_id": "synthetic-speaker",
        "speaker_name": "Synthetic speaker",
        "mode": "create",
        "evidence": {
            "capture_hash": "f" * 64,
            "conversation_ids": ["synthetic-recording"],
            "privacy_revisions": {OWNER: {"synthetic-source": 1}},
        },
        **changes,
    }


def audio(sample=100):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.full(16000, sample, dtype=np.int16).tobytes())
    return out.getvalue()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'gallery.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(SpeakerCatalogIdentity(id=1, catalog_id="d" * 32))
        session.commit()
    monkeypatch.setattr(operations, "get_db_session", sessions)
    from simple_speaker_recognition.core import gallery_catalog

    monkeypatch.setattr(gallery_catalog, "get_db_session", sessions)
    monkeypatch.setattr(unified_speaker_db, "get_db_session", sessions)
    gallery = unified_speaker_db.UnifiedSpeakerDB(2, tmp_path / "index", 0.5)
    backend = SimpleNamespace(
        load_wave=lambda path: np.ones(16000),
        async_embed=AsyncMock(return_value=np.array([[1.0, 0.0]])),
    )
    monkeypatch.setattr(routes, "get_audio_backend", lambda: backend)
    root = tmp_path / "audio"
    monkeypatch.setattr(
        routes, "get_auth", lambda: SimpleNamespace(enrollment_audio_dir=root)
    )
    monkeypatch.setattr(
        audio_processing, "get_audio_info", lambda path: {"duration_seconds": 1.0}
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: gallery
    return SimpleNamespace(
        app=app,
        gallery=gallery,
        backend=backend,
        sessions=sessions,
        root=root,
        engine=engine,
    )


async def prepare(client, op=OP, bound=None, sample=100):
    return await client.post(
        f"/enrollment/operations/{op}/prepare",
        headers={"X-Speaker-Catalog": "d" * 32},
        data={"binding": json.dumps(bound or binding())},
        files={"file": ("synthetic.wav", audio(sample), "audio/wav")},
    )


async def activate(client, op=OP, bound=None):
    return await client.post(
        f"/enrollment/operations/{op}/activate",
        json=bound or binding(),
        headers={"X-Speaker-Catalog": "d" * 32},
    )


async def quarantine(client, op=OP, owner=OWNER):
    return await client.post(
        f"/enrollment/operations/{op}/quarantine",
        json={"user_id": owner},
        headers={"X-Speaker-Catalog": "d" * 32},
    )


@pytest.mark.anyio
async def test_prepare_is_invisible_and_activation_replay_survives_restart(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        assert (await prepare(client)).json()["state"] == "prepared"
        with runtime.sessions() as session:
            assert session.query(Speaker).count() == 0
            assert session.query(SpeakerAudioSegment).count() == 0
        assert runtime.gallery.index.ntotal == 0
        assert not list(runtime.root.glob("*/*/enrollment_manifest.json"))
        response = await activate(client)
        assert response.status_code == 200
        assert response.json()["state"] == "active"
        runtime.gallery._load_state()
        assert (await prepare(client)).json() == response.json()
        assert (await activate(client)).json() == response.json()
        assert runtime.backend.async_embed.await_count == 1
        assert runtime.gallery.index.ntotal == 1
        with runtime.sessions() as session:
            assert session.query(SpeakerAudioSegment).count() == 1
            assert session.get(Speaker, "synthetic-speaker").audio_sample_count == 1


@pytest.mark.anyio
async def test_quarantine_before_prepare_and_cross_owner_replay(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        assert (await quarantine(client)).status_code == 200
        runtime.gallery._load_state()
        assert (await prepare(client)).status_code == 423
        assert (await activate(client)).status_code == 423
        assert (await quarantine(client, owner="different-owner")).status_code == 404
        assert runtime.backend.async_embed.await_count == 0


@pytest.mark.anyio
async def test_quarantine_wins_during_embedding_and_identical_requests_coalesce(
    runtime,
):
    started, finish = asyncio.Event(), asyncio.Event()

    async def embed(wav):
        started.set()
        await finish.wait()
        return np.array([[1.0, 0.0]])

    runtime.backend.async_embed.side_effect = embed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(prepare(client))
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(prepare(client))
        assert (await quarantine(client)).status_code == 200
        finish.set()
        assert [r.status_code for r in await asyncio.gather(first, second)] == [
            423,
            423,
        ]
        assert runtime.backend.async_embed.await_count == 1
        assert runtime.gallery.index.ntotal == 0
        assert (runtime.root / ".privacy-operations" / f"{OP}.wav").exists()


@pytest.mark.anyio
async def test_active_quarantine_removes_only_bound_contribution_and_preserves_other_tenant(
    runtime,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        other = binding(user_id="other-owner", speaker_id="other-speaker")
        assert (await prepare(client, "c" * 32, other)).status_code == 200
        assert (await activate(client, "c" * 32, other)).status_code == 200
        runtime.backend.async_embed.return_value = np.array([[0.0, 1.0]])
        appended = binding(mode="append")
        assert (await prepare(client, "b" * 32, appended, 200)).status_code == 200
        assert (await activate(client, "b" * 32, appended)).status_code == 200
        assert (await quarantine(client, "b" * 32)).status_code == 200
        with runtime.sessions() as session:
            speaker = session.get(Speaker, "synthetic-speaker")
            assert json.loads(speaker.embedding_data) == [1.0, 0.0]
            assert speaker.audio_sample_count == 1
            assert session.get(Speaker, "other-speaker").embedding_data
            assert session.query(SpeakerAudioSegment).count() == 2
        assert (await quarantine(client)).status_code == 200
        runtime.gallery._load_state()
        assert runtime.gallery.index.ntotal == 1
        assert set(runtime.gallery.faiss_to_speaker.values()) == {
            ("other-owner", "other-speaker")
        }
        assert (await activate(client)).status_code == 423
        assert len(list((runtime.root / ".privacy-operations").glob("*.wav"))) == 3


@pytest.mark.anyio
async def test_binding_hash_revision_and_duplicate_conflicts_do_not_mutate_gallery(
    runtime,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        await prepare(client)
        assert (await prepare(client, sample=200)).status_code == 409
        changed = binding()
        changed["evidence"]["privacy_revisions"][OWNER]["synthetic-source"] = 2
        assert (await activate(client, bound=changed)).status_code == 409
        staged = runtime.root / ".privacy-operations" / f"{OP}.wav"
        staged.write_bytes(b"altered")
        assert (await activate(client)).status_code == 423
        staged.write_bytes(audio())
        assert (await activate(client)).status_code == 200
        assert (
            await prepare(client, "b" * 32, binding(mode="append"))
        ).status_code == 200
        assert (
            await activate(client, "b" * 32, binding(mode="append"))
        ).status_code == 409
        with runtime.sessions() as session:
            assert session.query(SpeakerAudioSegment).count() == 1


@pytest.mark.anyio
async def test_model_failure_remains_retryable_but_never_enrolled(runtime):
    runtime.backend.async_embed.side_effect = RuntimeError(
        "private error must not escape"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        result = await prepare(client)
        assert result.status_code == 503
        assert "private error" not in result.text
        assert (await activate(client)).status_code == 409
        with runtime.sessions() as session:
            assert session.get(EnrollmentOperation, OP).state == "preparing"
        runtime.backend.async_embed.side_effect = None
        assert (await prepare(client)).status_code == 200
        assert runtime.gallery.index.ntotal == 0


def test_failed_and_empty_gallery_rebuild_never_serves_old_vectors(
    runtime, monkeypatch
):
    with runtime.sessions() as session:
        session.add(User(id=OWNER, username=OWNER))
        session.add(
            Speaker(
                id="synthetic-speaker",
                name="Synthetic speaker",
                user_id=OWNER,
                embedding_data="[1, 0]",
            )
        )
        session.commit()
    runtime.gallery._rebuild_faiss_mapping()
    assert runtime.gallery.index.ntotal == 1
    with runtime.sessions() as session:
        session.query(Speaker).delete()
        session.commit()
    runtime.gallery._rebuild_faiss_mapping()
    assert runtime.gallery.index.ntotal == 0
    assert runtime.gallery.faiss_to_speaker == {}
    runtime.gallery.index.add(np.array([[1.0, 0.0]], dtype=np.float32))
    monkeypatch.setattr(
        unified_speaker_db,
        "get_db_session",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    with pytest.raises(RuntimeError):
        runtime.gallery._rebuild_faiss_mapping()
    assert runtime.gallery.index.ntotal == 0


@pytest.mark.anyio
async def test_mixed_gallery_hold_survives_rebuild_and_cannot_be_reenrolled(runtime):
    from simple_speaker_recognition.core.enrollment_audit import (
        compute_audit,
        recompute_speaker_centroid,
    )
    from simple_speaker_recognition.core.gallery_privacy import GalleryHeld
    from simple_speaker_recognition.database.models import SpeakerPrivacyHold

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        with runtime.sessions() as session:
            speaker = session.get(Speaker, "synthetic-speaker")
            speaker.audio_sample_count = 3
            session.add(
                SpeakerAudioSegment(
                    speaker_id=speaker.id,
                    audio_file_path="retained-synthetic.wav",
                    start_time=0,
                    end_time=1,
                    duration_seconds=1,
                    embedding="[0, 1]",
                )
            )
            session.commit()
        assert (await quarantine(client)).status_code == 200
        with runtime.sessions() as session:
            assert session.get(SpeakerPrivacyHold, "synthetic-speaker")
            assert session.get(Speaker, "synthetic-speaker").embedding_data is None
            assert session.query(SpeakerAudioSegment).count() == 1
            assert compute_audit(session, OWNER)["speakers"] == []
            with pytest.raises(GalleryHeld):
                recompute_speaker_centroid(
                    session, runtime.gallery, "synthetic-speaker"
                )
            # A stale writer cannot resurrect a held voiceprint at read time.
            session.get(Speaker, "synthetic-speaker").embedding_data = "[1, 0]"
            session.commit()
        runtime.gallery._load_state()
        assert runtime.gallery.index.ntotal == 0
        assert runtime.gallery.get_speakers_with_embeddings(OWNER) == {}
        with pytest.raises(GalleryHeld):
            await runtime.gallery.verify(
                "synthetic-speaker", np.array([1.0, 0.0]), OWNER
            )
        with pytest.raises(GalleryHeld):
            await runtime.gallery.add_speaker(
                "synthetic-speaker", "Synthetic speaker", np.array([1.0, 0.0]), OWNER
            )
        assert (
            await prepare(client, "b" * 32, binding(mode="append"))
        ).status_code == 423


@pytest.mark.anyio
async def test_identical_successful_preparations_only_infer_once(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        results = await asyncio.gather(
            prepare(client), prepare(client), prepare(client)
        )
        assert all(r.status_code == 200 for r in results)
        assert runtime.backend.async_embed.await_count == 1
        assert runtime.gallery.index.ntotal == 0


@pytest.mark.anyio
async def test_quarantining_only_contribution_hides_profile_metadata(
    runtime, monkeypatch
):
    from pathlib import Path

    from simple_speaker_recognition.api.routers import speakers
    from simple_speaker_recognition.database.models import SpeakerPrivacyHold

    runtime.app.include_router(speakers.router)
    runtime.app.dependency_overrides[speakers.get_db] = lambda: runtime.gallery
    monkeypatch.setattr(speakers, "get_db_session", runtime.sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        with runtime.sessions() as session:
            retained = runtime.root / Path(
                session.get(EnrollmentOperation, OP).audio_file_path
            )
        assert (await quarantine(client)).status_code == 200
        runtime.gallery._load_state()
        for path in ("/speakers", "/speakers/export"):
            response = await client.get(path, params={"user_id": OWNER})
            assert response.status_code == 200
            assert "Synthetic speaker" not in response.text
            assert "synthetic-speaker" not in response.text
        with runtime.sessions() as session:
            assert session.get(SpeakerPrivacyHold, "synthetic-speaker")
        assert retained.exists()
        assert runtime.gallery.index.ntotal == 0


@pytest.mark.anyio
async def test_invalid_embedding_and_malformed_binding_never_enter_gallery(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        bad = binding()
        bad["evidence"]["privacy_revisions"][OWNER]["synthetic-source"] = True
        assert (await prepare(client, bound=bad)).status_code == 422
        for value in ([1, 2, 3], [float("nan"), 0], [0, 0]):
            runtime.backend.async_embed.return_value = np.array([value])
            assert (await prepare(client)).status_code == 409
            assert (await activate(client)).status_code == 409
        assert runtime.gallery.index.ntotal == 0


@pytest.mark.anyio
async def test_cancellation_leaves_durable_preparation_and_tombstone_blocks_recovery(
    runtime,
):
    entered = asyncio.Event()

    async def embed(wav):
        entered.set()
        await asyncio.Event().wait()

    runtime.backend.async_embed.side_effect = embed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(prepare(client))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with runtime.sessions() as session:
            assert session.get(EnrollmentOperation, OP).state == "preparing"
        assert (await quarantine(client)).status_code == 200
        assert (await prepare(client)).status_code == 423
        assert runtime.gallery.index.ntotal == 0


@pytest.mark.anyio
async def test_gallery_exports_audio_and_relabel_cannot_bypass_hold(
    runtime, monkeypatch
):
    from simple_speaker_recognition import database
    from simple_speaker_recognition.api import privacy as http_privacy
    from simple_speaker_recognition.api.routers import enrollment_audit, speakers
    from simple_speaker_recognition.core.gallery_privacy import GalleryHeld
    from simple_speaker_recognition.database.models import SpeakerPrivacyHold

    runtime.app.include_router(enrollment_audit.router)
    runtime.app.include_router(speakers.router)
    runtime.app.add_exception_handler(GalleryHeld, http_privacy.gallery_privacy_hold)
    runtime.app.dependency_overrides[enrollment_audit.get_db] = lambda: runtime.gallery
    runtime.app.dependency_overrides[speakers.get_db] = lambda: runtime.gallery
    monkeypatch.setattr(enrollment_audit, "get_db_session", runtime.sessions)
    monkeypatch.setattr(speakers, "get_db_session", runtime.sessions)
    monkeypatch.setattr(database, "get_db_session", runtime.sessions)
    monkeypatch.setattr(
        enrollment_audit,
        "get_auth",
        lambda: SimpleNamespace(enrollment_audio_dir=runtime.root),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        await prepare(client)
        result = (await activate(client)).json()
        segment_id = result["segment_id"]
        # Managed evidence cannot be moved away from its operation provenance.
        response = await client.post(
            f"/enrollment/segments/{segment_id}/relabel",
            data={"target_speaker_id": "other-speaker"},
        )
        assert response.status_code == 423
        with runtime.sessions() as session:
            session.add(
                SpeakerPrivacyHold(
                    speaker_id="synthetic-speaker", user_id=OWNER, operation_id=OP
                )
            )
            session.commit()
        response = await client.get(f"/enrollment/segments/{segment_id}/audio")
        assert response.status_code == 423
        assert b"RIFF" not in response.content
        for path in [
            "/speakers/synthetic-speaker/audio",
            "/speakers/synthetic-speaker/audio/synthetic.wav",
        ]:
            response = await client.get(path, params={"user_id": OWNER})
            assert response.status_code == 423
        response = await client.get("/speakers/export", params={"user_id": OWNER})
        assert response.status_code == 200
        assert response.json()["speakers"] == []
        response = await client.post(
            "/enrollment/candidates/score-embeddings",
            json={"speaker_id": "synthetic-speaker", "embeddings": [[1, 0]]},
        )
        assert response.status_code == 404


@pytest.mark.anyio
async def test_readonly_replica_rejects_all_operation_mutations(runtime):
    from simple_speaker_recognition.api.catalog_contract import ReadOnlyCatalog

    runtime.app.add_middleware(ReadOnlyCatalog, enabled=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        for response in [
            await prepare(client),
            await activate(client),
            await quarantine(client),
        ]:
            assert response.status_code == 409
        assert runtime.backend.async_embed.await_count == 0


@pytest.mark.anyio
async def test_quarantine_ack_failure_keeps_tombstone_and_clears_search_index(
    runtime, monkeypatch
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        monkeypatch.setattr(
            unified_speaker_db,
            "get_db_session",
            lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        )
        with pytest.raises(RuntimeError):
            await quarantine(client)
        with runtime.sessions() as session:
            assert session.get(EnrollmentOperation, OP).state == "quarantined"
            assert session.query(SpeakerAudioSegment).count() == 0
        assert runtime.gallery.index.ntotal == 0
        monkeypatch.setattr(unified_speaker_db, "get_db_session", runtime.sessions)
        assert (await quarantine(client)).status_code == 200
        assert (await activate(client)).status_code == 423


@pytest.mark.anyio
async def test_wrong_catalog_cannot_prepare_activate_or_acknowledge_quarantine(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app), base_url="http://test"
    ) as client:
        assert (await client.get("/enrollment/operations/catalog")).json()[
            "catalog_id"
        ] == "d" * 32
        for catalog_id in [None, "e" * 32]:
            headers = {"X-Speaker-Catalog": catalog_id} if catalog_id else {}
            result = await client.post(
                f"/enrollment/operations/{OP}/prepare",
                data={"binding": json.dumps(binding())},
                files={"file": ("synthetic.wav", audio(), "audio/wav")},
                headers=headers,
            )
            assert result.status_code == 409
            assert (
                await client.post(
                    f"/enrollment/operations/{OP}/activate",
                    json=binding(),
                    headers=headers,
                )
            ).status_code == 409
            assert (
                await client.post(
                    f"/enrollment/operations/{OP}/quarantine",
                    json={"user_id": OWNER},
                    headers=headers,
                )
            ).status_code == 409
        with runtime.sessions() as session:
            assert session.query(EnrollmentOperation).count() == 0
        runtime.backend.async_embed.assert_not_awaited()


def test_catalog_identity_is_initialized_once_and_survives_restart(
    runtime, monkeypatch
):
    from simple_speaker_recognition import database

    monkeypatch.setattr(database, "engine", runtime.engine)
    monkeypatch.setattr(database, "SessionLocal", runtime.sessions)
    with runtime.sessions() as session:
        session.query(SpeakerCatalogIdentity).delete()
        session.commit()
    database.init_db()
    with runtime.sessions() as session:
        catalog_id = session.get(SpeakerCatalogIdentity, 1).catalog_id
        assert len(catalog_id) == 32
    database.init_db()
    with runtime.sessions() as session:
        assert session.get(SpeakerCatalogIdentity, 1).catalog_id == catalog_id


@pytest.mark.anyio
async def test_standalone_binding_can_omit_original_recording(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app),
        base_url="http://test",
        headers={"X-Speaker-Catalog": "d" * 32},
    ) as client:
        standalone = binding(evidence=None)
        response = await client.post(
            f"/enrollment/operations/{OP}/prepare",
            data={"binding": json.dumps(standalone)},
            files={"file": ("clip.wav", audio(), "audio/wav")},
        )
        assert response.status_code == 200
        response = await client.post(
            f"/enrollment/operations/{OP}/activate", json=standalone
        )
        assert response.status_code == 200
        assert response.json()["state"] == "active"
