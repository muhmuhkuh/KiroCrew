import type {
  ConnectedRepo,
  Crew,
  CrewCounts,
  CrewSettings,
  Issue,
  PullRequest,
  RepoLabel,
  RepoPermissions,
  RepoSettings,
} from "../api";
import type { ListDetailView } from "../../../hooks/useListDetailView";
import type { RepoRef } from "./refLinks";
import type { RefreshPrefs } from "./format";
import type { SourceProvider } from "../api";

// Shared Issue Radar UI types and context contract. Type-only dependencies keep
// the contract importable without creating runtime cycles.

/** The repo the workspace is pointed at.
 *
 * `provider`/`host` are part of the IDENTITY, not decoration: they ride on every
 * request, and a `group/project` path names a different project on gitlab.com
 * than on a self-managed instance. They are optional so a value persisted before
 * GitLab support still loads — absent means public GitHub.
 *
 * Structurally a `RepoRef` (see `apiTypes.ts`), so it can be passed straight to any
 * API call without rebuilding it. */
export interface ActiveRepo {
 owner: string;
 repo: string;
 provider?: SourceProvider;
 host?: string;
}

/** Sort fields the issue list supports. Exported as a list so persisted state
 * can be validated at runtime (a key removed since it was persisted must not
 * survive a reload). */
export const SORT_KEYS = ["number", "updated", "priority"] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export type SortDir = "asc" | "desc";

/** Sort fields the pull-request list supports. Same shape as ``SortKey``. */
export type PrSortKey = "number" | "updated";

/** Which full-page dashboard is showing in the main area. Extend this list
 * (plus the registry in views/registry.tsx) to add a new dashboard — no other
 * shared file needs to change, so views can be built by separate agents. The
 * list is exported so persisted state can be validated at runtime (a tab that
 * was removed since it was persisted must not survive a reload). */
export const DASHBOARD_TABS = ['overview', 'graph', 'tagging', 'pipeline', 'ci-runs', 'reviews'] as const
export type DashboardTab = (typeof DASHBOARD_TABS)[number]

/** Main-area mode: a dashboard page, the issue list + detail split, the pull-
 * request list + detail split, the crew list + crew page split, or the settings
 * page. Each corresponds to one left-rail accordion section.
 *
 * `crews` is a MainView rather than a DashboardTab because a dashboard renders
 * full-width with no list column, and the crews surface needs the list + main
 * split (roster in column 2, the selected crew's page in column 3). */
export type MainView = "dashboard" | "issues" | "pulls" | "crews" | "settings";

/** Which left-rail accordion section is expanded (the others collapse to their
 * title bar). Follows MainView by default; a header click overrides. */
export type ExpandedSection =
 | "dashboards"
 | "filters"
 | "pulls"
 | "crews"
 | "settings";

/** What the crews main area is showing: one crew's own page, or nothing yet.
 * The column-2 crew list drives this — each crew row sets `{kind:'crew'}` with
 * that crew's id. `{kind:'none'}` is the state before a roster has loaded, and on
 * a repo with no crews at all; `context.tsx` opens the first crew as soon as one
 * exists, so it is never a page a user navigates TO. */
export type CrewView = { kind: "none" } | { kind: "crew"; id: string };

/** The `kind` discriminants, as a runtime list, so a persisted `CrewView` can be
 * validated on reload the way `SORT_KEYS` validates a persisted sort key. A
 * structurally valid kind is not enough on its own: `{kind:'crew'}` also carries
 * an id, and a crew that has since been retired (or belongs to another repo) must
 * not survive either — see `context.tsx`, which re-points the selection once the
 * crew list has loaded without it. */
export const CREW_VIEW_KINDS = ["none", "crew"] as const;

/** Chip filters over the crew roster. Independent predicates, NOT a partition:
 * the backend's own tallies are allowed to sum past the crew count (a paused crew
 * holding in-flight work counts in two), so nothing here should treat them as
 * slices of a whole. */
export const CREW_FILTERS = ["all", "working", "paused"] as const;
export type CrewFilter = (typeof CREW_FILTERS)[number];

