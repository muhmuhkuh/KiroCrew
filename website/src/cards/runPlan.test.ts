import { describe, expect, it, vi } from 'vitest'
import type { Card, CardPlanStep } from '../api/cards'
import '../api/client'
import { fillBody, fillPath, runCardPlan } from './runPlan'

const goalStep: CardPlanStep = {
  method: 'PUT',
  path: '/api/members/{{step0.member_id}}/goals/long-term',
  body: { member: '{{step0.name}}', long_term: 'watch PRs', expected: '' },
  fill: [
    { field: 'slug', source: 'step', step: 0, key: 'member_id', in: 'path' },
    { field: 'member', source: 'step', step: 0, key: 'name' },
  ],
}

describe('runPlan fills', () => {
  it('puts an earlier response into the path, encoded as one segment', () => {
    expect(fillPath(goalStep, [{ member_id: 'm-1a2b', name: 'scout' }])).toBe(
      '/api/members/m-1a2b/goals/long-term',
    )
    expect(fillPath(goalStep, [{ member_id: 'a/b', name: 'scout' }])).toBe(
      '/api/members/a%2Fb/goals/long-term',
    )
  })

  it('leaves the body alone for a path fill and fills its body fields', () => {
    expect(fillBody(goalStep, {}, [{ member_id: 'm-1a2b', name: 'scout' }])).toEqual({
      member: 'scout',
      long_term: 'watch PRs',
      expected: '',
    })
  })

  it('keeps a step without path fills on its planned path', () => {
    const plain: CardPlanStep = { method: 'POST', path: '/api/agents', body: { name: 'scout' } }
    expect(fillPath(plain, [])).toBe('/api/agents')
  })

  it('sends the filled path and keeps path fills out of the body', async () => {
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchSpy)
    try {
      const card = {
        id: 'card-1',
        revision: 1,
        plan: { apply: [{ method: 'POST', path: '/api/members', body: {} }, goalStep] },
      } as unknown as Card
      const result = await runCardPlan(card, 'apply', {
        resume: { step: 1, responses: [{ member_id: 'a/b', name: 'scout' }] },
      })
      expect(result).toEqual({ ok: true })
      expect(fetchSpy).toHaveBeenCalledTimes(1)
      const [path, init] = fetchSpy.mock.calls[0]
      expect(path).toBe('/api/members/a%2Fb/goals/long-term')
      expect(JSON.parse(String(init?.body))).toEqual({
        member: 'scout',
        long_term: 'watch PRs',
        expected: '',
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('refuses to send a path when an earlier response field is missing', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    try {
      const card = {
        id: 'card-1',
        revision: 1,
        plan: { apply: [{ method: 'POST', path: '/api/members', body: {} }, goalStep] },
      } as unknown as Card
      const result = await runCardPlan(card, 'apply', {
        resume: { step: 1, responses: [] },
      })
      expect(result).toMatchObject({ ok: false, step: 1 })
      expect(fetchSpy).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
