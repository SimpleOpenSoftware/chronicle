import asyncio

import httpx
import pytest
from fastapi import File, Form, UploadFile
from fastapi.responses import StreamingResponse
from test_enrollment_operations import (  # noqa: F401
    OWNER,
    activate,
    anyio_backend,
    prepare,
    quarantine,
    runtime,
)

from simple_speaker_recognition.api import gallery_fence
from simple_speaker_recognition.database.models import Speaker


@pytest.fixture
def fenced(runtime, monkeypatch):
    async def gallery():
        return runtime.gallery

    monkeypatch.setattr(gallery_fence, "get_gallery", gallery)
    runtime.app.add_middleware(gallery_fence.GalleryRevisionFence)
    return runtime


@pytest.mark.anyio
async def test_quarantine_during_identification_discards_response(fenced, caplog):
    entered, release = asyncio.Event(), asyncio.Event()

    @fenced.app.post("/identify")
    async def identify():
        import logging

        logging.getLogger("speaker_service").warning("synthetic-private-log-sentinel")
        entered.set()
        await release.wait()
        return {"speaker_name": "synthetic-private-sentinel"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        task = asyncio.create_task(client.post("/identify"))
        await asyncio.wait_for(entered.wait(), 5)
        assert (await quarantine(client)).status_code == 200
        release.set()
        result = await task
        assert result.status_code == 423
        assert "synthetic-private-sentinel" not in result.text
        assert "synthetic-private-log-sentinel" not in caplog.text


@pytest.mark.anyio
async def test_quarantine_during_streamed_audio_holds_entire_response(fenced):
    entered, release = asyncio.Event(), asyncio.Event()

    @fenced.app.get("/speakers/synthetic/audio/synthetic.wav")
    async def audio():
        async def chunks():
            yield b"synthetic-private-audio-start"
            entered.set()
            await release.wait()
            yield b"synthetic-private-audio-end"

        return StreamingResponse(chunks(), media_type="audio/wav")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        task = asyncio.create_task(
            client.get("/speakers/synthetic/audio/synthetic.wav")
        )
        await asyncio.wait_for(entered.wait(), 5)
        await quarantine(client)
        release.set()
        result = await task
        assert result.status_code == 423
        assert b"synthetic-private-audio" not in result.content


@pytest.mark.anyio
async def test_unknown_gallery_provenance_is_held_before_inference(fenced):
    called = []

    @fenced.app.post("/identify")
    async def identify():
        called.append(True)
        return {"speaker_name": "unverified"}

    with fenced.sessions() as session:
        session.add(
            Speaker(
                id="unverified",
                name="Synthetic speaker",
                user_id=OWNER,
                embedding_data="[1,0]",
                audio_sample_count=1,
            )
        )
        session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        result = await client.post("/identify")
        assert result.status_code == 423
        assert called == []
        catalog = (await client.get("/enrollment/operations/catalog")).json()
        assert catalog["unverified_speaker_ids"] == ["unverified"]


@pytest.mark.anyio
async def test_allowed_result_carries_exact_catalog_receipt_and_stale_requests_stop(
    fenced,
):
    @fenced.app.post("/identify")
    async def identify():
        return {"speaker_name": "Synthetic speaker"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        await prepare(client)
        await activate(client)
        catalog = (await client.get("/enrollment/operations/catalog")).json()
        result = await client.post("/identify")
        assert result.status_code == 200
        assert result.headers["X-Speaker-Catalog"] == catalog["catalog_id"]
        assert result.headers["X-Speaker-Gallery-Revision"] == catalog["revision"]
        result = await client.post(
            "/identify", headers={"X-Speaker-Gallery-Revision": "stale"}
        )
        assert result.status_code == 423


@pytest.mark.anyio
async def test_other_tenant_is_not_held_by_unverified_gallery(fenced):
    @fenced.app.post("/identify")
    async def identify():
        return {"found": False}

    with fenced.sessions() as session:
        session.add(
            Speaker(
                id="unverified",
                name="Synthetic speaker",
                user_id=OWNER,
                embedding_data="[1,0]",
                audio_sample_count=1,
            )
        )
        session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/identify",
            json={"user_id": "other-owner"},
            headers={"X-Speaker-Gallery-User": "other-owner"},
        )
        assert result.status_code == 200
        assert result.json() == {"found": False}
        result = await client.post(
            "/identify",
            json={"user_id": OWNER},
            headers={"X-Speaker-Gallery-User": "other-owner"},
        )
        assert result.status_code == 423


@pytest.mark.anyio
async def test_multipart_scope_check_replays_exact_upload(fenced):
    received = []

    @fenced.app.post("/identify")
    async def identify(user_id: str = Form(...), file: UploadFile = File(...)):
        received.append((user_id, await file.read(), file.filename))
        return {"found": False}

    payload = b"synthetic-audio" * 100000
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/identify",
            data={"user_id": OWNER},
            files={"file": ("synthetic.wav", payload, "audio/wav")},
            headers={"X-Speaker-Gallery-User": OWNER},
        )
        assert result.status_code == 200
        assert received == [(OWNER, payload, "synthetic.wav")]
        result = await client.post(
            "/identify",
            data={"user_id": OWNER},
            files={"file": ("synthetic.wav", payload, "audio/wav")},
            headers={"X-Speaker-Gallery-User": "other-owner"},
        )
        assert result.status_code == 423
        assert len(received) == 1


@pytest.mark.anyio
async def test_complete_standalone_enrollment_needs_no_source_journal(fenced):
    from simple_speaker_recognition.database.models import SpeakerAudioSegment

    with fenced.sessions() as session:
        session.add(
            Speaker(
                id="standalone",
                name="Standalone",
                user_id=OWNER,
                embedding_data="[1,0]",
                audio_sample_count=1,
            )
        )
        session.add(
            SpeakerAudioSegment(
                speaker_id="standalone",
                audio_file_path="clip.wav",
                start_time=0,
                end_time=1,
                duration_seconds=1,
                embedding="[1,0]",
            )
        )
        session.commit()

    @fenced.app.post("/identify")
    async def identify():
        return {"speaker_name": "Standalone"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fenced.app), base_url="http://test"
    ) as client:
        result = await client.post("/identify", json={"user_id": OWNER})
        assert result.status_code == 200
        assert result.json()["speaker_name"] == "Standalone"