/** Sort fields offered over the crew roster.
 *
 * Deliberately only three, because `GET /crews` answers with crew RECORDS plus
 * repo-wide tallies and carries no work items: a "busiest" or "least recently
 * active" sort would need one request per crew, or a per-crew summary the payload
 * does not have. These three are answerable from a record alone — `status` is
 * derived by the route and already on it. */
export const CREW_SORT_KEYS = ["status", "name", "created"] as const;
export type CrewSortKey = (typeof CREW_SORT_KEYS)[number];

/** Sub-sections of the General settings page the rail nav can jump to. */
export type GeneralAnchor = 'account' | 'repos' | 'gitlabGroups'

/** What the Settings main area is showing: the shared "General" page (account +
 * connected-repo list), or one specific repo's settings page. The rail's
 * Settings section drives this — General items set `{kind:'general'}`, and each
 * connected repo gets its own `{kind:'repo'}` page. */
export type SettingsTarget =
 | { kind: "general"; anchor?: GeneralAnchor }
 // Carries the provider identity for the same reason `ActiveRepo` does: the
 // settings pane reads and writes THIS repo's settings, and owner/repo alone
 // would address the wrong one on a mixed install.
 | {
    kind: "repo";
    owner: string;
    repo: string;
    provider?: SourceProvider;
    host?: string;
   };

export type StateFilter = "open" | "closed";

/** Pull-request state filter. ``merged`` and ``closed`` both fetch the closed
 * set from GitHub; the frontend splits them on ``merged_at`` (merged = has a
 * merge timestamp; closed = closed WITHOUT being merged). */
export type PrStateFilter = "open" | "closed" | "merged";


export interface IssueRadarContextValue {
  // ── repos ──
  repos: ConnectedRepo[];
  active: ActiveRepo;
  switchRepo: (r: ActiveRepo) => void;
  onAddRepo: () => void;
  /** The active repo's GitHub permissions (null until the repos list loads). */
  activePermissions: RepoPermissions | null;
  /** True when the current gh user can edit issues on the active repo
   * (triage/push/maintain/admin) — gates the label edit + close/reopen UI.
   * A read-only repo degrades to suggest-only (writes are hidden/disabled). */
  canWrite: boolean;

  // ── data ──
  me: string | null;
  issues: Issue[];
  repoLabels: RepoLabel[];
  issuesLoading: boolean;
  /** True while the rendered issues are only the cold-start first page and the
   * full list is still loading behind them (see the progressive first paint). */
  issuesPartial: boolean;
  issuesError: Error | null;
  labelsLoading: boolean;
  labelsError: Error | null;
  refresh: () => void;
  refreshing: boolean;

  // ── per-repo triage settings (for the active repo) ──
  /** The active repo's saved triage settings (defaults until loaded/configured). */
  repoSettings: RepoSettings;
  /** True when an issue counts as "needs triage" under the active repo's config
   * (a configured triage label, or — when enabled — no labels at all). */
  needsTriage: (iss: Issue) => boolean;
  /** True when an issue carries one of the active repo's good-first-issue labels. */
  isGoodFirstIssue: (iss: Issue) => boolean;
  /** Epoch-ms when the issues query last produced data (fetch or refresh);
   * 0 before the first load. Drives the "Updated Nm ago" footer label. */
  issuesUpdatedAt: number;

  // ── derived ──
  colorByName: Map<string, string>;
  countByLabel: Map<string, number>;
  sortedRepoLabels: RepoLabel[];
  /** login -> repo role (admin/maintain/…, or OWNER/MEMBER/COLLABORATOR in the
   * read-only fallback) for repo members, from the cached roster. Lets the
   * detail badge show a member's role instantly and drives the member filter. */
  memberRoleByLogin: Map<string, string>;
  filteredIssues: Issue[];
  sortedIssues: Issue[];
  activeIssue: Issue | null;

