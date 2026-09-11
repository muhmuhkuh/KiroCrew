"""Jira data access for Issue Radar, via the Jira REST API with the user's
own ambient credentials.

The counterpart to :mod:`github_client` / :mod:`gitlab_client`, and shaped as a
function-for-function mirror of them: same public names, same argument order,
same return shapes. Issue Radar's routes, caches, and React components were all
written against GitHub's field names, so this module normalizes Jira payloads
INTO those names rather than introducing a third vocabulary:

    Jira                          ->  normalized (GitHub-shaped)
    issue id                       number
    issue key (PROJ-123)           key           (extra, for display)
    browse URL                     url
    statusCategory "Done"          state "closed"
    reporter.displayName           author
    fields.labels: ["a"]           labels: ["a"]
    assignee.displayName           assignees: ["a"]
    comments.total                 comments
    changelog                      timeline events

Auth and credential handling deliberately follow a DIFFERENT model from the
other two clients. GitHub and GitLab shell out to ``gh`` / ``glab``, which own
their own token storage; Jira has no standard CLI, so this module talks to the
Jira REST API directly. Atlassian Cloud prefers the native Issue Radar OAuth
flow; ambient credentials remain the compatibility path:

``JIRA_OAUTH_CLIENT_ID`` / ``JIRA_OAUTH_CLIENT_SECRET`` /
``JIRA_OAUTH_REDIRECT_URI``
    Server-side Atlassian 3LO application settings. The user grant and rotating
    refresh token live in KiroCrew's protected OAuth store.
``JIRA_EMAIL`` / ``JIRA_API_TOKEN``
    Optional Basic-auth compatibility credentials for Cloud and the PAT path
    for Data Center. They are never echoed or stored by KiroCrew.

Host authorization mirrors ``source_providers._jira_ref`` and the GitLab
discipline exactly, and is RE-CHECKED at every call (the spawn boundary):

1. A host is reachable only if it ends in ``.atlassian.net`` (Atlassian Cloud,
   which identifies the product the way ``github.com`` does) or appears
   verbatim in the operator's ``dashboard.jira_hosts`` allowlist. Browser input
   can therefore never choose which instance the credential-bearing client
   talks to (SSRF).
2. The host is REQUIRED on every call, never defaulted. A call site that
   omitted it would otherwise silently target an arbitrary instance.
3. OAuth tokens and compatibility credentials are only ever attached after
   the host passed the allowlist check above. Unlike the CLI clients there is no
   child process whose environment could leak them -- the check IS the boundary.

The PR surface (pulls, merge, review, workflow runs) has no Jira equivalent --
Jira is an issue tracker, not a forge -- so every such function raises
``ProviderCliError`` with a clear message rather than approximating. The
frontend hides those tabs for Jira connections (via ``provider.terms``), and a
stray direct call fails loudly, never silently.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlparse

# pi-lens-ignore: reportMissingImports
from kiro_crew.apps.builtins.issue_radar.backend.jira_oauth import (
    authorization_for_host,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.sel import sel

from .errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
    sanitize_cli_stderr,
)

# Historical aliases, mirroring github_client so provider-agnostic callers can
# use either module interchangeably (see errors.py for why these are aliases).
GhCliError = ProviderCliError
GhSetupError = ProviderSetupError
GhPermissionError = ProviderPermissionError

JIRA_TIMEOUT_SEC = 20.0
JIRA_PAGINATE_TIMEOUT_SEC = 60.0

# Mirrors github_client's constants so the provider-agnostic routes can read
# either module's limits without branching.
CONTRIB_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 3650
PR_SEARCH_MAX = 300

# Atlassian Cloud is recognized by suffix, the same way source_providers does.
_CLOUD_SUFFIX = ".atlassian.net"
# Jira caps projects at 10 chars; the natural key is uppercase alphanumeric.
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")

# Jira REST API root for self-managed Jira. Cloud issue search moved to the
# enhanced token-pagination endpoint under API v3; the remaining project/issue
# calls keep the v2-compatible path so one client still serves both products.
_API_ROOT = "/rest/api/2"
_CLOUD_API_ROOT = "/rest/api/3"

# Max issues per JQL search page. Jira caps maxResults at 100.
_PAGE_SIZE = 100
# Hard cap on pages walked by a paginated read, so a pathological project
# cannot exhaust the request budget (mirrors gitlab_client).
_MAX_PAGES = 50
_JIRA_BOARD_NAME = "Entwicklung"
_AGILE_BOARD_ROOT = "/rest/agile/1.0"


# ── host authorization (spawn boundary) ──────────────────────────────────────


def allowed_hosts() -> frozenset[str]:
    """The operator's ``dashboard.jira_hosts`` allowlist.

    Read synchronously (this module is sync throughout, and routes call it via
    ``asyncio.to_thread``). Failure to read config yields an EMPTY set, so a
    broken config denies every self-managed host rather than widening access.
    """
    try:
        return frozenset(KiroCrewConfig.load().dashboard.jira_hosts)
    except Exception:  # noqa: BLE001 — fail closed, never widen
        return frozenset()


def _resolve_host(host: str) -> str:
    """Re-check ``host`` against the allowlist at the call boundary.

    ``parse_jira_project_url`` already validated the host when the project was
    connected, but this is re-checked here so a caller cannot reach an
    unauthorized instance even if a future code path forgets to validate, and so
    an operator REMOVING a host from the allowlist takes effect immediately on
    an already-connected project. An omitted host is refused rather than
    silently resolved.
    """
    if not host:
        raise ProviderCliError("a Jira host is required for API calls")
    normalized = host.lower().rstrip(".")
    if normalized.endswith(_CLOUD_SUFFIX) and len(normalized) > len(_CLOUD_SUFFIX):
        return normalized
    if normalized in allowed_hosts():
        return normalized
    raise ProviderCliError(
        f"Jira host {normalized!r} is not *.atlassian.net and is not listed in "
        "the dashboard.jira_hosts allowlist"
    )


def _credentials() -> tuple[str, str]:
    """The ambient Basic Auth credentials (email/username, API token).

    Missing credentials raise ``ProviderSetupError`` with ``reason="not_authenticated"``
    so the connect dialog can show login instructions instead of a raw error.
    """
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not token:
        raise ProviderSetupError(
            "Jira credentials are not configured: set JIRA_EMAIL and "
            "JIRA_API_TOKEN in the gateway environment (an Atlassian API token "
            "from https://id.atlassian.com/manage-profile/security/api-tokens)",
            reason="not_authenticated",
        )
    return email, token


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for every Jira API call (reads and writes). Fire-and-forget."""
    sel().log_api_access(
        caller="core:issue-radar",
        operation=f"issue_radar.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
    )


