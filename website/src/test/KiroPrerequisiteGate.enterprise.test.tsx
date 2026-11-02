/**
 * KiroPrerequisiteGate under an enterprise gateway whose login gate is closed
 * (`enterprise_login_required`): no bypass hands over the dashboard, and the
 * first-run screens still run in order, so each prerequisite keeps its remedy.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate, {
  kiroPrerequisiteIsBlocking,
  kiroPrerequisiteRefetchInterval,
} from '../components/KiroPrerequisiteGate'
import { renderWithProviders } from './helpers'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn(),
}))

vi.mock('../api/client', async () => ({
  ApiError: (await import('../api/apiError')).ApiError,
  api: {
    kiroPrerequisite: vi.fn(),
    repairKiroPrerequisiteSpecs: vi.fn(),
    kirocrewConfig: vi.fn(),
    acpBackends: vi.fn(),
    acpBackendRecheck: vi.fn(),
    patchConfig: vi.fn(),
    kasLoginStatus: vi.fn(),
  },
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import { api } from '../api/client'
import { probe, status } from './KiroPrerequisiteGateTestUtils'

const locked = { enterprise_login_required: true, installed: true }

const renderGate = () =>
  renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )

describe('KiroPrerequisiteGate enterprise login gate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('blocks and polls fast while the login gate is closed, even with ready latched', () => {
    expect(kiroPrerequisiteIsBlocking(status(locked))).toBe(true)
    expect(kiroPrerequisiteIsBlocking(status({ ...locked, ready: true }))).toBe(true)
    expect(kiroPrerequisiteRefetchInterval(status({ ...locked, ready: true }))).toBe(5_000)
    expect(kiroPrerequisiteRefetchInterval(status({ ready: true }))).toBe(30_000)
  })

  it('shows Crew sign-in on a signed-out sign-in-only gateway, then waits for it to boot', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, enterprise_sign_in_only: true }),
    )
    vi.mocked(api.kasLoginStatus).mockResolvedValue({
      authenticated: false, provider: null, identity: null, transport: 'device',
    })
    const { unmount } = renderGate()
    // Only an organisation sign-in can load the rules: Company SSO alone, already open.
    // Lost-run bound: two chained queries (the prerequisite status, then the
    // sign-in status the mounted KasLoginGate reads) resolve before this renders.
    expect(await screen.findByTestId('kas-login-sso-form', undefined, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue with GitHub' })).not.toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    unmount()

    vi.mocked(api.kasLoginStatus).mockResolvedValue({
      authenticated: true, provider: 'idc', identity: 'identity_center', transport: 'device',
    })
    renderGate()
    // Same two-query chain as above, so the same lost-run bound.
    expect(
      await screen.findByRole('status', { name: 'Your crew is almost ready.' }, { timeout: 10_000 }),
    ).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('tells the user to start Kiro Crew again once the sign-in landed, even after it exits', async () => {
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(
        status({
          ...locked,
          enterprise_sign_in_only: true,
          enterprise_signed_in_restart: true,
          enterprise_start_command: '/opt/kirocrew/bin/kirocrew gateway',
        }),
      )
      .mockRejectedValue(new Error('Failed to fetch'))
    // The sign-in screen's own cache still says signed out (a CLI sign-in).
    vi.mocked(api.kasLoginStatus).mockResolvedValue({
      authenticated: false, provider: null, identity: null, transport: 'device',
    })
    const { queryClient } = renderGate()
    // Lost-run bound: the prerequisite query resolves before this renders.
    expect(
      await screen.findByTestId('enterprise-signed-in-restart', undefined, { timeout: 10_000 }),
    ).toHaveTextContent('Signed in. Start Kiro Crew again.')
    // The way to do it is on the page, since the sign-in app has exited.
    expect(screen.getByTestId('enterprise-start-command')).toHaveTextContent(
      '/opt/kirocrew/bin/kirocrew gateway',
    )
    expect(
      screen.getByText('Run this in a terminal to start Kiro Crew, then open the link it prints.'),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('kas-login-sso-form')).not.toBeInTheDocument()
    // The sign-in app has exited: polls fail, and the message stays.
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['kiro-prerequisite'] })
    })
    expect(screen.getByTestId('enterprise-signed-in-restart')).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('shows the sign-in step instead of the dashboard for an established install', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('button', { name: 'Check sign-in again' })).toBeEnabled()
    expect(screen.getByText('kiro-cli login').tagName).toBe('CODE')
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('holds the dashboard while ready is latched but the gate has not reopened', async () => {
    // The probe can see the login a few seconds before the gateway's own
    // watch reopens the gate; the dashboard would only meet 403s meanwhile.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, ready: true, authenticated: true, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('button', { name: 'Check sign-in again' })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('shows a non-owner the owner screen, never the dashboard', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, setup_allowed: false, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByText(/gateway owner needs to finish setup/)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('keeps the install remedy when the CLI is missing', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, installed: false, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('link', { name: /Open Kiro CLI setup/ })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('keeps the sandbox remedy when verification is refused', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({
        ...locked,
        sandbox_unavailable: true,
        sandbox_failure_kind: 'no_backend',
        sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
      }),
    )
    renderGate()
    expect(await screen.findByText(/unshare\(CLONE_NEWNS\) failed/)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('ignores a usable independent harness and skips the refused reads', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ id: 'claude' })] })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('button', { name: 'Check sign-in again' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(api.kirocrewConfig).not.toHaveBeenCalled()
    expect(api.acpBackends).not.toHaveBeenCalled()
    // Choosing another agent writes config, which the closed gate refuses.
    expect(screen.queryByRole('button', { name: 'Use other coding agents' })).not.toBeInTheDocument()
  })

  it('keeps the app mounted and on screen when an established session relocks', async () => {
    // The relock screen is a follow-up; until then a relock leaves the app as
    // it is, and the gateway refuses its requests while the gate is closed.
    function Draft() {
      const [text, setText] = useState('')
      return <input aria-label="draft" value={text} onChange={e => setText(e.target.value)} />
    }
    const open = status({ ready: true, authenticated: true, installed: true, initial_setup_complete: true })
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(open)
      .mockResolvedValue(status({ ...locked, initial_setup_complete: true }))
    const { queryClient } = renderWithProviders(<KiroPrerequisiteGate><Draft /></KiroPrerequisiteGate>)
    fireEvent.change(await screen.findByLabelText('draft'), { target: { value: 'unsaved json' } })
    await waitFor(() => expect(queryClient.getQueryData(['kiro-prerequisite'])).toBeTruthy())

    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kiro-prerequisite'] }) })
    await waitFor(() => expect(
      (queryClient.getQueryData(['kiro-prerequisite']) as { enterprise_login_required?: boolean })
        .enterprise_login_required,
    ).toBe(true))
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
    expect(screen.queryByRole('button', { name: 'Check sign-in again' })).not.toBeInTheDocument()
  })

  // The gate reads Crew's vault sign-in: the gateway serves its command, and no
  // enterprise surface may name kiro-cli's login then.
  const VAULT_COMMAND = 'kirocrew kas login --provider idc --start-url'
  const FILLED_COMMAND = `${VAULT_COMMAND} https://acme.awsapps.com/start`
  const vaultLocked = {
    ...locked,
    enterprise_sign_in_command: VAULT_COMMAND,
    enterprise_sign_in_needs_start_url: true,
  }
  const START_URL_HINT = /Enter it first: the command below fills it in/

  it('names the vault sign-in on the sign-in screen, never kiro-cli login', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...vaultLocked, initial_setup_complete: true, installed: false, authenticated: true }),
    )
    renderGate()
    const screenEl = await screen.findByTestId('enterprise-sign-in-required')
    expect(within(screenEl).getByText(VAULT_COMMAND).tagName).toBe('CODE')
    expect(screen.queryByText('kiro-cli login')).not.toBeInTheDocument()
    // No kiro-cli install step: the vault gate does not need kiro-cli.
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    // The side panel names the organization sign-in, not a Kiro CLI install.
    expect(screen.queryByText(/Install Kiro CLI/)).not.toBeInTheDocument()
    expect(screen.getByText(/Sign this gateway in to your organization's account/)).toBeInTheDocument()
    // The start URL is unknown here, so a line OUTSIDE the copied text says where it is.
    expect(within(screenEl).getByTestId('enterprise-start-url-hint')).toHaveTextContent(START_URL_HINT)
    expect(within(screenEl).getByText(VAULT_COMMAND).textContent).not.toMatch(START_URL_HINT)
  })

  it('asks for the start URL first and keeps copy off until it is a valid https URL', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...vaultLocked, initial_setup_complete: true }))
    renderGate()
    const screenEl = await screen.findByTestId('enterprise-sign-in-required')
    const field = within(screenEl).getByLabelText("Your organization's start URL")
    const commandBox = within(screenEl).getByTestId('enterprise-sign-in-command')
    const copy = within(commandBox).getByRole('button')
    // The field comes before the line that says to run the command.
    expect(field.compareDocumentPosition(within(screenEl).getByText(/Run this in a terminal/)))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(copy).toBeDisabled()

    fireEvent.change(field, { target: { value: 'http://acme.awsapps.com/start' } })
    expect(copy).toBeDisabled()
    expect(within(commandBox).getByText(VAULT_COMMAND)).toBeInTheDocument()
    fireEvent.change(field, { target: { value: 'https://acme.awsapps.com/start; echo hi' } })
    expect(copy).toBeDisabled()

    fireEvent.change(field, { target: { value: ' https://acme.awsapps.com/start/# ' } })
    expect(copy).toBeEnabled()
    const shown = within(commandBox).getByText(FILLED_COMMAND).textContent ?? ''
    expect(shown).toBe(FILLED_COMMAND)
    expect(shown).not.toMatch(/[<>]/)
    // Client-side only: typing the URL sends nothing to the gateway.
    expect(api.patchConfig).not.toHaveBeenCalled()
    expect(api.acpBackendRecheck).not.toHaveBeenCalled()
  })

  it('copies a command the shell runs as-is: no < or >, and the known start URL filled in', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({
        ...vaultLocked,
        enterprise_sign_in_command: FILLED_COMMAND,
        enterprise_sign_in_needs_start_url: false,
        initial_setup_complete: true,
      }),
    )
    renderGate()
    const screenEl = await screen.findByTestId('enterprise-sign-in-required')
    const copied = within(screenEl).getByText(FILLED_COMMAND).textContent ?? ''
    expect(copied).not.toMatch(/[<>]/)
    // Known start URL: no field, and the copy button is on.
    expect(within(screenEl).queryByTestId('enterprise-start-url-input')).not.toBeInTheDocument()
    expect(within(screenEl).queryByTestId('enterprise-start-url-hint')).not.toBeInTheDocument()
    expect(within(within(screenEl).getByTestId('enterprise-sign-in-command')).getByRole('button')).toBeEnabled()
  })

  it('tells a non-owner on the vault sign-in screen to ask the owner', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...vaultLocked, initial_setup_complete: true, setup_allowed: false }),
    )
    renderGate()
    const screenEl = await screen.findByTestId('enterprise-sign-in-required')
    expect(within(screenEl).getByText(/Ask the .* owner to sign this gateway in again/)).toBeInTheDocument()
    expect(screen.queryByText(VAULT_COMMAND)).not.toBeInTheDocument()
  })

  it('sends the owner to a new window from the relock notice, where the command can be copied', async () => {
    // The notice cannot be selected, so a long filled command is never shown there.
    await relock(
      <Draft />,
      status({ ...vaultLocked, enterprise_sign_in_command: FILLED_COMMAND, initial_setup_complete: true }),
    )
    const notice = screen.getByTestId('enterprise-relock-notice')
    expect(within(notice).getByTestId('enterprise-relock-new-window')).toHaveTextContent(
      /open the dashboard in a new window or tab/,
    )
    expect(within(notice).queryByText(FILLED_COMMAND)).not.toBeInTheDocument()
    expect(notice.querySelector('code')).toBeNull()
    expect(within(notice).queryByText('kiro-cli login')).not.toBeInTheDocument()
    expect(notice.querySelectorAll('button, a, input, select, textarea, [tabindex]')).toHaveLength(0)
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
  })

  it('opens the dashboard once the gate reports open', async () => {
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(status({ ...locked, initial_setup_complete: true }))
      .mockResolvedValue(status({ ready: true, authenticated: true, installed: true, initial_setup_complete: true }))
    renderGate()
    fireEvent.click(await screen.findByRole('button', { name: 'Check sign-in again' }))
    await waitFor(() => expect(screen.getByText('Dashboard loaded')).toBeInTheDocument())
  })
})
