#!/usr/bin/env python3
"""Source adapter — normalize a GitHub PR link into a single ``ReviewTarget``.

The brain only ever sees a ``ReviewTarget``. Adding a new platform later is a new
adapter, not a brain change. This build ships the **GitHub PR** adapter only.

The network fetch itself is performed by the pipeline via the ``gh`` CLI; this
module is the deterministic, token-free part: parsing the fetched payload into a
``ReviewTarget``.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from sage_lib import store

# Matches the PR path grammar shared by github.com and GitHub Enterprise Server:
# /<owner>/<repo>/pull/<number>[...]. Applied to the PARSED URL path only, AFTER
# the hostname allowlist check — never to the raw link.
_PR_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)")
# Matches the GitLab merge-request path grammar (self-hosted and gitlab.com):
# /<namespace>/<project>/-/merge_requests/<iid>[...]. GitLab nests groups
# (group/subgroup), so the project path is everything before `/-/merge_requests/<n>`.
_MR_PATH_RE = re.compile(r"^(/.+?)/-/merge_requests/(\d+)")
# A change is a "fix" if its title/description signals a bug/revert/incident.
FIX_RE = re.compile(r"\b(fix(es|ed)?|revert(s|ed)?|bug|hotfix|regression|incident|patch)\b", re.I)
# GitHub-style issue reference (e.g. "#204") linked from the PR body.
GH_ISSUE_RE = re.compile(r"#(\d+)")


class AdapterError(ValueError):
    """Base class for adapter failures (fail-fast)."""


class UnsupportedPlatform(AdapterError):
    """The link's platform is not supported in this build."""


class AdapterParseError(AdapterError):
    """The fetched payload could not be normalized into a ReviewTarget."""


@dataclass
class ReviewTarget:
    """The single normalized shape the review brain consumes."""

    platform: str
    repo_identity: str          # host/org/repo — the learning key
    change_id: str
    url: str
    title: str = ""
    description: str = ""
    linked_issue: str = ""
    author: str = ""
    target_branch: str = ""
    revision: str = ""
    files: list[dict] = field(default_factory=list)        # [{path, diff}]
    existing_comments: list[dict] = field(default_factory=list)
    design_discussion: list[dict] = field(default_factory=list)
    is_fix: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

# Canonical public-GitHub hostnames. Named constants (rather than inline
# literals) keep membership tests on the parsed-host SET from reading as
# URL-substring checks — the sets these appear in hold bare hostnames, never
# URLs.
_GITHUB_HOST = "github.com"
_WWW_GITHUB_HOST = "www.github.com"
# Canonical public-GitLab hostname. Named constant (rather than an inline literal)
# keeps membership tests on the parsed-host SET from reading as URL-substring checks.
_GITLAB_HOST = "gitlab.com"
_WWW_GITLAB_HOST = "www.gitlab.com"


def canonical_host(host: str) -> str:
    """``www.github.com`` -> ``github.com``; otherwise the lowercased hostname.

    Persisted identities (``repo_identity``, change ids, reviewed keys) use the
    canonical form so the two spellings of github.com map to one record."""
    h = (host or "").strip().lower()
    if h == _WWW_GITHUB_HOST:
        return _GITHUB_HOST
    if h == _WWW_GITLAB_HOST:
        return _GITLAB_HOST
    return h


def _urlparse_host_path(text: str) -> tuple[str, str]:
    """Parse ``text`` into ``(hostname, path)``, reading a malformed URL as
    having neither.

    ``urlparse`` raises ``ValueError`` on some malformed input (an unmatched
    ``[`` is "Invalid IPv6 URL"), and ``.hostname`` can raise on a malformed
    netloc — both reachable from user-pasted link text, where one bad link must
    read as "not a link" (default-deny) rather than crash the whole request."""
    try:
        parsed = urlparse(text)
        return (parsed.hostname or "").lower(), parsed.path or ""
    except ValueError:
        return "", ""


