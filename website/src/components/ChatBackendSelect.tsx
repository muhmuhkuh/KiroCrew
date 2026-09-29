import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { api } from '../api/client'
import { useAppDispatch } from '../store'
import { triggerRefresh, updateSlot } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import ErrorNotice from './ErrorNotice'
import SimpleSelect from './SimpleSelect'

const INHERIT = '__inherit__'

export default function ChatBackendSelect({ slot }: { slot?: ChatSlot }) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const supported = slot?.backend_selection_supported === true
  const probes = useQuery({
    queryKey: ['acpBackends'],
    queryFn: () => api.acpBackends(),
    enabled: supported,
    retry: false,
    staleTime: 30_000,
  })
  const change = useMutation({
    mutationFn: ({ key, backend }: { key: string; backend: string | null }) => api.chatSlotBackend(key, backend),
    onSuccess: (result, { key }) => {
      dispatch(updateSlot({ key, acp_backend: result.acp_backend, model: result.model, served_model: '', model_withheld: null }))
      dispatch(triggerRefresh())
    },
    onError: () => { dispatch(triggerRefresh()) },
  })
  if (!slot) return null
  const backends = probes.data?.backends.filter(row => row.selectable) ?? []
  return (
    <div className="px-4 py-1 flex flex-wrap items-center gap-2 text-[12px] text-muted">
      <SimpleSelect
        aria-label={t('chatBackend.label')}
        options={[INHERIT, ...backends.map(row => row.id)]}
        optionLabels={[t('chatBackend.inherit'), ...backends.map(row => row.policy_id)]}
        value={slot.acp_backend ?? INHERIT}
        triggerFallback={slot.acp_backend ?? t('chatBackend.inherit')}
        onChange={value => change.mutate({ key: slot.key, backend: value === INHERIT ? null : value })}
        disabled={!supported || slot.running || change.isPending || !probes.data}
        className="h-8 max-w-full text-[12px]"
      />
      {!supported && <span>{t('chatBackend.managed')}</span>}
      {/* No hand-off: navigating away would discard the composer draft. */}
      <ErrorNotice variant="inline" message={change.error?.message || probes.error?.message || ''} />
    </div>
  )
}
