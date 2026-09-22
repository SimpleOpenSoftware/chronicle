from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from test_privacy_enrollment import allow, evidence, revoke  # noqa: F401
from test_privacy_enrollment_operations import enroll, enrollment_setup  # noqa: F401

from backend.services import privacy


@pytest.fixture
async def gallery(evidence, enrollment_setup, unused_tcp_port):
    await allow(evidence)
    await enroll(enrollment_setup)
    enrolled = await evidence.db.speaker_enrollment_operations.find_one({})
    state = SimpleNamespace(
        revision="a" * 64,
        calls=0,
        before_reply=None,
        receipt=True,
        held=[],
        speaker_id=enrolled["binding"]["speaker_id"],
    )
    client = enrollment_setup.client

    async def catalog(user_id=None):
        query = {"catalog_id": "d" * 32, "state": "active"}
        if user_id is not None:
            query["user_id"] = user_id
        rows = await evidence.db.speaker_enrollment_operations.find(query).to_list(None)
        return {
            "catalog_id": "d" * 32,
            "revision": state.revision,
            "unverified_speaker_ids": [],
            "held_speaker_ids": state.held,
            "active_operation_ids": [
                r["_id"] for r in rows if r["binding"]["speaker_id"] not in state.held
            ],
        }

    async def speakers(request):
        state.calls += 1
        assert request.headers["X-Speaker-Catalog"] == "d" * 32
        revision = request.headers["X-Speaker-Gallery-Revision"]
        if state.before_reply:
            await state.before_reply()
        headers = (
            {"X-Speaker-Catalog": "d" * 32, "X-Speaker-Gallery-Revision": revision}
            if state.receipt
            else {}
        )
        listed = [{"id": state.speaker_id, "name": "synthetic-private-sentinel"}]
        if request.query.get("user_id") == "different-owner":
            listed = [{"id": "synthetic-control", "name": "Synthetic control"}]
        elif state.speaker_id in state.held:
            listed = []
        return web.json_response({"speakers": listed}, headers=headers)

    app = web.Application()
    app.router.add_get("/speakers", speakers)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client.service_url = f"http://127.0.0.1:{unused_tcp_port}"
    client.enrollment_catalog = AsyncMock(side_effect=catalog)
    try:
        yield SimpleNamespace(client=client, state=state)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_explicit_enrollment_quarantine_blocks_gallery_before_network(
    evidence, gallery
):
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantine_pending"}}
    )
    with pytest.raises(privacy.PrivacyHeld):
        await gallery.client.get_enrolled_speakers(user_id="review-admin")
    assert gallery.state.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["preparing", "activating", "quarantine_pending"])
async def test_unresolved_enrollment_blocks_use_before_recovery(
    evidence, gallery, state
):
    await evidence.db.speaker_enrollment_operations.update_one(
        {}, {"$set": {"state": state}}
    )
    with pytest.raises(privacy.PrivacyHeld):
        await gallery.client.get_enrolled_speakers(user_id="review-admin")
    assert gallery.state.calls == 0


@pytest.mark.asyncio
async def test_quarantine_during_http_call_drops_body_and_logs(
    evidence, gallery, caplog
):
    gallery.state.before_reply = (
        lambda: evidence.db.speaker_enrollment_operations.update_many(
            {}, {"$set": {"state": "quarantined"}}
        )
    )
    with pytest.raises(privacy.PrivacyHeld):
        await gallery.client.get_enrolled_speakers(user_id="review-admin")
    assert gallery.state.calls == 1
    assert "synthetic-private-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_provider_revision_change_drops_result_without_policy_change(
    evidence, gallery
):
    async def changed():
        gallery.state.revision = "b" * 64

    gallery.state.before_reply = changed
    with pytest.raises(privacy.PrivacyHeld):
        await gallery.client.get_enrolled_speakers(user_id="review-admin")


