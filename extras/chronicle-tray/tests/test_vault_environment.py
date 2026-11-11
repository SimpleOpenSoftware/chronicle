import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chronicle_client.config as client_config
from chronicle_tray.sections import vault


def test_vault_environment_loads_from_repository_root(tmp_path, monkeypatch):
    """The tray reads its client config from the repository-root .env.

    That file is shared by every native client component (tray, vault sync,
    wearable client), so the tray must not carry its own copy of the location.
    """
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://chronicle.example\n"
        "CHRONICLE_API_KEY=chrn_abc123_secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(client_config, "REPO_ROOT", tmp_path)
    # The loader is memoised so repeated section startups don't re-read the file.
    monkeypatch.setattr(client_config, "_env_loaded", False)
    for name in ("BACKEND_URL", "CHRONICLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    vault._load_vault_environment()

    assert os.environ["BACKEND_URL"] == "https://chronicle.example"
    assert os.environ["CHRONICLE_API_KEY"] == "chrn_abc123_secret"


def test_vault_environment_is_loaded_only_once(tmp_path, monkeypatch):
    """Re-entry must not re-read the file — several tray sections call this."""
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://first.example\n", encoding="utf-8"
    )
    monkeypatch.setattr(client_config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(client_config, "_env_loaded", False)
    monkeypatch.delenv("BACKEND_URL", raising=False)

    vault._load_vault_environment()
    (tmp_path / ".env").write_text(
        "BACKEND_URL=https://second.example\n", encoding="utf-8"
    )
    vault._load_vault_environment()

    assert os.environ["BACKEND_URL"] == "https://first.example"


def test_space_vault_defaults_to_a_sibling_of_main(tmp_path):
    manager = object.__new__(vault.VaultSyncManager)
    manager.config = type("Config", (), {"local_vault_dir": str(tmp_path / "Main")})()
    space_id = "9f3523c8-af75-469d-995a-7179531f3fc8"

    local_dir = Path(
        manager._local_dir({"memory_space_id": space_id, "name": "Launch brainstorm"})
    )

    assert local_dir.parent == tmp_path / "Chronicle Spaces"
    assert local_dir != tmp_path / "Main"
    assert space_id[:8] in local_dir.name


def test_active_space_folders_are_automatically_reconciled(tmp_path, monkeypatch):
    manager = object.__new__(vault.VaultSyncManager)
    manager.state = vault.SharedState(
        folders={
            "active-existing": {
                "memory_space_id": "active-existing",
                "state": "active",
                "paired": True,
                "local_dir": str(tmp_path / "Existing"),
            }
        }
    )
    manager.config = type("Config", (), {"local_vault_dir": str(tmp_path / "Main")})()
    manager._inventory_lock = threading.Lock()
    monkeypatch.setattr(
        manager,
        "_folder_inventory",
        lambda: [
            {"memory_space_id": None, "name": "Main", "state": "active"},
            {
                "memory_space_id": "active-new",
                "name": "Fresh brainstorm",
                "state": "active",
            },
            {
                "memory_space_id": "active-existing",
                "name": "Already here",
                "state": "active",
            },
            {
                "memory_space_id": "merging-space",
                "name": "Frozen for review",
                "state": "merging",
            },
            {
                "memory_space_id": "archived-space",
                "name": "Old notebook",
                "state": "archived",
            },
        ],
    )
    reconciled = []
    monkeypatch.setattr(manager, "_pair", reconciled.append)

    manager._load_folders()

    assert reconciled == ["active-new"]


def test_scoped_pair_acknowledges_server_rescan(tmp_path, monkeypatch):
    class SyncthingStub:
        def start(self):
            pass

        def device_id(self):
            return "CLIENT-DEVICE"

        def ensure_server_device(self, *_args):
            pass

        def ensure_folder(self, *_args):
            pass

    manager = object.__new__(vault.VaultSyncManager)
    manager.state = vault.SharedState()
    manager.config = SimpleNamespace(
        api_key="client-key",
        backend_url="https://chronicle.example",
        device_name="capture-node",
        local_vault_dir=str(tmp_path / "Main"),
    )
    manager.syncthing = SyncthingStub()
    manager._lock = threading.Lock()
    space_id = "9f3523c8-af75-469d-995a-7179531f3fc8"
    monkeypatch.setattr(
        manager,
        "_folder_inventory",
        lambda: [{"memory_space_id": space_id, "name": "Launch", "state": "active"}],
    )
    monkeypatch.setattr(
        vault,
        "broker_pair",
        lambda *_args: {
            "folder_id": f"space-user-{space_id}",
            "folder_label": "Chronicle Space — Launch",
            "server_device_id": "SERVER-DEVICE",
            "sync_address": "tcp://chronicle.example:22000",
        },
    )
    actions = []
    monkeypatch.setattr(
        vault,
        "broker_space_action",
        lambda _url, _key, selected_space_id, action: (
            actions.append((selected_space_id, action))
            or {"healthy": True, "error": None}
        ),
    )

    manager._pair(space_id)

    assert actions == [(space_id, "rescan")]
    assert manager.state.snapshot()["folders"][space_id]["sync_state"] == "healthy"
