import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { api, type Voice } from '../lib/api'
import { usePins } from '../lib/usePins'

/**
 * The star button that lives immediately right of the voice picker.
 *
 * Sized and bordered like the picker itself so it reads as part of the form control rather
 * than as decoration. An earlier version put "☆ Add to favourites" in a full-width row
 * below the provider/voice row; that looked like a caption, and a reader looking for the
 * feature asked where it was twice. Position beats labelling.
 */
export function FavouriteStar({ voiceId }: { voiceId: string | null }) {
  const { t } = useTranslation()
  const { isPinned, toggle, atLimit, max } = usePins()

  // The provider and display name are stored alongside the id so the favourites list can
  // render a disabled provider's voice by name instead of showing a bare voice id.
  const { data } = useQuery({
    queryKey: ['voices', 'all'],
    queryFn: () => api.voices(),
    enabled: Boolean(voiceId),
  })
  const live = data?.voices.find((v) => v.voice_id === voiceId)
  const pinned = isPinned(voiceId)
  const blocked = !pinned && atLimit

  return (
    <button
      type="button"
      className={clsx(
        'flex w-11 shrink-0 items-center justify-center rounded-md border text-lg transition-colors',
        pinned
          ? 'border-warn/50 bg-warn/10 text-warn'
          : 'border-line bg-panel text-muted hover:border-warn/40 hover:text-warn',
        (!voiceId || blocked) && 'cursor-not-allowed opacity-40',
      )}
      disabled={!voiceId || blocked}
      onClick={() =>
        voiceId &&
        toggle(voiceId, { provider: live?.provider, name: live?.name })
      }
      aria-pressed={pinned}
      aria-label={pinned ? t('favourites.remove') : t('favourites.add')}
      data-testid="favourite-toggle"
      title={
        !voiceId
          ? t('favourites.noneSelected')
          : blocked
            ? t('favourites.limitReached', { max })
            : pinned
              ? t('favourites.remove')
              : t('favourites.add')
      }
    >
      <span aria-hidden="true">{pinned ? '★' : '☆'}</span>
    </button>
  )
}

interface RowProps {
  /** The voice currently selected in the Playground. */
  currentId: string | null
  /** Restricts which favourites are offered, mirroring the provider selector. */
  filterProvider?: string | null
  onPick: (voice: Voice) => void
}

/**
 * Quick-switch chips for the voices you keep coming back to.
 *
 * Renders nothing at all when there is nothing to show. An always-present box holding only
 * the words "Add to favourites" was the earlier version, and it read as a paragraph of
 * instructions sitting in the middle of the form.
 */
export default function Favourites({ currentId, filterProvider, onPick }: RowProps) {
  const { t } = useTranslation()
  const { rows, toggle, max, remove } = usePins()

  // Shares the picker's cache key, so this row costs no extra request and the two views
  // can never disagree about which provider a voice belongs to.
  const { data } = useQuery({
    queryKey: ['voices', filterProvider ?? 'all'],
    queryFn: () => api.voices({ provider: filterProvider ?? undefined }),
  })
  const available = useMemo(() => data?.voices ?? [], [data])

  // Server rows carry a denormalised name, so a favourite whose provider is currently
  // disabled still shows as a name with its provider rather than as a raw id.
  const resolved = rows.map((row) => {
    const live = available.find((v) => v.voice_id === row.voice_id)
    return {
      voiceId: row.voice_id,
      label: live?.name ?? row.name ?? row.voice_id,
      language: live?.languages[0] ?? null,
      provider: live?.provider ?? row.provider,
      live: Boolean(live),
    }
  })

  const staleCount = resolved.filter((r) => !r.live).length

  if (rows.length === 0) return null

  return (
    <div
      className="mt-2 flex flex-wrap items-center gap-1.5 text-xs"
      data-testid="favourites"
      aria-label={t('favourites.rowLabel')}
    >
      <span className="text-warn" aria-hidden="true">
        ★
      </span>
      {resolved.map((v) => (
        <span
          key={v.voiceId}
          className={clsx(
            'group inline-flex items-center gap-1 rounded-full border px-2 py-0.5',
            v.voiceId === currentId
              ? 'border-warn/50 bg-warn/10 text-text'
              : 'border-line text-muted hover:border-warn/40 hover:text-text',
            !v.live && 'opacity-50',
          )}
        >
          <button
            type="button"
            className="max-w-[15rem] truncate"
            onClick={() => {
              const live = available.find((x) => x.voice_id === v.voiceId)
              if (live) onPick(live)
            }}
            data-testid={`favourite-${v.voiceId}`}
            title={v.language ? `${v.label} · ${v.language}` : v.label}
          >
            {v.label}
          </button>
          <button
            type="button"
            className="leading-none text-muted-soft hover:text-bad"
            onClick={() => toggle(v.voiceId)}
            aria-label={t('favourites.removeNamed', { name: v.label })}
            title={t('favourites.removeNamed', { name: v.label })}
            data-testid={`favourite-remove-${v.voiceId}`}
          >
            ×
          </button>
        </span>
      ))}
      {staleCount > 0 && (
        // A `title` tooltip is invisible on touch and undiscoverable everywhere else.
        // Unavailable favourites are chips the user cannot act on, so the way to get rid of
        // them has to be a real, tappable control.
        <button
          type="button"
          className="rounded px-1.5 py-0.5 text-xs text-muted-soft underline decoration-dotted hover:text-fg"
          title={t('favourites.unavailableTitle')}
          onClick={() => {
            for (const v of resolved.filter((r) => !r.live)) remove.mutate(v.voiceId)
          }}
          data-testid="favourites-clear-unavailable"
        >
          {t('favourites.clearUnavailable')} ({staleCount})
        </button>
      )}
      <span className="ml-auto text-muted-soft">
        {rows.length}/{max}
      </span>
    </div>
  )
}