def allowed_hosts(config: dict | None = None) -> frozenset[str]:
    """The exact set of HOSTNAMES accepted as GitHub-API-compatible.

    The single resolution point for "which hosts are acceptable". Sourced from
    ``config.json``'s ``github_hosts`` list — GitHub Enterprise Server hosts are
    opt-in, mirroring ``gh auth login --hostname`` — and defaults to github.com,
    so behaviour is unchanged when nothing is configured. ``www.github.com`` is
    accepted whenever ``github.com`` is.

    Membership tests against this set MUST use the PARSED URL hostname and
    exact equality — never a substring, suffix, or regex-on-raw-URL test — so
    ``notgithub.com``, ``github.com.evil.example``, and a permitted host that
    appears only in a URL's path are all refused."""
    return _hosts_from_config(config, "github_hosts",
                              {_GITHUB_HOST, _WWW_GITHUB_HOST},
                              store.DEFAULT_GITHUB_HOSTS)


def gitlab_allowed_hosts(config: dict | None = None) -> frozenset[str]:
    """The exact set of hostnames accepted as GitLab-API-compatible.

    Sibling of ``allowed_hosts`` (GitHub): sourced from ``config.json``'s
    ``gitlab_hosts`` list — self-hosted GitLab instances are opt-in, mirroring
    ``glab auth login --hostname`` — and defaults to gitlab.com. ``www.gitlab.com``
    is accepted whenever ``gitlab.com`` is. Same exact-match semantics as the
    GitHub allowlist (parse to components, default-deny)."""
    return _hosts_from_config(config, "gitlab_hosts",
                              {_GITLAB_HOST, _WWW_GITLAB_HOST},
                              store.DEFAULT_GITLAB_HOSTS)


def _hosts_from_config(config: dict | None, key: str, canon: set[str],
                       default: list[str]) -> frozenset[str]:
    """Shared host-allowlist resolution for GitHub and GitLab (identical shape).

    Reads ``config[key]`` (a ``list[str]`` of bare hostnames, URLs tolerated),
    canonicalizes www→bare for the platform's canonical set, and falls back to
    the platform default when unset/empty. Encapsulated so the two platforms'
    allowlists cannot drift in semantics."""
    cfg = config if config is not None else store.read_config_quiet()
    raw = cfg.get(key) if isinstance(cfg, dict) else None
    hosts: set[str] = set()
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            h = str(entry or "").strip().lower()
            if "://" in h:  # tolerate a pasted URL in config (malformed -> skipped)
                h = _urlparse_host_path(h)[0]
            h = h.strip("/").rstrip(".")
            if h:
                hosts.add(h)
    if not hosts:
        hosts = set(default)
    if hosts & canon:
        hosts.update(canon)
    return frozenset(hosts)


def detect_platform(link: str, *, config: dict | None = None) -> str:
    """Return ``github`` or ``gitlab`` for a PR/MR link on an allowed host, else
    raise UnsupportedPlatform.

    The host is validated against the GitHub or GitLab allowlist by EXACT
    match of the PARSED URL hostname (not a substring of the raw link), so a
    URL where an allowed host merely appears in the path/query/userinfo (e.g.
    ``https://evil.example/github.com/x/pull/1``) or as a spoofable
    prefix/suffix (``notgithub.com``, ``github.com.evil.example``) is rejected,
    and a malformed URL reads as unsupported rather than raising ``ValueError``.
    Aligns with SSRF/allowlist guidance (parse to components, default-deny).

    Platform is decided by path grammar on an allowed host: ``/pull/<n>`` on a
    GitHub allowlisted host is ``github``, ``/-/merge_requests/<n>`` on a GitLab
    allowlisted host is ``gitlab``. A host present on BOTH allowlists with a
    matching path resolves by the path grammar (a GitLab path on a GitHub
    allowlisted host is refused)."""
    if not link or not isinstance(link, str):
        raise UnsupportedPlatform("empty or non-string link")
    host, path = _urlparse_host_path(link)
    if host in allowed_hosts(config) and "/pull/" in path:
        return "github"
    if host in gitlab_allowed_hosts(config) and "/-/merge_requests/" in path:
        return "gitlab"
    raise UnsupportedPlatform(
        f"unsupported link/platform: {link!r} (expected a GitHub PR or GitLab MR URL)")


