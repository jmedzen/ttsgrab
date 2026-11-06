import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import StudioKeyPool from '../components/StudioKeyPool'
import { api, formatMs } from '../lib/api'

export default function Providers() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })
  const workersQuery = useQuery({ queryKey: ['workers'], queryFn: api.workers })

  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:8880')
  const [kind, setKind] = useState<'tts' | 'openai_compat'>('tts')
  const [formError, setFormError] = useState<string | null>(null)

  const addWorker = useMutation({
    mutationFn: () => api.addWorker({ name, base_url: baseUrl, kind }),
    onSuccess: () => {
      setName('')
      void qc.invalidateQueries({ queryKey: ['workers'] })
      void qc.invalidateQueries({ queryKey: ['providers'] })
      void qc.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (e) => setFormError(String(e)),
  })

  const checkWorker = useMutation({
    mutationFn: (id: string) => api.checkWorker(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['workers'] }),
  })

  const removeWorker = useMutation({
    mutationFn: (id: string) => api.deleteWorker(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['workers'] })
      void qc.invalidateQueries({ queryKey: ['voices'] })
    },
  })

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold">{t('providers.title')}</h1>
        <p className="text-sm text-muted">{t('providers.subtitle')}</p>
      </header>

      <div className="grid gap-2 md:grid-cols-2">
        {(providersQuery.data?.providers ?? []).map((p) => (
          <div key={p.name} className="card p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2">
                <span
                  className={
                    p.enabled
                      ? 'h-2.5 w-2.5 rounded-full bg-ok'
                      : 'h-2.5 w-2.5 rounded-full bg-muted-soft'
                  }
                />
                <h2 className="font-medium">{p.name}</h2>
                {p.official ? (
                  <span className="badge bg-panel2 text-muted">
                    {t('providers.official')}
                  </span>
                ) : (
                  <span className="badge bg-warn/15 text-warn">
                    {t('providers.unofficial')}
                  </span>
                )}
                {p.stability !== 'stable' && (
                  <span className="badge bg-warn/15 text-warn">
                    {p.stability}
                  </span>
                )}
              </div>
              <span className="shrink-0 text-xs text-muted">{p.kind}</span>
            </div>

            <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-muted">{t('providers.voicesCount')}</dt>
                <dd>{p.voices}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted">cost</dt>
                <dd>{p.cost_class}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted">{t('providers.latency')}</dt>
                <dd>{formatMs(p.health?.ewma_ms ?? null)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted">failures</dt>
                <dd>{p.health?.failures ?? 0}</dd>
              </div>
            </dl>

            {/* The reason is a config key such as `free_web.enabled=false`. Useful when you
                are the one editing the config, noise when you are not — so it is a tooltip,
                and the visible text says what it actually means. */}
            {p.reason && (
              <p className="mt-2 rounded bg-panel2 px-2 py-1 text-xs text-muted" title={p.reason}>
                {t('providers.notConfigured')}
              </p>
            )}
            {p.note && <p className="mt-2 text-xs text-muted">{p.note}</p>}
            {p.tos_url && (
              <a
                className="mt-1 inline-block text-xs text-accent underline"
                href={p.tos_url}
                target="_blank"
                rel="noreferrer"
              >
                Terms
              </a>
            )}
          </div>
        ))}
      </div>

      <StudioKeyPool />

      <section className="card p-4">
        <h2 className="mb-3 font-medium">{t('providers.workers')}</h2>

        {(workersQuery.data?.workers ?? []).length === 0 ? (
          <p className="text-sm text-muted">{t('providers.noWorkers')}</p>
        ) : (
          <div className="space-y-2">
            {(workersQuery.data?.workers ?? []).map((w) => (
              <div
                key={w.id}
                className="flex flex-wrap items-center gap-3 rounded-lg border border-line p-2.5"
              >
                <span
                  className={
                    w.healthy
                      ? 'h-2.5 w-2.5 shrink-0 rounded-full bg-ok'
                      : 'h-2.5 w-2.5 shrink-0 rounded-full bg-bad'
                  }
                  title={w.healthy ? t('providers.healthy') : t('providers.unhealthy')}
                />
                <span className="font-medium">{w.name}</span>
                <span className="truncate font-mono text-xs text-muted">{w.base_url}</span>
                <span className="badge bg-panel2 text-muted">
                  {w.kind}
                </span>
                <span className="text-xs text-muted">{w.voices} voices</span>
                {w.latency_ms_ewma !== null && (
                  <span className="text-xs text-muted">{formatMs(w.latency_ms_ewma)}</span>
                )}
                {w.last_error && (
                  <span className="truncate text-xs text-bad">
                    {t('providers.lastError')}: {w.last_error}
                  </span>
                )}
                <div className="ml-auto flex gap-1.5">
                  <button
                    type="button"
                    className="btn-ghost px-2 py-1 text-xs"
                    onClick={() => checkWorker.mutate(w.id)}
                    disabled={checkWorker.isPending}
                  >
                    {t('providers.checkNow')}
                  </button>
                  <button
                    type="button"
                    className="btn-danger px-2 py-1 text-xs"
                    onClick={() => removeWorker.mutate(w.id)}
                  >
                    {t('common.delete')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        <form
          className="mt-4 grid gap-2 sm:grid-cols-[140px_minmax(0,1fr)_130px_auto]"
          onSubmit={(e) => {
            e.preventDefault()
            setFormError(null)
            addWorker.mutate()
          }}
        >
          <input
            className="input"
            placeholder={t('providers.workerName')}
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            pattern="[a-zA-Z0-9_-]+"
            data-testid="worker-name"
          />
          <input
            className="input"
            placeholder={t('providers.workerHint')}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            required
            data-testid="worker-url"
          />
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value as 'tts' | 'openai_compat')}>
            <option value="tts">tts (native)</option>
            <option value="openai_compat">openai_compat</option>
          </select>
          <button type="submit" className="btn-primary" disabled={addWorker.isPending}>
            {t('providers.addWorker')}
          </button>
        </form>
        {formError && (
          <p className="mt-2 text-xs text-bad" data-testid="worker-error">
            {formError}
          </p>
        )}
      </section>
    </div>
  )
}
