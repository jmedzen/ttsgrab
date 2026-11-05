import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type Voice } from '../lib/api'
import clsx from 'clsx'
import { useTranslation } from 'react-i18next'

interface Props {
  value: string | null
  onChange: (voice: Voice) => void
  filterProvider?: string | null
  className?: string
  /** Id of the visible <label>, so the popup button has an accessible name. */
  ariaLabelledBy?: string
}

/**
 * Voice picker: provider-grouped list with search and language filter.
 * Providers that are disabled never show up, because /v1/voices only returns enabled ones.
 */
export default function VoiceSelect({
  value,
  onChange,
  filterProvider,
  className,
  ariaLabelledBy,
}: Props) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  // A popup that only closes when you click one of its own options traps the user: they
  // have to pick something, or reload. Escape and an outside click are the two things
  // everyone tries.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    const onPointer = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onPointer)
    }
  }, [open])

  const { data, isLoading } = useQuery({
    queryKey: ['voices', filterProvider ?? 'all'],
    queryFn: () => api.voices({ provider: filterProvider ?? undefined }),
  })

  const grouped = useMemo(() => {
    const voices = data?.voices ?? []
    const q = search.trim().toLowerCase()
    const filtered = q
      ? voices.filter(
          (v) =>
            v.voice_id.toLowerCase().includes(q) ||
            v.name.toLowerCase().includes(q) ||
            v.languages.some((l) => l.toLowerCase().includes(q)),
        )
      : voices
    const byProvider = new Map<string, Voice[]>()
    for (const v of filtered) {
      const list = byProvider.get(v.provider) ?? []
      list.push(v)
      byProvider.set(v.provider, list)
    }
    return byProvider
  }, [data, search])

  const selected = useMemo(
    () => data?.voices.find((v) => v.voice_id === value) ?? null,
    [data, value],
  )

  return (
    <div className={clsx('relative', className)} ref={containerRef}>
      <button
        type="button"
        className="input flex items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
        data-testid="voice-select"
        // Without these a screen reader announces a plain button and gives no hint that
        // a list will appear, or whether it currently is.
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={ariaLabelledBy}
      >
        <span className="truncate">
          {isLoading
            ? t('common.loading')
            : selected
              ? `${selected.name} · ${selected.languages[0]}`
              : value ?? '—'}
        </span>
        <span className="ml-2 shrink-0 text-ink-400">▾</span>
      </button>

      {open && (
        <div className="absolute z-30 mt-1 max-h-80 w-full overflow-hidden rounded-lg border border-ink-200 bg-white shadow-lg dark:border-ink-700 dark:bg-ink-900">
          <div className="border-b border-ink-200 p-2 dark:border-ink-700">
            <input
              className="input"
              placeholder={t('voices.searchPlaceholder')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              autoFocus
              data-testid="voice-search"
              aria-label={t('voices.searchPlaceholder')}
            />
          </div>
          <div className="max-h-64 overflow-y-auto">
            {grouped.size === 0 && (
              <p className="px-3 py-4 text-sm text-ink-500">{t('voices.empty')}</p>
            )}
            {[...grouped.entries()].map(([provider, voices]) => (
              <div key={provider}>
                <div className="sticky top-0 bg-ink-50 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-ink-500 dark:bg-ink-800">
                  {provider} · {voices.length}
                </div>
                {voices.slice(0, 60).map((v) => (
                  <button
                    key={v.voice_id}
                    type="button"
                    className={clsx(
                      'flex w-full items-center justify-between px-3 py-1.5 text-left text-sm hover:bg-ink-100 dark:hover:bg-ink-800',
                      v.voice_id === value && 'bg-ink-100 dark:bg-ink-800',
                    )}
                    onClick={() => {
                      onChange(v)
                      setOpen(false)
                    }}
                  >
                    <span className="truncate">
                      <span className="font-mono text-xs text-ink-500">{v.languages[0]}</span>{' '}
                      {v.name}
                    </span>
                    <span className="ml-2 flex shrink-0 gap-1">
                      {v.cost_class === 'free' && (
                        <span className="badge bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400">
                          {t('common.free')}
                        </span>
                      )}
                      {v.cloning && (
                        <span className="badge bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-400">
                          clone
                        </span>
                      )}
                    </span>
                  </button>
                ))}
                {voices.length > 60 && (
                  <p className="px-3 py-1 text-xs text-ink-500">
                    {t('voices.moreHidden', { count: voices.length - 60 })}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