  // ── filters / sort ──
  /** Free-text search over the issue list (title, #number, author, labels).
   * A middle-column concern only — folded into filteredIssues/sortedIssues,
   * which nothing outside the list consumes. */
  query: string;
  setQuery: (q: string) => void;
  selectedLabels: Set<string>;
  toggleLabel: (name: string) => void;
  /** Jira status names currently present in the loaded issue set, with counts. */
  sortedIssueStatuses: Array<{ name: string; count: number }>;
  selectedStatuses: Set<string>;
  toggleStatus: (name: string) => void;
  requestedByMe: boolean;
  toggleRequestedByMe: () => void;
  assignedToMe: boolean;
  toggleAssignedToMe: () => void;
  /** Filter the list to issues opened by a repo member (OWNER/MEMBER/
   * COLLABORATOR author association). */
  createdByMember: boolean;
  toggleCreatedByMember: () => void;
  /** True when at least one loaded issue was opened by a repo member — gates
   * the "created by member" filter (disabled when the repo has none). */
  hasMemberIssues: boolean;
  stateFilter: StateFilter;
  setStateFilter: (s: StateFilter) => void;
  anyFilterActive: boolean;
  clearFilters: () => void;
  sortKey: SortKey;
  sortDir: SortDir;
  cycleSort: (key: SortKey) => void;

  // ── selection ──
  selectedIssue: number | null;
  setSelectedIssue: (n: number | null) => void;

  // ── pull requests ──
  pulls: PullRequest[];
  pullsLoading: boolean;
  /** True when `pulls` holds only the cold-open first page (un-enriched) while
   * the full enriched list loads behind it — the PR twin of `issuesPartial`. */
  pullsPartial: boolean;
  pullsError: Error | null;
  refreshPulls: () => void;
  pullsRefreshing: boolean;
  /** Epoch-ms when the pulls query last produced data; 0 before first load. */
  pullsUpdatedAt: number;
  /** True when a per-person PR filter is on, so the list came from GitHub SEARCH
   * (whole-repo, complete for that person) rather than the bounded list. The
   * list footer uses it to drop the "capped at 100" caveat. */
  prPersonFilterActive: boolean;
  /** Set when the SEARCH result itself hit the server's cap, so the footer can say
   * "newest N" — the search escapes the list's page cap but has one of its own,
   * and claiming completeness anyway would just move the original lie. */
  prSearchTruncatedAt: number | null;
  /** Open PR count per label name (drives the PR filter counts). */
  countByPrLabel: Map<string, number>;
  /** Free-text search over the PR list (title, #number, author, branch, labels). */
  prQuery: string;
  setPrQuery: (q: string) => void;
  prSelectedLabels: Set<string>;
  togglePrLabel: (name: string) => void;
  prAuthoredByMe: boolean;
  togglePrAuthoredByMe: () => void;
  prAssignedToMe: boolean;
  togglePrAssignedToMe: () => void;
  prReviewRequestedByMe: boolean;
  togglePrReviewRequestedByMe: () => void;
  prDraftOnly: boolean;
  togglePrDraftOnly: () => void;
  /** Keep only PRs opened by a repo member (roster role, or GitHub's
   * author_association as a fallback) — the PR twin of createdByMember. */
  prCreatedByMember: boolean;
  togglePrCreatedByMember: () => void;
  /** False when the current PR set contains no member-authored PR, so the row
   * can be hidden rather than offering a filter that yields nothing. */
  hasMemberPulls: boolean;
  prStateFilter: PrStateFilter;
  setPrStateFilter: (s: PrStateFilter) => void;
  anyPrFilterActive: boolean;
  clearPrFilters: () => void;
  prSortKey: PrSortKey;
  prSortDir: SortDir;
  cyclePrSort: (key: PrSortKey) => void;
  selectedPull: number | null;
  setSelectedPull: (n: number | null) => void;
  filteredPulls: PullRequest[];
  sortedPulls: PullRequest[];
  activePull: PullRequest | null;

  // ── bulk PR selection (transient) ──
  /** The PR numbers ticked for a mass action. Deliberately NOT persisted: a
   * restored selection would let a later visit apply an action to rows the user
   * ticked in a different sitting and has since forgotten. */
  checkedPulls: Set<number>;
  /** Tick/untick one PR. */
  togglePullChecked: (n: number) => void;
  /** Tick every PR currently RENDERED (the filtered+sorted set), or clear them
   * all when they are already ticked. Scoped to what is on screen so "select all"
   * can never reach a row the active filter is hiding. */
  toggleAllPullsChecked: () => void;
  /** Drop the whole selection — after a bulk action, a repo switch, or Escape. */
  clearCheckedPulls: () => void;
  /** The server's bulk-action cap, so the bulk bar chunks on the real limit. */
  prBulkMax: number;

