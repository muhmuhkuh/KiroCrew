import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

/** Add an explicitly shared tab/system audio source to the transcription graph. */
export function useMeetingAudio(
  context: RefObject<AudioContext | null>,
  worklet: RefObject<AudioWorkletNode | null>,
) {
  const [active, setActive] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(false)
  const generation = useRef(0)
  const requesting = useRef(false)
  const stream = useRef<MediaStream | null>(null)
  const source = useRef<MediaStreamAudioSourceNode | null>(null)

  const stop = useCallback(() => {
    generation.current += 1
    requesting.current = false
    source.current?.disconnect()
    source.current = null
    stream.current?.getTracks().forEach(track => track.stop())
    stream.current = null
    setActive(false)
    setPending(false)
  }, [])

  useEffect(() => stop, [stop])

  const start = useCallback(async () => {
    if (requesting.current || stream.current || !context.current || !worklet.current) return
    requesting.current = true
    const attempt = ++generation.current
    setPending(true)
    setError(false)
    let captured: MediaStream | null = null
    try {
      // Must run directly from the click: display capture requires user activation.
      captured = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: true,
        systemAudio: 'include',
      } as DisplayMediaStreamOptions)
      if (attempt !== generation.current) {
        captured.getTracks().forEach(track => track.stop())
        return
      }
      const audio = captured.getAudioTracks()
      if (!audio.length || audio.every(track => track.readyState === 'ended')) {
        throw new Error('no_audio_track')
      }
      const ctx = context.current
      const node = worklet.current
      if (!ctx || !node) throw new Error('transcription_stopped')
      source.current = ctx.createMediaStreamSource(new MediaStream(audio))
      source.current.connect(node)
      stream.current = captured
      audio.forEach(track => track.addEventListener('ended', () => {
        if (attempt === generation.current) stop()
      }, { once: true }))
      // Only PCM audio is sent; release the required screen-capture video immediately.
      captured.getVideoTracks().forEach(track => track.stop())
      setActive(true)
    } catch (cause) {
      captured?.getTracks().forEach(track => track.stop())
      if (attempt !== generation.current) return
      stop()
      if (!(cause instanceof DOMException && cause.name === 'NotAllowedError')) setError(true)
    } finally {
      if (attempt === generation.current) {
        requesting.current = false
        setPending(false)
      }
    }
  }, [context, worklet, stop])

  return { active, pending, error, start, stop }
}
