"""Atlassian OAuth 2.0 (3LO) for Issue Radar's Jira REST client.

The Jira MCP server has a separate OAuth resource and token audience. Issue Radar
therefore owns a small server-side 3LO flow for the Jira REST API instead of
reading kiro-cli or mcp-remote credential caches.

Client configuration comes from the gateway environment (normally its protected
``.env``): ``JIRA_OAUTH_CLIENT_ID``, ``JIRA_OAUTH_CLIENT_SECRET`` and
``JIRA_OAUTH_REDIRECT_URI``. User access and rotating refresh tokens live in the
KiroCrew keystone file ``jira_oauth_tokens.json``; its path is protected by the
agent sensitive-path floor and is never returned through an API response.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlparse

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel

from .errors import ProviderCliError, ProviderSetupError

logger = logging.getLogger(__name__)

AUTHORIZATION_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
_TOKEN_FILE = "jira_oauth_tokens.json"
_STATE_TTL_SECONDS = 600.0
_REFRESH_SKEW_SECONDS = 60.0
_HTTP_TIMEOUT_SECONDS = 20.0
_CLOUD_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")

# The scopes cover Issue Radar's current reads and issue mutations. ``offline_access``
# is required for Atlassian's rotating refresh-token flow.
OAUTH_SCOPES = (
    "read:jira-work",
    "write:jira-work",
    "read:jira-user",
    "read:board-scope:jira-software",
    "read:issue-details:jira",
    "offline_access",
)

_pending_lock = threading.Lock()
_refresh_lock = threading.Lock()
_pending: dict[str, tuple[str, float]] = {}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _token_path():
    return config_dir() / _TOKEN_FILE


def _lock_path():
    return _token_path().with_name(f"{_TOKEN_FILE}.lock")


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _expires_in(value: object) -> float:
    try:
        numeric = value if isinstance(value, (int, float, str)) else 3600.0
        return max(float(numeric), 1.0)
    except (TypeError, ValueError):
        return 3600.0


def _config() -> tuple[str, str, str]:
    client_id = _env("JIRA_OAUTH_CLIENT_ID")
    client_secret = _env("JIRA_OAUTH_CLIENT_SECRET")
    redirect_uri = _env("JIRA_OAUTH_REDIRECT_URI")
    if not all((client_id, client_secret, redirect_uri)):
        raise ProviderSetupError(
            "Jira OAuth is not configured: set JIRA_OAUTH_CLIENT_ID, "
            "JIRA_OAUTH_CLIENT_SECRET and JIRA_OAUTH_REDIRECT_URI in the gateway "
            "environment",
            reason="oauth_not_configured",
        )
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ProviderSetupError(
            "JIRA_OAUTH_REDIRECT_URI must use HTTPS, except for localhost",
            reason="oauth_not_configured",
        )
    return client_id, client_secret, redirect_uri


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _cloud_host(host: str) -> str:
    normalized = _normalize_host(host)
    if not normalized or not normalized.endswith(".atlassian.net"):
        raise ProviderSetupError(
            "Atlassian OAuth is available for Jira Cloud hosts only; use the "
            "existing PAT configuration for Jira Server/Data Center",
            reason="oauth_cloud_only",
        )
    return normalized


def _audit(outcome: str, host: str, error: str = "") -> None:
    sel().log_api_access(
        caller="core:issue-radar",
        operation="issue_radar.jira_oauth",
        outcome=outcome,
        source="builtin-app",
        resources=host[:200],
        error=error[:200] if error else "",
    )


def _read_tokens() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(_token_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(host): dict(record)
        for host, record in raw.items()
        if isinstance(host, str) and isinstance(record, dict)
    }


def _write_tokens(tokens: dict[str, dict[str, object]]) -> None:
    with (
        _lock_path().open("a+", encoding="utf-8") as lock_file,
        platform_compat.file_lock(lock_file.fileno(), exclusive=True, required=True),
    ):
        # Re-read under the cross-process lock so two gateway processes never
        # lose a different host's grant in a read-modify-write race.
        current = _read_tokens()
        current.update(tokens)
        atomic_write(
            _token_path(),
            json.dumps(current, indent=2, sort_keys=True),
            fsync=True,
            restrict_to_owner=True,
        )


def _read_token(host: str) -> dict[str, object] | None:
    normalized = _normalize_host(host)
    with (
        _lock_path().open("a+", encoding="utf-8") as lock_file,
        platform_compat.file_lock(lock_file.fileno(), exclusive=False, required=True),
    ):
        return _read_tokens().get(normalized)


def _store_token(host: str, record: dict[str, object]) -> None:
    _write_tokens({_normalize_host(host): record})


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    bearer: str = "",
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ProviderCliError(f"Atlassian OAuth request failed (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderCliError("could not reach the Atlassian OAuth service") from exc
    try:
        if not raw.strip():
            return None
        parsed = json.loads(raw)
        return parsed
    except json.JSONDecodeError as exc:
        raise ProviderCliError("Atlassian OAuth returned unexpected output") from exc


def start_authorization(host: str) -> str:
    """Create a short-lived state and return Atlassian's consent URL."""
    resolved = _cloud_host(host)
    client_id, _client_secret, redirect_uri = _config()
    state = secrets.token_urlsafe(32)
    now = _now()
    with _pending_lock:
        cutoff = now - _STATE_TTL_SECONDS
        for key, (_, created) in list(_pending.items()):
            if created < cutoff:
                _pending.pop(key, None)
        _pending[state] = (resolved, now)
    query = urllib.parse.urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": " ".join(OAUTH_SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
    )
    _audit("started", resolved)
    return f"{AUTHORIZATION_URL}?{query}"


