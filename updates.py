"""
Node code-version reporting + self-update.

A Chronicle node is a git checkout driven by services.py, so version truth is
``git describe`` on the checkout. Updates move the checkout, then rebuild and
restart the enabled services from the new code. Two modes, mirroring how nodes
are installed:

  - branch mode:  HEAD is on a branch with an upstream (dev checkouts,
                  edge/install.sh --branch installs) → ``git pull --rebase
                  --autostash`` on that branch.
  - release mode: HEAD is detached (the root install.sh clones a release tag)
                  or an explicit target tag was given → fetch tags and check
                  out the target (latest ``v*`` tag by default).

On a compose failure after the checkout moved, the checkout is rolled back to
the previous commit (detached, so local branches are never rewritten) and the
services are restarted from the old code.

Native client components (tray, ScreenPipe collector — see clients.py) also run
from this checkout via user units; after either outcome they're restarted
best-effort so they pick up whatever code the checkout ends on. On client-only
nodes there are no compose services at all — the update is just checkout move +
client-unit restarts.

Used by ``./services update`` (CLI) and the node agent's ``/update`` routes
(edge/service_manager.py), which the hub fans out across the cluster.
"""

import os
import re
import subprocess
from pathlib import Path

import clients
import services

REPO_ROOT = Path(__file__).resolve().parent

# Fetches hit the network; builds after an update can take minutes and stream
# through services.py's own machinery, so only git itself needs a timeout here.
_GIT_TIMEOUT = 60

_RELEASE_TAG_RE = re.compile(r"^v(\d+(?:\.\d+)*)$")


class UpdateError(Exception):
    """A git step failed in a way that should abort the update."""


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if check and result.returncode != 0:
        raise UpdateError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result


def _git_out(*args: str) -> str:
    """Stdout of a git command, '' on failure."""
    result = _git(*args)
    return result.stdout.strip() if result.returncode == 0 else ""


def repo_version() -> dict:
    """This checkout's identity: {describe, commit, branch, dirty}.

    ``branch`` is None when HEAD is detached (release-tag installs). ``describe``
    falls back to the short commit on tagless clones (--always).
    """
    branch = _git_out("rev-parse", "--abbrev-ref", "HEAD")
    return {
        "describe": _git_out("describe", "--tags", "--always", "--dirty"),
        "commit": _git_out("rev-parse", "--short", "HEAD"),
        "branch": None if branch in ("", "HEAD") else branch,
        "dirty": bool(_git_out("status", "--porcelain")),
    }


def _latest_release_tag() -> str | None:
    """Highest ``vX[.Y[.Z]]`` tag known locally (fetch tags first)."""
    tags = [t for t in _git_out("tag", "-l", "v*").splitlines() if t]
    versioned = [
        (tuple(int(p) for p in m.group(1).split(".")), t)
        for t in tags
        if (m := _RELEASE_TAG_RE.match(t))
    ]
    return max(versioned)[1] if versioned else None


def _resolve_target(target: str | None) -> dict:
    """What this node should update to: {ref, kind, commit}.

    Explicit ``target`` wins (a tag or any ref). Otherwise branch mode when HEAD
    has an upstream, else the latest release tag. Raises UpdateError when no
    target can be determined (tagless clone with no upstream).
    """
    if target:
        commit = _git_out("rev-parse", "--short", f"{target}^{{commit}}")
        if not commit:
            raise UpdateError(f"Unknown update target {target!r} (not a ref or tag)")
        kind = (
            "tag" if _git_out("rev-parse", "--verify", f"refs/tags/{target}") else "ref"
        )
        return {"ref": target, "kind": kind, "commit": commit}

    upstream = _git_out("rev-parse", "--abbrev-ref", "@{u}")
    if repo_version()["branch"] and upstream:
        return {
            "ref": upstream,
            "kind": "branch",
            "commit": _git_out("rev-parse", "--short", upstream),
        }

    latest = _latest_release_tag()
    if not latest:
        raise UpdateError(
            "No update target: HEAD has no upstream branch and no v* release tags exist"
        )
    return {
        "ref": latest,
        "kind": "tag",
        "commit": _git_out("rev-parse", "--short", f"{latest}^{{commit}}"),
    }


def check_update(target: str | None = None, fetch: bool = True) -> dict:
    """Compare this checkout against its update target (fetches by default).

    Returns {current, target, update_available} and never raises — an ``error``
    key reports fetch/resolution problems instead, so agent /update checks stay
    a clean JSON round-trip even on offline nodes.
    """
    result: dict = {
        "current": repo_version(),
        "target": None,
        "update_available": False,
    }
    try:
        if fetch:
            # --force: take origin's tags as truth — git ≥2.20 otherwise refuses
            # to move a local tag that diverged ("would clobber existing tag"),
            # which stale tags from renamed/forked origins trigger.
            fetched = _git("fetch", "--tags", "--force", "origin")
            if fetched.returncode != 0:
                result["error"] = (
                    f"git fetch failed: {(fetched.stderr or fetched.stdout).strip()}"
                )
                return result
        resolved = _resolve_target(target)
    except (UpdateError, subprocess.TimeoutExpired) as e:
        result["error"] = str(e)
        return result

    result["target"] = resolved
    head = _git_out("rev-parse", "--short", "HEAD")
    if resolved["kind"] == "branch":
        behind = _git_out("rev-list", "--count", f"HEAD..{resolved['ref']}")
        result["update_available"] = bool(behind) and int(behind) > 0
    else:
        result["update_available"] = (
            bool(resolved["commit"]) and resolved["commit"] != head
        )
    return result