# ── single HTTP chokepoint ───────────────────────────────────────────────────


def _jira_request(
    host: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = JIRA_TIMEOUT_SEC,
) -> dict[str, Any]:
    """One Jira REST call with the allowlist-checked host and OAuth or PAT auth.

    ``path`` is an API path (e.g. ``/rest/api/2/search?...``) built purely from
    validated values; nothing user-controlled reaches it un-quoted. Returns the
    parsed JSON object. Raises :class:`ProviderCliError` on transport failure,
    :class:`ProviderPermissionError` on 403, and the same classes for 401/404
    (mirroring the CLI clients' mapping).
    """
    resolved = _resolve_host(host)
    oauth = authorization_for_host(resolved)
    if oauth is not None:
        cloud_id, access_token = oauth
        api_path = path.replace("/rest/api/2", "/rest/api/3", 1)
        url = f"https://api.atlassian.com/ex/jira/{quote(cloud_id, safe='')}{api_path}"
        auth_label = "Jira OAuth"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json" if payload is not None else "text/plain",
        }
    else:
        email, token = _credentials()
        url = f"https://{resolved}{path}"
        basic = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        auth_label = "JIRA_API_TOKEN"
        headers = {
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/json" if payload is not None else "text/plain",
        }
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    # pi-lens-ignore: no-boolean-in-except
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace"))
            detail_value = parsed.get("errorMessages") or parsed.get("message") or parsed
            if isinstance(detail_value, list):
                detail = "; ".join(str(item) for item in detail_value)
            else:
                detail = str(detail_value)
            detail = detail[:200]
        # pi-lens-ignore: no-boolean-in-except
        except Exception:  # noqa: BLE001
            detail = str(exc.reason or "")
        tail = sanitize_cli_stderr(detail or f"HTTP {exc.code}")
        _audit("jira_api", url, "failure", error=f"HTTP {exc.code}")
        if exc.code in (401, 403):
            raise ProviderPermissionError(
                f"Jira refused the call ({method} {path}) — your {auth_label} "
                f"lacks access: {tail}"
            ) from exc
        raise ProviderCliError(
            f"Jira API call failed ({method} {path}, HTTP {exc.code}): {tail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _audit("jira_api", url, "failure", error=str(exc)[:200])
        raise ProviderCliError(
            f"could not reach Jira host {resolved!r} ({method} {path}): {exc}"
        ) from exc
    try:
        return json.loads(body) if body.strip() else {}
    except json.JSONDecodeError as exc:
        _audit("jira_api", url, "failure", error="unparseable JSON")
        raise ProviderCliError(f"Jira returned unexpected output for {path}") from exc


# Long-running paginated reads (full open-issue backlogs) get a larger budget,
# mirroring github_client's GH_PAGINATE_TIMEOUT_SEC.
def _jira_search(
    host: str,
    jql: str,
    fields: list[str],
    *,
    timeout: float = JIRA_PAGINATE_TIMEOUT_SEC,
    paginate: bool = True,
) -> list[dict]:
    """Run a Jira JQL search and return the raw issue rows (normalized later).

    ``paginate=True`` walks every page up to ``_MAX_PAGES``; ``False`` returns
    the first page only. ``jql`` is embedded via :func:`quote` before it reaches
    the query string, so it cannot inject parameters.
    """
    rows: list[dict] = []
    resolved = _resolve_host(host)
    if resolved.endswith(_CLOUD_SUFFIX):
        next_page_token = ""
        for _ in range(_MAX_PAGES):
            qs = (
                f"jql={quote(jql, safe='')}"
                f"&fields={quote(','.join(fields), safe=',')}&maxResults={_PAGE_SIZE}"
            )
            if next_page_token:
                qs += f"&nextPageToken={quote(next_page_token, safe='')}"
            data = _jira_request(host, "GET", f"{_CLOUD_API_ROOT}/search/jql?{qs}", timeout=timeout)
            batch = data.get("issues") or []
            rows.extend(batch)
            if not paginate or not batch or bool(data.get("isLast")):
                break
            token = data.get("nextPageToken")
            if not isinstance(token, str) or not token or token == next_page_token:
                break
            next_page_token = token
        return rows

    start = 0
    while True:
        qs = (
            f"jql={quote(jql, safe='')}"
            f"&fields={','.join(fields)}&maxResults={_PAGE_SIZE}&startAt={start}"
        )
        data = _jira_request(host, "GET", f"{_API_ROOT}/search?{qs}", timeout=timeout)
        batch = data.get("issues") or []
        rows.extend(batch)
        total = _safe_int(data.get("total"), default=len(rows))
        if not paginate or not batch or len(rows) >= total or start >= _MAX_PAGES * _PAGE_SIZE:
            break
        start += len(batch)
    return rows


# ── URL parsing ──────────────────────────────────────────────────────────────


def parse_jira_project_url(
    link: str, *, allowed_hosts: frozenset[str] | None = None
) -> tuple[str, str]:
    """Parse a Jira project URL into ``(host, project_key)``.

    Accepts ``https://org.atlassian.net/jira/software/projects/PROJ/...``,
    ``https://org.atlassian.net/browse/PROJ-123`` (the project is the key
    prefix), and any path that contains a ``/projects/<KEY>`` or ``/browse/<KEY>``
    segment. The host is validated against the allowlist (Cloud auto-allowed);
    an unlisted self-managed host raises :class:`RepoUrlError`.

    Returns ``(host, project_key)`` where ``host`` is the canonical lowercase
    host (no trailing dot) and ``project_key`` is the uppercase project key.
    """
    from .errors import RepoUrlError

    if not link or not isinstance(link, str):
        raise RepoUrlError("Jira link is empty")
    try:
        parsed = urlparse(link.strip())
        netloc = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise RepoUrlError(f"unparseable URL: {link!r}") from exc
    if not netloc:
        raise RepoUrlError("expected a Jira URL like https://org.atlassian.net/browse/PROJ-123")

    # Validate the host exactly like source_providers._jira_ref: Cloud suffix
    # auto-allowed, self-managed needs an exact allowlist entry.
    is_cloud = netloc.endswith(_CLOUD_SUFFIX) and len(netloc) > len(_CLOUD_SUFFIX)
    if not is_cloud and (allowed_hosts is None or netloc not in allowed_hosts):
        raise RepoUrlError(
            f"Jira host {netloc!r} is not *.atlassian.net and is not listed in "
            "the dashboard.jira_hosts allowlist"
        )

    path = parsed.path
    key = ""
    # pi-lens-ignore: reportAttributeAccessIssue
    m = re.search(r"/browse/([A-Z][A-Z0-9]{1,9}-\d+)", path, re.IGNORECASE)
    if m:
        key = m.group(1).split("-")[0].upper()
    else:
        # pi-lens-ignore: reportAttributeAccessIssue
        m = re.search(r"/projects/([A-Z][A-Z0-9]{1,9})(?:/|$)", path, re.IGNORECASE)
        if m:
            key = m.group(1).upper()
    if not key or not _PROJECT_RE.fullmatch(key):
        raise RepoUrlError(
            f"expected a Jira URL like https://org.atlassian.net/browse/PROJ-123 "
            f"(could not find a project key in {link!r})"
        )
    return netloc, key


# ── normalization ────────────────────────────────────────────────────────────


def _status_category(issue: dict) -> str:
    """The Jira status category name for an issue's current status."""
    status = (issue.get("fields") or {}).get("status") or {}
    category = status.get("statusCategory") if isinstance(status, dict) else {}
    category_key = category.get("key", "") if isinstance(category, dict) else ""
    status_name = status.get("name", "") if isinstance(status, dict) else ""
    # "new" / "indeterminate" -> open; "done" -> closed. The name fallback
    # keeps stale/custom Jira workflows from leaking a literal Done issue into
    # the open list when the category metadata is incomplete.
    return (
        "closed" if category_key == "done" or str(status_name).strip().lower() == "done" else "open"
    )


def _author_name(user: Any) -> str | None:
    """The display name of a Jira user object, or ``None``."""
    if isinstance(user, dict):
        return user.get("displayName") or user.get("name")
    return None


def _field_name(value: Any) -> str | None:
    """The name of a Jira status/priority object, or ``None``."""
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    return None


def _norm_labels(raw: Any) -> list[str]:
    """Jira's ``labels`` field is a list of plain strings (no colour)."""
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


_ADF_BLOCK_TYPES = frozenset(
    {
        "blockquote",
        "bulletList",
        "codeBlock",
        "doc",
        "heading",
        "listItem",
        "orderedList",
        "paragraph",
        "panel",
        "table",
        "tableCell",
        "tableRow",
    }
)


def _jira_text(value: Any) -> str:
    """Convert Jira's Atlassian Document Format or a plain string to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_jira_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    if isinstance(text, str):
        return text
    if value.get("type") == "hardBreak":
        return "\n"
    content = value.get("content")
    if not isinstance(content, list):
        return ""
    parts = [_jira_text(item) for item in content]
    separator = "\n" if value.get("type") in _ADF_BLOCK_TYPES else ""
    return separator.join(part for part in parts if part)


def _safe_int(value: object, default: int = 0) -> int:
    """Coerce a value from a Jira API payload to an int, never raising.

    Jira responses are external input, not guaranteed-shaped like ``gh`` output,
    so a malformed ``id``/``total``/comment count must degrade to ``default``
    rather than surface as an unhandled ``ValueError``/``TypeError``.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] in "+-" and text[1:].isdigit())):
            try:
                return int(text)
            except ValueError:  # pragma: no cover — guarded by isdigit above
                return default
    return default


