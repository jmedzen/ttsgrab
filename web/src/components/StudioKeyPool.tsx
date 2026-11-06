import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, type StudioPool } from '../lib/api'
import clsx from 'clsx'

/**
 * Google AI Studio key pool.
 *
 * Free and paid are shown as separate groups because they mean different things: a free key
 * has a hard requests-per-day ceiling (10 RPD for Gemini TTS at the time of writing), while
 * a billed key does not. Collapsing them into one list would hide the only number the
 * operator actually needs — how many free requests are left today.
 *
 * The pool never returns secrets; only labels and counters. The input is cleared after a
 * successful add so a pasted key does not linger in the DOM.
 */
export default function StudioKeyPool() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [key, setKey] = useState('')
  const [kind, setKind] = useState<'free' | 'paid'>('free')
  const [error, setError] = useState<string | null>(null)

  const poolQuery = useQuery({
    queryKey: ['studio-keys'],
    queryFn: api.studioKeys,
    // A key pool is admin-only. A 401 is an expected state for a normal API-key user, not a
    // failure worth retrying or shouting about.
    retry: false,
  })

  const addMutation = useMutation({
    mutationFn: () => api.addStudioKey(key.trim(), kind),
    onSuccess: (data: StudioPool) => {
      setKey('')
      setError(null)
      qc.setQueryData(['studio-keys'], data)
      void qc.invalidateQueries({ queryKey: ['providers'] })
      void qc.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const removeMutation = useMutation({
    mutationFn: (label: string) => api.removeStudioKey(label),
    onSuccess: (data: StudioPool) => {
      qc.setQueryData(['studio-keys'], data)
      void qc.invalidateQueries({ queryKey: ['providers'] })
      void qc.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  if (poolQuery.isError) {
    return (
      <section className="card p-4">
        <h2 className="mb-1 font-medium">{t('studio.title')}</h2>
        <p className="text-sm text-muted">{t('studio.adminsOnly')}</p>
      </section>
    )
  }

  const pool = poolQuery.data
  const keys = pool?.keys ?? []
  const free = keys.filter((k) => k.kind === 'free')
  const paid = keys.filter((k) => k.kind === 'paid')

  return (
    <section className="card p-4" data-testid="studio-key-pool">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <h2 className="font-medium">{t('studio.title')}</h2>
        {pool && (
          <span className={clsx('badge', pool.enabled ? 'badge-ok' : 'badge-muted')}>
            {pool.enabled ? t('providers.enabled') : t('providers.notConfigured')}
          </span>
        )}
      </div>
      <p className="mb-3 text-xs text-muted">{t('studio.subtitle')}</p>

      {pool?.reason && <p className="mb-3 text-xs text-warn">{pool.reason}</p>}

      {pool && (
        <div className="mb-3 flex flex-wrap gap-4 text-xs">
          <span className="text-muted">
            {t('studio.freeRemaining')}:{' '}
            <strong className={pool.free_remaining_today > 0 ? 'text-ok' : 'text-bad'}>
              {pool.free_remaining_today}
            </strong>
          </span>
          <span className="text-muted">
            {t('studio.paidKeys')}: <strong className="text-text">{pool.paid_keys}</strong>
            {pool.paid_unlimited && <span className="ml-1 text-muted">· {t('studio.noDailyCap')}</span>}
          </span>
        </div>
      )}

      {keys.length === 0 ? (
        <p className="mb-3 text-sm text-muted">{t('studio.empty')}</p>
      ) : (
        <div className="mb-3 space-y-1.5">
          {[...free, ...paid].map((k) => (
            <div
              key={k.label}
              className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-2.5 py-1.5"
            >
              <span className={k.kind === 'free' ? 'badge-warn' : 'badge-accent'}>
                {k.kind === 'free' ? t('studio.free') : t('studio.paid')}
              </span>
              <span className="font-mono text-xs">{k.label}</span>
              {k.kind === 'free' ? (
                <span className="text-xs text-muted">
                  {k.requests_today}/{k.rpd_limit} {t('studio.today')}
                </span>
              ) : (
                <span className="text-xs text-muted">
                  {k.total_requests} {t('studio.requestsTotal')}
                </span>
              )}
              {k.exhausted && <span className="badge-bad">{t('studio.exhausted')}</span>}
              {k.cooling_down && <span className="badge-warn">{t('studio.cooling')}</span>}
              {k.failures > 0 && (
                <span className="text-xs text-bad">
                  {t('studio.failures')}: {k.failures}
                </span>
              )}
              <button
                type="button"
                className="btn-ghost ml-auto px-2 py-0.5 text-xs"
                onClick={() => removeMutation.mutate(k.label)}
                disabled={removeMutation.isPending}
              >
                {t('common.delete')}
              </button>
            </div>
          ))}
        </div>
      )}

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (key.trim()) addMutation.mutate()
        }}
      >
        <div className="min-w-0 flex-1">
          <label className="label" htmlFor="studio-key-input">
            {t('studio.addKey')}
          </label>
          <input
            id="studio-key-input"
            className="input font-mono"
            type="password"
            autoComplete="off"
            placeholder="AIza…"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            data-testid="studio-key-input"
          />
        </div>
        <div>
          <label className="label" htmlFor="studio-key-kind">
            {t('studio.kind')}
          </label>
          <select
            id="studio-key-kind"
            className="input"
            value={kind}
            onChange={(e) => setKind(e.target.value as 'free' | 'paid')}
            data-testid="studio-key-kind"
          >
            <option value="free">{t('studio.free')}</option>
            <option value="paid">{t('studio.paid')}</option>
          </select>
        </div>
        <button
          type="submit"
          className="btn-primary"
          disabled={!key.trim() || addMutation.isPending}
          data-testid="studio-key-add"
        >
          {addMutation.isPending ? t('common.loading') : t('studio.add')}
        </button>
      </form>

      {error && (
        <p role="alert" className="mt-2 text-xs text-bad">
          {error}
        </p>
      )}

      <p className="mt-3 text-xs text-muted">
        <a
          className="link-accent"
          href="https://aistudio.google.com/apikey"
          target="_blank"
          rel="noreferrer"
        >
          {t('studio.getKey')}
        </a>
        {' · '}
        {t('studio.freeTierNote')}
      </p>
    </section>
  )
}
