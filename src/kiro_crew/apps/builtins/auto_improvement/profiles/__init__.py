"""Target profiles — the plug-in seam the spine measures through.

A profile is the ONLY target-specific code in the app: it supplies the six adapters
of :class:`..spine.profile.TargetProfile` (ruler, build gate, bug runner, edit
allowlist, isolation recipe, PR recipe) plus the calibration parameters. The
dependency runs one way — a profile imports the spine, the spine never imports a
profile — so adding a target means adding a package here and nothing else.

:func:`build_profile` is the single entry point the run supervisor calls. It lives
here rather than in the profile module so the supervisor never has to know which
package implements the configured target.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PROFILE_IDS", "build_profile"]

#: Selectable profile ids (config key ``profile``). ``github-repo`` is the reference
#: implementation and the default; ``gitlab-repo`` is selected by the persisted
#: ``provider`` (or inferred from the target URL) when the setup target is GitLab.
#: An unknown id falls back to the provider-derived profile rather than raising,
#: because a stale config value should not brick the Start button.
PROFILE_IDS = ("github-repo", "gitlab-repo")


def _infer_provider(config: dict[str, Any]) -> str:
    """Derive the provider for a legacy config that predates the ``provider`` key.

    The authoritative value is written by ``POST /setup-clone`` (Stage 2); this
    is only the fallback for configs written before the GitLab port. Cheap
    string checks only — the ``host`` key is the reliable marker (setup always
    stores it), with ``target_url`` as a secondary hint. Fails closed to
    ``"github"`` (the pre-port behavior).
    """
    host = str((config or {}).get("host") or "").lower().strip()
    if host and host not in {"github.com", "www.github.com"}:
        return "gitlab"
    target = str((config or {}).get("target_url") or "").lower().strip()
    if target.startswith("https://"):
        netloc = target.split("/")[2] or ""
        if netloc and netloc.split(":", 1)[0] not in {"github.com", "www.github.com"}:
            return "gitlab"
    return "github"


def build_profile(config: dict[str, Any]) -> Any:
    """Construct the configured :class:`~..spine.profile.TargetProfile`.

    The profile module is imported lazily inside this function on purpose:
    ``auto_improvement/__init__.py`` is deliberately a plain re-export because it runs
    on every gateway boot, and importing the profile (and through it the whole spine)
    at module scope would undo that. Nothing here is needed until a run starts.

    Dispatch key: the persisted ``provider`` (or the inferred one). The ``profile``
    config key stays writable but is advisory — provider, which is derived from the
    validated setup URL, is authoritative, so a stale ``profile`` value can never
    point the app at the wrong forge (or brick the Start button).

    Raises :class:`ValueError` when no repository is configured — a user-fixable setup
    problem the supervisor turns into a 409, not a crash.
    """
    cfg = config or {}
    provider = str(cfg.get("provider") or _infer_provider(cfg)).lower()
    if provider == "gitlab":
        from .gitlab_repo.profile import build_profile as _build_gitlab_repo

        return _build_gitlab_repo(cfg)
    from .github_repo.profile import build_profile as _build_github_repo

    return _build_github_repo(cfg)
