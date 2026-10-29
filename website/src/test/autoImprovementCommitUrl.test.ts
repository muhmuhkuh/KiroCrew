import { describe, it, expect } from 'vitest'

import { commitUrlOf } from '../apps/auto-improvement/lib/links'

/** A committed finding: the ledger stores a bare sha in `cr`/`pr`. */
function committed(sha = '1537c449'): { pr: string; cr: string } {
  return { pr: sha, cr: sha }
}

describe('commitUrlOf (provider-aware commit link)', () => {
  it('builds a github.com commit link by default', () => {
    expect(commitUrlOf(committed(), 'zedmor/kiro-crew')).toBe(
      'https://github.com/zedmor/kiro-crew/commit/1537c449',
    )
  })

  it('builds a gitlab.com commit link when provider is gitlab', () => {
    expect(commitUrlOf(committed(), 'zedmor/kiro-crew', 'gitlab')).toBe(
      'https://gitlab.com/zedmor/kiro-crew/-/commit/1537c449',
    )
  })

  it('builds a self-managed gitlab commit link with the persisted host', () => {
    expect(commitUrlOf(committed(), 'group/sub/project', 'gitlab', 'gitlab.example.test')).toBe(
      'https://gitlab.example.test/group/sub/project/-/commit/1537c449',
    )
  })

  it('falls back to gitlab.com when a gitlab host is missing or github-shaped', () => {
    expect(commitUrlOf(committed(), 'g/p', 'gitlab', '')).toBe(
      'https://gitlab.com/g/p/-/commit/1537c449',
    )
    expect(commitUrlOf(committed(), 'g/p', 'gitlab', 'github.com')).toBe(
      'https://gitlab.com/g/p/-/commit/1537c449',
    )
  })

  it('refuses non-sha values and unknown repo shapes (never guess)', () => {
    expect(commitUrlOf({ pr: 'https://x/y', cr: '' }, 'g/p', 'gitlab')).toBeNull()
    expect(commitUrlOf(committed(), 'not-a-repo', 'gitlab')).toBeNull()
    expect(commitUrlOf({ pr: '', cr: '' }, 'g/p')).toBeNull()
  })
})
