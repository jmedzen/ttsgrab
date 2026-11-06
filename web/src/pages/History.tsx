import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import QueryError from '../components/QueryError'
import { api, formatBytes, formatMs, formatUsd } from '../lib/api'
import AudioPlayer from '../components/AudioPlayer'

export default function History() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['history', q],
    queryFn: () => api.history({ limit: 100, q: q || undefined }),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteHistory(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['history'] }),
  })

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t('history.title')}</h1>
          <p className="text-sm text-muted">{t('history.subtitle')}</p>
        </div>
        <input
          className="input w-64"
          placeholder={t('common.search')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          data-testid="history-search"
        />
      </header>

      {isLoading && <p className="text-sm text-muted">{t('common.loading')}</p>}
      {isError && <QueryError error={error} onRetry={() => void refetch()} />}
      {!isLoading && !isError && (data?.items ?? []).length === 0 && (
        <p className="py-12 text-center text-sm text-muted">{t('history.empty')}</p>
      )}

      <div className="space-y-2">
        {(data?.items ?? []).map((item) => (
          <div key={item.id} className="card p-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="badge bg-accent/10 text-accent">
                {item.provider}
              </span>
              <span className="font-mono text-muted">{item.voice_id}</span>
              <span className="text-muted">{new Date(item.created_at).toLocaleString()}</span>
              <span className="text-muted">{formatMs(item.latency_ms)}</span>
              <span className="text-muted">{formatBytes(item.audio_bytes)}</span>
              <span className="text-muted">{formatUsd(item.cost_micro_usd)}</span>
              {item.from_cache && (
                <span className="badge bg-ok/15 text-ok">
                  cache
                </span>
              )}
              {item.requested_provider && item.requested_provider !== item.provider && (
                <span className="badge bg-warn/15 text-warn">
                  fallback
                </span>
              )}
              <div className="ml-auto flex gap-1.5">
                {item.audio_url && (
                  <button
                    type="button"
                    className="btn-ghost px-2 py-1 text-xs"
                    onClick={() => setOpenId(openId === item.id ? null : item.id)}
                  >
                    {t('history.replay')}
                  </button>
                )}
                <button
                  type="button"
                  className="btn-danger px-2 py-1 text-xs"
                  onClick={() => {
                    if (confirm(t('history.deleteConfirm'))) remove.mutate(item.id)
                  }}
                >
                  {t('common.delete')}
                </button>
              </div>
            </div>

            <p className="mt-2 line-clamp-2 text-sm text-muted">{item.text}</p>

            {openId === item.id && item.audio_url && (
              <div className="mt-2">
                <AudioPlayer src={item.audio_url} format={item.format} autoPlay />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
