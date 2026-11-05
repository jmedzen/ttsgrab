import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, formatBytes, formatMs, formatUsd } from '../lib/api'
import AudioPlayer from '../components/AudioPlayer'

export default function History() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
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
          <p className="text-sm text-ink-500">{t('history.subtitle')}</p>
        </div>
        <input
          className="input w-64"
          placeholder={t('common.search')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          data-testid="history-search"
        />
      </header>

      {isLoading && <p className="text-sm text-ink-500">{t('common.loading')}</p>}
      {!isLoading && (data?.items ?? []).length === 0 && (
        <p className="py-12 text-center text-sm text-ink-500">{t('history.empty')}</p>
      )}

      <div className="space-y-2">
        {(data?.items ?? []).map((item) => (
          <div key={item.id} className="card p-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="badge bg-accent-600/10 text-accent-600 dark:text-accent-400">
                {item.provider}
              </span>
              <span className="font-mono text-ink-500">{item.voice_id}</span>
              <span className="text-ink-500">{new Date(item.created_at).toLocaleString()}</span>
              <span className="text-ink-500">{formatMs(item.latency_ms)}</span>
              <span className="text-ink-500">{formatBytes(item.audio_bytes)}</span>
              <span className="text-ink-500">{formatUsd(item.cost_micro_usd)}</span>
              {item.from_cache && (
                <span className="badge bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400">
                  cache
                </span>
              )}
              {item.requested_provider && item.requested_provider !== item.provider && (
                <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
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

            <p className="mt-2 line-clamp-2 text-sm text-ink-700 dark:text-ink-300">{item.text}</p>

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
