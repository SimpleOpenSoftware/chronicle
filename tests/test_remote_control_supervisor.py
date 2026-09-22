import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import services


def test_remote_control_unit_stops_cleanly_when_auth_is_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(services, "_SYSTEMD_USER_DIR", tmp_path)
    monkeypatch.setattr(services, "_claude_rc_dir", lambda: tmp_path)
    monkeypatch.setattr(
        services,
        "_claude_rc_command",
        lambda: ["/opt/claude", "remote-control", "--name", "Kraken"],
    )
    monkeypatch.setattr(
        services.shutil,
        "which",
        lambda command: {
            "tmux": "/usr/bin/tmux",
            "claude": "/opt/claude",
            "uv": "/usr/bin/uv",
        }.get(command),
    )

    unit_path = services._write_remote_control_unit()
    unit = unit_path.read_text(encoding="utf-8")
    supervisor = (tmp_path / "chronicle-remote-control.sh").read_text(encoding="utf-8")

    assert "/opt/claude auth status" in supervisor
    assert "exit 0" in supervisor
    assert "Restart=on-failure" in unit
    assert "Restart=always" not in unit
    assert "StartLimitIntervalSec=300" in unit
    assert "StartLimitBurst=5" in unit
    assert "ExecStop=-/usr/bin/tmux" in unit
    assert supervisor.rstrip().endswith("exit 1")
