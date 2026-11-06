import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type Voice } from '../lib/api'
import clsx from 'clsx'
import { useTranslation } from 'react-i18next'
import { loadPins, savePins, togglePin } from '../lib/pins'

interface Props {
  value: string | null
  onChange: (voice: Voice) => void
  /** When set, the list is restricted to this provider. */
  filterProvider?: string | null
  className?: string
  /** Id of the visible <label>, so the popup button has an accessible name. */
  ariaLabelledBy?: string
}

/**
 * Voice picker: pinned first, then provider-grouped, with search.
 *
 * Providers that are disabled never appear, because /v1/voices only returns enabled ones.
 *
 * `filterProvider` is not merely a filter. When the caller pins it to a provider, a voice
 * belonging to a different provider is no longer valid, so the parent is told to change it.
 * Filtering the list while keeping a stale selection would send a request the backend has
 * to reject, which the user experiences as "the provider is broken".
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
  const [pins, setPins] = useState<string[]>(() => loadPins())
  const containerRef = useRef<HTMLDivElement>(null)

  // A popup that only closes when you click one of its own options traps the user: they
  // have to pick something, or reload. Escape and an outside click are what everyone tries.
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

  const { data, isLoading, isError } = useQuery({
    queryKey: ['voices', filterProvider ?? 'all'],
    queryFn: () => api.voices({ provider: filterProvider ?? undefined }),
  })

  const voices = useMemo(() => data?.voices ?? [], [data])

  const selected = useMemo(
    () => voices.find((v) => v.voice_id === value) ?? null,
    [voices, value],
  )

  // Follow the provider: when the parent restricts the list, a selection outside it is
  // stale, so pick the first voice the new provider can actually speak. Without this the
  // form can read "provider = edge" while still holding a Google voice.
  useEffect(() => {
    if (!filterProvider || !voices.length) return
    if (selected && selected.provider === filterProvider) return
    const replacement = voices.find((v) => v.provider === filterProvider)
    if (replacement) onChange(replacement)
    // `onChange` is deliberately omitted: callers pass an inline arrow, so depending on it
    // would re-run this on every render and keep overriding the user's own choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterProvider, voices, selected?.voice_id])

  const pinned = useMemo(
    () =>
      pins
        .map((id) => voices.find((v) => v.voice_id === id))
        .filter((v): v is Voice => Boolean(v)),
    [pins, voices],
  )

  const onTogglePin = (voiceId: string) => {
    const next = togglePin(pins, voiceId)
    setPins(next)
    savePins(next)
  }

  const grouped = useMemo(() => {
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
  }, [voices, search])

  const renderRow = (v: Voice, opts: { pinnedRow?: boolean } = {}) => {
    const isPinned = pins.includes(v.voice_id)
    return (
      <div
        key={`${opts.pinnedRow ? 'pin-' : ''}${v.voice_id}`}
        className={clsx(
          'group flex w-full items-center justify-between gap-2 px-3 py-1.5 text-sm',
          v.voice_id === value ? 'bg-accent/15' : 'hover:bg-panel2',
        )}
      >
        <button
          type="button"
          className="min-w-0 flex-1 truncate text-left"
          onClick={() => {
            onChange(v)
            setOpen(false)
          }}
        >
          <span className="font-mono text-xs text-muted">{v.languages[0]}</span> {v.name}
        </button>
        <span className="flex shrink-0 items-center gap-1">
          {v.cost_class === 'free' && <span className="badge-ok">{t('common.free')}</span>}
          {v.cloning && <span className="badge-accent">clone</span>}
          <button
            type="button"
            className={clsx(
              'rounded px-1 text-sm leading-none transition-opacity',
              isPinned ? 'text-warn' : 'text-muted-soft opacity-0 group-hover:opacity-100',
            )}
            aria-pressed={isPinned}
            aria-label={
              isPinned ? t('voices.unpin', { name: v.name }) : t('voices.pin', { name: v.name })
            }
            title={
              isPinned ? t('voices.unpin', { name: v.name }) : t('voices.pin', { name: v.name })
            }
            onClick={() => onTogglePin(v.voice_id)}
            // Scoped by section: a pinned voice is rendered twice (once in the pinned
            // group, once in its provider), and duplicate test ids would make both the
            // tests and any DOM-inspection tooling ambiguous.
            data-testid={`pin-${opts.pinnedRow ? 'pinned-' : ''}${v.voice_id}`}
          >
            {isPinned ? '★' : '☆'}
          </button>
        </span>
      </div>
    )
  }

  return (
    <div className={clsx('relative', className)} ref={containerRef}>
      <button
        type="button"
        className="input flex items-center justify-between text-left"
        onClick={() => setOpen((v) => !v)}
        data-testid="voice-select"
        // Without these a screen reader announces a plain button and gives no hint that a
        // list will appear, or whether it currently is.
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={ariaLabelledBy}
      >
        <span className="truncate">
          {isLoading
            ? t('common.loading')
            : selected
              ? `${selected.name} · ${selected.languages[0]}`
              : (value ?? '—')}
        </span>
        <span className="ml-2 shrink-0 text-muted-soft">▾</span>
      </button>

      {open && (
        <div className="card absolute z-40 mt-1 max-h-80 w-full overflow-hidden shadow-lg">
          <div className="border-b border-line p-2">
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
            {isError && (
              // An empty list caused by a 401 looks identical to "this provider has no
              // voices"; say which it is, or the user has nothing to act on.
              <p className="px-3 py-4 text-sm text-bad" role="alert">
                {t('voices.loadFailed')}
              </p>
            )}
            {!isError && grouped.size === 0 && (
              <p className="px-3 py-4 text-sm text-muted">{t('voices.empty')}</p>
            )}

            {pinned.length > 0 && (
              <>
                <div className="sticky top-0 z-10 bg-panel2 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-warn">
                  ★ {t('voices.pinned')} · {pinned.length}
                </div>
                {pinned.map((v) => renderRow(v, { pinnedRow: true }))}
              </>
            )}

            {[...grouped.entries()].map(([provider, list]) => (
              <div key={provider}>
                <div className="sticky top-0 z-10 bg-panel2 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-muted">
                  {provider} · {list.length}
                </div>
                {list.slice(0, 60).map((v) => renderRow(v))}
                {list.length > 60 && (
                  <p className="px-3 py-1 text-xs text-muted">
                    {t('voices.moreHidden', { count: list.length - 60 })}
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
