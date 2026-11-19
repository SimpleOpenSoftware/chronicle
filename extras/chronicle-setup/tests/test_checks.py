"""Tests for the host-health checks.

Every fixture below is real output captured from the 2026-07-29 incident on a
production node, not invented strings — the point of these checks is to recognise
that exact failure again, so the tests assert against what the tools actually said.

Nothing here touches the network, a container engine, or systemd: ``subprocess.run``
and ``shutil.which`` are faked at the module boundary, following the pattern in
``tests/unit/test_wizard_strixhalo.py``.
"""

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from chronicle_setup import checks
from chronicle_setup.checks import (
    FAIL,
    NOT_APPLICABLE,
    OK,
    WARN,
    CheckContext,
    CheckResult,
)

# --------------------------------------------------------------------------- fixtures
# Recorded from the live node.

STATUS_RUNNING = json.dumps(
    {
        "BackendState": "Running",
        "AuthURL": "",
        "Self": {
            "HostName": "backend-node",
            "DNSName": "backend-node.example.ts.net.",
            "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::10"],
            "Online": True,
            "KeyExpiry": "2027-01-28T05:28:38Z",
        },
    }
)

# The broken state: daemon up, interface present, but no key.
STATUS_LOGGED_OUT = json.dumps(
    {"BackendState": "NeedsLogin", "AuthURL": "", "Self": {"HostName": "backend-node"}}
)

# Epoch of the KeyExpiry above. Derived rather than hand-written, because an
# off-by-one-day constant silently weakens every expiry assertion below.
KEY_EXPIRY_EPOCH = datetime(2027, 1, 28, 5, 28, 38, tzinfo=timezone.utc).timestamp()

PREFS_WITH_OPERATOR = json.dumps({"WantRunning": True, "OperatorUser": "alex"})
# After `tailscale login`, OperatorUser is silently absent — this is what broke Caddy.
PREFS_WITHOUT_OPERATOR = json.dumps({"WantRunning": True, "LoggedOut": False})

CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
CERT_SUMMARY = (
    "issuer=C = US, O = Let's Encrypt, CN = YE1\n"
    "subject=CN = backend-node.example.ts.net\n"
    "notAfter=Oct 28 13:47:26 2026 GMT\n"
)


class FakeRunner:
    """Dispatch fake subprocess results by matching tokens in the argv.

    Records every argv so tests can assert on the exact command a repair would run,
    which matters more than its output — a repair that shells the wrong thing is the
    dangerous failure.
    """

    def __init__(self, rules, default=(1, "", "")):
        self.rules = rules
        self.default = default
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for tokens, result in self.rules:
            if all(any(tok in part for part in argv) for tok in tokens):
                if isinstance(result, Exception):
                    raise result
                rc, out, err = result
                return SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        rc, out, err = self.default
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def argv_containing(self, token):
        return [c for c in self.calls if any(token in part for part in c)]


