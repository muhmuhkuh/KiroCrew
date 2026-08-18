"""GitLabPRRecipe — the MR-drafting half of the repo-PR seam.

Mirrors the GitHub recipe tests (``test_pr_recipe.py``): the draft-only policy
is pinned mechanically, the happy path asserts the exact ``glab`` argv and the
host-pinned env, and every failure degrades to the durable queue. The shared
fail-closed machinery (push scan, prose redaction, queue degradation) is the
GitHub base's and is covered there; here we pin what the subclass changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import pr_recipe as gh_pr
from kiro_crew.apps.builtins.auto_improvement.profiles.gitlab_repo.pr_recipe import (
    GitLabPRRecipe,
    extract_mr_url,
)


def _recipe(tmp_path: Path, **kw) -> GitLabPRRecipe:
    return GitLabPRRecipe(
        user="zedmor",
        clone_path=tmp_path / "clone",
        pr_queue_dir=tmp_path / "queue",
        base_ref=kw.pop("base_ref", "origin/main"),
        **kw,
    )


class TestProtocolConformance:
    def test_satisfies_the_spine_seam(self, tmp_path: Path) -> None:
        recipe = _recipe(tmp_path)
        assert callable(recipe.draft)
        assert recipe.namespace == "gitlab/zedmor"

    def test_default_namespace_without_user(self, tmp_path: Path) -> None:
        recipe = GitLabPRRecipe(user="", clone_path=tmp_path / "c", pr_queue_dir=tmp_path / "q")
        assert recipe.namespace == "gitlab"

    def test_provider_identity(self) -> None:
        assert GitLabPRRecipe.cli_name == "glab"
        assert GitLabPRRecipe.provider_name == "gitlab"


class TestExtractMrUrl:
    def test_parses_a_public_mr_url_from_trailing_chatter(self) -> None:
        out = "hook noise\nhttps://gitlab.com/zedmor/kiro-crew/-/merge_requests/7\nmore\n"
        assert extract_mr_url(out) == "https://gitlab.com/zedmor/kiro-crew/-/merge_requests/7"

    def test_nested_groups(self) -> None:
        assert (
            extract_mr_url("https://gitlab.com/group/sub/proj/-/merge_requests/12")
            == "https://gitlab.com/group/sub/proj/-/merge_requests/12"
        )

    def test_self_managed_host(self) -> None:
        assert (
            extract_mr_url("https://gitlab.example.test/g/p/-/merge_requests/3")
            == "https://gitlab.example.test/g/p/-/merge_requests/3"
        )

    def test_rejects_non_mr_urls(self) -> None:
        assert extract_mr_url("https://github.com/o/r/pull/7") is None
        assert extract_mr_url("https://gitlab.com/o/r/-/issues/7") is None
        assert extract_mr_url("") is None


class TestDraftArgv:
    def test_glab_mr_create_draft_flags(self, tmp_path: Path) -> None:
        body = tmp_path / "queue" / "cafe.pr.md"
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text("# perf: faster\n\nBody text\n", encoding="utf-8")
        recipe = _recipe(tmp_path)
        argv = recipe._build_draft_argv(
            summary="perf: faster", body_path=body, branch="auto-improvement/perf-cafe"
        )
        assert argv[:3] == ["glab", "mr", "create"]
        assert "--draft" in argv
        assert "--source-branch" in argv and "auto-improvement/perf-cafe" in argv
        assert "--target-branch" in argv and "main" in argv
        assert "--title" in argv and "perf: faster" in argv
        assert "--yes" in argv  # headless: never open the interactive submit prompt
        # The redacted body travels as a single argv element — never a shell.
        desc = argv[argv.index("--description") + 1]
        assert "# perf: faster" in desc and "Body text" in desc

    def test_omits_target_branch_without_a_base(self, tmp_path: Path) -> None:
        body = tmp_path / "q.pr.md"
        body.write_text("x", encoding="utf-8")
        recipe = _recipe(tmp_path, base_ref=None)
        argv = recipe._build_draft_argv(
            summary="fix: x", body_path=body, branch="auto-improvement/bug-cafe"
        )
        assert "--target-branch" not in argv


class TestDraftHappyPath:
    def test_successful_draft_returns_the_mr_url_and_pins_the_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = "gitlab.example.test"
        monkeypatch.setenv("GITLAB_TOKEN", "supersecret")
        monkeypatch.setattr(gh_pr.shutil, "which", lambda _n: "/usr/bin/glab")
        monkeypatch.setattr(
            gh_pr.GitHubPRRecipe, "_push_fix_branch", lambda self, *, branch: (True, branch)
        )
        seen: dict[str, object] = {}

        def fake_run(cmd, **kw):  # noqa: ANN001
            seen["cmd"] = list(cmd)
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(
                cmd, 0, f"https://{host}/group/proj/-/merge_requests/99\n", ""
            )

        monkeypatch.setattr(gh_pr.subprocess, "run", fake_run)
        recipe = _recipe(tmp_path, host=host)
        out = recipe.draft(summary="perf: faster", description="body", diff="d", fingerprint="cafe")
        assert out == f"https://{host}/group/proj/-/merge_requests/99"
        assert "--draft" in seen["cmd"] and "--source-branch" in seen["cmd"]
        assert "--target-branch" in seen["cmd"] and "main" in seen["cmd"]
        # The resolved host is pinned in the child env — never a glab-config default.
        env = seen["env"]
        assert env is not None and env["GITLAB_HOST"] == host
        # GITLAB_TOKEN is host-unbound: withheld for a self-managed instance.
        assert "GITLAB_TOKEN" not in env

    def test_public_gitlab_keeps_the_token(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "supersecret")
        monkeypatch.setattr(gh_pr.shutil, "which", lambda _n: "/usr/bin/glab")
        monkeypatch.setattr(
            gh_pr.GitHubPRRecipe, "_push_fix_branch", lambda self, *, branch: (True, branch)
        )
        seen: dict[str, object] = {}

        def fake_run(cmd, **kw):  # noqa: ANN001
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(
                cmd, 0, "https://gitlab.com/g/p/-/merge_requests/9\n", ""
            )

        monkeypatch.setattr(gh_pr.subprocess, "run", fake_run)
        recipe = _recipe(tmp_path, host="gitlab.com")
        out = recipe.draft(summary="fix: x", description="body", diff="d", fingerprint="beef")
        assert out == "https://gitlab.com/g/p/-/merge_requests/9"
        assert seen["env"]["GITLAB_HOST"] == "gitlab.com"
        assert seen["env"].get("GITLAB_TOKEN") == "supersecret"


class TestCliEnv:
    def test_self_managed_pins_host_and_withholds_token(self, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "tok")
        env = _recipe(Path("/tmp/x"), host="gitlab.example.test:8443")._cli_env()
        assert env["GITLAB_HOST"] == "gitlab.example.test:8443"
        assert "GITLAB_TOKEN" not in env
        assert env["GLAB_PAGER"] == "cat" and env["NO_COLOR"] == "1"

    def test_gitlab_com_keeps_token(self, monkeypatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "tok")
        env = _recipe(Path("/tmp/x"), host="gitlab.com")._cli_env()
        assert env["GITLAB_HOST"] == "gitlab.com"
        assert env["GITLAB_TOKEN"] == "tok"

    def test_missing_host_defaults_to_gitlab_com(self) -> None:
        env = _recipe(Path("/tmp/x"))._cli_env()
        assert env["GITLAB_HOST"] == "gitlab.com"


class TestTransportAndDegradation:
    def test_fetch_url_is_never_rewritten(self, tmp_path: Path) -> None:
        recipe = _recipe(tmp_path, host="gitlab.com")
        assert recipe._prefer_authenticated_fetch_url("https://gitlab.com/g/p.git") == (
            "https://gitlab.com/g/p.git"
        )

    def test_no_glab_on_path_degrades_to_queue(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(gh_pr.shutil, "which", lambda _n: None)
        recipe = _recipe(tmp_path)
        assert (
            recipe.draft(summary="fix: x", description="b", diff="d", fingerprint="ff01")
            == "QUEUED:ff01"
        )
        assert (tmp_path / "queue" / "ff01.diff").read_text() == "d"

    def test_failed_push_degrades_to_queue(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(gh_pr.shutil, "which", lambda _n: "/usr/bin/glab")

        def fake_git(self, *args, timeout=30.0):  # noqa: ANN001
            return subprocess.CompletedProcess(args, 1, "", "remote rejected")

        monkeypatch.setattr(gh_pr.GitHubPRRecipe, "_git", fake_git)
        recipe = _recipe(tmp_path, fetch_url="https://gitlab.com/g/p.git")
        assert (
            recipe.draft(summary="fix: x", description="b", diff="d", fingerprint="ff02")
            == "QUEUED:ff02"
        )

    def test_draft_argv_never_publishes_or_merges(self, tmp_path: Path) -> None:
        body = tmp_path / "q.pr.md"
        body.write_text("x", encoding="utf-8")
        recipe = _recipe(tmp_path)
        argv = recipe._build_draft_argv(
            summary="fix: x", body_path=body, branch="auto-improvement/bug-cafe"
        )
        joined = " ".join(argv)
        assert "--draft" in argv
        for forbidden in ("--web", "merge", "ready", "--auto", "--push"):
            assert forbidden not in joined


class TestProfileDispatch:
    """``profiles.build_profile`` routes on the persisted provider (Stage 3-5)."""

    def test_gitlab_provider_selects_the_gitlab_recipe(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement import profiles
        from kiro_crew.apps.builtins.auto_improvement.profiles.gitlab_repo.profile import (
            GitLabRepoProfile,
        )

        profile = profiles.build_profile(
            {"clone": "/nowhere", "provider": "gitlab", "host": "gitlab.example.test"}
        )
        assert isinstance(profile, GitLabRepoProfile)
        assert profile.id == "gitlab-repo"
        assert isinstance(profile.pr_recipe, GitLabPRRecipe)

    def test_github_default_selects_the_github_recipe(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement import profiles
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
            GitHubRepoProfile,
        )

        profile = profiles.build_profile({"clone": "/nowhere"})
        assert isinstance(profile, GitHubRepoProfile)
        assert profile.id == "github-repo"
        assert not isinstance(profile.pr_recipe, GitLabPRRecipe)
