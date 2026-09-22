from pathlib import Path

from backend import app_factory, cron_scheduler
from backend.config_loader import load_config


def test_production_cron_registration_keeps_timeline_explicit(monkeypatch):
    registered = {}
    monkeypatch.setattr(
        app_factory,
        "register_cron_job",
        lambda name, entrypoint: registered.setdefault(name, entrypoint),
    )

    app_factory.register_application_cron_jobs()

    assert registered["notification_dispatch"] is app_factory.queue_due_notifications
    assert registered["notification_receipts"] is app_factory.queue_receipt_check
    assert (
        registered["rolling_reconciliation_scan"] is app_factory.reconcile_dirty_ranges
    )
    assert (
        registered["timeline_publication_recovery"]
        is app_factory.recover_timeline_publications
    )
    assert (
        registered["timeline_episode_dispatch_recovery"]
        is app_factory.dispatch_ready_episodes
    )
    assert "timeline_analysis" not in registered
    assert registered["immich_memories"] is app_factory.scan_immich_memories


def test_effective_scheduler_enables_timeline_publication_recovery(monkeypatch):
    config_dir = Path(__file__).parents[2] / "config"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CONFIG_FILE", "missing-test-overrides.yml")
    monkeypatch.setattr(
        cron_scheduler,
        "load_config",
        lambda: load_config(force_reload=True),
    )
    scheduler = cron_scheduler.CronScheduler()

    scheduler._load_jobs_from_config()

    recovery = scheduler.jobs["timeline_publication_recovery"]
    assert recovery.enabled is True
    assert recovery.schedule == "*/5 * * * *"


def test_effective_scheduler_enables_timeline_episode_dispatch_recovery(monkeypatch):
    config_dir = Path(__file__).parents[2] / "config"
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CONFIG_FILE", "missing-test-overrides.yml")
    monkeypatch.setattr(
        cron_scheduler,
        "load_config",
        lambda: load_config(force_reload=True),
    )
    scheduler = cron_scheduler.CronScheduler()

    scheduler._load_jobs_from_config()

    recovery = scheduler.jobs["timeline_episode_dispatch_recovery"]
    assert recovery.enabled is True
    assert recovery.schedule == "*/5 * * * *"


async def test_registered_immich_scan_imports_and_links_candidates(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import httpx
    from beanie.odm.fields import ExpressionField

    from backend.services import device_context, immich_discovery

    registered = {}
    monkeypatch.setattr(
        app_factory,
        "register_cron_job",
        lambda name, fn: registered.setdefault(name, fn),
    )
    app_factory.register_application_cron_jobs()
    checkpoint = datetime(2026, 9, 16, tzinfo=timezone.utc)
    source = SimpleNamespace(
        status="offline", health={}, last_seen_at=None, save=AsyncMock()
    )
    sources = MagicMock()
    sources.find_one = AsyncMock(return_value=source)
    items = MagicMock()
    items.captured_at = ExpressionField("captured_at")
    items.find_one = AsyncMock(return_value=None)
    items.find.return_value.to_list = AsyncMock(return_value=[])
    monkeypatch.setattr(immich_discovery, "CaptureSource", sources)
    monkeypatch.setattr(immich_discovery, "DeviceInputItem", items)
    monkeypatch.setattr(
        immich_discovery, "resolve_immich_user_id", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        immich_discovery, "_settings", lambda: ("https://immich.test", "test-key")
    )
    monkeypatch.setattr(immich_discovery, "utcnow", lambda: checkpoint)
    store = AsyncMock(return_value=True)
    monkeypatch.setattr(immich_discovery, "_store_immich_candidate", store)
    window = dict(
        user_id="user",
        conversation_id="conversation",
        created_at=checkpoint,
        audio_total_duration=60,
    )

    async def windows():
        yield window

    conversations = MagicMock()
    conversations.get_pymongo_collection.return_value.find.return_value = windows()
    monkeypatch.setattr(immich_discovery, "Conversation", conversations)
    linker = AsyncMock()
    monkeypatch.setattr(device_context, "request_conversation_context_jobs", linker)
    photo = dict(id="photo", type="IMAGE", fileCreatedAt=checkpoint.isoformat())
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/api/search/metadata"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(
            200, json={"assets": {"items": [photo], "nextPage": None}}
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        immich_discovery.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw),
    )
    result = await registered["immich_memories"]()

    assert result == {"status": "ok", "assets": 1, "accepted": 1}
    assert len(requests) == 1
    store.assert_awaited_once_with("user", "immich-default", photo)
    assert source.status == "online"
    assert source.last_seen_at == checkpoint
    assert source.health == {"last_scan_candidates": 1, "last_scan_accepted": 1}
    source.save.assert_awaited_once()
    assert linker.await_args.args[0].conversation_id == "conversation"
