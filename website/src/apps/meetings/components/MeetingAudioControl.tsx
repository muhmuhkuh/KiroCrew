import { Volume2 } from 'lucide-react'

import ErrorNotice from '../../../components/ErrorNotice'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import type { useMeetingAudio } from '../hooks/useMeetingAudio'

interface Props {
  audio: ReturnType<typeof useMeetingAudio>
  enabled: boolean
}

export default function MeetingAudioControl({ audio, enabled }: Props) {
  return (
    <div className="mb-2 flex flex-col items-start gap-2">
      <Btn
        disabled={audio.pending || (!audio.active && !enabled)}
        aria-pressed={audio.active}
        onClick={() => {
          if (audio.active) audio.stop()
          else void audio.start()
        }}
      >
        <Volume2 size={14} aria-hidden="true" />
        {audio.active
          ? i18nT('apps.meetings.audio.stop')
          : i18nT('apps.meetings.audio.start')}
      </Btn>
      {audio.error && (
        // No hand-off: the broadcast composer may contain an unsent draft.
        <ErrorNotice message={i18nT('apps.meetings.audio.failed')} />
      )}
    </div>
  )
}