  // ── refresh preferences ──
  // Named `refreshPrefs`, not `refresh`: that name is already the manual-refresh
  // ACTION above, and one identifier meaning both a verb and a settings bag is how a
  // call site ends up invoking the wrong one.
  /** How often the lists and detail panes re-read, how long a fetch stays fresh,
   * and whether polling continues in a backgrounded tab. Every field is validated
   * against its offered choices — see `coerceRefreshPrefs`. */
  refreshPrefs: RefreshPrefs;
  /** Patch one or more refresh preferences. Persisted with the rest of the UI
   * state, so it survives leaving the app and coming back. */
  setRefreshPrefs: (patch: Partial<RefreshPrefs>) => void;

  // ── AI output language ──
  /** The language the Investigate and Review agents are told to write in, as a
   * BCP-47 tag, or `''` for "follow the dashboard language". Independent of the
   * dashboard language on purpose: an English interface with Chinese findings is
   * a supported combination. */
  aiLanguage: string;
  /** Choose the agent output language. `''` restores follow-the-dashboard. */
  setAiLanguage: (code: string) => void;

  // ── cross-reference sheet ──
  /** The open stack of same-repo issue/PR references, innermost LAST. Empty when
   * the sheet is closed. A ref opened from inside the sheet pushes onto it, so
   * "back" walks the trail you followed. */
  refStack: RepoRef[];
  /** Open a same-repo issue/PR in the bottom sheet (or push it onto the stack
   * when the sheet is already open). Re-opening the ref already on top is a
   * no-op, so a double-click can't stack the same target twice. */
  openRef: (ref: RepoRef) => void;
  /** Drop the innermost sheet entry — back to the one that referenced it, or
   * closed when it was the only one. */
  popRef: () => void;
  /** Close the sheet outright, discarding the whole stack. */
  closeRefs: () => void;

  // ── navigation ──
  mainView: MainView;
  dashboardTab: DashboardTab;
  openDashboard: (tab: DashboardTab) => void;
  openIssues: () => void;
  openPulls: () => void;
  openSettings: (target?: SettingsTarget) => void;
  /** What the Settings main area is showing (the General page, or a repo page). */
  settingsTarget: SettingsTarget;
  expanded: ExpandedSection;
  setExpanded: (s: ExpandedSection) => void;

  // ── crews ──
  /** Every non-retired crew in the active repo. */
  crews: Crew[];
  /** The server's roster tallies. Read from the response rather than counted
   * here: they are computed from each crew's OPEN WORK ITEMS, which this payload
   * does not carry, so there is nothing client-side to derive them from. */
  crewCounts: CrewCounts;
  /** Repo-wide crew protocol settings (claim TTL, commit trailer); null until
   * the roster loads. Deliberately part of THIS query's result rather than a
   * second fetch — the one route answers both. */
  crewSettings: CrewSettings | null;
  crewsLoading: boolean;
  crewsError: Error | null;
  /** Which crews page the main area is showing (one crew, or nothing yet). */
  crewView: CrewView;
  setCrewView: (v: CrewView) => void;
  /** Which chip filter the crew list is applying. */
  crewFilter: CrewFilter;
  setCrewFilter: (f: CrewFilter) => void;
  /** Active roster sort field and direction, and the cycler the rail drives:
   * clicking the active field flips the direction, another switches to it. */
  crewSortKey: CrewSortKey;
  crewSortDir: SortDir;
  cycleCrewSort: (key: CrewSortKey) => void;
  /** Open the crews surface, optionally jumping straight to a page — the same
   * shape as `openSettings(target?)`, so a rail row can navigate in one call
   * instead of setting the page and the view separately. */
  openCrews: (view?: CrewView) => void;
  /** Which pane a narrow viewport is showing, for the list-detail drill-down.
   * Hosted here rather than in the shell because the row handlers that drill in
   * live in the list components, which already consume this context — passing a
   * callback down through three lists would be the same state, threaded.
   *
   * Deliberately NOT persisted, unlike `selectedIssue`: a restored open detail
   * would put a phone on the detail pane before the user picked anything, with
   * the list unreachable behind it. */
  listDetail: ListDetailView;
}
