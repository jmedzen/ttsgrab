import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, ApiRequestError, formatMs, formatUsd, type AudioFormat, type Policy, type TTSResult } from '../lib/api'
import AudioPlayer from '../components/AudioPlayer'
import VoiceSelect from '../components/VoiceSelect'

const SAMPLE_ZH = '你好，這是 ttsgrab 的語音合成測試。這一句用來確認繁體中文的發音與語氣。'
const SAMPLE_EN = 'Hello — this is a ttsgrab synthesis test. This sentence checks prosody and timing.'

const FORMATS: AudioFormat[] = ['mp3', 'wav', 'opus', 'flac']
const POLICIES: Policy[] = ['cheapest', 'fastest', 'best_quality', 'free_only', 'local_only']

export default function Playground() {
  const { t } = useTranslation()
  const [text, setText] = useState(SAMPLE_ZH)
  const [voice, setVoice] = useState<string | null>('zh-TW-HsiaoChenNeural')
  const [provider, setProvider] = useState<string>('')
  const [format, setFormat] = useState<AudioFormat>('mp3')
  const [sampleRate, setSampleRate] = useState<string>('')
  const [speed, setSpeed] = useState(1)
  const [pitch, setPitch] = useState(0)
  const [volume, setVolume] = useState(1)
  const [policy, setPolicy] = useState<Policy>('cheapest')
  const [strict, setStrict] = useState(false)
  const [bypassCache, setBypassCache] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [advanced, setAdvanced] = useState(false)

  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<TTSResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })

  const activeProvider = useMemo(
    () => providersQuery.data?.providers.find((p) => p.name === provider) ?? null,
    [providersQuery.data, provider],
  )

  const body = useMemo(
    () => ({
      text,
      voice: voice ?? '',
      provider: provider || null,
      format,
      sample_rate: sampleRate ? Number(sampleRate) : null,
      speed,
      pitch,
      volume,
      policy,
      strict,
      bypass_cache: bypassCache,
      prompt: prompt || null,
    }),
    [text, voice, provider, format, sampleRate, speed, pitch, volume, policy, strict, bypassCache, prompt],
  )

  const estimateQuery = useQuery({
    queryKey: ['estimate', body],
    queryFn: () => api.estimate(body),
    enabled: text.length > 0 && !!voice,
    staleTime: 10_000,
  })

  // Free a previous blob URL when a new result replaces it.
  useEffect(() => {
    return () => {
      if (result) URL.revokeObjectURL(result.blobUrl)
    }
  }, [result])

  async function run() {
    if (!voice || !text.trim()) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.synthesize({
        ...body,
        format,
      })
      setResult(res)
    } catch (err) {
      if (err instanceof ApiRequestError) {
        const fallback = err.error.suggested_fallback?.length
          ? ` — try: ${err.error.suggested_fallback.join(', ')}`
          : ''
        setError(`[${err.error.code}] ${err.error.message}${fallback}`)
      } else {
        setError(String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
      <section className="card p-5">
        <header className="mb-4">
          <h1 className="text-xl font-semibold">{t('playground.title')}</h1>
          <p className="text-sm text-ink-500">{t('playground.subtitle')}</p>
        </header>

        <label className="label" htmlFor="text">
          {t('playground.textLabel')}
        </label>
        <textarea
          id="text"
          className="input h-40 resize-y font-normal leading-relaxed"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t('playground.textPlaceholder')}
          data-testid="text-input"
        />
        <div className="mt-1 flex items-center justify-between text-xs text-ink-500">
          <span>
            {text.length} {t('playground.characters')}
          </span>
          <span className="flex gap-2">
            <button type="button" className="hover:text-accent-500" onClick={() => setText(SAMPLE_ZH)}>
              繁中範例
            </button>
            <button type="button" className="hover:text-accent-500" onClick={() => setText(SAMPLE_EN)}>
              EN sample
            </button>
          </span>
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="provider">
              {t('playground.providerLabel')}
            </label>
            <select
              id="provider"
              className="input"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              data-testid="provider-select"
            >
              <option value="">— auto ({policy}) —</option>
              {providersQuery.data?.providers
                .filter((p) => p.enabled)
                .map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                    {!p.official ? ' (unofficial)' : ''}
                  </option>
                ))}
            </select>
          </div>
          <div>
            <label className="label">{t('playground.voiceLabel')}</label>
            <VoiceSelect value={voice} onChange={(v) => setVoice(v.voice_id)} />
          </div>
        </div>

        {activeProvider && !activeProvider.official && (
          <p className="mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
            ⚠ {t('playground.warnUnofficial')} {t('playground.warnTos')}
            {activeProvider.tos_url && (
              <>
                {' '}
                <a className="underline" href={activeProvider.tos_url} target="_blank" rel="noreferrer">
                  ToS
                </a>
              </>
            )}
          </p>
        )}

        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div>
            <label className="label" htmlFor="format">
              {t('playground.formatLabel')}
            </label>
            <select
              id="format"
              className="input"
              value={format}
              onChange={(e) => setFormat(e.target.value as AudioFormat)}
            >
              {FORMATS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="policy">
              {t('playground.policyLabel')}
            </label>
            <select
              id="policy"
              className="input"
              value={policy}
              onChange={(e) => setPolicy(e.target.value as Policy)}
            >
              {POLICIES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="sr">
              {t('playground.sampleRateLabel')}
            </label>
            <select id="sr" className="input" value={sampleRate} onChange={(e) => setSampleRate(e.target.value)}>
              <option value="">auto</option>
              {[16000, 22050, 24000, 44100, 48000].map((r) => (
                <option key={r} value={r}>
                  {r} Hz
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <Slider label={t('playground.speedLabel')} value={speed} min={0.5} max={2} step={0.05} onChange={setSpeed} suffix={`${speed.toFixed(2)}×`} />
          <Slider label={t('playground.pitchLabel')} value={pitch} min={-6} max={6} step={0.5} onChange={setPitch} suffix={`${pitch > 0 ? '+' : ''}${pitch} st`} />
          <Slider label={t('playground.volumeLabel')} value={volume} min={0} max={1} step={0.05} onChange={setVolume} suffix={`${Math.round(volume * 100)}%`} />
        </div>

        <button
          type="button"
          className="mt-4 text-xs text-ink-500 underline hover:text-accent-500"
          onClick={() => setAdvanced((v) => !v)}
        >
          {advanced ? '▾' : '▸'} {t('playground.advanced')}
        </button>

        {advanced && (
          <div className="mt-3 space-y-3 rounded-lg border border-ink-200 p-3 dark:border-ink-700">
            <div>
              <label className="label" htmlFor="prompt">
                {t('playground.promptLabel')}
              </label>
              <input id="prompt" className="input" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Speak warmly, slightly slower." />
              <p className="mt-1 text-xs text-ink-500">{t('playground.promptHint')}</p>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} />
              <span>
                {t('playground.strictLabel')}
                <span className="block text-xs text-ink-500">{t('playground.strictHint')}</span>
              </span>
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={bypassCache} onChange={(e) => setBypassCache(e.target.checked)} />
              {t('playground.bypassCacheLabel')}
            </label>
          </div>
        )}

        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            className="btn-primary px-4 py-2"
            onClick={run}
            disabled={busy || !text.trim() || !voice}
            data-testid="synthesize"
          >
            {busy ? t('playground.synthesizing') : t('playground.synthesize')}
          </button>
          {estimateQuery.data && (
            <span className="text-xs text-ink-500" data-testid="estimate">
              ≈ {formatMs(estimateQuery.data.estimated_duration_ms)} ·{' '}
              {formatUsd(estimateQuery.data.estimated_cost_micro_usd)}
              {estimateQuery.data.within_free_tier && ' · free tier'}
            </span>
          )}
        </div>

        {error && (
          <p
            className="mt-4 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300"
            data-testid="error"
          >
            {error}
          </p>
        )}

        <div className="mt-5" data-testid="result">
          {result ? (
            <div className="space-y-3">
              <AudioPlayer
                key={result.blobUrl}
                src={result.blobUrl}
                format={result.format}
                autoPlay
              />
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="badge bg-accent-600/10 text-accent-600 dark:text-accent-400">
                  {result.provider}
                </span>
                <span className="badge bg-ink-100 text-ink-600 dark:bg-ink-800 dark:text-ink-300">
                  {result.voiceId}
                </span>
                <span className="text-ink-500">{formatMs(result.latencyMs)}</span>
                <span className="text-ink-500">{formatUsd(result.costMicroUsd)}</span>
                {result.cached && (
                  <span className="badge bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400">
                    {t('playground.cachedNotice')}
                  </span>
                )}
                {result.fallbackFrom && (
                  <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                    {t('playground.fallbackNotice', { from: result.fallbackFrom, to: result.provider })}
                  </span>
                )}
                <a className="btn-ghost ml-auto px-2 py-1 text-xs" href={result.blobUrl} download={`ttsgrab.${result.format}`}>
                  {t('common.download')}
                </a>
              </div>
              {result.unsupported.length > 0 && (
                <p className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
                  {t('playground.unsupportedTitle')}: {result.unsupported.join(', ')}
                </p>
              )}
            </div>
          ) : (
            <p className="text-sm text-ink-500">{t('playground.noResult')}</p>
          )}
        </div>
      </section>

      <aside className="space-y-4">
        <section className="card p-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-ink-500">
            {t('playground.params')}
          </h2>
          <dl className="space-y-2 text-sm">
            <Row label="chars" value={String(text.length)} />
            <Row label="voice" value={voice ?? '—'} mono />
            <Row label="provider" value={provider || `auto (${policy})`} />
            <Row label="format" value={`${format}${sampleRate ? ` @ ${sampleRate}Hz` : ''}`} />
            {estimateQuery.data && (
              <>
                <Row label="est. duration" value={formatMs(estimateQuery.data.estimated_duration_ms)} />
                <Row label="est. cost" value={formatUsd(estimateQuery.data.estimated_cost_micro_usd)} />
              </>
            )}
          </dl>
        </section>

        <section className="card p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-ink-500">
            {t('nav.providers')}
          </h2>
          <ul className="space-y-1.5 text-sm">
            {providersQuery.data?.providers.map((p) => (
              <li key={p.name} className="flex items-center gap-2">
                <span
                  className={
                    p.enabled
                      ? 'h-2 w-2 shrink-0 rounded-full bg-emerald-500'
                      : 'h-2 w-2 shrink-0 rounded-full bg-ink-400'
                  }
                />
                <span className={p.enabled ? '' : 'text-ink-500'}>{p.name}</span>
                {!p.official && (
                  <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                    unofficial
                  </span>
                )}
                {p.reason && <span className="truncate text-xs text-ink-500">{p.reason}</span>}
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  )
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  suffix,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (v: number) => void
  suffix: string
}) {
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="label mb-0">{label}</span>
        <span className="font-mono text-xs text-ink-500">{suffix}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label={label}
      />
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-ink-500">{label}</dt>
      <dd className={mono ? 'truncate font-mono text-xs' : 'truncate text-right'}>{value}</dd>
    </div>
  )
}
