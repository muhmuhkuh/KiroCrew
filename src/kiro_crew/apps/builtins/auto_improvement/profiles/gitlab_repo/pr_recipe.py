"""Field ⑤ — draft a GitLab merge request, never publish-ready, never merged.

GitLab mirrors GitHub's model: an MR is a comparison between two refs that both
exist on the remote, so the fix branch must be pushed before ``glab mr create``
can reference it. The push is narrowed exactly like the GitHub recipe — a
generated, app-namespaced branch pushed to the clone's fetch URL for that one
ref, run through the spine's non-overridable protected-branch denylist, with
fail-closed credential scanning of the pushed content — and every other safety
control (prose redaction, draft-only, queue degradation to ``QUEUED:<fp>``)
is shared with :class:`..github_repo.pr_recipe.GitHubPRRecipe`, which is this
class's base.

Only the GitLab-specific half of the seam lives here:

* the ``glab mr create --draft`` argv (flags verified against
  ``glab mr create --help`` on glab 1.113.0 — there is no ``--description-file``,
  so the body goes over ``--description`` as a single argv element, which is
  safe because the subprocess is spawned with a list argv, never a shell);
* MR URL extraction (``https://<host>/<namespace>/<project>/-/merge_requests/<iid>``);
* host routing, following the codebase's established glab pattern (the same
  rules ``source_providers`` and ``issue_radar.gitlab_client`` use): the
  resolved host is pinned in the child env as **``GITLAB_HOST``** — never
  defaulted to gitlab.com — and ``GITLAB_TOKEN`` is **withheld** for
  self-managed instances, because that token is host-unbound and forwarding a
  gitlab.com PAT to a self-managed server would leak it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..github_repo.pr_recipe import GitHubPRRecipe

__all__ = ["GitLabPRRecipe", "extract_mr_url"]

#: Shape of a real GitLab MR reference in ``glab mr create`` stdout. The host
#: may be gitlab.com or an allowlisted self-managed instance, and the project
#: path may contain nested groups — only the ``/-/merge_requests/<iid>`` tail is
#: structural. Scan every line for the FIRST real MR URL (the clone can emit
#: trailing git-hook chatter after it, exactly like ``gh``).
_MR_URL_RE = re.compile(r"https://[^\s/]+/[^\s]+/-/merge_requests/\d+")


def extract_mr_url(stdout: str) -> str | None:
    """Return the first real GitLab MR URL in ``stdout``, else None."""
    match = _MR_URL_RE.search(stdout or "")
    return match.group(0) if match else None


class GitLabPRRecipe(GitHubPRRecipe):
    """Draft a GitLab MR from a push-disabled clone. Never publishes, never merges."""

    cli_name = "glab"
    provider_name = "gitlab"

    def _prefer_authenticated_fetch_url(self, url: str) -> str:
        """GitLab pushes stay on the validated HTTPS fetch URL as-is.

        GitHub rewrites to SSH when ``gh`` prefers it; GitLab has no analogous
        ``git_protocol`` setting, and glab's auth flows through its own
        ``GITLAB_CONFIG_DIR``/git credential helper over HTTPS. The owner/repo
        identity came from the validated clone spec, so not rewriting cannot
        retarget the push.
        """
        return url

    def _cli_env(self) -> dict[str, str]:
        """Pin ``GITLAB_HOST`` and withhold ``GITLAB_TOKEN`` for self-managed.

        Mirrors ``source_providers._run_json``/``gitlab_client._glab_env``: the
        resolved host is set in the child env so a self-managed default in
        glab's own config cannot redirect the CLI to a different instance, and
        the host-unbound token is dropped whenever the target is not gitlab.com.
        """
        env = dict(os.environ)
        host = self.host or "gitlab.com"
        env["GITLAB_HOST"] = host
        if host != "gitlab.com":
            env.pop("GITLAB_TOKEN", None)
        env["GLAB_PAGER"] = "cat"
        env["NO_COLOR"] = "1"
        return env

    def _build_draft_argv(self, *, summary: str, body_path: Path, branch: str) -> list[str]:
        """The ``glab mr create --draft`` argv. ``--yes`` keeps it headless."""
        cmd = [
            "glab",
            "mr",
            "create",
            "--draft",
            "--source-branch",
            branch,
            "--title",
            summary,
            "--description",
            body_path.read_text(encoding="utf-8"),
            "--yes",
        ]
        if self.base_branch:
            cmd += ["--target-branch", self.base_branch]
        return cmd

    def _extract_url(self, stdout: str) -> str | None:
        """Pull the first real GitLab MR URL out of the CLI's stdout."""
        return extract_mr_url(stdout)
