"""Jira client tests for Issue Radar.

Covers the Jira-specific behaviour that would be silent or dangerous if broken,
mirroring the GitLab test's priorities for its own client:

  * URL parsing -- browse vs projects paths, and the SSRF guard (an unlisted
    self-managed host must be refused; Cloud is auto-allowed by suffix).
  * Host authorization at the CALL boundary and the credentials are only ever
    attached to an allowlisted host.
  * Normalization -- Jira payloads arrive in the exact GitHub-shaped dicts the
    routes, caches, and React components consume.
  * The PR surface refuses loudly instead of approximating (Jira is not a forge).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import jira_client as jira
from kiro_crew.apps.builtins.issue_radar.backend import (
    jira_oauth,
    routes,
    store,
)
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderSetupError,
)


def _search_payload(rows: int = 2) -> dict:
    return {
        "startAt": 0,
        "maxResults": 100,
        "total": rows,
        "issues": [
            {
                "id": str(100 + i),
                "key": f"PROJ-{i + 1}",
                "self": f"https://acme.atlassian.net/rest/api/2/issue/{100 + i}",
                "fields": {
                    "summary": f"Issue {i + 1}",
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "priority": {"name": "High", "id": "2"},
                    "labels": ["bug"] if i == 0 else [],
                    "comment": {"total": 1} if i == 0 else {"total": 0},
                    "creator": {"displayName": "Ada"},
                    "assignee": {"displayName": "Bob"} if i == 0 else None,
                    "description": "body…",
                    "created": "2024-01-01T10:00:00.000+0000",
                    "updated": "2024-01-02T10:00:00.000+0000",
                },
            }
            for i in range(rows)
        ],
    }


class ParseJiraUrlTests(unittest.TestCase):
    def test_browse_path_yields_project_key(self):
        host, key = jira.parse_jira_project_url(
            "https://acme.atlassian.net/browse/PROJ-123",
            allowed_hosts=frozenset(),
        )
        self.assertEqual((host, key), ("acme.atlassian.net", "PROJ"))

    def test_projects_path_yields_project_key(self):
        host, key = jira.parse_jira_project_url(
            "https://acme.atlassian.net/jira/software/projects/PROJ/boards/3",
            allowed_hosts=frozenset(),
        )
        self.assertEqual(key, "PROJ")

    def test_cloud_suffix_auto_allowed(self):
        # *.atlassian.net needs no allowlist entry.
        jira.parse_jira_project_url(
            "https://acme.atlassian.net/browse/PROJ-123", allowed_hosts=frozenset()
        )

    def test_self_managed_must_be_allowlisted(self):
        from kiro_crew.apps.builtins.issue_radar.backend.errors import RepoUrlError

        with self.assertRaises(RepoUrlError):
            jira.parse_jira_project_url(
                "https://jira.internal/browse/PROJ-123", allowed_hosts=frozenset()
            )

    def test_self_managed_in_allowlist_ok(self):
        host, key = jira.parse_jira_project_url(
            "https://jira.internal/browse/PROJ-123",
            allowed_hosts=frozenset({"jira.internal"}),
        )
        self.assertEqual((host, key), ("jira.internal", "PROJ"))


class ResolveHostTests(unittest.TestCase):
    def test_cloud_auto_allowed(self):
        self.assertEqual(jira._resolve_host("Acme.Atlassian.NET."), "acme.atlassian.net")

    def test_unlisted_self_managed_refused(self):
        with (
            mock.patch.object(jira, "allowed_hosts", return_value=frozenset()),
            self.assertRaises(ProviderCliError),
        ):
            jira._resolve_host("jira.internal")

    def test_listed_self_managed_allowed(self):
        with mock.patch.object(jira, "allowed_hosts", return_value=frozenset({"jira.internal"})):
            self.assertEqual(jira._resolve_host("jira.internal"), "jira.internal")


class AuthTests(unittest.TestCase):
    def test_missing_credentials_raise_setup_error(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(ProviderSetupError):
            jira._credentials()

    def test_credentials_read_from_env(self):
        with mock.patch.dict(
            "os.environ", {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "tok"}, clear=True
        ):
            self.assertEqual(jira._credentials(), ("a@b.c", "tok"))

    def test_current_login_uses_display_name_for_assignee_matching(self):
        with mock.patch.object(
            jira,
            "_jira_request",
            return_value={
                "displayName": "Bob",
                "emailAddress": "bob@example.com",
            },
        ):
            self.assertEqual(jira.get_current_login(host="acme.atlassian.net"), "Bob")


class JiraOAuthRequestTests(unittest.TestCase):
    def test_rest_call_uses_cloud_bearer_endpoint(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = b"{}"
        with (
            mock.patch.object(jira, "authorization_for_host", return_value=("cloud-123", "access")),
            mock.patch.object(jira.urllib.request, "urlopen", return_value=response) as opened,
        ):
            jira._jira_request("acme.atlassian.net", "GET", "/rest/api/2/myself")

        request = opened.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.atlassian.com/ex/jira/cloud-123/rest/api/3/myself",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer access")

    def test_http_error_list_detail_becomes_readable_provider_error(self):
        error = jira.urllib.error.HTTPError(
            "https://acme.atlassian.net/rest/api/2/search",
            410,
            "Gone",
            Message(),
            mock.Mock(
                read=mock.Mock(return_value=b'{"errorMessages":["search endpoint retired"]}')
            ),
        )
        with (
            mock.patch.dict(
                "os.environ", {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "tok"}, clear=False
            ),
            mock.patch.object(jira.urllib.request, "urlopen", side_effect=error),
            self.assertRaises(ProviderCliError) as raised,
        ):
            jira._jira_request("acme.atlassian.net", "GET", "/rest/api/2/search")

        self.assertIn("HTTP 410", str(raised.exception))
        self.assertIn("search endpoint retired", str(raised.exception))

    def test_cloud_search_uses_enhanced_jql_endpoint(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = b'{"issues": [], "isLast": true}'
        with (
            mock.patch.dict(
                "os.environ", {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "tok"}, clear=False
            ),
            mock.patch.object(jira.urllib.request, "urlopen", return_value=response) as opened,
        ):
            self.assertEqual(
                jira._jira_search(
                    "acme.atlassian.net", "project = PROJ", ["summary"], paginate=False
                ),
                [],
            )

        request = opened.call_args.args[0]
        self.assertIn("/rest/api/3/search/jql?", request.full_url)
        self.assertNotIn("startAt=", request.full_url)


class JiraBoardTests(unittest.TestCase):
    def test_open_issues_use_named_board_membership(self):
        issue = _search_payload(1)["issues"][0]
        responses = [
            {"values": [{"id": 42, "name": "Entwicklung"}]},
            {"startAt": 0, "maxResults": 100, "total": 1, "issues": [issue]},
        ]
        with mock.patch.object(jira, "_jira_request", side_effect=responses) as request:
            rows = jira.list_open_issues("PROJ", "", host="acme.atlassian.net")

        self.assertEqual([row["key"] for row in rows], ["PROJ-1"])
        board_call = request.call_args_list[1].args
        self.assertIn("/rest/agile/1.0/board/42/issue?", board_call[2])
        self.assertIn("statusCategory%20%21%3D%20done", board_call[2])

    def test_missing_named_board_keeps_project_fallback(self):
        with (
            mock.patch.object(jira, "_jira_board_id", return_value=None),
            mock.patch.object(jira, "_jira_search", return_value=[]) as search,
        ):
            self.assertEqual(jira.list_open_issues("PROJ", "", host="acme.atlassian.net"), [])

        self.assertIn('project="PROJ"', search.call_args.args[1])


class JiraOAuthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ir-jira-oauth-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _env(self):
        return {
            "JIRA_OAUTH_CLIENT_ID": "client-id",
            "JIRA_OAUTH_CLIENT_SECRET": "client-secret",
            "JIRA_OAUTH_REDIRECT_URI": "http://127.0.0.1:8790/api/apps/issue-radar/jira/oauth/callback",
        }

    def test_start_binds_state_to_cloud_host(self):
        with (
            mock.patch.dict("os.environ", self._env(), clear=True),
            mock.patch.object(jira_oauth, "_audit"),
        ):
            url = jira_oauth.start_authorization("Acme.Atlassian.NET.")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(
            query["scope"],
            [
                "read:jira-work write:jira-work read:jira-user read:board-scope:jira-software "
                "read:issue-details:jira offline_access"
            ],
        )
        self.assertEqual(query["state"][0] in jira_oauth._pending, True)
        self.assertEqual(jira_oauth._pending[query["state"][0]][0], "acme.atlassian.net")

    def test_complete_stores_secret_free_status_and_uses_resource_cloud_id(self):
        token_path = self.root / "jira_oauth_tokens.json"
        with (
            mock.patch.dict("os.environ", self._env(), clear=False),
            mock.patch.object(jira_oauth, "_token_path", return_value=token_path),
            mock.patch.object(jira_oauth, "_audit"),
            mock.patch.object(
                jira_oauth,
                "_http_json",
                side_effect=[
                    {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600},
                    [
                        {
                            "id": "cloud-123",
                            "url": "https://acme.atlassian.net",
                            "scopes": ["read:jira-work"],
                        }
                    ],
                ],
            ),
        ):
            url = jira_oauth.start_authorization("acme.atlassian.net")
            state = parse_qs(urlparse(url).query)["state"][0]
            self.assertEqual(jira_oauth.complete_authorization("code", state), "acme.atlassian.net")
            authorization = jira_oauth.authorization_for_host("acme.atlassian.net")
            self.assertIsNotNone(authorization)
            assert authorization is not None
            self.assertEqual(authorization[0], "cloud-123")
            self.assertEqual(
                jira_oauth.status("acme.atlassian.net"),
                {"host": "acme.atlassian.net", "connected": True},
            )
            stored = json.loads(token_path.read_text())
            self.assertEqual(stored["acme.atlassian.net"]["access_token"], "access")

    def test_oauth_rejects_server_host(self):
        with self.assertRaises(ProviderSetupError) as raised:
            jira_oauth.start_authorization("jira.internal")
        self.assertEqual(raised.exception.reason, "oauth_cloud_only")


class NormalizationTests(unittest.TestCase):
    def test_issue_rows_normalize_to_github_shape(self):
        issues = [jira._norm_issue(i) for i in _search_payload()["issues"]]
        first = issues[0]
        self.assertEqual(first["number"], 100)
        self.assertEqual(first["key"], "PROJ-1")
        self.assertEqual(first["title"], "Issue 1")
        self.assertEqual(first["status"], "To Do")
        self.assertEqual(first["priority"], "High")
        self.assertEqual(first["state"], "open")
        self.assertEqual(first["labels"], ["bug"])
        self.assertEqual(first["comments"], 1)
        self.assertEqual(first["author"], "Ada")
        self.assertEqual(first["assignees"], ["Bob"])
        self.assertTrue(first["url"].endswith("/browse/PROJ-1"))

    def test_done_status_name_is_closed_even_with_incomplete_category(self):
        issue = {"fields": {"status": {"name": "Done", "statusCategory": {"key": "indeterminate"}}}}
        self.assertEqual(jira._status_category(issue), "closed")

    def test_adf_description_becomes_text(self):
        issue = {
            "id": "101",
            "key": "PROJ-101",
            "self": "https://acme.atlassian.net/rest/api/3/issue/101",
            "fields": {
                "description": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "First"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]},
                    ],
                }
            },
        }
        self.assertEqual(jira._norm_issue(issue)["body"], "First\nSecond")


class NotSupportedSurfaceTests(unittest.TestCase):
    """Every PR/forge-only function refuses loudly with the same error class."""

    def test_pr_functions_refuse(self):
        # ``args`` gives each stub enough positional args to reach its raise
        # (the stubs raise before touching any argument values).
        for name, args in (
            ("list_open_pulls", ("PROJ", "repo")),
            ("merge_pull_request", ("PROJ", "repo", 5)),
            ("submit_pr_review", ("PROJ", "repo", 5, "approve")),
            ("list_pr_workflow_runs", ("PROJ", "repo", "sha")),
            ("list_repo_collaborators", ("PROJ", "repo")),
        ):
            fn = getattr(jira, name)
            with self.assertRaises(ProviderCliError, msg=name):
                fn(*args)


class ConnectRouteTests(unittest.IsolatedAsyncioTestCase):
    """The /connect route for a Jira project URL."""

    def _req(self, payload: dict) -> web.Request:
        request = make_mocked_request("POST", "/api/apps/issue-radar/connect")

        async def _json(*_args: object, **_kwargs: object) -> object:
            return payload

        request.json = _json  # type: ignore[method-assign]
        return request

    def _body(self, response) -> dict:
        return json.loads(response.text)

    async def test_jira_url_connects_with_mapped_repo(self):
        summary = {"full_name": "Acme Widgets", "private": True, "permissions": {"push": True}}
        with (
            mock.patch.object(jira, "verify_repo_access", return_value=summary) as verify,
            mock.patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock.Mock(dashboard=mock.Mock(jira_hosts=[])),
            ),
        ):
            response = await routes._handle_connect(
                self._req(
                    {
                        "url": "https://acme.atlassian.net/browse/PROJ-123",
                        "repo": "acme/widgets",
                    }
                )
            )

        self.assertEqual(response.status, 200)
        payload = self._body(response)
        self.assertEqual(payload["provider"], "jira")
        self.assertEqual(payload["owner"], "PROJ")
        # The manual Git-slug mapping round-trips.
        self.assertEqual(payload["repo"], "acme/widgets")
        # The host must reach the client.
        self.assertEqual(verify.call_args.kwargs.get("host"), "acme.atlassian.net")

    async def test_jira_url_without_mapping_uses_project_identity(self):
        summary = {"full_name": "Acme Widgets", "permissions": {}}
        with (
            mock.patch.object(jira, "verify_repo_access", return_value=summary),
            mock.patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock.Mock(dashboard=mock.Mock(jira_hosts=[])),
            ),
        ):
            response = await routes._handle_connect(
                self._req(
                    {
                        "url": "https://acme.atlassian.net/browse/PROJ-123",
                    }
                )
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(self._body(response)["repo"], "PROJ")

    def test_empty_jira_mapping_is_canonicalized_for_requests(self):
        request = make_mocked_request(
            "GET",
            "/api/apps/issue-radar/issues?owner=PROJ&repo=&provider=jira&host=acme.atlassian.net",
        )
        key = routes._key_from_request(request)
        self.assertEqual(key.repo, "PROJ")
        with mock.patch.object(routes.store, "is_repo_connected", side_effect=[False, True]):
            self.assertTrue(routes._connected(key))

    async def test_unlisted_self_managed_host_is_refused_at_connect(self):
        with mock.patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            return_value=mock.Mock(dashboard=mock.Mock(jira_hosts=[])),
        ):
            response = await routes._handle_connect(
                self._req(
                    {
                        "url": "https://jira.internal/browse/PROJ-123",
                    }
                )
            )
        self.assertEqual(response.status, 400)


class StoreIdentityTests(unittest.TestCase):
    """Jira projects are stored + gated like any other provider."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ir-jira-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_jira_gate_does_not_authorize_github(self):
        store.add_connected_repo(
            "PROJ", "acme/widgets", provider="jira", host="acme.atlassian.net", root=self.root
        )
        self.assertTrue(
            store.is_repo_connected(
                "PROJ", "acme/widgets", self.root, provider="jira", host="acme.atlassian.net"
            )
        )
        # Same owner/repo as a GITHUB repo is a different identity.
        self.assertFalse(store.is_repo_connected("PROJ", "acme/widgets", self.root))

    def test_jira_host_is_part_of_identity(self):
        store.add_connected_repo(
            "PROJ", "w", provider="jira", host="acme.atlassian.net", root=self.root
        )
        self.assertFalse(
            store.is_repo_connected(
                "PROJ", "w", self.root, provider="jira", host="other.atlassian.net"
            )
        )

    def test_jira_provider_root_is_isolated(self):
        root = store.provider_root(root=self.root, provider="jira", host="acme.atlassian.net")
        gh_root = store.provider_root(root=self.root)
        self.assertNotEqual(root, gh_root)
        self.assertTrue(str(root).endswith("@providers/jira/acme.atlassian.net"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
