/** Provider-aware link building for the auto-improvement UI. */

/** A short commit sha, as the direct-commit path records it in the ledger's `cr`. */
export const SHA_RE = /^[0-9a-f]{7,40}$/i

/** The minimal finding shape the link builders read (ledger rows). */
export interface FindingLike {
  pr?: string | null
  cr?: string | null
}

/**
 * The forge URL for a committed finding's sha, or null when we cannot build one.
 *
 * The direct-commit path stores a bare sha (e.g. `1537c449`) rather than a url, so
 * a generic "accept http…" link renders NOTHING for a committed finding. `repo` is
 * the `owner/name` (or nested `group/sub/project`) display string the config
 * carries, and `provider`/`host` come from the same config (persisted at setup),
 * so the link is correct for GitHub and GitLab without guessing a host.
 */
export function commitUrlOf(
  finding: FindingLike,
  repo: string,
  provider?: string,
  host?: string,
): string | null {
  const sha = (finding.pr || finding.cr || '').trim()
  if (!SHA_RE.test(sha)) return null
  // Only build a url for an owner/name we recognize; never guess a host. GitLab
  // namespaces may be nested, so accept 2+ path segments.
  if (!/^[\w.-]+(?:\/[\w.-]+)+$/.test(repo)) return null
  if (provider === 'gitlab') {
    const h = host && host !== 'github.com' ? host : 'gitlab.com'
    return `https://${h}/${repo}/-/commit/${sha}`
  }
  return `https://github.com/${repo}/commit/${sha}`
}
