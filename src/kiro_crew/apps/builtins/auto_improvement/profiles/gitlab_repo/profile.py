"""The GitLab target profile: the shared repo profile with the MR recipe swapped in.

Everything the :class:`..github_repo.profile.GitHubRepoProfile` assembles — the
suite ruler, the build gate, the bug runner, the edit allowlist, the push-disabled
isolation, the calibration params — is host-agnostic. The only provider-specific
field is ⑤, the PR/MR recipe. So the GitLab profile is a thin subclass that pins
:class:`GitLabPRRecipe` and reuses the GitHub assembler verbatim.
"""

from __future__ import annotations

from typing import cast

from ..github_repo.profile import GitHubRepoProfile
from ..github_repo.profile import build_profile as _build_github_profile
from .pr_recipe import GitLabPRRecipe

__all__ = ["GitLabRepoProfile", "build_profile"]


class GitLabRepoProfile(GitHubRepoProfile):
    """The reference Target Profile for any Python GitLab repo with a pytest suite."""

    id = "gitlab-repo"

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("recipe_cls", GitLabPRRecipe)
        super().__init__(**kwargs)


def build_profile(config: dict) -> GitLabRepoProfile:
    """Assemble a :class:`GitLabRepoProfile` from the app's on-disk config."""
    profile = _build_github_profile(config or {}, profile_cls=GitLabRepoProfile)
    # The assembler is annotated with its default class; at runtime it returns an
    # instance of ``profile_cls``, hence the cast.
    return cast(GitLabRepoProfile, profile)
