import { createSelector } from '@reduxjs/toolkit'
import type { ChatSlot } from '../../types'
import { slotSurfaceKey } from '../slotIdentity'
import { isChatPageSurface } from '../../utils/channelOrigin'
import { crewModeEnabled } from '../../lib/crewMode'
import { crewMemberKeys, heldOrphanTest } from './unread'
import type { DashboardState } from '../dashboardSlice'

/**
 * Count unread slots whose surface matches `mode`. Slots present in
 * `unreadSlots` but missing from `slots` (e.g. deleted but not yet drained)
 * are treated as the default chat surface (`""`) so they keep contributing
 * to the Chat badge rather than vanishing silently.
 *
 * Note — intentional asymmetry with `filterUnreadKeysBySurface` in
 * `surfaces/registry.ts`: that helper drops orphan keys (the sidebar can't
 * display them regardless), whereas this one keeps them so the badge stays
 * stable across the brief race between `removeSlotOptimistic` and
 * `fetchSlots.fulfilled`.
 *
 * The exception is an orphan no reconcile will drain: any orphan while the
 * gateway labels its lists partial, or one the gateway holds (`slotsHeldKeys`, or a
 * channel key under `slotsHoldChannels`). The premise above does not hold for it. Its unread entry is kept so the dot returns with the tab, but
 * it is left out of the badge, which would otherwise count a session the
 * sidebar cannot show.
 */
function countUnreadByMode(
  slots: ChatSlot[],
  unread: string[],
  mode: string,
  heldKeys: readonly string[] = [],
  holdChannels = false,
  listPartial = false,
): number {
  if (unread.length === 0) return 0
  const surfaceByKey = new Map(slots.map(s => [s.key, slotSurfaceKey(s)]))
  const isHeldOrphan = heldOrphanTest(new Set(surfaceByKey.keys()), heldKeys, holdChannels, listPartial)
  // A Crew Mode crew member is no row of the person's lists (its root's Crew
  // panel lists it), so its unread entry is kept for that panel but not counted.
  const crewMembers = crewMemberKeys(slots)
  // The chat surface ('') counts every slot the chat page renders, legacy
  // Autopilot slots included (see `isChatPageSurface`).
  const isChatSurface = mode === ''
  let count = 0
  for (const k of unread) {
    if (isHeldOrphan(k) || crewMembers.has(k)) continue
    const sk = surfaceByKey.get(k) ?? ''
    if (isChatSurface ? isChatPageSurface(sk) : sk === mode) count++
  }
  return count
}

/**
 * Memoized factory for "unread count for slots whose surface === mode".
 * One memo cache per `mode` argument so registry surfaces don't trash each
 * other's memoization. Built-in nav badges should not call this directly —
 * they go through `selectSurfaceBadgeCount(navId)` from `surfaces/registry`,
 * which routes to this factory only when a surface declares `slotMode`.
 */
type UnreadByModeSelector = (state: { dashboard: DashboardState }) => number
const _unreadByModeCache = new Map<string, UnreadByModeSelector>()
export function selectUnreadByMode(mode: string): UnreadByModeSelector {
  let sel = _unreadByModeCache.get(mode)
  if (!sel) {
    // The Crew Mode preview decides whether crew members count (`crewMemberKeys`).
    // It is passed as an argument, not read inside an input selector: the
    // selector memoizes on its arguments, so a flip with the same state would
    // otherwise answer from the cache.
    const count = createSelector(
      (state: { dashboard: DashboardState }) => state.dashboard.slots,
      (state: { dashboard: DashboardState }) => state.dashboard.unreadSlots,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsHeldKeys,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsHoldChannels,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsListPartial,
      (_state: { dashboard: DashboardState }, crewMode: boolean) => crewMode,
      (slots, unread, heldKeys, holdChannels, listPartial) =>
        countUnreadByMode(slots, unread, mode, heldKeys ?? [], holdChannels === true, listPartial === true),
    )
    sel = state => count(state, crewModeEnabled())
    _unreadByModeCache.set(mode, sel)
  }
  return sel
}