def _resource_for_host(host: str, resources: Any) -> tuple[str, list[str]]:
    resolved = _cloud_host(host)
    if not isinstance(resources, list):
        raise ProviderSetupError(
            "Atlassian returned no accessible Jira sites for this account",
            reason="oauth_no_site",
        )
    for item in resources:
        if not isinstance(item, dict):
            continue
        resource_url = item.get("url")
        try:
            resource_host = (
                urlparse(resource_url).hostname if isinstance(resource_url, str) else None
            )
        except ValueError:
            continue
        if _normalize_host(resource_host or "") != resolved:
            continue
        cloud_id = item.get("id")
        if not isinstance(cloud_id, str) or not _CLOUD_ID_RE.fullmatch(cloud_id):
            break
        scopes = [scope for scope in item.get("scopes", []) if isinstance(scope, str)]
        return cloud_id, scopes
    raise ProviderSetupError(
        f"Atlassian OAuth is not authorized for Jira host {resolved!r}",
        reason="oauth_no_site",
    )


def complete_authorization(code: str, state: str) -> str:
    """Exchange a callback code, bind it to its state, and persist the grant."""
    if not code or len(code) > 4096 or not state or len(state) > 4096:
        raise ProviderSetupError("invalid Jira OAuth callback", reason="oauth_callback_invalid")
    with _pending_lock:
        pending = _pending.pop(state, None)
    if pending is None or pending[1] < _now() - _STATE_TTL_SECONDS:
        raise ProviderSetupError(
            "Jira OAuth state is invalid or expired", reason="oauth_state_invalid"
        )
    host = pending[0]
    client_id, client_secret, redirect_uri = _config()
    try:
        token = _http_json(
            TOKEN_URL,
            method="POST",
            payload={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
            raise ProviderSetupError(
                "Atlassian did not return an access token", reason="oauth_exchange_failed"
            )
        access_token = cast(str, token["access_token"])
        resources = _http_json(RESOURCES_URL, bearer=access_token)
        cloud_id, scopes = _resource_for_host(host, resources)
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ProviderSetupError(
                "Atlassian did not return a refresh token; include offline_access in the OAuth grant",
                reason="oauth_exchange_failed",
            )
        _store_token(
            host,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "cloud_id": cloud_id,
                "expires_at": _now() + _expires_in(token.get("expires_in", 3600)),
                "scopes": scopes,
            },
        )
    except ProviderSetupError:
        _audit("failed", host, "authorization setup failed")
        raise
    except ProviderCliError as exc:
        _audit("failed", host, str(exc))
        raise ProviderSetupError(
            "Atlassian OAuth authorization failed", reason="oauth_exchange_failed"
        ) from exc
    _audit("completed", host)
    return host


def _refresh(host: str, record: dict[str, object]) -> dict[str, object]:
    client_id, client_secret, _redirect_uri = _config()
    refresh_token = record.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ProviderSetupError(
            "Jira OAuth authorization has expired; authorize again", reason="oauth_reauthorize"
        )
    response = _http_json(
        TOKEN_URL,
        method="POST",
        payload={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    response_map = response if isinstance(response, dict) else {}
    access_token = response_map.get("access_token")
    new_refresh = response_map.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ProviderSetupError(
            "Jira OAuth authorization expired; authorize again", reason="oauth_reauthorize"
        )
    refreshed: dict[str, object] = dict(record)
    refreshed["access_token"] = access_token
    if isinstance(new_refresh, str) and new_refresh:
        refreshed["refresh_token"] = new_refresh
    refreshed["expires_at"] = _now() + _expires_in(response_map.get("expires_in", 3600))
    _store_token(host, refreshed)
    return refreshed


def authorization_for_host(host: str) -> tuple[str, str] | None:
    """Return ``(cloud_id, access_token)`` for a connected Cloud host."""
    resolved = _normalize_host(host)
    record = _read_token(resolved)
    if not record:
        return None
    access_token = record.get("access_token")
    cloud_id = record.get("cloud_id")
    expires_at = record.get("expires_at", 0)
    if not isinstance(access_token, str) or not isinstance(cloud_id, str):
        return None
    try:
        expired = _expires_in(expires_at) <= _now() + _REFRESH_SKEW_SECONDS
    except (TypeError, ValueError):
        expired = True
    if not expired:
        return cloud_id, access_token
    with _refresh_lock:
        latest = _read_token(resolved)
        if not latest:
            return None
        try:
            latest_expired = (
                _expires_in(latest.get("expires_at", 0)) <= _now() + _REFRESH_SKEW_SECONDS
            )
        except (TypeError, ValueError):
            latest_expired = True
        refreshed = _refresh(resolved, latest) if latest_expired else latest
    new_access = refreshed.get("access_token")
    new_cloud = refreshed.get("cloud_id")
    if not isinstance(new_access, str) or not isinstance(new_cloud, str):
        raise ProviderSetupError(
            "Jira OAuth authorization is invalid; authorize again", reason="oauth_reauthorize"
        )
    return new_cloud, new_access


def status(host: str) -> dict[str, object]:
    """Return a secret-free OAuth status payload for the connect UI."""
    resolved = _normalize_host(host)
    record = _read_token(resolved)
    return {"host": resolved, "connected": bool(record and record.get("cloud_id"))}


def revoke(host: str) -> bool:
    """Forget the local grant; provider-side revocation remains Atlassian-owned."""
    resolved = _normalize_host(host)
    with (
        _lock_path().open("a+", encoding="utf-8") as lock_file,
        platform_compat.file_lock(lock_file.fileno(), exclusive=True, required=True),
    ):
        tokens = _read_tokens()
        removed = tokens.pop(resolved, None) is not None
        if removed:
            atomic_write(
                _token_path(),
                json.dumps(tokens, indent=2, sort_keys=True),
                fsync=True,
                restrict_to_owner=True,
            )
    if removed:
        _audit("revoked", resolved)
    return removed