def _norm_issue(issue: dict) -> dict:
    """One raw Jira search/issue row -> the GitHub-shaped issue dict."""
    fields = issue.get("fields") or {}
    return {
        "number": _safe_int(issue.get("id")),
        "key": issue.get("key") or "",
        "title": fields.get("summary") or "",
        "url": _browse_url(issue.get("self") or "", issue.get("key") or ""),
        "status": _field_name(fields.get("status")),
        "priority": _field_name(fields.get("priority")),
        "labels": _norm_labels(fields.get("labels")),
        "comments": _safe_int(
            (fields.get("comment") or {}).get("total")
            if isinstance(fields.get("comment"), dict)
            else 0
        ),
        "reactions": 0,
        "thumbs_up": 0,
        "author_association": None,
        "updated_at": fields.get("updated"),
        "state": _status_category(issue),
        "author": _author_name(fields.get("creator") or fields.get("reporter")),
        "assignees": [a for a in [_author_name(fields.get("assignee"))] if a],
        "body": _jira_text(fields.get("description")),
        "created_at": fields.get("created"),
    }


def _browse_url(self_url: str, key: str) -> str:
    """The browse URL for an issue key, derived from the ``self`` API URL."""
    # self looks like https://host/rest/api/2/issue/12345 — the browse page is
    # https://host/browse/KEY.
    m = re.match(r"(https?://[^/]+)", self_url or "")
    base = m.group(1) if m else ""
    return f"{base}/browse/{key}" if base and key else ""