def _sanitize_seg(s: str) -> str:
    """Make an owner/repo segment safe for use in a change-id (which names a
    result record on disk). Non ``[A-Za-z0-9.]`` chars — including ``-`` — become
    ``_``. Excluding ``-`` is deliberate: ``-`` is the segment delimiter in
    ``github_change_id`` (``GH-<owner>-<repo>-<n>``), so keeping it inside a
    segment would make different owner/repo pairs collide (e.g. ``a-b``/``c`` vs
    ``a``/``b-c``). Stripping it to ``_`` keeps ``-`` unambiguous as the delimiter."""
    return re.sub(r"[^A-Za-z0-9.]", "_", str(s or "")).strip("_") or "unknown"


def github_pr_ref(link: str, *, config: dict | None = None) -> tuple[str, str, str, str]:
    """Parse ``(host, owner, repo, number)`` from a PR URL on an allowed GitHub
    host. Fails fast.

    Host membership uses the same PARSED-hostname exact-match allowlist as
    ``detect_platform`` (never a substring of the raw link). The host comes
    back canonicalized (``www.github.com`` -> ``github.com``) so identities
    derived from it are stable. A scheme-less link (``github.com/o/r/pull/1``)
    is tolerated by retrying with ``https://``; a malformed link is rejected
    like any other non-PR link (never a ``ValueError`` out of ``urlparse``)."""
    if not link or not isinstance(link, str):
        raise AdapterParseError(f"not a GitHub PR link: {link!r}")
    text = link.strip()
    host, path = _urlparse_host_path(text)
    if not host and "://" not in text:
        host, path = _urlparse_host_path("https://" + text)
    if host not in allowed_hosts(config):
        raise AdapterParseError(f"not a GitHub PR link: {link!r}")
    m = _PR_PATH_RE.match(path)
    if not m:
        raise AdapterParseError(f"not a GitHub PR link: {link!r}")
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    repo = re.sub(r"\.git$", "", repo)  # tolerate a trailing .git
    return canonical_host(host), owner, repo, number


def github_pr_parts(link: str) -> tuple[str, str, str]:
    """Parse ``(owner, repo, number)`` from a PR URL on an allowed GitHub host.
    Fails fast. Callers that need the host use ``github_pr_ref``."""
    _host, owner, repo, number = github_pr_ref(link)
    return owner, repo, number


def gitlab_pr_ref(link: str, *, config: dict | None = None) -> tuple[str, str, str]:
    """Parse ``(host, namespace, iid)`` from a GitLab MR URL on an
    allowed GitLab host. Fails fast.

    GitLab nests groups, so the project path is EVERYTHING before
    ``/-/merge_requests/<n>`` (e.g. ``group/subgroup/project``); it is kept
    verbatim (not split on ``/``) because the API addresses it as a single
    URL-escaped ``project_id`` (``group%2Fsubgroup%2Fproject``). Host membership
    uses the same PARSED-hostname exact-match allowlist as ``detect_platform``;
    a scheme-less link is tolerated by retrying with ``https://``."""
    if not link or not isinstance(link, str):
        raise AdapterParseError(f"not a GitLab MR link: {link!r}")
    text = link.strip()
    host, path = _urlparse_host_path(text)
    if not host and "://" not in text:
        host, path = _urlparse_host_path("https://" + text)
    if host not in gitlab_allowed_hosts(config):
        raise AdapterParseError(f"not a GitLab MR link: {link!r}")
    m = _MR_PATH_RE.match(path)
    if not m:
        raise AdapterParseError(f"not a GitLab MR link: {link!r}")
    namespace, iid = m.group(1).lstrip("/"), m.group(2)
    if not namespace or "." in namespace.split("/")[0]:
        raise AdapterParseError(f"not a GitLab MR link: {link!r}")
    return canonical_host(host), namespace, iid


def gitlab_pr_parts(link: str) -> tuple[str, str]:
    """Parse ``(namespace, iid)`` from a GitLab MR URL. Callers that need the
    host use ``gitlab_pr_ref``."""
    _host, namespace, iid = gitlab_pr_ref(link)
    return namespace, iid


