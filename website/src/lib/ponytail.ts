export const PONYTAIL_MODES = ['off', 'lite', 'full', 'ultra'] as const
export type PonytailMode = (typeof PONYTAIL_MODES)[number]
export type PonytailOverride = PonytailMode | ''

export const PONYTAIL_DEFAULT: PonytailMode = 'full'

export function isPonytailMode(value: unknown): value is PonytailMode {
  return typeof value === 'string' && (PONYTAIL_MODES as readonly string[]).includes(value)
}

export function normalizePonytail(value: unknown): PonytailMode {
  return isPonytailMode(value) ? value : PONYTAIL_DEFAULT
}

export function normalizePonytailOverride(value: unknown): PonytailOverride {
  return value === '' || isPonytailMode(value) ? value : ''
}

export function resolvePonytail(override: unknown, globalDefault: unknown): PonytailMode {
  const local = normalizePonytailOverride(override)
  return local === '' ? normalizePonytail(globalDefault) : local
}
