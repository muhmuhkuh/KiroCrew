import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useMeetingAudio } from '../apps/meetings/hooks/useMeetingAudio'

function fixture(withAudio = true) {
  const audio = Object.assign(new EventTarget(), { stop: vi.fn(), readyState: 'live' })
  const video = { stop: vi.fn() }
  const stream = {
    getAudioTracks: () => withAudio ? [audio] : [],
    getVideoTracks: () => [video],
    getTracks: () => withAudio ? [audio, video] : [video],
  }
  const capture = vi.fn().mockResolvedValue(stream)
  vi.stubGlobal('navigator', { mediaDevices: { getDisplayMedia: capture } })
  vi.stubGlobal('MediaStream', class { constructor(public tracks: unknown[]) {} })
  const source = { connect: vi.fn(), disconnect: vi.fn() }
  const context = { current: { createMediaStreamSource: vi.fn().mockReturnValue(source) } as unknown as AudioContext }
  const node = { current: {} as AudioWorkletNode }
  const hook = renderHook(() => useMeetingAudio(context, node))
  return { ...hook, audio, video, stream, capture, source, context, node }
}

afterEach(() => { vi.unstubAllGlobals() })

describe('meeting audio sharing', () => {
  it('mixes audio into transcription, releases video, and disconnects on stop', async () => {
    const f = fixture()
    await act(async () => { await f.result.current.start() })
    expect(f.source.connect).toHaveBeenCalledWith(f.node.current)
    expect(f.result.current.active).toBe(true)
    expect(f.video.stop).toHaveBeenCalledOnce()
    expect(f.audio.stop).not.toHaveBeenCalled()
    act(() => { f.result.current.stop() })
    expect(f.source.disconnect).toHaveBeenCalledOnce()
    expect(f.audio.stop).toHaveBeenCalledOnce()
    expect(f.result.current.active).toBe(false)
  })

  it('reports a stream without audio and releases its tracks', async () => {
    const f = fixture(false)
    await act(async () => { await f.result.current.start() })
    expect(f.result.current.error).toBe(true)
    expect(f.result.current.pending).toBe(false)
    expect(f.result.current.active).toBe(false)
    expect(f.video.stop).toHaveBeenCalled()
    expect(f.source.connect).not.toHaveBeenCalled()
  })

  it('treats cancellation as leaving microphone transcription alone', async () => {
    const f = fixture()
    f.capture.mockRejectedValue(new DOMException('cancelled', 'NotAllowedError'))
    await act(async () => { await f.result.current.start() })
    expect(f.result.current.error).toBe(false)
    expect(f.result.current.pending).toBe(false)
    expect(f.source.connect).not.toHaveBeenCalled()
  })

  it('discards a picker result that arrives after stop', async () => {
    const f = fixture()
    let resolve!: (value: unknown) => void
    f.capture.mockReturnValue(new Promise(value => { resolve = value }))
    let starting!: Promise<void>
    act(() => { starting = f.result.current.start() })
    act(() => { f.result.current.stop() })
    await act(async () => { resolve(f.stream); await starting })
    expect(f.audio.stop).toHaveBeenCalledOnce()
    expect(f.video.stop).toHaveBeenCalledOnce()
    expect(f.source.connect).not.toHaveBeenCalled()
    expect(f.result.current.active).toBe(false)
  })

  it('releases audio when the user ends sharing or the component unmounts', async () => {
    const f = fixture()
    await act(async () => { await f.result.current.start() })
    act(() => { f.audio.dispatchEvent(new Event('ended')) })
    expect(f.result.current.active).toBe(false)
    expect(f.source.disconnect).toHaveBeenCalledOnce()
    await act(async () => { await f.result.current.start() })
    f.unmount()
    expect(f.audio.stop).toHaveBeenCalledTimes(2)
  })

  it('opens only one picker for concurrent starts', async () => {
    const f = fixture()
    await act(async () => { await Promise.all([f.result.current.start(), f.result.current.start()]) })
    expect(f.capture).toHaveBeenCalledOnce()
  })
})