def link_names_a_host(link: str) -> bool:
    """Whether ``link`` plausibly NAMES a network host — an explicit
    ``scheme://`` form (even with an unparseable host) or a leading
    domain-shaped segment (``ghe.corp/…``).

    Callers use this to distinguish a URL whose host failed validation (must
    FAIL CLOSED — routing it at a default host could cross GitHub instances)
    from a bare legacy change token (``CR-1``) that carries no host to cross
    to. Deliberately over-matches: a dot in the first segment reads as a
    domain, because refusing is the safe side."""
    if not link or not isinstance(link, str):
        return False
    text = link.strip()
    if _urlparse_host_path(text)[0]:
        return True
    if "://" in text:  # a scheme is present but the host is unparseable
        return True
    return "." in text.split("/", 1)[0]


def parse_gitlab_repo_ref(link: str, *, config: dict | None = None) -> tuple[str, str, str]:
    """Parse an allowlisted GitLab URL as ``(host, namespace, project)``."""
    if not link or not isinstance(link, str):
        raise UnsupportedPlatform("empty or non-string repo link")
    host, path = _urlparse_host_path(link)
    hosts = gitlab_allowed_hosts(config)
    if host not in hosts:
        raise UnsupportedPlatform(f"unsupported GitLab repo host: {link!r}")
    if "/-/" in path:
        path = path.split("/-/", 1)[0]
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise AdapterParseError(f"not a GitLab repo link: {link!r}")
    parts[-1] = re.sub(r"\.git$", "", parts[-1])
    segment = re.compile(r"^[A-Za-z0-9._-]+$")
    if any(part in (".", "..") or not segment.match(part) for part in parts):
        raise AdapterParseError(f"invalid GitLab namespace/project in {link!r}")
    return canonical_host(host), "/".join(parts[:-1]), parts[-1]


def parse_any_repo_ref(link: str, *, config: dict | None = None) -> tuple[str, str, str, str]:
    """Parse a GitHub or GitLab repo URL as ``(provider, host, owner, repo)``."""
    host, _path = _urlparse_host_path(link if isinstance(link, str) else "")
    if host in allowed_hosts(config):
        parsed_host, owner, repo = parse_repo_ref(link, config=config)
        return "github", parsed_host, owner, repo
    parsed_host, namespace, project = parse_gitlab_repo_ref(link, config=config)
    return "gitlab", parsed_host, namespace, project


def parse_repo_ref(link: str, *, config: dict | None = None) -> tuple[str, str, str]:
    """Parse ``(host, owner, repo)`` from a GitHub REPO URL (no ``/pull/``).

    Mirrors ``detect_platform``'s PARSED-hostname allowlist (default-deny,
    SSRF/allowlist guidance) but accepts a bare repo URL like
    ``https://github.com/<owner>/<repo>`` so a batch of that repo's open PRs can
    be enumerated. Raises ``UnsupportedPlatform`` for a host outside the
    allowlist (including a malformed URL, which parses to no host) and
    ``AdapterParseError`` when the owner/repo path segments are missing."""
    if not link or not isinstance(link, str):
        raise UnsupportedPlatform("empty or non-string repo link")
    host, path = _urlparse_host_path(link)
    hosts = allowed_hosts(config)
    if host not in hosts:
        raise UnsupportedPlatform(
            f"unsupported repo host: {link!r} "
            f"(expected a repo URL on one of: {', '.join(sorted(hosts))})")
    if "/pull/" in path:
        # A PR URL, not a repo URL — route the user to the paste flow so we don't
        # silently review the PR's whole repo.
        raise AdapterParseError(
            f"that's a PR URL, not a repo URL: {link!r} (paste it in the PR box)")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise AdapterParseError(f"not a GitHub repo link: {link!r}")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    _seg = re.compile(r"^[A-Za-z0-9._-]+$")
    if owner in (".", "..") or repo in (".", "..") or not (_seg.match(owner) and _seg.match(repo)):
        raise AdapterParseError(f"invalid owner/repo in {link!r}")
    return canonical_host(host), owner, repo


def parse_repo_url(link: str) -> tuple[str, str]:
    """Parse ``(owner, repo)`` from a GitHub REPO URL (no ``/pull/``). Callers
    that need the host use ``parse_repo_ref``."""
    _host, owner, repo = parse_repo_ref(link)
    return owner, repo


