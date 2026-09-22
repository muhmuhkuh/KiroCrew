"""GitLab support for the clone/setup layer — the front door of a run.

The app's front door (`backend/clone_setup.py`) accepted only `github.com`
before the GitLab port. These tests pin the GitLab behavior added there:
provider/host derivation from the URL, nested-group namespaces, the
self-managed host allowlist (gitlab.com ∪ dashboard.gitlab_hosts), the
generalized remote slug (identity pinning), and the egress host allowlist.

The security-critical GitLab URL parsing itself is reused from
``issue_radar.backend.gitlab_client`` and is covered by that app's own tests;
here we pin how auto-improvement DISPATCHES on it and how the derived values
flow into CloneSpec / remote validation.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client


def _valid(url: str) -> clone_setup.CloneSpec:
    spec, err = clone_setup.validate_target_url(url)
    assert spec is not None, f"expected {url!r} to validate, got: {err}"
    return spec


class TestValidateTargetUrlGitLab:
    def test_public_gitlab_project_url(self) -> None:
        spec = _valid("https://gitlab.com/zedmor/kiro-crew")
        assert spec.provider == "gitlab"
        assert spec.host == "gitlab.com"
        assert spec.display == "zedmor/kiro-crew"
        assert spec.clone_url == "https://gitlab.com/zedmor/kiro-crew.git"

    def test_nested_group_namespace(self) -> None:
        spec = _valid("https://gitlab.com/group/sub/project")
        assert spec.provider == "gitlab"
        assert spec.display == "group/sub/project"
        assert spec.clone_url == "https://gitlab.com/group/sub/project.git"
        # A `/` in the namespace must not leak into the scratch dir name.
        assert spec.dir_name == "group--sub--project"

    def test_mr_page_url_is_stripped_to_the_project(self) -> None:
        spec = _valid("https://gitlab.com/group/project/-/merge_requests/7")
        assert spec.display == "group/project"
        assert spec.clone_url == "https://gitlab.com/group/project.git"

    def test_github_urls_still_take_the_github_path(self) -> None:
        spec = _valid("https://github.com/zedmor/kiro-crew")
        assert spec.provider == "github"
        assert spec.host == "github.com"
        assert spec.clone_url == "https://github.com/zedmor/kiro-crew.git"

    def test_www_gitlab_host_resolves_to_gitlab_com(self) -> None:
        spec = _valid("https://www.gitlab.com/group/project")
        assert spec.provider == "gitlab"
        assert spec.host == "gitlab.com"

    def test_scheme_and_host_refusals_are_provider_aware(self) -> None:
        assert clone_setup.validate_target_url("http://gitlab.com/group/project")[1].startswith(
            "Only https://"
        )
        assert "GitLab" in clone_setup.validate_target_url("https://evilgithub.com/o/r")[1]
        # Subdomain/suffix confusion against gitlab.com must be refused, exactly
        # like the existing github.com.attacker.net refusal.
        assert clone_setup.validate_target_url("https://gitlab.com.evil.net/o/r")[1]
        assert clone_setup.validate_target_url("https://evilgitlab.com/o/r")[1]

    def test_self_managed_host_requires_the_allowlist(self, monkeypatch) -> None:
        host = "gitlab.example.test"
        # DNS-rebinding defense-in-depth is unit-tested elsewhere; stub it so the
        # allowlist behavior is what this test exercises.
        monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda _h: False)

        # Not listed -> refused (fail closed).
        monkeypatch.setattr(gitlab_client, "allowed_hosts", lambda: frozenset())
        spec, err = clone_setup.validate_target_url(f"https://{host}/group/project")
        assert spec is None
        assert "GitLab" in err

        # Listed -> accepted, host preserved for the clone URL.
        monkeypatch.setattr(gitlab_client, "allowed_hosts", lambda: frozenset({host}))
        spec = _valid(f"https://{host}/group/sub/project")
        assert spec.provider == "gitlab"
        assert spec.host == host
        assert spec.clone_url == f"https://{host}/group/sub/project.git"

    def test_self_managed_host_with_port_is_allowlisted_as_host_port(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda _h: False)
        monkeypatch.setattr(
            gitlab_client, "allowed_hosts", lambda: frozenset({"gitlab.example.test:8443"})
        )
        spec = _valid("https://gitlab.example.test:8443/group/project")
        assert spec.host == "gitlab.example.test:8443"
        assert spec.clone_url == "https://gitlab.example.test:8443/group/project.git"


class TestRemoteSlugGeneralized:
    """Identity pinning must handle GitLab nested namespaces (and keep GitHub)."""

    def test_github_https_and_ssh_unchanged(self) -> None:
        assert clone_setup._remote_slug("https://github.com/Owner/Repo.git") == "owner/repo"
        assert clone_setup._remote_slug("git@github.com:Owner/Repo.git") == "owner/repo"

    def test_gitlab_nested_namespace_https(self) -> None:
        assert (
            clone_setup._remote_slug("https://gitlab.com/Group/Sub/Project.git")
            == "group/sub/project"
        )

    def test_gitlab_nested_namespace_ssh(self) -> None:
        assert (
            clone_setup._remote_slug("git@gitlab.com:Group/Sub/Project.git")
            == "group/sub/project"
        )

    def test_local_paths_stay_identityless(self) -> None:
        assert clone_setup._remote_slug("/tmp/upstream.git") == ""
        assert clone_setup._remote_slug("file:///tmp/upstream.git") == ""


class TestRemoteHostAllowlistGitLab:
    def test_gitlab_com_is_a_push_target(self) -> None:
        assert clone_setup._is_allowed_remote("https://gitlab.com/group/project.git")
        assert clone_setup._is_allowed_remote("git@gitlab.com:group/project.git")

    def test_evil_gitlab_hosts_are_refused(self) -> None:
        assert not clone_setup._is_allowed_remote("https://evilgitlab.com/o/r.git")
        assert not clone_setup._is_allowed_remote("https://gitlab.com.evil.net/o/r.git")

    def test_self_managed_host_follows_the_allowlist(self, monkeypatch) -> None:
        host = "gitlab.example.test"
        monkeypatch.setattr(gitlab_client, "allowed_hosts", lambda: frozenset({host}))
        assert clone_setup._is_allowed_remote(f"https://{host}/group/project.git")
        assert clone_setup._is_allowed_remote(f"git@{host}:group/project.git")

        monkeypatch.setattr(gitlab_client, "allowed_hosts", lambda: frozenset())
        assert not clone_setup._is_allowed_remote(f"https://{host}/group/project.git")


class TestResolveOriginUrlGitLab:
    def test_gitlab_origin_url_matching_target_url_is_resolved(self, monkeypatch) -> None:
        monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda _h: False)
        cfg = {
            "origin_url": "https://gitlab.com/group/sub/project.git",
            "target_url": "https://gitlab.com/group/sub/project",
        }
        assert clone_setup.resolve_origin_url(cfg) == "https://gitlab.com/group/sub/project.git"

    def test_gitlab_origin_url_pinned_to_a_different_repo_is_refused(self) -> None:
        cfg = {
            "origin_url": "https://gitlab.com/group/sub/other.git",
            "target_url": "https://gitlab.com/group/sub/project",
        }
        # Identity pinning compares slugs; a different project must fail closed.
        assert clone_setup.resolve_origin_url(cfg) == ""

    def test_self_managed_origin_requires_target_on_same_allowlisted_host(
        self, monkeypatch
    ) -> None:
        host = "gitlab.example.test"
        monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda _h: False)
        monkeypatch.setattr(gitlab_client, "allowed_hosts", lambda: frozenset({host}))

        cfg = {
            "origin_url": f"https://{host}/group/project.git",
            "target_url": f"https://{host}/group/project",
        }
        assert clone_setup.resolve_origin_url(cfg) == f"https://{host}/group/project.git"

        # Origin on an instance that was REMOVED from the allowlist -> no push target.
        monkeypatch.setattr(gitlab_client, "allowed_hosts", lambda: frozenset())
        assert clone_setup.resolve_origin_url(cfg) == ""

    def test_setup_result_carries_provider_and_host(self, monkeypatch) -> None:
        monkeypatch.setattr(clone_setup, "_host_is_blocked", lambda _h: False)
        spec = _valid("https://gitlab.com/group/sub/project")
        result = clone_setup._ok(spec, Path("/tmp/nowhere"), reused=True)
        assert result["provider"] == "gitlab"
        assert result["host"] == "gitlab.com"
        assert result["display"] == "group/sub/project"
