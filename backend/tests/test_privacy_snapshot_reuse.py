"""A request reuses policy data without sharing mutable admission bookkeeping."""

import asyncio
import gc
import weakref
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from test_privacy_enrollment import START, allow, evidence, revoke  # noqa: F401

from backend.services import privacy
from backend.services.reference_dependencies import seal


async def derivative(evidence):
    await allow(evidence)
    original = await evidence.db.conversations.find_one({})
    receipt = await seal("evidence-owner", [original])
    return {
        "user_id": "evidence-owner",
        "client_id": "ordinary-device",
        "created_at": START + timedelta(days=1),
        "privacy_reference_receipt": receipt,
    }


async def test_record_admission_loads_same_owner_full_policy_once(
    evidence, monkeypatch
):
    row = await derivative(evidence)
    load = AsyncMock(wraps=privacy._load_capture_snapshot)
    monkeypatch.setattr(privacy, "_load_capture_snapshot", load)
    snapshot = await privacy.require_record(row)
    assert load.await_count == 1
    original_snapshot = snapshot.gallery_dependencies.snapshots["evidence-owner"]
    assert original_snapshot is not snapshot
    assert snapshot._checked_identifiers == {"ordinary-device"}
    assert original_snapshot._checked_identifiers == {"screenpipe-test"}
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.assert_current("evidence-owner", snapshot)


async def test_bounded_capture_policy_cannot_replace_original_reference_coverage(
    evidence, monkeypatch
):
    row = await derivative(evidence)
    load = AsyncMock(wraps=privacy._load_capture_snapshot)
    monkeypatch.setattr(privacy, "_load_capture_snapshot", load)
    start = START + timedelta(days=1)
    snapshot = await privacy.load_snapshot(
        "evidence-owner", start, start + timedelta(seconds=10)
    )
    assert load.await_count == 2
    assert snapshot.permits_record(row)
    await revoke(evidence)
    current = await privacy.load_snapshot(
        "evidence-owner", start, start + timedelta(seconds=10)
    )
    assert not current.permits_record(row)


async def test_new_request_reloads_policy_after_a_completed_override(
    evidence, monkeypatch
):
    row = await derivative(evidence)
    load = AsyncMock(wraps=privacy._load_capture_snapshot)
    monkeypatch.setattr(privacy, "_load_capture_snapshot", load)
    assert (await privacy.require_record(row)).permits_record(row)
    await revoke(evidence)
    with pytest.raises(privacy.PrivacyHeld):
        await privacy.require_record(row)
    assert load.await_count == 2


@pytest.mark.parametrize("bounded", [False, True])
async def test_completed_admission_releases_dependency_policies_without_cyclic_gc(
    evidence, bounded
):
    row = await derivative(evidence)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        bounds = (
            (row["created_at"], row["created_at"] + timedelta(seconds=10))
            if bounded
            else ()
        )
        snapshot = await privacy.load_snapshot("evidence-owner", *bounds)
        assert snapshot.permits_record(row)
        await privacy.assert_current("evidence-owner", snapshot)
        dependencies = weakref.ref(snapshot.gallery_dependencies)
        original = weakref.ref(
            snapshot.gallery_dependencies.snapshots["evidence-owner"]
        )
        del snapshot
        # Let asyncio release the completed to_thread result callbacks too.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Completed requests must not retain the entire historical policy until
        # a process-wide generation-two collection stops other capture work.
        assert dependencies() is None
        assert original() is None
    finally:
        if was_enabled:
            gc.enable()
