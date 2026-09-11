"""The GitLab target profile: the shared repo profile with the MR recipe swapped in.

Everything the :class:`..github_repo.profile.GitHubRepoProfile` assembles — the
suite ruler, the build gate, the bug runner, the edit allowlist, the push-disabled
isolation, the calibration params — is host-agnostic. The only provider-specific
field is ⑤, the PR/MR recipe. So the GitLab profile is a thin subclass that pins
:class:`GitLabPRRecipe` and reuses the GitHub assembler verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ...backend import store
from ...spine.contracts import TRACK_BUG

from ..github_repo.profile import GitHubRepoProfile
from ..github_repo.profile import _resolve_origin_url
from .pr_recipe import GitLabPRRecipe

__all__ = ["GitLabRepoProfile", "build_profile"]


class GitLabRepoProfile(GitHubRepoProfile):
    """The reference Target Profile for any Python GitLab repo with a pytest suite."""

    id = "gitlab-repo"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pr_recipe = GitLabPRRecipe(
            user=kwargs.get("user", ""),
            clone_path=self.clone_path,
            pr_queue_dir=kwargs["pr_queue_dir"],
            base_ref=kwargs.get("base_ref", "origin/main"),
            fetch_url=kwargs.get("origin_url") or None,
        )


def build_profile(config: dict) -> GitLabRepoProfile:
    """Assemble a :class:`GitLabRepoProfile` from the app's on-disk config."""
    cfg = config or {}
    clone = str(cfg.get("clone") or "").strip()
    if not clone:
        raise ValueError("no repository configured — run setup-clone first")
    branch = str(cfg.get("branch") or "").strip()
    base_ref = "origin/main" if not branch else branch if "/" in branch else f"origin/{branch}"
    return cast(
        GitLabRepoProfile,
        GitLabRepoProfile(
            clone_path=Path(clone),
            pr_queue_dir=store.pr_queue_dir(),
            user=str(cfg.get("prUser") or cfg.get("user") or ""),
            base_ref=base_ref,
            track=str(cfg.get("track") or TRACK_BUG),
            benchmark_cmd=str(cfg.get("benchmarkCommand") or ""),
            scope_base=str(cfg.get("scopeDiffBase") or ""),
            origin_url=_resolve_origin_url(cfg),
            allowed_globs=(
                [str(g) for g in cfg["editAllowlist"]]
                if isinstance(cfg.get("editAllowlist"), list) and cfg["editAllowlist"]
                else None
            ),
            baseline_reps=int(cfg.get("calibrationReps") or 5),
            noise_floor_s=float(cfg.get("noiseFloorSeconds") or 0.25),
            log_dir=store.logs_dir(),
        ),
    )
