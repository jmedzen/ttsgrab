import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  api,
  formatBytes,
  formatMs,
  formatUsd,
  type AudioFormat,
  type CompareRow,
} from '../lib/api'
import AudioPlayer from '../components/AudioPlayer'

interface Target {
  provider: string
  voice_id: string
  key: string
}

let seq = 0
const newTarget = (provider = '', voice_id = ''): Target => ({
  provider,
  voice_id,
  key: `t${++seq}`,
})

export default function Compare() {
  const { t } = useTranslation()
  const [text, setText] = useState('同一段文字，一次比較多個供應商。這句用來聽出差異。')
  const [format, setFormat] = useState<AudioFormat>('mp3')
  const [targets, setTargets] = useState<Target[]>([
    newTarget('edge', 'zh-TW-HsiaoChenNeural'),
    newTarget('edge', 'zh-TW-YunJheNeural'),
  ])
  const voicesQuery = useQuery({ queryKey: ['voices', 'all'], queryFn: () => api.voices() })
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })

  const mutation = useMutation({
    mutationFn: () =>
      api.compare({
        text,
        format,
        targets: targets
          .filter((x) => x.provider && x.voice_id)
          .map(({ provider, voice_id }) => ({ provider, voice_id })),
      }),
  })

  const rows: CompareRow[] = mutation.data?.results ?? []
  const active = providersQuery.data?.providers.filter((p) => p.enabled) ?? []

  function updateTarget(key: string, patch: Partial<Target>) {
    setTargets((prev) => prev.map((x) => (x.key === key ? { ...x, ...patch } : x)))
  }

  const voicesFor = (providerName: string) =>
    (voicesQuery.data?.voices ?? []).filter((v) => v.provider === providerName)

  return (
    <div className="space-y-5">
      <section className="card p-5">
        <header className="mb-4">
          <h1 className="text-xl font-semibold">{t('compare.title')}</h1>
          <p className="text-sm text-muted">{t('compare.subtitle')}</p>
        </header>

        <textarea
          className="input h-24 resize-y leading-relaxed"
          value={text}
          onChange={(e) => setText(e.target.value)}
          data-testid="compare-text"
        />

        <div className="mt-4 space-y-2">
          {targets.map((target, index) => (
            <div key={target.key} className="flex flex-wrap items-center gap-2">
              <span className="w-6 shrink-0 text-sm text-muted">{index + 1}.</span>
              <select
                className="input w-44 shrink-0"
                value={target.provider}
                onChange={(e) => updateTarget(target.key, { provider: e.target.value, voice_id: '' })}
                aria-label={t('compare.target')}
              >
                <option value="">—</option>
                {active.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
              <select
                className="input min-w-0 flex-1"
                value={target.voice_id}
                onChange={(e) => updateTarget(target.key, { voice_id: e.target.value })}
                aria-label={t('playground.voiceLabel')}
              >
                <option value="">—</option>
                {voicesFor(target.provider).map((v) => (
                  <option key={v.voice_id} value={v.voice_id}>
                    {v.voice_id} · {v.languages[0]}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn-danger px-2 py-1 text-xs"
                onClick={() => setTargets((prev) => prev.filter((x) => x.key !== target.key))}
              >
                ✕
              </button>
            </div>
          ))}
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn-ghost"
            onClick={() => setTargets((prev) => [...prev, newTarget()])}
            disabled={targets.length >= 8}
          >
            + {t('compare.addTarget')}
          </button>
          <select
            className="input w-24"
            value={format}
            onChange={(e) => setFormat(e.target.value as AudioFormat)}
            aria-label={t('playground.formatLabel')}
          >
            {(['mp3', 'wav', 'opus', 'flac'] as AudioFormat[]).map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn-primary ml-auto px-4 py-2"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !text.trim()}
            data-testid="compare-run"
          >
            {mutation.isPending ? t('compare.running') : t('compare.run')}
          </button>
        </div>

        {mutation.error && (
          <p className="mt-3 rounded-lg border border-bad/45 bg-bad/10 px-3 py-2 text-sm text-bad bg-bad/15">
            {String(mutation.error)}
          </p>
        )}
      </section>

      {rows.length > 0 && (
        <section
          className="card overflow-x-auto p-0"
          data-testid="compare-results"
          // A scrollable region that is not focusable cannot be scrolled with a keyboard,
          // and without a role a screen reader does not announce that anything is there.
          role="region"
          aria-label={t('compare.resultsLabel')}
          tabIndex={0}
        >
          <table className="w-full min-w-[820px] text-sm">
            <thead className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
              <tr>
                <th className="px-4 py-2.5">{t('compare.target')}</th>
                <th className="px-4 py-2.5">Audio</th>
                <th className="px-3 py-2.5 text-right">{t('compare.firstByte')}</th>
                <th className="px-3 py-2.5 text-right">{t('compare.totalTime')}</th>
                <th className="px-3 py-2.5 text-right">{t('compare.duration')}</th>
                <th className="px-3 py-2.5 text-right">{t('compare.rtf')}</th>
                <th className="px-3 py-2.5 text-right">{t('compare.bytes')}</th>
                <th className="px-3 py-2.5 text-right">{t('compare.cost')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr
                  key={`${row.provider}-${row.voice_id}-${i}`}
                  className="border-b border-line last:border-0 border-line/60"
                >
                  <td className="px-4 py-3">
                    <div className="font-medium">{row.provider}</div>
                    <div className="font-mono text-xs text-muted">{row.voice_id}</div>
                    {row.error && (
                      <div className="mt-1 text-xs text-bad">{row.error}</div>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {row.audio_url ? (
                      <AudioPlayer src={row.audio_url} format={row.format} compact className="min-w-[220px]" />
                    ) : (
                      <span className="text-xs text-muted">—</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-right font-mono text-xs">
                    {formatMs(row.first_byte_ms)}
                  </td>
                  <td className="px-3 py-3 text-right font-mono text-xs">
                    {formatMs(row.latency_ms)}
                  </td>
                  <td className="px-3 py-3 text-right font-mono text-xs">
                    {formatMs(row.duration_ms)}
                  </td>
                  <td className="px-3 py-3 text-right font-mono text-xs">
                    {row.rtf !== null && row.rtf !== undefined ? row.rtf.toFixed(3) : '—'}
                  </td>
                  <td className="px-3 py-3 text-right font-mono text-xs">
                    {row.bytes ? formatBytes(row.bytes) : '—'}
                  </td>
                  <td className="px-3 py-3 text-right font-mono text-xs">
                    {formatUsd(row.cost_micro_usd ?? 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {mutation.data && (
        <p className="text-xs text-muted">
          {mutation.data.text_chars} {t('playground.characters')} · RTF = synthesis time ÷ audio
          duration (lower is faster than real time)
        </p>
      )}
    </div>
  )
}
