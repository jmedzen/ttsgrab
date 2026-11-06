import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, type SettingKind } from '../lib/api'
import QueryError from '../components/QueryError'

/**
 * Every setting the gateway reads, in one place, saved to config.yaml.
 *
 * This page exists because configuration used to be scattered across a YAML file, a `.env`,
 * environment variables and the database — so "how do I change the Google API key?" had
 * four possible answers and nobody could tell which applied. For a single-tenant box there
 * is no reason for that.
 *
 * Two rules the UI must honour:
 *
 * 1. **A masked secret is not a value.** `••••1234` is what the API returns; showing it in
 *    an input and saving it back would replace the real key with the mask. A password field
 *    that is empty unless you type is the only safe presentation.
 * 2. **An env override is not a bug.** Environment variables outrank config.yaml by
 *    design, so a value written here may have no effect. Saying which keys are shadowed is
 *    better than letting someone change a setting that does nothing.
 */

const GROUPS: { title: string; keys: string[] }[] = [
  {
    title: 'providers',
    keys: [
      'edge.enabled',
      'free_web.enabled',
      'free_web.gtts_enabled',
      'free_web.translate_tts_enabled',
      'google_cloud.enabled',
      'google_cloud.api_key',
      'google_cloud.credentials_file',
      'google_cloud.monthly_budget_micro_usd',
      'google_ai_studio.enabled',
      'google_ai_studio.free_api_keys',
      'google_ai_studio.paid_api_keys',
      'azure.enabled',
      'azure.api_key',
      'azure.region',
    ],
  },
  {
    title: 'routing',
    keys: [
      'router.default_policy',
      'router.fallback_chain',
      'router.monthly_budget_micro_usd',
    ],
  },
  {
    title: 'output',
    keys: [
      'write_metadata',
      'longform.chunk_threshold',
      'longform.max_concurrency',
      'cache.enabled',
      'cache.dir',
      'cache.max_bytes',
      'cache.ttl_seconds',
    ],
  },
  {
    title: 'security',
    keys: [
      'security.admin_token',
      'security.allow_localhost_without_token',
      'security.rate_limit_per_minute',
    ],
  },
  {
    title: 'favourites',
    keys: ['favourites.seed_enabled', 'favourites.seed'],
  },
]

/** Human labels; the raw dotted path is shown underneath as the thing config.yaml stores. */
const LABELS: Record<string, string> = {
  'edge.enabled': 'Edge TTS',
  'free_web.enabled': 'Free web providers',
  'free_web.gtts_enabled': 'gTTS',
  'free_web.translate_tts_enabled': 'Google Translate TTS',
  'google_cloud.enabled': 'Google Cloud TTS',
  'google_cloud.api_key': 'Google Cloud API key',
  'google_cloud.credentials_file': 'Service account JSON path',
  'google_cloud.monthly_budget_micro_usd': 'Monthly budget (micro-USD)',
  'google_ai_studio.enabled': 'Google AI Studio',
  'google_ai_studio.free_api_keys': 'AI Studio free keys',
  'google_ai_studio.paid_api_keys': 'AI Studio paid keys',
  'azure.enabled': 'Azure Speech',
  'azure.api_key': 'Azure key',
  'azure.region': 'Azure region',
  'router.default_policy': 'Default routing policy',
  'router.fallback_chain': 'Fallback chain',
  'router.monthly_budget_micro_usd': 'Global monthly budget (micro-USD)',
  write_metadata: 'Write provenance into audio files',
  'longform.chunk_threshold': 'Long-form split threshold (chars)',
  'longform.max_concurrency': 'Long-form concurrency',
  'cache.enabled': 'Disk cache',
  'cache.dir': 'Cache directory',
  'cache.max_bytes': 'Cache size limit (bytes)',
  'cache.ttl_seconds': 'Cache TTL (seconds)',
  'security.admin_token': 'Admin token',
  'security.allow_localhost_without_token': 'Allow localhost without a token',
  'security.rate_limit_per_minute': 'Rate limit (requests/minute)',
  'favourites.seed_enabled': 'Seed favourites on first boot',
  'favourites.seed': 'Seeded favourites',
}

function isSecret(kind: SettingKind): boolean {
  return kind === 'secret' || kind === 'secret_list'
}