@pytest.fixture
def have_all_binaries(monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")


def install(monkeypatch, runner):
    monkeypatch.setattr(checks.subprocess, "run", runner)
    return runner


RUNNING_CONTAINER = (["inspect"], (0, "true\n", ""))
NO_CONTAINER = (["inspect"], (1, "", "no such container"))


# ------------------------------------------------------------------------ container DNS


def test_dns_ok_when_container_resolves(monkeypatch, have_all_binaries):
    install(
        monkeypatch,
        FakeRunner(
            [
                RUNNING_CONTAINER,
                (["getent"], (0, "162.159.140.245  api.openai.com\n", "")),
            ]
        ),
    )
    result = checks.check_container_dns(
        CheckContext(engine="podman", dns_containers=("backend",))
    )
    assert result.status == OK
    assert result.repair is None


def test_dns_fails_on_servfail_and_offers_repair(monkeypatch, have_all_binaries):
    """The observed fault: getent exits non-zero because aardvark returns SERVFAIL."""
    install(
        monkeypatch,
        FakeRunner([RUNNING_CONTAINER, (["getent"], (2, "", ""))]),
    )
    result = checks.check_container_dns(
        CheckContext(engine="podman", dns_containers=("backend",), network="net")
    )
    assert result.status == FAIL
    assert "cannot resolve" in result.detail
    assert result.repair is not None


def test_dns_not_applicable_without_engine(monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    result = checks.check_container_dns(CheckContext(dns_containers=("backend",)))
    assert result.status == NOT_APPLICABLE


def test_dns_not_applicable_when_no_container_running(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([NO_CONTAINER]))
    result = checks.check_container_dns(
        CheckContext(engine="podman", dns_containers=("backend",))
    )
    assert result.status == NOT_APPLICABLE


def test_dns_not_applicable_when_getent_absent(monkeypatch, have_all_binaries):
    """A slim image without getent must not be reported as a DNS outage."""
    install(
        monkeypatch,
        FakeRunner([RUNNING_CONTAINER, (["getent"], (127, "", "not found"))]),
    )
    result = checks.check_container_dns(
        CheckContext(engine="podman", dns_containers=("backend",))
    )
    assert result.status == NOT_APPLICABLE


# -------------------------------------------------------------------- container MagicDNS


def test_magicdns_ok_when_container_resolves_the_node(monkeypatch, have_all_binaries):
    install(
        monkeypatch,
        FakeRunner(
            [
                (["status"], (0, STATUS_RUNNING, "")),
                RUNNING_CONTAINER,
                (["getent"], (0, "100.64.0.10  backend-node.example.ts.net\n", "")),
            ]
        ),
    )
    result = checks.check_container_magicdns(
        CheckContext(engine="podman", dns_containers=("backend",))
    )
    assert result.status == OK


def test_magicdns_fails_when_only_public_upstreams_are_pinned(
    monkeypatch, have_all_binaries
):
    """The regression: public DNS resolves, so check_container_dns stays green."""
    runner = install(
        monkeypatch,
        FakeRunner(
            [
                (["status"], (0, STATUS_RUNNING, "")),
                RUNNING_CONTAINER,
                (["getent", "api.openai.com"], (0, "162.159.140.245  x\n", "")),
                (["getent", "ts.net"], (2, "", "")),
            ]
        ),
    )
    ctx = CheckContext(engine="podman", dns_containers=("backend",), network="net")

    assert checks.check_container_dns(ctx).status == OK

    result = checks.check_container_magicdns(ctx)
    assert result.status == FAIL
    assert "backend-node.example.ts.net" in result.detail
    assert "100.100.100.100" in (result.remedy or "")
    # Probed by the node's own name, trailing dot stripped.
    assert runner.argv_containing("backend-node.example.ts.net")


def test_magicdns_not_applicable_without_a_tailnet(monkeypatch, have_all_binaries):
    """A host with no tailnet is not misreported as broken."""
    install(
        monkeypatch,
        FakeRunner([(["status"], (0, STATUS_LOGGED_OUT, "")), RUNNING_CONTAINER]),
    )
    result = checks.check_container_magicdns(
        CheckContext(engine="podman", dns_containers=("backend",))
    )
    assert result.status == NOT_APPLICABLE


def test_magicdns_not_applicable_when_getent_absent(monkeypatch, have_all_binaries):
    install(
        monkeypatch,
        FakeRunner(
            [
                (["status"], (0, STATUS_RUNNING, "")),
                RUNNING_CONTAINER,
                (["getent"], (127, "", "not found")),
            ]
        ),
    )
    result = checks.check_container_magicdns(
        CheckContext(engine="podman", dns_containers=("backend",))
    )
    assert result.status == NOT_APPLICABLE


# ------------------------------------------------------------------------ tailscale


def test_login_ok_when_running(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["status"], (0, STATUS_RUNNING, ""))]))
    result = checks.check_tailscale_login(CheckContext())
    assert result.status == OK
    assert "backend-node.example.ts.net" in result.detail


