import { useQuery } from '@tanstack/react-query'
import QueryError from '../components/QueryError'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, formatUsd, type Voice } from '../lib/api'
import AudioPlayer from '../components/AudioPlayer'
import { FavouriteStar } from '../components/Favourites'
import { usePins } from '../lib/usePins'

const PREVIEW_TEXT = '這是語音試聽。This is a preview.'

export default function Voices() {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [language, setLanguage] = useState('')
  const [provider, setProvider] = useState('')
  const [freeOnly, setFreeOnly] = useState(false)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const { rows: favourites } = usePins()

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['voices', { search, language, provider, freeOnly }],
    queryFn: () =>
      api.voices({
        search: search || undefined,
        language: language || undefined,
        provider: provider || undefined,
        freeOnly,
      }),
  })

  const languages = useMemo(() => data?.languages ?? [], [data])

  async function preview(voice: Voice) {
    setPreviewing(voice.voice_id)
    setPreviewError(null)
    try {
      const res = await api.synthesize({
        text: PREVIEW_TEXT,
        voice: voice.voice_id,
        provider: voice.provider,
        format: 'mp3',
      })
      if (previewUrl) URL.revokeObjectURL(previewUrl)
      setPreviewUrl(res.blobUrl)
    } catch (err) {
      setPreviewError(String(err))
    } finally {
      setPreviewing(null)
    }
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">{t('voices.title')}</h1>
        <p className="text-sm text-muted">{t('voices.subtitle')}</p>
      </header>

      {favourites.length > 0 && (
        <section className="card space-y-2 p-4" data-testid="voices-favourites">
          <div className="flex items-baseline justify-between">
            <h2 className="text-sm font-semibold">
              <span className="mr-1 text-warn">★</span>
              {t('voices.favouritesTitle')}
            </h2>
            <span className="text-xs text-muted-soft">
              {t('voices.favouritesCount', { count: favourites.length })}
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {favourites.map((f) => (
              <span
                key={f.voice_id}
                className="inline-flex items-center gap-1 rounded-full border border-warn/40 bg-warn/5 px-2.5 py-1 text-xs"
                data-testid={`voices-favourite-${f.voice_id}`}
              >
                <span className="max-w-[16rem] truncate" title={f.voice_id}>
                  {f.name ?? f.voice_id}
                </span>
                {f.provider && <span className="text-muted-soft">{f.provider}</span>}
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="card space-y-3 p-4">
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_160px_160px_auto]">
          <input
            className="input"
            placeholder={t('voices.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            data-testid="voices-search"
          />
          <select className="input" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">{t('common.all')} — provider</option>
            {(data?.providers ?? []).map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="">{t('common.all')} — language</option>
            {languages.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-2 whitespace-nowrap text-sm">
            <input type="checkbox" checked={freeOnly} onChange={(e) => setFreeOnly(e.target.checked)} />
            {t('voices.freeOnly')}
          </label>
        </div>

        {previewUrl && (
          <AudioPlayer key={previewUrl} src={previewUrl} autoPlay />
        )}
        {previewError && (
          <p className="text-xs text-bad">{previewError}</p>
        )}
      </section>

      <p className="text-xs text-muted">
        {isLoading
          ? t('common.loading')
          : isError
            ? t('common.loadFailed')
            : t('voices.total', { count: data?.total ?? 0 })}
      </p>

      {isError && <QueryError error={error} onRetry={() => void refetch()} className="mb-3" />}

      {!isError && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {(data?.voices ?? []).map((v) => (
            <div key={`${v.provider}-${v.voice_id}`} className="card flex flex-col gap-1.5 p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium" title={v.name}>
                    {v.name}
                  </div>
                  <div className="truncate font-mono text-[11px] text-muted">{v.voice_id}</div>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    className="btn-ghost px-2 py-1 text-xs"
                    onClick={() => preview(v)}
                    disabled={previewing === v.voice_id}
                  >
                    {previewing === v.voice_id ? '…' : '▶'}
                  </button>
                  <FavouriteStar voiceId={v.voice_id} />
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1">
                <span className="badge bg-panel2 text-muted">
                  {v.provider}
                </span>
                <span className="badge bg-panel2 text-muted">
                  {v.languages.slice(0, 2).join(' / ')}
                </span>
                {v.gender !== 'unknown' && (
                  <span className="badge bg-panel2 text-muted">
                    {v.gender}
                  </span>
                )}
                {v.cost_class === 'free' ? (
                  <span className="badge bg-ok/15 text-ok">
                    {t('common.free')}
                  </span>
                ) : (
                  <span className="badge bg-warn/15 text-warn">
                    {formatUsd(v.price_usd_per_mchar * 1_000_000)}/1M
                  </span>
                )}
                {v.cloning && (
                  <span className="badge bg-accent/15 text-accent">
                    clone
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {!isLoading && !isError && (data?.voices ?? []).length === 0 && (
        <p className="py-12 text-center text-sm text-muted">{t('voices.empty')}</p>
      )}
    </div>
  )
}
