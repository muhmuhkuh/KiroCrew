import { useEffect, useState } from 'react'
import { Check, Sparkles } from 'lucide-react'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import {
  normalizePonytail,
  normalizePonytailOverride,
  resolvePonytail,
  type PonytailMode,
  type PonytailOverride,
} from '../lib/ponytail'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'

interface Props {
  slot: string
  override: unknown
  globalDefault: unknown
  disabled?: boolean
}

function modeLabel(mode: PonytailMode): string {
  switch (mode) {
    case 'off': return i18nT('components.ponytailModeDropdown.off')
    case 'lite': return i18nT('components.ponytailModeDropdown.lite')
    case 'full': return i18nT('components.ponytailModeDropdown.full')
    case 'ultra': return i18nT('components.ponytailModeDropdown.ultra')
  }
}

function modeDescription(mode: PonytailMode): string {
  switch (mode) {
    case 'off': return i18nT('components.ponytailModeDropdown.off_description')
    case 'lite': return i18nT('components.ponytailModeDropdown.lite_description')
    case 'full': return i18nT('components.ponytailModeDropdown.full_description')
    case 'ultra': return i18nT('components.ponytailModeDropdown.ultra_description')
  }
}

/** Header control for the effective Ponytail mode and its per-chat override. */
export default function PonytailModeDropdown({ slot, override, globalDefault, disabled = false }: Props) {
  const actualOverride = normalizePonytailOverride(override)
  const [optimisticOverride, setOptimisticOverride] = useState<PonytailOverride | null>(null)
  const [saving, setSaving] = useState(false)
  const displayedOverride = optimisticOverride ?? actualOverride
  const effectiveMode = resolvePonytail(displayedOverride, globalDefault)
  const configuredDefault = normalizePonytail(globalDefault)

  useEffect(() => {
    if (optimisticOverride !== null && actualOverride === optimisticOverride) {
      setOptimisticOverride(null)
    }
  }, [actualOverride, optimisticOverride])

  const selectMode = (next: PonytailOverride) => {
    if (next === displayedOverride || saving) return
    const previous = displayedOverride
    setOptimisticOverride(next)
    setSaving(true)
    if (typeof api.chatSlotPonytail !== 'function') {
      setOptimisticOverride(null)
      setSaving(false)
      return
    }
    api.chatSlotPonytail(slot, next)
      .catch(() => {
        setOptimisticOverride(previous === actualOverride ? null : previous)
      })
      .finally(() => setSaving(false))
  }

  const activeLabel = modeLabel(effectiveMode)
  const inherits = displayedOverride === ''

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={`inline-flex items-center gap-1.5 h-7 px-2 rounded-md text-[11px] font-medium transition-colors border-none cursor-pointer pointer-events-auto ${effectiveMode === 'off' ? 'text-muted hover:text-text' : 'text-accent bg-accent/10 hover:bg-accent/20'} disabled:cursor-not-allowed disabled:opacity-50`}
          disabled={disabled || saving}
          title={i18nT('components.ponytailModeDropdown.change_mode')}
          aria-label={i18nT('components.ponytailModeDropdown.active_mode', { mode: activeLabel })}
        >
          <Sparkles size={13} className="shrink-0" />
          <span className="hidden sm:inline">{i18nT('components.ponytailModeDropdown.ponytail')}</span>
          <span>{activeLabel}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[250px]">
        <DropdownMenuLabel>
          {i18nT('components.ponytailModeDropdown.active_mode', { mode: activeLabel })}
        </DropdownMenuLabel>
        <DropdownMenuLabel className="pt-0 font-normal text-muted">
          {inherits
            ? i18nT('components.ponytailModeDropdown.using_global_default', { mode: modeLabel(configuredDefault) })
            : i18nT('components.ponytailModeDropdown.chat_override')}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => selectMode('')}>
          <span className="flex h-4 w-4 items-center justify-center">
            {inherits && <Check size={14} className="text-accent" />}
          </span>
          <span className="flex-1">{i18nT('components.ponytailModeDropdown.use_global_default')}</span>
          <span className="text-[11px] text-muted">{modeLabel(configuredDefault)}</span>
        </DropdownMenuItem>
        {(['off', 'lite', 'full', 'ultra'] as const).map(mode => (
          <DropdownMenuItem key={mode} onSelect={() => selectMode(mode)}>
            <span className="flex h-4 w-4 items-center justify-center">
              {displayedOverride === mode && <Check size={14} className="text-accent" />}
            </span>
            <span className="flex-1">
              <span className="block">{modeLabel(mode)}</span>
              <span className="block text-[11px] text-muted">{modeDescription(mode)}</span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