def test_login_fails_when_logged_out(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["status"], (0, STATUS_LOGGED_OUT, ""))]))
    result = checks.check_tailscale_login(CheckContext())
    assert result.status == FAIL
    assert "NeedsLogin" in result.detail
    # Logging in needs a browser, so this must never claim to be auto-repairable.
    assert result.repair is None
    assert "tailscale login" in result.remedy


def test_login_not_applicable_without_tailscale(monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    assert checks.check_tailscale_login(CheckContext()).status == NOT_APPLICABLE


def test_key_expiry_ok_when_distant(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["status"], (0, STATUS_RUNNING, ""))]))
    # 100 days before the fixture's expiry.
    monkeypatch.setattr(checks.time, "time", lambda: KEY_EXPIRY_EPOCH - 100 * 86400)
    assert checks.check_tailscale_key_expiry(CheckContext()).status == OK


def test_key_expiry_warns_inside_window(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["status"], (0, STATUS_RUNNING, ""))]))
    # Five days before the fixture's expiry.
    monkeypatch.setattr(checks.time, "time", lambda: KEY_EXPIRY_EPOCH - 5 * 86400)
    result = checks.check_tailscale_key_expiry(CheckContext(warn_expiry_days=14))
    assert result.status == WARN
    # Key expiry is a control-plane setting; the node cannot fix it.
    assert result.repair is None
    assert "admin" in result.remedy


def test_key_expiry_fails_when_already_expired(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["status"], (0, STATUS_RUNNING, ""))]))
    monkeypatch.setattr(checks.time, "time", lambda: KEY_EXPIRY_EPOCH + 86400)
    assert checks.check_tailscale_key_expiry(CheckContext()).status == FAIL


def test_key_expiry_ok_when_disabled(monkeypatch, have_all_binaries):
    """No KeyExpiry means expiry is disabled — the desired end state, not a gap."""
    status = json.dumps(
        {"BackendState": "Running", "Self": {"HostName": "backend-node"}}
    )
    install(monkeypatch, FakeRunner([(["status"], (0, status, ""))]))
    result = checks.check_tailscale_key_expiry(CheckContext())
    assert result.status == OK
    assert "disabled" in result.detail


def test_operator_ok_when_set(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["prefs"], (0, PREFS_WITH_OPERATOR, ""))]))
    result = checks.check_tailscale_operator(CheckContext(operator_user="alex"))
    assert result.status == OK


def test_operator_fails_when_cleared_by_login(monkeypatch, have_all_binaries):
    """Reproduces the second half of the outage: login silently dropped the pref."""
    install(monkeypatch, FakeRunner([(["prefs"], (0, PREFS_WITHOUT_OPERATOR, ""))]))
    result = checks.check_tailscale_operator(CheckContext(operator_user="alex"))
    assert result.status == FAIL
    assert "unset" in result.detail
    assert result.repair is not None


def test_operator_not_applicable_without_expected_user(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["prefs"], (0, PREFS_WITH_OPERATOR, ""))]))
    assert checks.check_tailscale_operator(CheckContext()).status == NOT_APPLICABLE


# --------------------------------------------------------------------- socket freshness


@pytest.fixture
def fake_socket(tmp_path):
    sock = tmp_path / "tailscaled.sock"
    sock.write_text("")
    return sock


def test_socket_ok_when_mtimes_match(monkeypatch, have_all_binaries, fake_socket):
    mtime = int(fake_socket.stat().st_mtime)
    install(
        monkeypatch,
        FakeRunner([RUNNING_CONTAINER, (["stat"], (0, f"{mtime}\n", ""))]),
    )
    result = checks.check_socket_mount_fresh(
        CheckContext(
            engine="podman",
            socket_containers=("caddy",),
            socket_path=str(fake_socket),
        )
    )
    assert result.status == OK


def test_socket_fails_when_container_holds_deleted_inode(
    monkeypatch, have_all_binaries, fake_socket
):
    """The Caddy fault: container mtime frozen days behind the host's."""
    stale = int(fake_socket.stat().st_mtime) - 3 * 86400
    install(
        monkeypatch,
        FakeRunner([RUNNING_CONTAINER, (["stat"], (0, f"{stale}\n", ""))]),
    )
    result = checks.check_socket_mount_fresh(
        CheckContext(
            engine="podman",
            socket_containers=("caddy",),
            socket_path=str(fake_socket),
        )
    )
    assert result.status == FAIL
    assert "caddy" in result.detail
    assert result.repair is not None


