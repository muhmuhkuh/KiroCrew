import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import TurnBlock from '../pages/chat/TurnBlock'
import SearchHighlightContext from '../hooks/SearchHighlightContext'
import { useReportRowActive } from '../pages/chat/rowActivity'

const tool = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 Running: step${n}`, ts: `t${n}` }, idx: n })
const hidden = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `✅ step${n}`, ts: `h${n}` }, idx: 200 + n })
const think = (content: string, ts: string, idx: number): TurnItem => ({ kind: 'single', msg: { role: 'thinking', content, ts }, idx })
const done = { kind: 'single', msg: { role: 'assistant', content: 'Done.', ts: 'z' }, idx: 50 } as TurnItem
const turnOf = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
const renderItem = (it: TurnItem, i: number) => it.kind === 'single' && it.msg.content.startsWith('✅')
  ? null
  : <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
const wrap = (i: number) => screen.getByTestId(`item-${i}`).closest('[data-fold-row]') as HTMLElement
/** A row is folded when its keyed wrapper is the height-0 marked one. */
const isFolded = (i: number) => wrap(i).getAttribute('data-collapsed') === 'true'
const rowClass = (i: number) => wrap(i).className
const contentClass = (i: number) => wrap(i).firstElementChild?.className ?? ''
const tools = (n: number) => Array.from({ length: n }, (_, i) => tool(i))

function LiveRow({ item, index, active }: { item: TurnItem; index: number; active: boolean }) {
  useReportRowActive(active)
  return <div data-testid={`item-${index}`}>{item.kind === 'single' ? item.msg.content : 'group'}</div>
}

describe('TurnBlock — running turn shows only the last 5 actions', () => {
  it('7 actions: 5 open, 2 behind one fold row', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    const fold = screen.getByRole('button', { name: '2 earlier steps' })
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    expect([0, 1].every(isFolded)).toBe(true)
    expect([2, 3, 4, 5, 6].some(isFolded)).toBe(false)
    // Folded rows are hidden from assistive tech and the tab order.
    expect(wrap(0)).toHaveAttribute('aria-hidden', 'true')
    expect(wrap(0)).toHaveAttribute('inert')
  })

  it('5 actions or fewer: no fold row', () => {
    render(<TurnBlock turn={turnOf(tools(5))} renderItem={renderItem} />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('a hidden completion row is omitted and does not affect the live-window count', () => {
    // tool0, its hidden ✅ row, then tool1..tool6: seven visible/countable calls.
    const items = [tool(0), hidden(0), ...tools(7).slice(1)]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: '2 earlier steps' })).toBeInTheDocument()
    expect(screen.queryByTestId('item-1')).toBeNull()
    expect([0, 2].every(isFolded)).toBe(true)
    expect([3, 4, 5, 6, 7].some(isFolded)).toBe(false)
  })

  it('the reasoning row never folds, even while it streams', () => {
    const items = [think('plan', 'th', 99), ...tools(7), think('more', 'th2', 100)]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    // mergeTurnThinking hoists both bursts into row 0.
    expect(isFolded(0)).toBe(false)
    expect(screen.getByRole('button', { name: '2 earlier steps' })).toBeInTheDocument()
    expect([1, 2].every(isFolded)).toBe(true)
  })

  it('a live tool row stays unfolded and is removed from the fold count', () => {
    const renderLiveItem = (it: TurnItem, i: number) => (
      <LiveRow item={it} index={i} active={it.kind === 'single' && it.msg.ts === 't0'} />
    )
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderLiveItem} />)
    expect(isFolded(0)).toBe(false)
    expect(isFolded(1)).toBe(true)
    // The live row precedes the folded window, so the label stays neutral.
    expect(screen.getByRole('button', { name: '1 tool call' })).toBeInTheDocument()
  })

  it('never folds answer text', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'Here is what I found so far.', ts: 'a' }, idx: 100 },
      ...tools(7),
    ]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(isFolded(0)).toBe(false)
    expect([1, 2].every(isFolded)).toBe(true)
  })

  it('expanding shows every row and the state holds as new actions arrive', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    fireEvent.click(screen.getByRole('button', { name: '2 earlier steps' }))
    const open = screen.getByRole('button', { name: 'Hide earlier steps' })
    expect(open).toHaveAttribute('aria-expanded', 'true')
    expect(tools(7).some((_, i) => isFolded(i))).toBe(false)
    rerender(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: 'Hide earlier steps' })).toHaveAttribute('aria-expanded', 'true')
    expect(tools(8).some((_, i) => isFolded(i))).toBe(false)
    fireEvent.click(open)
    expect(screen.getByRole('button', { name: '3 earlier steps' })).toBeInTheDocument()
  })

  it('folds the same keyed row with the grid and opacity transition', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    expect(rowClass(0)).toContain('grid-rows-[0fr]')
    expect(rowClass(0)).toContain('opacity-0')
    expect(rowClass(0)).toContain('transition-[grid-template-rows,opacity]')
    expect(rowClass(0)).toContain('duration-200')
    expect(contentClass(0)).toContain('overflow-hidden')
    expect(rowClass(6)).toContain('grid-rows-[1fr]')
    rerender(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    expect(rowClass(7)).toContain('grid-rows-[1fr]')
    expect(rowClass(7)).not.toContain('opacity-0')
    expect(rowClass(2)).toContain('grid-rows-[0fr]')
  })

  it('reduced motion disables the row transition while preserving the fold', () => {
    render(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    expect(isFolded(2)).toBe(true)
    expect(rowClass(2)).toContain('motion-reduce:transition-none')
  })

  it('a row keeps its element across fold and unfold, and an in-place row across the completion flip', () => {
    const note: TurnItem = { kind: 'single', msg: { role: 'error', content: 'a step failed', ts: 'e' }, idx: 90 }
    const base = [note, ...tools(7)]
    const { rerender } = render(<TurnBlock turn={turnOf(base)} renderItem={renderItem} />)
    const tool2 = wrap(3)
    const noteRow = wrap(0)
    rerender(<TurnBlock turn={turnOf([...base, tool(7)])} renderItem={renderItem} />)
    expect(isFolded(3)).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '3 earlier steps' }))
    expect(wrap(3)).toBe(tool2) // folded and unfolded in place, never remounted
    rerender(<TurnBlock turn={turnOf([...base, tool(7), done], true)} renderItem={renderItem} />)
    expect(wrap(0)).toBe(noteRow) // the settled render reuses the same row element
  })

  it('a host-owned fold state survives a remount', () => {
    const onChange = vi.fn()
    const { unmount } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '2 earlier steps' }))
    expect(onChange).toHaveBeenCalledWith('k', true)
    unmount()
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} disclosure={true} />)
    expect(screen.getByRole('button', { name: 'Hide earlier steps' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('a search match in a folded row opens the fold', () => {
    const search = { term: 'step0', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: 0 }
    render(
      <SearchHighlightContext.Provider value={search}>
        <TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />
      </SearchHighlightContext.Provider>,
    )
    expect(screen.getByRole('button', { name: 'Hide earlier steps' })).toHaveAttribute('aria-expanded', 'true')
    expect(isFolded(0)).toBe(false)
  })

  it('an opened live fold stays open when the turn finishes', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    fireEvent.click(screen.getByRole('button', { name: '2 earlier steps' }))
    rerender(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
  })

  it('host-owned open state stays open across completion and remount', () => {
    const onChange = vi.fn()
    const props = { renderItem, disclosureKey: 'k', onDisclosureChange: onChange }
    const { rerender, unmount } = render(<TurnBlock turn={turnOf(tools(7))} {...props} disclosure={true} />)
    rerender(<TurnBlock turn={turnOf([...tools(7), done], true)} {...props} disclosure={true} />)
    expect(onChange).not.toHaveBeenCalled()
    unmount()
    // The host retains disclosure=true for the turn; a cold remount reads it.
    render(<TurnBlock turn={turnOf([...tools(7), done], true)} {...props} disclosure={true} />)
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('an unopened live fold settles into the existing collapse', () => {
    const onChange = vi.fn()
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} />)
    rerender(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} />)
    expect(screen.getByRole('button', { name: '7 tool calls' })).toHaveAttribute('aria-expanded', 'false')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('a finished turn keeps the existing collapse, with no live fold', () => {
    render(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: '7 tool calls' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /earlier step/ })).toBeNull()
  })
})