export default function Settings() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const query = useQuery({ queryKey: ['settings'], queryFn: api.settings })

  // Edits are staged, not live: this page writes a file and needs a restart, so applying
  // on every keystroke would both spam the disk and make "did it save?" unanswerable.
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [saved, setSaved] = useState<string[] | null>(null)

  /** Every edit clears the "saved" banner: it describes the previous save, not this state. */
  function edit(key: string, value: unknown) {
    setSaved(null)
    setDraft((d) => ({ ...d, [key]: value }))
  }



  const envShadowed = useMemo(
    () => new Set((query.data?.env_overrides ?? []).map((o) => o.key)),
    [query.data],
  )

  const save = useMutation({
    mutationFn: () => api.saveSettings(draft),
    onSuccess: (res) => {
      // Order matters: the confirmation must survive the refetch that follows, so the
      // draft is cleared here and `saved` is only reset by the next edit.
      setDraft({})
      setSaved(res.written)
      void qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })

  if (query.isLoading) return <p className="text-sm text-muted">{t('common.loading')}</p>
  if (query.isError) {
    return <QueryError error={query.error} onRetry={() => void query.refetch()} />
  }
  if (!query.data) return null

  const { values, editable, path, exists } = query.data
  const dirty = Object.keys(draft).length > 0

  function renderField(key: string) {
    const kind = editable[key] as SettingKind | undefined
    if (!kind) return null
    const value = key in draft ? draft[key] : values[key]
    const secret = isSecret(kind)
    const shadowed = envShadowed.has(key)

    return (
      <label key={key} className="grid gap-1 py-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] sm:items-center sm:gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm">
            <span className="truncate">{LABELS[key] ?? key}</span>
            {shadowed && (
              <span
                className="shrink-0 rounded bg-warn/15 px-1 text-[10px] text-warn"
                title={t('config.envOverrideHint')}
              >
                {t('config.envOverride')}
              </span>
            )}
          </div>
          <code className="block truncate text-[11px] text-muted-soft">{key}</code>
        </div>

        <div className="min-w-0">
          {kind === 'bool' ? (
            <input
              type="checkbox"
              checked={Boolean(value)}
              onChange={(e) => edit(key, e.target.checked)}
              data-testid={`setting-${key}`}
            />
          ) : kind === 'int' ? (
            <input
              type="number"
              className="input w-full"
              value={value == null ? '' : String(value)}
              onChange={(e) =>
                edit(key, e.target.value === '' ? null : Number(e.target.value))
              }
              data-testid={`setting-${key}`}
            />
          ) : kind === 'list' || kind === 'secret_list' ? (
            <input
              className="input w-full font-mono text-xs"
              // Comma-separated is enough for these lists and avoids a second component.
              placeholder={secret ? t('config.secretKeep') : ''}
              value={
                Array.isArray(value) && !secret
                  ? (value as string[]).join(', ')
                  : key in draft
                    ? String(draft[key] ?? '')
                    : ''
              }
              onChange={(e) =>
                edit(
                  key,
                  e.target.value
                    .split(',')
                    .map((v) => v.trim())
                    .filter(Boolean),
                )
              }
              data-testid={`setting-${key}`}
            />
          ) : secret ? (
            // Never pre-filled: the API only knows the masked form, and saving that back
            // would destroy the real secret.
            <input
              type="password"
              className="input w-full font-mono text-xs"
              placeholder={
                value ? `${String(value)} — ${t('config.secretKeep')}` : t('config.secretUnset')
              }
              value={key in draft ? String(draft[key] ?? '') : ''}
              onChange={(e) => edit(key, e.target.value)}
              data-testid={`setting-${key}`}
            />
          ) : (
            <input
              className="input w-full"
              value={value == null ? '' : String(value)}
              onChange={(e) => edit(key, e.target.value)}
              data-testid={`setting-${key}`}
            />
          )}
        </div>
      </label>
    )
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">{t('config.title')}</h1>
        <p className="text-sm text-muted">{t('config.subtitle')}</p>
      </header>

      <section className="card space-y-1 p-4 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <span className="text-muted">{t('config.file')}</span>{' '}
            <code className="font-mono text-xs">{path}</code>
            <span className="ml-2 text-xs text-muted-soft">
              {exists ? t('config.fileExists') : t('config.fileMissing')}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {dirty && (
              <span className="text-xs text-warn">
                {t('config.unsaved', { count: Object.keys(draft).length })}
              </span>
            )}
            <button
              type="button"
              className="btn-primary px-3 py-1.5 text-sm"
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate()}
              data-testid="settings-save"
            >
              {save.isPending ? t('config.saving') : t('config.save')}
            </button>
          </div>
        </div>

        {save.isError && <p className="text-xs text-bad">{String(save.error)}</p>}

        {saved && (
          <p className="rounded bg-ok/10 px-2 py-1 text-xs text-ok" data-testid="settings-saved">
            {t('config.savedNote', { count: saved.length })}
          </p>
        )}

        <p className="text-xs text-muted-soft">{t('config.restartNote')}</p>
        {envShadowed.size > 0 && (
          <p className="text-xs text-warn">{t('config.envOverrideNote')}</p>
        )}
      </section>

      {GROUPS.map((group) => {
        const keys = group.keys.filter((k) => editable[k])
        if (keys.length === 0) return null
        return (
          <section key={group.title} className="card p-4" data-testid={`settings-${group.title}`}>
            <h2 className="mb-1 text-sm font-semibold">{t(`config.group.${group.title}`)}</h2>
            <div className={clsx('divide-y divide-line')}>{keys.map(renderField)}</div>
          </section>
        )
      })}
    </div>
  )
}