@pytest.mark.asyncio
async def test_journal_membership_change_cannot_release_stale_result(evidence, gallery):
    async def changed():
        await evidence.db.speaker_enrollment_operations.update_one(
            {}, {"$set": {"state": "quarantined"}}
        )

    gallery.state.before_reply = changed
    with pytest.raises(privacy.PrivacyHeld):
        await gallery.client.get_enrolled_speakers(user_id="review-admin")


@pytest.mark.asyncio
async def test_missing_response_receipt_stays_held_even_if_inner_client_catches_error(
    evidence, gallery
):
    gallery.state.receipt = False
    with pytest.raises(privacy.PrivacyHeld):
        await gallery.client.get_enrolled_speakers(user_id="review-admin")


@pytest.mark.asyncio
async def test_other_tenant_remains_usable(evidence, gallery):
    await revoke(evidence)
    result = await gallery.client.get_enrolled_speakers(user_id="different-owner")
    assert len(result["speakers"]) == 1
    assert result["speakers"][0]["name"] == "Synthetic control"
    assert gallery.state.calls == 1


@pytest.mark.asyncio
async def test_provider_held_profile_is_omitted_but_direct_target_stays_held(
    evidence, gallery
):
    from backend.services.speaker_gallery_privacy import GalleryRead

    await revoke(evidence)
    gallery.state.held = [gallery.state.speaker_id]
    result = await gallery.client.get_enrolled_speakers(user_id="review-admin")
    assert result["speakers"] == []
    with pytest.raises(privacy.PrivacyHeld):
        await GalleryRead(
            gallery.client, user_id="review-admin", speaker_id=gallery.state.speaker_id
        ).start()


@pytest.mark.asyncio
async def test_unverified_or_unjournaled_provider_gallery_is_held(evidence, gallery):
    for catalog in [
        {
            "catalog_id": "d" * 32,
            "revision": "a" * 64,
            "unverified_speaker_ids": ["unverified"],
            "active_operation_ids": [],
        },
        {
            "catalog_id": "d" * 32,
            "revision": "a" * 64,
            "unverified_speaker_ids": [],
            "active_operation_ids": ["unknown-operation"],
        },
    ]:
        gallery.client.enrollment_catalog = AsyncMock(return_value=catalog)
        with pytest.raises(privacy.PrivacyHeld):
            await gallery.client.get_enrolled_speakers(user_id="review-admin")
    assert gallery.state.calls == 0


@pytest.mark.asyncio
async def test_result_retains_scope_after_http_return_without_serializing_it(
    evidence, gallery
):
    import json

    from backend.services.speaker_gallery_privacy import result_scope

    result = await gallery.client.get_enrolled_speakers(user_id="review-admin")
    scope = result_scope(result)
    row = await evidence.db.speaker_enrollment_operations.find_one({})
    assert scope.receipt()["operation_ids"] == [row["_id"]]
    assert "catalog_id" not in json.dumps(result)
    await evidence.db.speaker_enrollment_operations.update_many(
        {}, {"$set": {"state": "quarantined"}}
    )
    writes = []
    with pytest.raises(privacy.PrivacyHeld):
        async with scope.publication(privacy.ConversationPrivacyFilter()):
            writes.append(True)
    assert writes == []


@pytest.mark.asyncio
async def test_publication_checks_target_source_as_well_as_gallery(evidence, gallery):
    from backend.services.speaker_gallery_privacy import result_scope

    result = await gallery.client.get_enrolled_speakers(user_id="different-owner")
    target = privacy.ConversationPrivacyFilter()
    assert len(await target.filter([{"conversation_id": "synthetic-recording"}])) == 1
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        async with result_scope(result).publication(target):
            pytest.fail("A changed target cannot publish")


def test_plain_response_cannot_be_published_as_verified_gallery_output():
    from backend.services.speaker_gallery_privacy import result_scope

    with pytest.raises(privacy.PrivacyHeld):
        result_scope({"segments": []})
