import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('../api/client', () => ({
  api: { chatSlotPonytail: vi.fn() },
}))

import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import PonytailModeDropdown from '../components/PonytailModeDropdown'
import { api } from '../api/client'

function renderDropdown(
  props: Partial<React.ComponentProps<typeof PonytailModeDropdown>> = {},
) {
  return render(
    <PonytailModeDropdown
      slot="dashboard:ponytail"
      override=""
      globalDefault="full"
      {...props}
    />,
  )
}

describe('PonytailModeDropdown', () => {
  beforeEach(() => {
    vi.mocked(api.chatSlotPonytail).mockReset()
    vi.mocked(api.chatSlotPonytail).mockResolvedValue({ ok: true, ponytail: 'full' })
  })

  it('renders every mode label and description in the menu', () => {
    renderDropdown({ override: '', globalDefault: 'ultra' })

    expect(screen.getByRole('button', { name: 'Active Ponytail: Ultra' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Active Ponytail: Ultra' }))

    const menu = screen.getByRole('menu')
    expect(menu).toHaveTextContent('Use global default')
    expect(menu).toHaveTextContent('Off')
    expect(menu).toHaveTextContent('Do not add Ponytail coding guidance.')
    expect(menu).toHaveTextContent('Lite')
    expect(menu).toHaveTextContent('Keep coding changes lean.')
    expect(menu).toHaveTextContent('Full')
    expect(menu).toHaveTextContent('Use the complete minimal-change ladder.')
    expect(menu).toHaveTextContent('Ultra')
    expect(menu).toHaveTextContent('Challenge speculative work.')
  })

  it('uses a chat override and saves a selected mode', async () => {
    renderDropdown({ override: 'off', globalDefault: 'full' })
    fireEvent.click(screen.getByRole('button', { name: 'Active Ponytail: Off' }))
    fireEvent.click(screen.getAllByRole('menuitem')[2])

    expect(api.chatSlotPonytail).toHaveBeenCalledWith('dashboard:ponytail', 'lite')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Active Ponytail: Lite' })).toBeInTheDocument())
  })

  it('rolls back an optimistic selection when saving fails', async () => {
    vi.mocked(api.chatSlotPonytail).mockRejectedValueOnce(new Error('offline'))
    renderDropdown({ override: '', globalDefault: 'full' })
    fireEvent.click(screen.getByRole('button', { name: 'Active Ponytail: Full' }))
    fireEvent.click(screen.getAllByRole('menuitem')[1])

    await waitFor(() => expect(screen.getByRole('button', { name: 'Active Ponytail: Full' })).toBeInTheDocument())
  })

  it('does not call the API for the already displayed mode', () => {
    renderDropdown({ override: 'full', globalDefault: 'off' })
    fireEvent.click(screen.getByRole('button', { name: 'Active Ponytail: Full' }))
    fireEvent.click(screen.getAllByRole('menuitem')[3])

    expect(api.chatSlotPonytail).not.toHaveBeenCalled()
  })

  it('renders disabled state without opening the menu', () => {
    renderDropdown({ disabled: true })
    const trigger = screen.getByRole('button', { name: 'Active Ponytail: Full' })
    expect(trigger).toBeDisabled()
    fireEvent.click(trigger)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
