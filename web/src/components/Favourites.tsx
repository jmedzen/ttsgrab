import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { api, type Voice } from '../lib/api'
import { usePins } from '../lib/usePins'

interface Props {
  /** The voice currently selected in the Playground. */
  currentId: string | null
  /** Restricts which favourites are offered, mirroring the provider selector. */
  filterProvider?: string | null
  onPick: (voice: Voice) => void
}

/**
 * Favourite voices, one click from the Playground.
 *
 * The star in the picker already worked, but it was two levels down: open the dropdown,
 * find the row, hit a small star that is invisible until you hover it. This puts the two
 * actions where the playback controls are — star the voice you are listening to, and jump
 * between the ones you keep coming back to.
 *
 * Choosing a favourite deliberately does **not** synthesise. Switching what you are about
 * to say and pressing Synthesise is one decision; making the second click spend money (or
 * a free request) is not something the user asked for here.
 */
export default function Favourites({ currentId, filterProvider, onPick }: Props) {
  const { t } = useTranslation()
  const { pins, isPinned, toggle, atLimit, max } = usePins()

  // Shares the picker's cache key, so opening this row costs no extra request and the two
  // views can never disagree about which provider a voice belongs to.
  const { data } = useQuery({
    queryKey: ['voices', filterProvider ?? 'all'],
    queryFn: () => api.voices({ provider: filterProvider ?? undefined }),
  })
  const available = useMemo(() => data?.voices ?? [], [data])
  const current = useMemo(
    () => available.find((v) => v.voice_id === currentId) ?? null,
    [available, currentId],
  )

  const currentPinned = isPinned(current?.voice_id ?? null)
  const resolved = pins
    .map((id) => available.find((v) => v.voice_id === id))
    .filter((v): v is Voice => Boolean(v))

  // Pin ids that no longer resolve (provider disabled, worker evicted). Shown so the user
  // can see why a favourite vanished and clear it, rather than silently losing it.
  const stale = pins.filter((id) => !available.some((v) => v.voice_id === id))

  return (
    <div className="mt-3 rounded-lg border border-line bg-panel2/60 px-3 py-2" data-testid="favourites">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={clsx(
            'flex items-center gap-1.5 rounded px-2 py-1 text-sm transition-colors',
            currentPinned
              ? 'text-warn hover:bg-warn/10'
              : 'text-muted hover:bg-panel2 hover:text-warn',
            !current && 'cursor-not-allowed opacity-40',
          )}
          disabled={!current || (!currentPinned && atLimit)}
          onClick={() => current && toggle(current.voice_id)}
          aria-pressed={currentPinned}
          data-testid="favourite-toggle"
          title={
            !current
              ? t('favourites.noneSelected')
              : !currentPinned && atLimit
                ? t('favourites.limitReached', { max })
                : currentPinned
                  ? t('favourites.remove')
                  : t('favourites.add')
          }
        >
          <span className="text-base leading-none">{currentPinned ? '★' : '☆'}</span>
          <span>{currentPinned ? t('favourites.saved') : t('favourites.add')}</span>
        </button>

        <span className="text-muted-soft">|</span>

        {resolved.length === 0 && pins.length === 0 && (
          <span className="text-xs text-muted">{t('favourites.emptyHint')}</span>
        )}

        {resolved.map((v) => (
          <span
            key={v.voice_id}
            className={clsx(
              'group inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs',
              v.voice_id === current?.voice_id
                ? 'border-warn/50 bg-warn/10 text-text'
                : 'border-line text-muted hover:border-warn/40 hover:text-text',
            )}
          >
            <button
              type="button"
              className="max-w-[16rem] truncate"
              onClick={() => onPick(v)}
              data-testid={`favourite-${v.voice_id}`}
              title={`${v.name} · ${v.languages[0]}`}
            >
              {v.name}
            </button>
            <button
              type="button"
              className="leading-none text-muted-soft hover:text-bad"
              onClick={() => toggle(v.voice_id)}
              aria-label={t('favourites.removeNamed', { name: v.name })}
              title={t('favourites.removeNamed', { name: v.name })}
              data-testid={`favourite-remove-${v.voice_id}`}
            >
              ×
            </button>
          </span>
        ))}

        {stale.length > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded border border-line px-2 py-0.5 text-xs text-muted-soft"
            title={stale.join(', ')}
          >
            {t('favourites.unavailable', { count: stale.length })}
            <button
              type="button"
              className="leading-none hover:text-bad"
              onClick={() => stale.forEach((id) => toggle(id))}
              aria-label={t('favourites.clearUnavailable')}
              data-testid="favourite-clear-stale"
            >
              ×
            </button>
          </span>
        )}

        {pins.length > 0 && (
          <span className="ml-auto text-xs text-muted-soft">
            {pins.length}/{max}
          </span>
        )}
      </div>
    </div>
  )
}