def github_change_id(owner: str, repo: str, number: str | int,
                     host: str = "github.com") -> str:
    """Filesystem-safe, platform-namespaced change id: ``GH-<owner>-<repo>-<n>``.
    Unlike a raw URL, this is a valid filename.

    A non-github.com (GitHub Enterprise) host gets a leading sanitized host
    segment (``GH-<host>-<owner>-<repo>-<n>``) so the same owner/repo/number on
    two hosts cannot share one result file. github.com ids keep the host-less
    shape byte-identical so already-persisted records still resolve."""
    h = canonical_host(host)
    prefix = f"GH-{_sanitize_seg(h)}-" if h and h != "github.com" else "GH-"
    return f"{prefix}{_sanitize_seg(owner)}-{_sanitize_seg(repo)}-{number}"


def github_review_key(owner: str, repo: str, number: str | int,
                      host: str = "github.com") -> str:
    """Collision-free canonical identity for the durable reviewed-index key.

    Distinct from ``github_change_id``: that value ALSO names an on-disk result
    file, so it runs owner/repo through ``_sanitize_seg`` — which collapses ``-``
    to ``_`` to keep ``-`` unambiguous as its segment delimiter. That sanitization
    is lossy: ``acme/service-api`` and ``acme/service_api`` both become
    ``GH-acme-service_api-<n>``, so two DIFFERENT repos with the same PR number
    shared one ``reviewed.json`` key and clobbered each other's dedup record —
    silently skipping a requested review when their PR heads happened to share a
    commit SHA (as mirrored repos can).

    This key never names a file, so it keeps owner/repo verbatim and joins with
    ``/`` (a character GitHub owner/repo can never contain), giving a lossless,
    unambiguous identity. Owner/repo are lower-cased because GitHub treats them
    case-insensitively for identity. The key is HOST-qualified so the same
    owner/repo on two GitHub hosts cannot collide; the github.com default keeps
    already-persisted keys byte-identical."""
    h = canonical_host(host) or "github.com"
    return f"{h}/{str(owner).lower()}/{str(repo).lower()}#{number}"


def gitlab_change_id(namespace: str, iid: str | int, host: str = "gitlab.com") -> str:
    """Filesystem-safe, platform-namespaced GitLab change id: ``GL-<ns>-<iid>``.

    Calls it a ``change id`` (matching GitHub's ``GH-…``) so the review path,
    results store, and reviewed index treat GitLab records exactly like GitHub
    ones. The project path (``group/subgroup/project``) runs through
    ``_sanitize_seg`` (``/`` and ``-`` → ``_``) so the id is a valid filename;
    a non-gitlab.com (self-hosted) host gets a leading sanitized host segment so
    the same namespace/iid on two hosts cannot share one result file."""
    h = canonical_host(host)
    prefix = f"GL-{_sanitize_seg(h)}-" if h and h != "gitlab.com" else "GL-"
    return f"{prefix}{_sanitize_seg(namespace)}-{iid}"


def gitlab_review_key(namespace: str, iid: str | int, host: str = "gitlab.com") -> str:
    """Collision-free canonical identity for the GitLab reviewed-index key.

    Mirrors ``github_review_key`` (host-qualified, lossless ``/``-joined
    identity) so GitLab dedup records cannot collide across hosts or between
    the lossy ``GL-…`` file name and this key."""
    h = canonical_host(host) or "gitlab.com"
    return f"{h}/{str(namespace).lower()}#{iid}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _author_alias(raw: dict) -> str:
    a = _first(raw, "author", "authorAlias", "owner", default="")
    if isinstance(a, dict):
        return _first(a, "alias", "login", "name", default="")
    return str(a) if a else ""


def detect_is_fix(title: str, description: str) -> bool:
    return bool(FIX_RE.search(f"{title}\n{description}"))


def extract_linked_issue(text: str) -> str:
    """Extract a linked GitHub issue reference (``#123``) from the PR body."""
    m = GH_ISSUE_RE.search(text or "")
    return f"#{m.group(1)}" if m else ""


# ---------------------------------------------------------------------------
# GitHub adapter
# ---------------------------------------------------------------------------

