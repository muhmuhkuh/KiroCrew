import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import MeetingAudioControl from '../apps/meetings/components/MeetingAudioControl'
import { i18nT } from '../i18n/t'

function audio(active = false) {
  return { active, pending: false, error: false, start: vi.fn().mockResolvedValue(undefined), stop: vi.fn() }
}

describe('meeting audio control', () => {
  it('starts sharing from a click and exposes the active toggle state', () => {
    const source = audio()
    const view = render(<MeetingAudioControl audio={source} enabled />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.meetings.audio.start') }))
    expect(source.start).toHaveBeenCalledOnce()
    view.rerender(<MeetingAudioControl audio={{ ...source, active: true }} enabled />)
    const stop = screen.getByRole('button', { name: i18nT('apps.meetings.audio.stop') })
    expect(stop).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(stop)
    expect(source.stop).toHaveBeenCalledOnce()
  })

  it('cannot open a picker while paused or a picker is pending', () => {
    const source = audio()
    const view = render(<MeetingAudioControl audio={source} enabled={false} />)
    const button = screen.getByRole('button')
    expect(button).toBeDisabled()
    view.rerender(<MeetingAudioControl audio={{ ...source, pending: true }} enabled />)
    expect(button).toBeDisabled()
  })

<<<<<<< HEAD
  it('keeps stop available when an active share is paused by a status request', () => {
    const source = audio(true)
    render(<MeetingAudioControl audio={source} enabled={false} />)
    const stop = screen.getByRole('button', { name: i18nT('apps.meetings.audio.stop') })
    expect(stop).toBeEnabled()
    fireEvent.click(stop)
    expect(source.stop).toHaveBeenCalledOnce()
    expect(source.start).not.toHaveBeenCalled()
  })

=======
>>>>>>> 7d8677836b (feat: add Meetings Audio feature (MeetingAudioControl, useMeetingAudio, tests))
  it('reports a missing audio source with recovery guidance', () => {
    render(<MeetingAudioControl audio={{ ...audio(), error: true }} enabled />)
    expect(screen.getByText(i18nT('apps.meetings.audio.failed'))).toBeInTheDocument()
  })
})