# ── reads ────────────────────────────────────────────────────────────────────


def verify_repo_access(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> dict:
    """Verify the Jira project exists and the ambient credentials can read it.

    ``owner`` is the Jira project key (e.g. ``PROJ``); ``repo`` is the manually
    mapped Git repo slug, which Jira does not look at. ``host`` is required and
    re-allowlist-checked on every call (mirrors GitLab). Returns a small summary
    dict on success; raises :class:`ProviderCliError` on any failure, which the
    caller maps to a 502 (upstream/auth problem).
    """
    data = _jira_request(host, "GET", _project_path(owner), timeout=timeout)
    return {
        "full_name": data.get("name") or owner,
        "private": True,  # Jira projects are access-controlled by default
        "open_issues_count": 0,  # filled by list_open_issues; kept cheap here
        "description": data.get("description") or "",
        # The token-bearer is by definition authorized: the credential itself is
        # the authorization (there is no separate "triage" role to probe cheaply).
        "permissions": {"push": True, "pull": True, "triage": True},
    }


def _project_path(owner: str) -> str:
    """The API path for a project or issue key, validated to a safe shape."""
    if not _PROJECT_RE.fullmatch(owner):
        raise ProviderCliError(f"invalid Jira project key: {owner!r}")
    return f"{_API_ROOT}/project/{owner}"


def _jira_board_id(owner: str, host: str, timeout: float) -> int | None:
    """Return the named board for ``owner``, or ``None`` when it is absent.

    The project filter keeps a same-named board in another project from changing
    the Issue Radar set. A missing board keeps older project-wide connections
    usable; when the board exists, all list reads use its membership endpoint.
    """
    qs = (
        f"projectKeyOrId={quote(owner, safe='')}"
        f"&name={quote(_JIRA_BOARD_NAME, safe='')}&maxResults=50"
    )
    data = _jira_request(host, "GET", f"{_AGILE_BOARD_ROOT}/board?{qs}", timeout=timeout)
    values = data.get("values") or []
    for board in values:
        if not isinstance(board, dict) or board.get("name") != _JIRA_BOARD_NAME:
            continue
        board_id = _safe_int(board.get("id"), default=0)
        if board_id > 0:
            return board_id
    return None


def _jira_board_search(
    owner: str,
    host: str,
    jql: str,
    fields: list[str],
    *,
    timeout: float,
    paginate: bool,
) -> list[dict]:
    """Search the configured board, falling back for projects without it."""
    board_id = _jira_board_id(owner, host, timeout)
    if board_id is None:
        return _jira_search(
            host, f'project="{owner}" AND {jql}', fields, timeout=timeout, paginate=paginate
        )

    rows: list[dict] = []
    start = 0
    while True:
        qs = (
            f"startAt={start}&maxResults={_PAGE_SIZE}"
            f"&jql={quote(jql, safe='')}&fields={quote(','.join(fields), safe=',')}"
        )
        data = _jira_request(
            host, "GET", f"{_AGILE_BOARD_ROOT}/board/{board_id}/issue?{qs}", timeout=timeout
        )
        batch = data.get("issues") or []
        rows.extend(batch)
        total = _safe_int(data.get("total"), default=len(rows))
        if not paginate or not batch or len(rows) >= total or start >= _MAX_PAGES * _PAGE_SIZE:
            break
        start += len(batch)
    return rows


def list_open_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """ALL open issues on the Entwicklung board (paginated across every page)."""
    jql = "statusCategory != done ORDER BY updated DESC"
    rows = _jira_board_search(owner, host, jql, _ISSUE_FIELDS, timeout=timeout, paginate=True)
    return [_norm_issue(i) for i in rows if _status_category(i) == "open"]


def list_open_issues_first_page(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> list[dict]:
    """The newest ``_PAGE_SIZE`` open issues in ONE request (no pagination)."""
    jql = "statusCategory != done ORDER BY updated DESC"
    rows = _jira_board_search(owner, host, jql, _ISSUE_FIELDS, timeout=timeout, paginate=False)
    return [_norm_issue(i) for i in rows if _status_category(i) == "open"]


def list_closed_issues(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> list[dict]:
    """The 100 most-recently-updated CLOSED issues on the board (bounded)."""
    jql = "statusCategory = done ORDER BY updated DESC"
    return [
        _norm_issue(i)
        for i in _jira_board_search(
            owner, host, jql, _ISSUE_FIELDS, timeout=timeout, paginate=False
        )
    ]


_ISSUE_FIELDS = [
    "summary",
    "status",
    "priority",
    "labels",
    "comment",
    "creator",
    "reporter",
    "assignee",
    "description",
    "updated",
    "created",
]


def list_recent_open_issues(
    owner: str, repo: str, limit: int = 30, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> list[dict]:
    """The ``limit`` most-recently-CREATED open issues, newest first — the cheap
    single-page poll the background watcher uses to detect new issues."""
    lim = max(1, min(_safe_int(limit, 30), 100))
    jql = "statusCategory != done ORDER BY created DESC"
    fields = ["summary", "status", "priority", "creator", "created"]
    rows = _jira_board_search(owner, host, jql, fields, timeout=timeout, paginate=False)
    return [_norm_issue(i) for i in rows[:lim] if _status_category(i) == "open"]


def probe_open_list(
    owner: str, repo: str, kind: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> dict:
    """Return ``{"total_count": int, "top_updated_at": str | None}`` for a
    project's OPEN issues (``kind="issue"``).

    Jira has no pull requests, so ``kind="pr"`` raises :class:`ProviderCliError`
    (the frontend never asks for it — it hides the PR tab for Jira).
    """
    if kind != "issue":
        raise ProviderCliError(f"Jira has no {kind!r} list to probe")
    jql = "statusCategory != done ORDER BY updated DESC"
    rows = _jira_board_search(owner, host, jql, ["updated"], timeout=timeout, paginate=False)
    top = rows[0].get("fields", {}).get("updated") if rows else None
    return {"total_count": len(rows), "top_updated_at": top if isinstance(top, str) else None}


def list_repo_labels(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> list[dict]:
    """The distinct labels seen across the project's open issues.

    Jira has no project-level label registry like GitHub; labels are a free-text
    field per issue. Deriving the set from the open issues is the honest
    equivalent the left-rail filter column needs. Returns
    ``[{name, color, description}]`` with a neutral colour (Jira labels carry no
    colour in the API).
    """
    jql = f'project="{owner}" ORDER BY updated DESC'
    rows = _jira_search(host, jql, ["labels"], timeout=timeout, paginate=False)
    seen: dict[str, None] = {}
    for i in rows:
        for name in _norm_labels((i.get("fields") or {}).get("labels")):
            seen.setdefault(name, None)
    return [{"name": n, "color": "888888", "description": ""} for n in sorted(seen)]


def derive_members(issues: list[dict]) -> list[dict]:
    """The distinct issue authors (Jira reporters/creators) among ``issues``.

    Jira has no collaborator roster endpoint a triage view can reach without
    admin rights, so this derived set is the full story. Returns
    ``[{"login", "association"}]`` sorted by login.
    """
    best: dict[str, str] = {}
    for iss in issues:
        login = iss.get("author")
        if not login:
            continue
        best.setdefault(login, "MEMBER")
    return [{"login": login, "association": assoc} for login, assoc in sorted(best.items())]


def list_repo_collaborators(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> list[dict]:
    """Not supported: Jira has no collaborator roster endpoint."""
    del host
    raise ProviderCliError("Jira has no collaborator roster to list")


def get_current_login(*, host: str = "", timeout: float = JIRA_TIMEOUT_SEC) -> str | None:
    """The authenticated account's display name via Jira's ``myself`` endpoint."""
    data = _jira_request(host, "GET", f"{_API_ROOT}/myself", timeout=timeout)
    return data.get("displayName") or data.get("emailAddress") or data.get("name")


def list_contributed_repos(login: str, **kwargs: object) -> tuple[list[dict], bool]:
    """Jira has no contributed-repo history; the recent-repos rail is empty."""
    del login, kwargs
    return [], False


def get_issue_detail(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> dict:
    """Full detail for one Jira issue by its numeric id.

    ``number`` is the Jira issue id (the ``number`` field of the list rows);
    the key (``PROJ-123``) is carried in the returned ``key`` field. Returns the
    richer field set the detail pane renders.
    """
    issue = _jira_request(
        host,
        "GET",
        f"{_API_ROOT}/issue/{_safe_int(number)}?fields=" + ",".join(_DETAIL_FIELDS),
        timeout=timeout,
    )
    fields = issue.get("fields") or {}
    return {
        "number": _safe_int(issue.get("id")),
        "key": issue.get("key") or "",
        "title": fields.get("summary") or "",
        "status": _field_name(fields.get("status")),
        "priority": _field_name(fields.get("priority")),
        "body": _jira_text(fields.get("description")),
        "state": _status_category(issue),
        "state_reason": None,
        "url": _browse_url(issue.get("self") or "", issue.get("key") or ""),
        "author": _author_name(fields.get("creator") or fields.get("reporter")),
        "author_association": None,
        "created_at": fields.get("created"),
        "updated_at": fields.get("updated"),
        "closed_at": fields.get("resolutiondate"),
        "closed_by": None,
        "comments": _safe_int(
            (fields.get("comment") or {}).get("total")
            if isinstance(fields.get("comment"), dict)
            else 0
        ),
        "locked": False,
        "labels": [
            {"name": n, "color": "888888", "description": ""}
            for n in _norm_labels(fields.get("labels"))
        ],
        "assignees": [a for a in [_author_name(fields.get("assignee"))] if a],
        "milestone": None,
        "reactions": None,
    }


_DETAIL_FIELDS = [
    "summary",
    "status",
    "priority",
    "labels",
    "comment",
    "creator",
    "reporter",
    "assignee",
    "description",
    "updated",
    "created",
    "resolutiondate",
]


def list_issue_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> list[dict]:
    """The activity timeline for one issue: comments + status/label/assignee
    changes from the changelog, normalized to the GitHub-shaped event list the
    UI renders (``comment``, ``labeled``/``unlabeled``, ``assigned``/``unassigned``,
    ``closed``/``reopened``)."""
    issue = _jira_request(
        host,
        "GET",
        f"{_API_ROOT}/issue/{_safe_int(number)}?fields=status,comment,created,updated,creator&expand=changelog",
        timeout=timeout,
    )
    fields = issue.get("fields") or {}
    events: list[dict] = []

    comments = (
        (fields.get("comment") or {}).get("comments")
        if isinstance(fields.get("comment"), dict)
        else None
    )
    for c in comments or []:
        events.append(
            {
                "kind": "comment",
                "id": c.get("id"),
                "actor": _author_name(c.get("author")),
                "created_at": c.get("created"),
                "updated_at": c.get("updated"),
                "body": _jira_text(c.get("body")),
                "author_association": None,
                "reactions": None,
            }
        )

    for history in (issue.get("changelog") or {}).get("histories") or []:
        actor = _author_name(history.get("author"))
        created = history.get("created")
        for item in history.get("items") or []:
            events.extend(_changelog_event(item, actor, created))

    return sorted(events, key=lambda e: e.get("created_at") or "")


def _changelog_event(item: dict, actor: str | None, created: str | None) -> list[dict]:
    """Map one changelog item to a list of timeline events (a label change can
    add AND remove several at once), or ``[]`` to drop it."""
    field = (item.get("field") or "").lower()
    if field == "status":
        to = (item.get("to") or "").lower()
        kind = "closed" if to in ("done", "closed", "resolved") else "reopened"
        return [{"kind": kind, "actor": actor, "created_at": created}]
    if field == "labels":
        froms = [x for x in (item.get("from") or "").split(",") if x]
        tos = [x for x in (item.get("to") or "").split(",") if x]
        out: list[dict] = []
        for lab in tos:
            if lab not in froms:
                out.append(_label_event("labeled", lab, actor, created))
        for lab in froms:
            if lab not in tos:
                out.append(_label_event("unlabeled", lab, actor, created))
        return out
    if field == "assignee":
        if item.get("to"):
            return [
                {
                    "kind": "assigned",
                    "actor": actor,
                    "created_at": created,
                    "assignee": item.get("toString"),
                }
            ]
        return [{"kind": "unassigned", "actor": actor, "created_at": created, "assignee": None}]
    return []


def _label_event(kind: str, name: str, actor: str | None, created: str | None) -> dict:
    return {
        "kind": kind,
        "actor": actor,
        "created_at": created,
        "label": {"name": name, "color": "888888"},
    }


def get_ref_summary(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> dict:
    """Compact summary of one Jira issue by id (see :func:`get_issue_detail`)."""
    issue = _jira_request(
        host,
        "GET",
        f"{_API_ROOT}/issue/{_safe_int(number)}?fields=summary,status,created,updated,creator,labels,comment",
        timeout=timeout,
    )
    fields = issue.get("fields") or {}
    return {
        "number": _safe_int(issue.get("id")),
        "key": issue.get("key") or "",
        "title": fields.get("summary") or "",
        "state": _status_category(issue),
        "state_reason": None,
        "url": _browse_url(issue.get("self") or "", issue.get("key") or ""),
        "author": _author_name(fields.get("creator") or fields.get("reporter")),
        "author_association": None,
        "created_at": fields.get("created"),
        "updated_at": fields.get("updated"),
        "closed_at": fields.get("resolutiondate"),
        "comments": _safe_int(
            (fields.get("comment") or {}).get("total")
            if isinstance(fields.get("comment"), dict)
            else 0
        ),
        "is_pr": False,
        "draft": False,
        "merged_at": None,
        "labels": [{"name": n, "color": "888888"} for n in _norm_labels(fields.get("labels"))],
    }


# ── writes ───────────────────────────────────────────────────────────────────


def add_issue_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    host: str = "",
    timeout: float = JIRA_TIMEOUT_SEC,
) -> dict:
    """Post a comment on a Jira issue (``POST .../issue/{id}/comment``).

    Returns ``{"id", "body", "author"}`` shaped like the timeline comment rows.
    """
    data = _jira_request(
        host,
        "POST",
        f"{_API_ROOT}/issue/{_safe_int(number)}/comment",
        payload={"body": body},
        timeout=timeout,
    )
    return {
        "id": data.get("id"),
        "body": data.get("body") or body,
        "author": _author_name(data.get("author")),
    }


def add_pr_comment(owner: str, repo: str, number: int, body: str, **kwargs: object) -> dict:
    """Jira has no pull requests; see :func:`_unsupported`."""
    del kwargs
    raise _unsupported("pull-request comments")


def _labels_now(host: str, number: int, timeout: float) -> list[str]:
    """Read the issue's current labels (Jira's labels field is a full replace)."""
    issue = _jira_request(
        host, "GET", f"{_API_ROOT}/issue/{_safe_int(number)}?fields=labels", timeout=timeout
    )
    return _norm_labels((issue.get("fields") or {}).get("labels"))


def _set_labels(host: str, number: int, labels: list[str], timeout: float) -> list[dict]:
    """Replace an issue's labels via PUT and return the shaped label set."""
    data = _jira_request(
        host,
        "PUT",
        f"{_API_ROOT}/issue/{_safe_int(number)}",
        payload={"fields": {"labels": list(labels)}},
        timeout=timeout,
    )
    del data
    return [{"name": n, "color": "888888", "description": ""} for n in labels]


def add_issue_labels(
    owner: str,
    repo: str,
    number: int,
    labels: list[str],
    *,
    host: str = "",
    timeout: float = JIRA_TIMEOUT_SEC,
) -> list[dict]:
    """Add ``labels`` to a Jira issue (read-modify-write: Jira replaces the
    whole labels field on PUT). Idempotent and additive like GitHub's."""
    current = _labels_now(host, number, timeout)
    merged = current + [lab for lab in labels if lab and lab not in current]
    return _set_labels(host, number, merged, timeout)


def remove_issue_label(
    owner: str,
    repo: str,
    number: int,
    label: str,
    *,
    host: str = "",
    timeout: float = JIRA_TIMEOUT_SEC,
) -> list[dict] | None:
    """Remove ONE label from a Jira issue by rewriting the labels field without
    it. Returns the remaining labels, or ``None`` when the label was absent
    (so the caller re-reads the authoritative set)."""
    current = _labels_now(host, number, timeout)
    if label not in current:
        return None
    remaining = [lab for lab in current if lab != label]
    return _set_labels(host, number, remaining, timeout)


def set_issue_state(
    owner: str,
    repo: str,
    number: int,
    state: str,
    state_reason: str | None = None,
    *,
    host: str = "",
    timeout: float = JIRA_TIMEOUT_SEC,
) -> dict:
    """Close or reopen a Jira issue via a workflow transition.

    Jira does not set status directly — status changes go through workflow
    transitions. ``state="closed"`` finds a transition whose target status is in
    the Done category; ``state="open"`` finds one whose target is not Done.
    Returns ``{"state", "state_reason"}`` like the GitHub client. Raises
    :class:`ProviderCliError` when no matching transition exists (custom
    workflow without a usable transition — the caller surfaces it as-is).
    """
    del owner, repo, state_reason
    transitions = (
        _jira_request(
            host,
            "GET",
            f"{_API_ROOT}/issue/{_safe_int(number)}/transitions?expand=transitions.fields",
            timeout=timeout,
        ).get("transitions")
        or []
    )
    want_done = state == "closed"
    for tr in transitions:
        cat = ((tr.get("to") or {}).get("statusCategory") or {}).get("key", "")
        if want_done and cat == "done" or (not want_done and cat != "done"):
            _jira_request(
                host,
                "POST",
                f"{_API_ROOT}/issue/{_safe_int(number)}/transitions",
                payload={"transition": {"id": tr["id"]}},
                timeout=timeout,
            )
            return {"state": "closed" if want_done else "open", "state_reason": None}
    raise ProviderCliError(
        f"no Jira workflow transition {'to Done' if want_done else 'out of Done'} "
        f"exists for issue {number}"
    )


def create_label(
    owner: str,
    repo: str,
    name: str,
    color: str = "888888",
    description: str = "",
    **kwargs: object,
) -> dict:
    """Jira has no project-level label registry to create one in."""
    del owner, repo, name, color, description, kwargs
    raise _unsupported("creating labels")


def get_repo_permissions(
    owner: str, repo: str, *, host: str = "", timeout: float = JIRA_TIMEOUT_SEC
) -> dict:
    """The token-bearer's permissions for the project (see
    :func:`verify_repo_access` — the credential is the authorization)."""
    perms = verify_repo_access(owner, repo, host=host, timeout=timeout).get("permissions")
    return perms if isinstance(perms, dict) else {}


def _unsupported(what: str) -> ProviderCliError:
    return ProviderCliError(f"Jira does not support {what}")


# ── PR surface: no Jira equivalent — every function refuses loudly ──────────


def list_issue_timeline_pr(*args: object, **kwargs: object) -> list[dict]:
    del args, kwargs
    raise _unsupported("pull requests")


def list_pr_timeline(owner: str, repo: str, number: int, **kwargs: object) -> list[dict]:
    del owner, repo, number, kwargs
    raise _unsupported("pull requests")


def list_open_pulls(owner: str, repo: str, **kwargs: object) -> list[dict]:
    del owner, repo, kwargs
    raise _unsupported("pull requests")


def list_open_pulls_first_page(owner: str, repo: str, **kwargs: object) -> list[dict]:
    del owner, repo, kwargs
    raise _unsupported("pull requests")


def list_closed_pulls(owner: str, repo: str, **kwargs: object) -> list[dict]:
    del owner, repo, kwargs
    raise _unsupported("pull requests")


def get_pr_detail(owner: str, repo: str, number: int, **kwargs: object) -> dict:
    del owner, repo, number, kwargs
    raise _unsupported("pull requests")


def list_pr_checks(owner: str, repo: str, sha: str, **kwargs: object) -> list[dict]:
    del owner, repo, sha, kwargs
    raise _unsupported("pull requests")


def summarize_checks(checks: list[dict]) -> dict:
    del checks
    raise _unsupported("pull requests")


def enrich_pulls(
    owner: str, repo: str, pulls: list[dict], state: str, **kwargs: object
) -> list[dict]:
    del owner, repo, pulls, state, kwargs
    raise _unsupported("pull requests")


def enrich_pulls_by_number(
    owner: str, repo: str, pulls: list[dict], **kwargs: object
) -> list[dict]:
    del owner, repo, pulls, kwargs
    raise _unsupported("pull requests")


def enrichment_complete(pulls: list[dict]) -> bool:
    del pulls
    raise _unsupported("pull requests")


def search_pulls(owner: str, repo: str, **kwargs: object) -> list[dict]:
    del owner, repo, kwargs
    raise _unsupported("pull requests")


def build_pr_search_query(owner: str, repo: str, **kwargs: object) -> str:
    del owner, repo, kwargs
    raise _unsupported("pull requests")


def set_pr_state(owner: str, repo: str, number: int, state: str, **kwargs: object) -> dict:
    del owner, repo, number, state, kwargs
    raise _unsupported("pull requests")


def submit_pr_review(
    owner: str,
    repo: str,
    number: int,
    event: str,
    body: str = "",
    head_sha: str = "",
    **kwargs: object,
) -> dict:
    del owner, repo, number, event, body, head_sha, kwargs
    raise _unsupported("pull requests")


def merge_pull_request(
    owner: str, repo: str, number: int, method: str = "", head_sha: str = "", **kwargs: object
) -> dict:
    del owner, repo, number, method, head_sha, kwargs
    raise _unsupported("pull requests")


def enable_auto_merge(
    owner: str, repo: str, number: int, method: str = "", **kwargs: object
) -> dict:
    del owner, repo, number, method, kwargs
    raise _unsupported("pull requests")


def disable_auto_merge(owner: str, repo: str, number: int, **kwargs: object) -> dict:
    del owner, repo, number, kwargs
    raise _unsupported("pull requests")


def list_pr_workflow_runs(owner: str, repo: str, sha: str, **kwargs: object) -> list[dict]:
    del owner, repo, sha, kwargs
    raise _unsupported("pull requests")


def cancel_workflow_run(owner: str, repo: str, run_id: int, **kwargs: object) -> dict:
    del owner, repo, run_id, kwargs
    raise _unsupported("pull requests")


def rerun_workflow_run(owner: str, repo: str, run_id: int, **kwargs: object) -> dict:
    del owner, repo, run_id, kwargs
    raise _unsupported("pull requests")


# ── legacy alias (mirrors github_client; unused by routes, kept for parity) ──


def parse_github_repo_url(link: str) -> tuple[str, str]:
    """Alias kept so ``provider.parse_repo_url`` style callers type-check; Jira
    URLs are parsed by :func:`parse_jira_project_url` instead."""
    del link
    raise _unsupported("GitHub repo URLs")