def test_socket_not_applicable_without_host_socket(
    monkeypatch, have_all_binaries, tmp_path
):
    result = checks.check_socket_mount_fresh(
        CheckContext(socket_path=str(tmp_path / "absent.sock"))
    )
    assert result.status == NOT_APPLICABLE


def test_socket_ignores_containers_without_the_mount(
    monkeypatch, have_all_binaries, fake_socket
):
    """Containers that never mount the socket must not be reported as stale.

    Callers pass every container in the compose project, most of which have no
    Tailscale mount; stat fails for those. A genuinely stale mount stats fine and
    returns an old mtime, so a non-zero exit can only mean 'not mounted'.
    """
    install(
        monkeypatch,
        FakeRunner(
            [RUNNING_CONTAINER, (["stat"], (1, "", "No such file or directory"))]
        ),
    )
    result = checks.check_socket_mount_fresh(
        CheckContext(
            engine="podman",
            socket_containers=("mongo", "redis"),
            socket_path=str(fake_socket),
        )
    )
    assert result.status == NOT_APPLICABLE
    assert result.repair is None


def test_socket_not_applicable_when_nothing_mounts_it(
    monkeypatch, have_all_binaries, fake_socket
):
    install(monkeypatch, FakeRunner([NO_CONTAINER]))
    result = checks.check_socket_mount_fresh(
        CheckContext(
            engine="podman", socket_containers=("caddy",), socket_path=str(fake_socket)
        )
    )
    assert result.status == NOT_APPLICABLE


# ------------------------------------------------------------------------------- TLS


def test_tls_ok_with_valid_cert(monkeypatch, have_all_binaries):
    install(
        monkeypatch,
        FakeRunner(
            [
                (["s_client"], (0, CERT_PEM, "")),
                (["x509"], (0, CERT_SUMMARY, "")),
            ]
        ),
    )
    result = checks.check_tls_cert(
        CheckContext(https_host="backend-node.example.ts.net")
    )
    assert result.status == OK
    assert "Let's Encrypt" in result.detail


def test_tls_uses_sni(monkeypatch, have_all_binaries):
    """Without -servername, Caddy serves its localhost cert and the outage hides."""
    runner = install(
        monkeypatch,
        FakeRunner(
            [(["s_client"], (0, CERT_PEM, "")), (["x509"], (0, CERT_SUMMARY, ""))]
        ),
    )
    checks.check_tls_cert(CheckContext(https_host="backend-node.example.ts.net"))
    argv = runner.argv_containing("s_client")[0]
    assert "-servername" in argv
    assert "backend-node.example.ts.net" in argv


def test_tls_fails_when_no_certificate_served(monkeypatch, have_all_binaries):
    """What the broken node did: handshake produced no certificate at all."""
    install(monkeypatch, FakeRunner([(["s_client"], (1, "", "connect: errno=111"))]))
    result = checks.check_tls_cert(
        CheckContext(https_host="backend-node.example.ts.net")
    )
    assert result.status == FAIL
    assert "cert access denied" in result.remedy


def test_tls_warns_when_expiring(monkeypatch, have_all_binaries):
    install(
        monkeypatch,
        FakeRunner(
            [(["s_client"], (0, CERT_PEM, "")), (["x509"], (1, CERT_SUMMARY, ""))]
        ),
    )
    result = checks.check_tls_cert(
        CheckContext(https_host="backend-node.example.ts.net")
    )
    assert result.status == WARN


def test_tls_not_applicable_when_https_disabled(monkeypatch, have_all_binaries):
    assert checks.check_tls_cert(CheckContext()).status == NOT_APPLICABLE


# --------------------------------------------------------------------------- repairs


