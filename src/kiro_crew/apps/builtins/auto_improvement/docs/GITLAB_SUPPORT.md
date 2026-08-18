# Auto-Improvement — GitLab support design

Scope: make the auto-improvement builtin app work against **GitLab**
repositories — `gitlab.com` and self-managed instances on the operator's
`dashboard.gitlab_hosts` allowlist — in addition to the current GitHub-only
target. Drafted MRs instead of PRs, `glab` instead of `gh`, MR-status/CI reads
through the existing provider-neutral reader.

The port's thesis, mirroring `PORT_PLAN.md`: **the spine is already
target-agnostic; the GitHub coupling lives in exactly five spots** (clone/setup,
the PR recipe, the profile registry, the routes wiring, and the frontend
copy/links), and GitLab read infrastructure already exists in Kiro Crew
(`source_providers`, `issue_radar.gitlab_client`). Only MR **creation** is
genuinely new code.

## What the app does today (one paragraph)

Measurement-first self-improvement loop. It builds and proves a metric (the
"ruler") against a target repo, then runs keep-or-revert cycles: discover →
propose in parallel worktrees → deterministically gate → measure A/B (perf) or
RED/GREEN (bug) → keep only if the win clears the noise band → **draft** a
reviewable change. GitHub is the only code host today.

## Coupling inventory — GitHub-only today

| # | Module | Coupling | GitLab change |
|---|---|---|---|
| 1 | `backend/clone_setup.py` | `_ALLOWED_HOSTS = {github.com}`, `_GITHUB_RE`, `_gh_prefers_ssh`, `validate_target_url` errors, `_ALLOWED_REMOTE_HOSTS = {github.com}`, `_remote_slug` (owner/repo only), `resolve_origin_url` host checks | Host detection + GitLab URL parse + gitlab remote allowlist + nested-namespace slug |
| 2 | `profiles/github_repo/pr_recipe.py` | `GitHubPRRecipe`: `gh pr create --draft`, `_PR_URL_RE` (github.com), `_HTTPS_REMOTE_RE`, `_gh_prefers_ssh` | New `GitLabPRRecipe` (`glab mr create --draft`), MR URL extraction, host-aware transport |
| 3 | `profiles/__init__.py` | `PROFILE_IDS = ("github-repo",)`; `build_profile` hard-wires `github_repo` | Register `gitlab-repo`; dispatch on provider |
| 4 | `profiles/github_repo/profile.py` | `GitHubRepoProfile` hard-codes `GitHubPRRecipe` (L1556) | Recipe injected by dispatch; class itself is host-agnostic |
| 5 | `backend/routes.py` | imports `GitHubPRRecipe` (L30); reads `githubUser` (L845); constructs recipe in the draft-publish path; `POST /setup-clone` returns GitHub-shaped `_ok()` dict | Recipe factory by provider; `provider`/`host` persisted at setup; `gitlabUser` |
| 6 | Frontend | `AutoImprovementPage.tsx:126` hard-codes `https://github.com/<repo>/commit/<sha>`; `SetupPanel.tsx:194` GitHub placeholder; no profile/provider selector; i18n copy ("GitHub repository to improve", manifest highlights) | Provider/host-aware commit URL; neutral placeholder + provider badge; i18n |
| 7 | Manifest/docs | `app.json` tag `"github"`, `dependencies.commands: ["git","gh"]`, description; README/MANUAL GitHub-only | Add `glab`, generalize copy |
| 8 | Tests | `test_pr_recipe.py` (gh flags, github URL extraction — **asserts non-GitHub URLs are rejected**), `test_github_profile.py`, `test_dogfood_learnings.py` host/slug checks, `test_pr_watchers.py` GitHub-shaped status dicts | GitLab-variant tests (below) |

**Already provider-neutral** (verified): `spine/*` (no host references), `backend/pr_checks.py` (lazy-imports `kiro_crew.dashboard.handlers.source_providers`, already GitLab-aware: `allow_failure`, `state`-vs-`conclusion`), `backend/pr_watchers.py` (`is_watchable_pr` already matches `/-/merge_requests/N`; `assert_origin_neutralized` host-agnostic), `backend/commit.py`, `profile.py`'s ruler/gate/isolation/allowlist (the edit allowlist already excludes `.gitlab-ci.yml`), `clone_setup._disable_push` / `checkout_branch` / `list_clone_branches`.

## Design decisions

### D1. Provider is derived from the target URL and persisted at setup