def parse_github_payload(raw: dict | str, *, link: str | None = None) -> ReviewTarget:
    """Normalize a GitHub PR payload into a ReviewTarget. The worker assembles
    this payload from ``gh api``: the ``pulls/{n}`` object merged with a ``files``
    array (each carrying its per-file ``patch``) and optional ``comments``. Tolerant
    of field-name variants (``filename``/``path``, ``patch``/``diff``); fails fast
    when there is no usable content. ``owner``/``repo``/``number`` are taken from
    the payload (``base.repo.full_name`` + ``number``) and fall back to the link/
    ``html_url`` so the adapter works whether or not the caller echoes the URL."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterParseError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdapterParseError("payload must be a JSON object")

    _base = raw.get("base")
    base: dict = _base if isinstance(_base, dict) else {}
    _head = raw.get("head")
    head: dict = _head if isinstance(_head, dict) else {}
    _base_repo = base.get("repo")
    base_repo: dict = _base_repo if isinstance(_base_repo, dict) else {}

    # NOTE: do NOT fall back to raw["id"] — on GitHub that is the internal
    # database id (e.g. 1847293847), NOT the PR number in the URL. Using it would
    # produce a change_id that mismatches _cid()'s URL-derived id (write/read would
    # hit different result files). The link/html_url fallback below supplies the
    # number when the payload omits it.
    number = _first(raw, "number", default="")
    owner = repo = host = ""
    full = _first(base_repo, "full_name", default="")
    if full and "/" in full:
        owner, repo = full.split("/", 1)

    # Fill any missing part (including the host) from the link, then from
    # html_url. Without a parseable URL the host defaults to github.com.
    html_url = _first(raw, "html_url", "url", default="")
    for candidate in (link, html_url):
        if owner and repo and number and host:
            break
        if not candidate:
            continue
        try:
            lh, lo, lr, ln = github_pr_ref(candidate)
        except AdapterParseError:
            continue
        host = host or lh
        owner = owner or lo
        repo = repo or lr
        number = number or ln
    host = host or "github.com"

    if not (owner and repo and number):
        raise AdapterParseError(
            "could not determine GitHub owner/repo/number from payload or link")

    description = _first(raw, "body", "description", default="")
    title = _first(raw, "title", default="") or (description.splitlines()[0] if description else "")

    raw_files = raw.get("files") or raw.get("diffs") or []
    files: list[dict] = []
    for d in raw_files:
        if not isinstance(d, dict):
            continue
        path = _first(d, "filename", "path", "name", default="")
        diff = _first(d, "patch", "diff", "unifiedDiff", default="")
        if path:
            files.append({"path": path, "diff": diff})

    # Fail fast: a PR with neither files nor a description is unusable.
    if not files and not description:
        raise AdapterParseError("payload has no files and no description")

    # GitHub author lives under user.login (fall back to the generic extractor).
    author = ""
    user = raw.get("user")
    if isinstance(user, dict):
        author = _first(user, "login", "name", default="")
    if not author:
        author = _author_alias(raw)

    revision = (_first(head, "sha", default="")
                or _first(raw, "head_sha", "sha", "revision", default=""))
    target_branch = (_first(base, "ref", default="")
                     or _first(raw, "base_ref", "targetBranch", default=""))

    comments = raw.get("comments") or raw.get("review_comments") or raw.get("allComments") or []
    if not isinstance(comments, list):
        comments = []

    return ReviewTarget(
        platform="github",
        repo_identity=f"{host}/{owner}/{repo}",
        change_id=github_change_id(owner, repo, number, host=host),
        url=html_url or f"https://{host}/{owner}/{repo}/pull/{number}",
        title=title,
        description=description,
        linked_issue=extract_linked_issue(description),
        author=str(author) if author else "",
        target_branch=target_branch,
        revision=str(revision),
        files=files,
        existing_comments=comments,
        design_discussion=[],
        is_fix=detect_is_fix(title, description),
    )


def normalize(link: str, raw_payload: dict | str) -> ReviewTarget:
    """Top-level entry: detect platform, then parse. Fails fast on unsupported."""
    platform = detect_platform(link)
    if platform == "github":
        return parse_github_payload(raw_payload, link=link)
    if platform == "gitlab":
        return parse_gitlab_payload(raw_payload, link=link)
    raise UnsupportedPlatform(f"unsupported platform: {platform!r}")


def parse_gitlab_payload(raw: dict | str, *, link: str | None = None) -> ReviewTarget:
    """Normalize a GitLab MR payload into a ReviewTarget.

    The worker assembles this payload from ``glab api``: the merge-request
    object merged with a ``changes`` array (each carrying ``old_path``/``new_path``
    and ``diff``) and optional ``notes``. Tolerant of field-name variants; fails
    fast when there is no usable content. ``namespace``/``iid`` are taken from
    the payload (``web_url`` / ``iid``) and fall back to the link so the adapter
    works whether or not the caller echoes the URL."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterParseError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdapterParseError("payload must be a JSON object")

    iid = _first(raw, "iid", "number", default="")
    namespace_host = ""

    # GitLab exposes the project path as `web_url` (https://<host>/<ns>/<proj>)
    # and/or `references.full` (`<ns>/<proj>!<iid>`); fall back to the link.
    refs = raw.get("references")
    ref_full = refs.get("full") if isinstance(refs, dict) else ""
    web_url = _first(raw, "web_url", "url", default="")
    namespace = ""
    for candidate in (link, web_url, ref_full):
        if not candidate:
            continue
        try:
            lh, lns, li = gitlab_pr_ref(candidate)
        except AdapterParseError:
            continue
        namespace_host = namespace_host or lh
        namespace = namespace or lns
        iid = iid or li
        break
    if not isinstance(namespace_host, str) or not namespace_host:
        namespace_host = "gitlab.com"

    if not (namespace and iid):
        raise AdapterParseError(
            "could not determine GitLab namespace/MR iid from payload or link")

    description = _first(raw, "description", "body", default="")
    title = _first(raw, "title", default="") or (description.splitlines()[0] if description else "")

    raw_changes = raw.get("changes") or raw.get("diffs") or raw.get("files") or []
    files: list[dict] = []
    for d in raw_changes if isinstance(raw_changes, list) else []:
        if not isinstance(d, dict):
            continue
        path = _first(d, "new_path", "path", "filename", "name", default="")
        diff = _first(d, "diff", "patch", "unifiedDiff", default="")
        if path:
            files.append({"path": path, "diff": diff})

    if not files and not description:
        raise AdapterParseError("payload has no files and no description")

    author = ""
    author_obj = raw.get("author")
    if isinstance(author_obj, dict):
        author = _first(author_obj, "username", "name", "login", default="")
    if not author:
        author = _author_alias(raw)

    revision = (_first(raw, "sha", "diff_refs", default="")
                or _first(raw, "head_sha", "revision", default=""))
    if isinstance(revision, dict):
        revision = _first(revision, "head_sha", "sha", default="")
    target_branch = (_first(raw, "target_branch", "targetBranch", "base_ref",
                            default=""))

    comments = raw.get("notes") or raw.get("comments") or raw.get("allComments") or []
    if not isinstance(comments, list):
        comments = []

    return ReviewTarget(
        platform="gitlab",
        repo_identity=f"{namespace_host}/{namespace}",
        change_id=gitlab_change_id(namespace, iid, host=namespace_host),
        url=web_url or f"https://{namespace_host}/{namespace}/-/merge_requests/{iid}",
        title=title,
        description=description,
        linked_issue=extract_linked_issue(description),
        author=str(author) if author else "",
        target_branch=target_branch,
        revision=str(revision),
        files=files,
        existing_comments=comments,
        design_discussion=[],
        is_fix=detect_is_fix(title, description),
    )


def validate_review_target(target: ReviewTarget) -> list[str]:
    """Non-fatal warnings about a normalized target — surfaces likely GitHub
    payload-mapping gaps before review. Empty == looks complete."""
    warns: list[str] = []
    if not target.files:
        warns.append("no files/diffs parsed — check the payload's `files` mapping")
    else:
        if not any(f.get("diff") for f in target.files):
            warns.append("files present but all diffs are empty — check the `patch` field name")
    if not target.title and not target.description:
        warns.append("no title or description — check the payload field names")
    if not target.target_branch:
        warns.append("no target branch parsed (branch-gate checks will be skipped)")
    if target.repo_identity.endswith("/unknown"):
        warns.append("repo could not be determined — learning key will be 'unknown'")
    return warns