def test_repair_operator_tries_unprivileged_before_sudo(monkeypatch, have_all_binaries):
    runner = install(monkeypatch, FakeRunner([(["set"], (0, "", ""))]))
    assert checks.repair_tailscale_operator("alex") is True
    assert runner.calls == [["tailscale", "set", "--operator=alex"]]


def test_repair_operator_falls_back_to_sudo(monkeypatch, have_all_binaries):
    runner = install(
        monkeypatch,
        FakeRunner(
            [(["sudo"], (0, "", "")), (["set"], (1, "", "access denied"))],
        ),
    )
    assert checks.repair_tailscale_operator("alex") is True
    assert runner.calls[-1] == ["sudo", "-n", "tailscale", "set", "--operator=alex"]


def test_repair_restart_restarts_each_container(monkeypatch, have_all_binaries):
    runner = install(monkeypatch, FakeRunner([(["restart"], (0, "", ""))]))
    assert checks.repair_restart_containers("podman", ("caddy", "backend")) is True
    assert runner.calls == [
        ["podman", "restart", "caddy"],
        ["podman", "restart", "backend"],
    ]


def test_repair_restart_reports_failure(monkeypatch, have_all_binaries):
    install(monkeypatch, FakeRunner([(["restart"], (1, "", "no such container"))]))
    assert checks.repair_restart_containers("podman", ("caddy",)) is False


def test_repair_dns_churns_a_container_on_the_network(monkeypatch, have_all_binaries):
    runner = install(monkeypatch, FakeRunner([(["run"], (0, "", ""))]))
    ctx = CheckContext(engine="podman", network="chronicle-network")
    assert checks.repair_container_dns(ctx) is True
    argv = runner.calls[0]
    assert "--network" in argv and "chronicle-network" in argv
    assert "--rm" in argv


def test_repair_dns_declines_without_a_network(monkeypatch, have_all_binaries):
    runner = install(monkeypatch, FakeRunner([]))
    assert checks.repair_container_dns(CheckContext(engine="podman")) is False
    assert runner.calls == []


# -------------------------------------------------------------------------- registry


def test_run_all_checks_never_raises(monkeypatch):
    """A broken probe must not be able to stop a service from starting."""

    def explode(ctx):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(checks, "ALL_CHECKS", (explode,))
    results = checks.run_all_checks(CheckContext())
    assert len(results) == 1
    assert results[0].status == NOT_APPLICABLE
    assert "probe blew up" in results[0].detail


def test_run_all_checks_covers_every_registered_check(monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    results = checks.run_all_checks(CheckContext())
    assert len(results) == len(checks.ALL_CHECKS)
    # Nothing configured: everything must be not_applicable, never a false alarm.
    assert {r.status for r in results} == {NOT_APPLICABLE}


def test_worst_status_ranks_failure_above_warning():
    results = [
        CheckResult("a", "a", OK),
        CheckResult("b", "b", WARN),
        CheckResult("c", "c", FAIL),
    ]
    assert checks.worst_status(results) == FAIL
    assert checks.worst_status(results[:2]) == WARN
    assert (
        checks.worst_status([CheckResult("z", "z", NOT_APPLICABLE)]) == NOT_APPLICABLE
    )


def test_check_result_as_dict_is_json_safe():
    result = CheckResult("id", "t", FAIL, "d", "r", repair=lambda: True)
    payload = result.as_dict()
    json.dumps(payload)
    assert payload["repairable"] is True
    assert CheckResult("id", "t", OK).as_dict()["repairable"] is False


def test_timeout_degrades_to_not_applicable(monkeypatch, have_all_binaries):
    """A hung binary must read as unknown, not as a failure."""

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    install(monkeypatch, timeout)
    assert checks.check_tailscale_login(CheckContext()).status == NOT_APPLICABLE


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2027-01-28T05:28:38Z", KEY_EXPIRY_EPOCH),
        ("2027-01-28T05:28:38+00:00", KEY_EXPIRY_EPOCH),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_parse_rfc3339(raw, expected):
    got = checks._parse_rfc3339(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)
