"""Preflight: are the external tools a run depends on actually present?

A run shells out to a handful of binaries. Discovering a missing one halfway
through a cycle wastes the whole cycle, so the UI asks this up front and can show
what to install.

Reports rather than repairs. The upstream version could install its internal
toolchain packages itself; here the hard dependencies (``git`` plus ``gh`` or
``glab`` for the selected forge) are things a user installs and authenticates
deliberately — silently installing an authenticated CLI on someone's behalf is
not this app's business. Only the optional linter, which is a plain pip package
in the app's own environment, is offered as an install.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Any

from kiro_crew.security import redact_and_truncate

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 15.0


def _which(binary: str) -> str:
    return shutil.which(binary) or ""


def _cli_authenticated(
    binary: str, login_command: str, *, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Whether a provider CLI is present and has a live login."""
    if not _which(binary):
        return False, f"{binary} is not on PATH"
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": _PROBE_TIMEOUT_S,
    }
    if env is not None:
        kwargs["env"] = env
    try:
        proc = subprocess.run([binary, "auth", "status"], **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {binary} auth status: {exc}"
    if proc.returncode != 0:
        return False, f"{binary} is present but not logged in — run `{login_command}`"
    return True, "authenticated"


def _gh_authenticated() -> tuple[bool, str]:
    """Whether ``gh`` has a live login."""
    return _cli_authenticated("gh", "gh auth login")


def _glab_authenticated(host: str = "") -> tuple[bool, str]:
    """Whether ``glab`` has a live login for the configured GitLab host."""
    env: dict[str, str] | None = None
    if host:
        env = dict(os.environ)
        env["GITLAB_HOST"] = host
    return _cli_authenticated("glab", "glab auth login", env=env)


def check_deps(provider: str = "github", host: str = "") -> dict[str, Any]:
    """Report every dependency, whether it is satisfied, and how to fix it.

    ``required`` entries block a run; an unsatisfied optional entry only narrows
    what discovery can find.
    """
    git_path = _which("git")
    selected_provider = str(provider or "github").lower()
    cli_id = "glab" if selected_provider == "gitlab" else "gh"
    cli_name = "GitLab CLI (glab)" if cli_id == "glab" else "GitHub CLI (gh)"
    cli_login = "glab auth login" if cli_id == "glab" else "gh auth login"
    cli_ok, cli_detail = _glab_authenticated(host) if cli_id == "glab" else _gh_authenticated()
    ruff_path = _which("ruff")

    deps: list[dict[str, Any]] = [
        {
            "id": "git",
            "name": "git",
            "required": True,
            "ok": bool(git_path),
            "detail": git_path or "not found on PATH",
            "fix": "install git",
            "installable": False,
        },
        {
            "id": cli_id,
            "name": cli_name,
            "required": True,
            "ok": cli_ok,
            "detail": cli_detail,
            "fix": f"install the {cli_name}, then run `{cli_login}`",
            "installable": False,
        },
        {
            "id": "ruff",
            "name": "ruff (grounds bug discovery)",
            "required": False,
            "ok": bool(ruff_path),
            "detail": ruff_path or "not found — discovery falls back to a compile check",
            "fix": "install ruff into the app environment",
            "installable": True,
        },
    ]
    blocking = [d["id"] for d in deps if d["required"] and not d["ok"]]
    return {"deps": deps, "ok": not blocking, "blocking": blocking}


def install_deps() -> dict[str, Any]:
    """Install the optional dependencies that can be installed safely.

    Only ``ruff``, and only into the interpreter already running this app — never
    a system-wide install, and never an authenticated CLI.
    """
    if _which("ruff"):
        return {"ok": True, "installed": [], "detail": "ruff already present"}
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "ruff"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "installed": [], "error": f"install failed: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        # pip inherits the gateway environment, so an authenticated private
        # index echoes its request URL — token and all — to stderr on an auth
        # failure. The dashboard route that serves this payload redacts what
        # it sends, but its regexes need the full credential shape to match:
        # bounding first can cut a token mid-match, leaving a fragment no
        # downstream pass can recognise. redact_and_truncate scrubs the whole
        # line BEFORE its bound, so a recognised credential straddling the
        # 200-char boundary cannot leak as an unredacted partial.
        safe_tail = redact_and_truncate(tail[0], 200)
        return {"ok": False, "installed": [], "error": f"pip failed: {safe_tail}"}
    return {"ok": True, "installed": ["ruff"], "detail": "ruff installed"}