`POST /setup-clone` is the only writer of `clone`/`target_url`/`origin_url`
today; it stays the only writer, and additionally persists **`provider`**
(`"github"` | `"gitlab"`) and **`host`** (the GitLab instance host, or
`github.com`). Provider is never guessed later: every downstream consumer
(`build_profile`, recipe factory, frontend link builder) reads it from config.
A hand-edited config that omits `provider` falls back to re-deriving it from the
validated `target_url` (fail closed — unknown/unparseable → `"github"` legacy
behavior, matching today's default).

### D2. Clone/setup reuses the battle-tested GitLab parser + host allowlist

`issue_radar.backend.gitlab_client` already ships the security-critical pieces:
`parse_gitlab_repo_url(link, allowed_hosts=...) -> (host, namespace, project)`
(HTTPS-only, no userinfo, host allowlist = `gitlab.com` ∪
`dashboard.gitlab_hosts`, nested groups, `/-/` page-marker stripping,
reserved-segment and charset checks, SSRF-guarded) and `allowed_hosts()`
(loads the operator's allowlist from config). Auto-improvement **lazy-imports
these inside the validation function** — the same pattern `pr_checks.py` uses
for `source_providers` (documented exception to `top-level-imports`). No
duplicated parser, no import cycle (`gitlab_client` imports only
`apps.registry`/`config.loader`/`sel`).

`validate_target_url` becomes a dispatcher:

* host `github.com`/`www.github.com` → existing strict `_GITHUB_RE` path,
  unchanged behavior, `provider="github"`, `host="github.com"`;
* host on the GitLab allowlist → `parse_gitlab_repo_url` → clone URL
  `https://<host>/<namespace>/<project>.git` (HTTPS always — no SSH
  transport-preference dance for GitLab; `glab` auth flows through
  `GLAB_CONFIG_DIR`/git credential helper), `provider="gitlab"`,
  `host=<resolved host>`;
* anything else → same refusal today gives, with a GitLab-aware message.

`_host_is_blocked` (DNS-rebinding defense-in-depth) applies to both providers.
`CloneSpec` gains `provider` and `host` fields; `dir_name` for GitLab
namespaces with `/` uses `--` separators (e.g. `group--subgroup--project`).

### D3. GitLab remote allowlist is the config allowlist, not a constant

`_ALLOWED_REMOTE_HOSTS` today is a module constant `{github.com}`. For GitLab
the allowed remote hosts are `github.com` ∪ `gitlab.com` ∪ the operator's
`dashboard.gitlab_hosts`. The remote validation helpers
(`_is_allowed_remote`, `_remote_slug`) become provider-aware:

* `_remote_slug` generalizes from `owner/repo` to **all-but-last-path-segment /
  last segment** — a GitLab nested namespace `group/subgroup/project` slugs to
  `group/subgroup/project` (and GitHub `owner/repo` is the 2-segment case of the
  same rule);
* `_is_allowed_remote` accepts any host on the union allowlist (constant +
  config-loaded, checked via `gitlab_client.allowed_hosts()`, exact host match,
  never `endswith`);
* `resolve_origin_url` identity-pinning logic is unchanged — it compares slugs
  — so nested GitLab namespaces are pinned correctly with the generalized slug.

### D4. MR creation is a new `GitLabPRRecipe` mirroring `GitHubPRRecipe`

No `glab mr create` exists anywhere in the repo today (verified by grep) — this
is the only genuinely new host code. `GitLabPRRecipe` lives in
`profiles/gitlab_repo/pr_recipe.py` and satisfies the same spine `PRRecipe`
protocol (`namespace` + `draft`). The whole fail-closed machinery is shared by
extraction into a common base or by composition:

* `_push_fix_branch` (push the one generated branch to the pinned fetch URL,
  `--force-with-lease`), `_authorize` (spine denylist), `_scan_pushable_content`
  (fail-closed credential scan), `_scannable_base`, prose redaction,
  queue degradation (`QUEUED:<fp>`) — **host-agnostic, reused verbatim**;
* GitLab-specific parts: `glab mr create --draft --source-branch <branch>
  --target-branch <base> --title <summary> --description-file <body>` (flag
  names verified against `glab mr create --help` at implementation time), MR URL
  extraction `https://<host>/<namespace>/<project>/-/merge_requests/<iid>`;
* host routing follows the codebase's established glab pattern (verified in
  `source_providers`/`gitlab_client`): the resolved host is pinned in the child
  env as **`GITLAB_HOST`** (never `--hostname`, never defaulted to gitlab.com),
  `GITLAB_TOKEN` is withheld for self-managed hosts, `GLAB_CONFIG_DIR` passes
  through, and the `glab` binary is resolved via `validate_provider_executable`
  (override `KIROCREW_GLAB_BIN`, strict `KIROCREW_PROVIDER_BIN_STRICT=1`);
* branch namespace stays `auto-improvement/<kind>-<fingerprint>` (valid for
  GitLab refs too).

Draft-only is preserved: `glab mr create --draft` is the mechanical half; the
recipe never publishes, marks ready, or merges. The `publish_if_authorized`
analog for GitLab (`glab mr update --ready`? — verified at implementation)
keeps the same "never merge, fail-closed gate" pin.

### D5. Profile registry dispatches on provider; one shared repo profile class

`profiles/__init__.py`:

```python
PROFILE_IDS = ("github-repo", "gitlab-repo")

def build_profile(config):
    provider = config.get("provider") or infer_provider(config)  # D1
    if provider == "gitlab":
        from .gitlab_repo.profile import build_profile as b
        return b(config)
    from .github_repo.profile import build_profile as b
    return b(config)
```

`GitHubRepoProfile`'s assembly (ruler, gates, isolation, allowlist, calibration)
is host-agnostic; only field ⑤ (the PR recipe) differs. `github_repo/profile.py`
gains a `recipe_factory`/`recipe` parameter (default `GitHubPRRecipe`), and the
new tiny `gitlab_repo/profile.py` reuses the same assembler with
`GitLabPRRecipe`. The `profile` config key (writable, previously ignored)
becomes meaningful: `"gitlab-repo"` requires `provider == "gitlab"`, otherwise
falls back to the provider-derived profile (a stale value must never brick the
Start button — same policy as today).

### D6. Config schema

| Key | Written by | Read by | Change |
|---|---|---|---|
| `provider` | `POST /setup-clone` only | profile dispatch, recipe factory, frontend | **new** |
| `host` | `POST /setup-clone` only | recipe factory, frontend commit/MR links, watcher env | **new** |
| `gitlabUser` | PUT /config | recipe namespace label | **new** (display-only, like `githubUser`) |
| `githubUser` | PUT /config | unchanged | stays (legacy) |
| `profile` | PUT /config | `build_profile` dispatch | now honored (`github-repo`/`gitlab-repo`) |

All other keys unchanged. `_CONFIG_WRITABLE` gains `gitlabUser` only — `provider`
and `host` move **only** through `setup-clone` (same allowlist philosophy as
`clone`/`target_url`).

### D7. Routes wiring

* `POST /setup-clone` → persists `provider` + `host` alongside `target_url`/
  `origin_url`; 409-mid-run guard unchanged;
* draft-publish path: a small `_recipe_for(config)` factory replaces the direct
  `GitHubPRRecipe` construction (L844) and picks `GitHubPRRecipe`/
  `GitLabPRRecipe` from `config["provider"]` (+ host for glab), reading
  `gitlabUser`/`githubUser` accordingly;
* `GET /config` already returns the raw JSON — `provider`/`host` flow to the
  frontend for free;
* watcher runner env: for GitLab MRs the watcher agent needs `glab` in its env
  (host auth + network) — mirror the existing `gh` consent/`watcherAcceptEgressRisk`
  gate; the CLI allowlist (`app.json` `dependencies.commands`) gains `glab`.

### D8. Frontend & i18n

* `AutoImprovementPage.commitUrlOf` builds `https://github.com/<repo>/commit/<sha>`
  today; becomes provider-aware using `GET /config`'s `provider` + `host`:
  GitLab form is `https://<host>/<repo>/-/commit/<sha>`;
* `SetupPanel` placeholder: single neutral field, provider auto-detected on
  Connect (placeholder `https://github.com/owner/repo or
  https://gitlab.com/group/project`); show a `GitLab`/`GitHub` badge next to
  `target_display` once configured; no separate provider selector needed (D1 —
  provider comes from the URL);
* i18n: add `autoImprovement.repoLabel` neutralization ("Repository to improve"
  or keep brand-specific via interpolation), generalize manifest
  description/highlight_4 copy, keep every key in the established structure
  (top-level copy in `en.manual.json`, store metadata under
  `apps.autoImprovement.manifest.*`); `en.context.json` notes updated; all other
  locales get the new keys via the i18n check tooling.

### D9. Manifest & docs

* `app.json`: description "GitHub *or GitLab* repository", tags add `"gitlab"`,
  `dependencies.commands` add `"glab"`, permissions unchanged
  (`/api/source/pull-request` is the provider-neutral reader);
* `README.md` / `MANUAL.md`: document the GitLab connect flow
  (`https://gitlab.com/<group>/<project>` or a self-managed host listed in
  `dashboard.gitlab_hosts`), `glab auth` requirement, MR vocabulary;
  `PORT_PLAN.md` gains a "GitLab port" addendum; this doc is the reference.

## Safety invariants — preserved verbatim

1. **Push-disabled clones** — `DISABLED_NO_PUSH` on both origin URLs, asserted
   before preflight; applies to GitLab clones identically.
2. **Draft-only** — `glab mr create --draft`; never publish/merge/ready
   unattended.
3. **Protected-branch denylist** — non-overridable; unchanged (spine-level).
4. **Edit allowlist** — unchanged; `.gitlab-ci.yml` already excluded.
5. **Do-not-pollute gate** — unchanged.
6. **Second independent reproduce before drafting** — unchanged.
7. **Host allowlist / SSRF** — GitLab hosts reachable only if on
   `dashboard.gitlab_hosts`; DNS-rebinding check applies to both providers;
   remote identity pinned by generalized slug.

## Test plan

* `test_pr_recipe.py` — add `TestGitLabPRRecipe`: `glab` invocation flags
  (`--draft`, `--source-branch`, `--target-branch`, `--title`, description-file),
  MR URL extraction from trailing chatter, **rejects non-GitLab hosts**,
  `--hostname` targeting for self-managed, queue-degradation twins (no glab,
  push-disabled, failed push, unscannable content), shared fail-closed scan
  tests keep running against the shared base. Reuse the fake-CLI
  (`monkeypatch` `shutil.which` + `subprocess.run`) and local-bare-repo
  fixtures.
* `test_dogfood_learnings.py` — `TestTheStoredPushDestinationIsValidated`
  parametrized with GitLab evil hosts (suffix/subdomain confusion against
  `gitlab.com`), nested-namespace slug pinning, `gitlab.com` + self-managed
  accept cases; `setup_safe_clone` GitLab end-to-end (bare remote is
  provider-agnostic — GitLab URL parsing needs a `validate_target_url` unit
  twin).
* `test_profiles_entry_cov80`/`test_github_profile.py` — dispatch tests:
  `provider=gitlab` builds a profile whose `pr_recipe` is `GitLabPRRecipe`,
  `namespace == "gitlab/…"`; unknown/stale `profile` falls back; protocol
  conformance unchanged.
* `test_reconcile_and_publish.py` / `test_pr_watchers.py` — GitLab-shaped
  status dicts (MR URL, `state`, `mergeable`) fed to the already-neutral
  verdict/watcher logic; `is_watchable_pr` MR twin asserted; GitLab
  publish/ready analog "never merges" pin.
* `test_pr_checks.py` — already GitLab-aware; add only if GitLab verdict
  semantics differ (e.g. `merge_when_pipeline_succeeds`).
* Frontend: `commitUrlOf` GitLab form unit test (vitest); i18n key check.
* E2E (Stage 7): a GitLab MR drafted from a real repo with `glab` authenticated
  (Martin's `gitlab.bildungsinnovator.com` or a throwaway project), verifying
  MR creation + status pull, or documented as a manual verification step.

## File-by-file change map

```
backend/clone_setup.py                    # D2, D3 — dispatcher, gitlab parse, remote allowlist, slug
backend/routes.py                         # D6, D7 — persist provider/host, recipe factory, gitlabUser
profiles/__init__.py                      # D5 — registry + dispatch
profiles/github_repo/pr_recipe.py         # D4 — extract shared base (host-agnostic parts)
profiles/github_repo/profile.py           # D5 — recipe injection param
profiles/gitlab_repo/pr_recipe.py         # D4 — NEW GitLabPRRecipe
profiles/gitlab_repo/profile.py           # D5 — NEW thin assembler
app.json                                  # D9
website/src/apps/auto-improvement/*.tsx   # D8
website/src/i18n/locales/en.json          # D8
website/src/i18n/locales/en.manual.json   # D8
website/src/i18n/en.context.json          # D8
README.md, docs/MANUAL.md                 # D9
docs/GITLAB_SUPPORT.md                    # this doc
tests/*                                   # test plan above
```