def _enabled_services() -> list[str]:
    """The services this node runs — same set ``./services start --all`` uses."""
    return [
        s
        for s in services.SERVICES
        if services.check_service_enabled(s)
        or (s == "langfuse" and services._langfuse_enabled_in_backend())
    ]


def _apply_checkout(resolved: dict, progress) -> None:
    """Move the checkout to the resolved target. Raises UpdateError on failure."""
    if resolved["kind"] == "branch":
        # Rebase + autostash mirrors edge/install.sh: local commits are replayed,
        # uncommitted changes are stashed around the pull.
        progress(f"Pulling {resolved['ref']}…")
        _git("pull", "--rebase", "--autostash", check=True)
    else:
        # A tag/ref checkout can't carry uncommitted changes across safely —
        # refuse instead of guessing (branch mode handles the dirty case).
        if repo_version()["dirty"]:
            raise UpdateError(
                "Checkout has uncommitted changes — commit/stash them, or update "
                "a branch checkout instead"
            )
        progress(f"Checking out {resolved['ref']}…")
        _git("checkout", "--detach", resolved["ref"], check=True)


def _restart_enabled_services(build: bool, progress) -> str | None:
    """``up`` every enabled service; returns the first failing service or None."""
    for name in _enabled_services():
        progress(f"Restarting {name}…")
        if not services.run_compose_command(name, "up", build=build):
            return name
    return None


def _restart_client_units(progress) -> None:
    """Restart installed client components (tray, collector — see clients.py).

    They ``uv run`` straight from this checkout, so restarting is all an update
    needs. Best-effort: a tray that fails to relaunch is reported but never
    fails (or rolls back) the node update — on client-only nodes there may be
    nothing else to restart at all.
    """
    for unit, ok in clients.restart_installed(progress):
        if ok:
            services.console.print(f"[green]✅ Restarted {unit}[/green]")
        else:
            services.console.print(
                f"[yellow]⚠️  {unit} failed to restart — check it manually "
                f"(systemctl --user / launchctl)[/yellow]"
            )


def perform_update(
    target: str | None = None,
    prebuilt: str | None = None,
    restart_services: bool = True,
    progress=None,
) -> bool:
    """Update this node's code and restart its services from the new checkout.

    ``target``   — explicit tag/ref; default resolves per _resolve_target().
    ``prebuilt`` — image tag: pull ``CHRONICLE_REGISTRY`` images at that tag
                   instead of building locally (same env contract as
                   ``./services start --use-prebuilt``).
    ``progress`` — optional callable(str) for step-by-step phase reporting
                   (the node agent surfaces it to the WebUI).

    Rollback: if a service fails to come up on the new code, the checkout is
    restored to the previous commit (detached — local branches are never
    rewritten) and the services are restarted from the old code.
    """
    progress = progress or (lambda msg: services.console.print(f"[cyan]{msg}[/cyan]"))

    progress("Fetching updates…")
    try:
        # --force: take origin's tags as truth (see check_update).
        _git("fetch", "--tags", "--force", "origin", check=True)
        resolved = _resolve_target(target)
        prev_commit = _git_out("rev-parse", "HEAD")

        head_before = _git_out("rev-parse", "--short", "HEAD")
        if resolved["commit"] == head_before:
            progress(f"Already up to date at {repo_version()['describe']}")
            return True

        _apply_checkout(resolved, progress)
    except (UpdateError, subprocess.TimeoutExpired) as e:
        services.console.print(f"[red]❌ Update failed: {e}[/red]")
        return False

    services.console.print(
        f"[green]✅ Code updated to {repo_version()['describe']}[/green]"
    )
    if not restart_services:
        return True

    if prebuilt:
        # Same env contract the compose files consume for prebuilt images.
        os.environ.setdefault("CHRONICLE_REGISTRY", "ghcr.io/simpleopensoftware/")
        os.environ["CHRONICLE_TAG"] = prebuilt

    failed = _restart_enabled_services(build=not prebuilt, progress=progress)
    if failed is None:
        _restart_client_units(progress)
        return True

    # Roll back: old code, old services. Best-effort — report both outcomes.
    services.console.print(
        f"[red]❌ {failed} failed to start on the new code — rolling back to "
        f"{prev_commit[:8]}[/red]"
    )
    progress(f"Rolling back to {prev_commit[:8]}…")
    rollback = _git("checkout", "--detach", prev_commit)
    if rollback.returncode != 0:
        services.console.print(
            f"[red]❌ Rollback checkout failed: {rollback.stderr.strip()} — "
            "manual intervention needed[/red]"
        )
        return False
    refailed = _restart_enabled_services(build=not prebuilt, progress=progress)
    # Client units run from the checkout too — put them back on the old code.
    _restart_client_units(progress)
    if refailed:
        services.console.print(
            f"[red]❌ {refailed} also failed on the previous code — the update "
            "did not cause this; check the service logs[/red]"
        )
    else:
        services.console.print("[yellow]↩️  Rolled back; services restored[/yellow]")
    return False
