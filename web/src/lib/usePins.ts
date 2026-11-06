/**
 * Favourite voices, read from and written to the gateway.
 *
 * These used to live in localStorage. That was wrong for what this product is: one person
 * or a small team running a single instance. Starring a voice on your laptop and not
 * finding it on your phone is not a "favourite", it is a bookmark. The server is the right
 * home, and `/v1/favourites` is a table of tens of rows with nothing sensitive in it.
 *
 * TanStack Query owns the cache so both the Playground star and the Voices page read one
 * source. Mutations are optimistic: starring is a UI gesture, and waiting for a round trip
 * before the star fills in feels broken on a slow link.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'
import { api, type FavouriteVoice } from './api'

/** Mirrors MAX_FAVOURITES in the API; the server is still the authority. */
export const MAX_PINS = 24

const KEY = ['favourites'] as const

export interface PinsApi {
  pins: string[]
  rows: FavouriteVoice[]
  isPinned: (voiceId: string | null) => boolean
  toggle: (voiceId: string, meta?: { provider?: string; name?: string }) => void
  /** Used to drop favourites whose provider has gone away. */
  remove: { mutate: (voiceId: string) => void }
  atLimit: boolean
  max: number
  isLoading: boolean
}

export function usePins(): PinsApi {
  const qc = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: KEY,
    queryFn: () => api.favourites(),
    // The list changes only when someone stars something, and it is shared, so a stale
    // copy for half a minute is harmless.
    staleTime: 30_000,
  })

  const rows = data?.favourites ?? []

  const add = useMutation({
    mutationFn: (v: { voiceId: string; provider?: string; name?: string }) => api.addFavourite(v),
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: KEY })
      const previous = qc.getQueryData<{ favourites: FavouriteVoice[] }>(KEY)
      qc.setQueryData<{ favourites: FavouriteVoice[] }>(KEY, {
        favourites: [
          ...(previous?.favourites ?? []),
          {
            voice_id: v.voiceId,
            provider: v.provider ?? null,
            name: v.name ?? null,
            position: previous?.favourites.length ?? 0,
            created_at: new Date().toISOString(),
          },
        ],
      })
      return { previous }
    },
    onError: (_e, _v, ctx) => {
      // Roll back: a star that silently failed is worse than one that never lit.
      if (ctx?.previous) qc.setQueryData(KEY, ctx.previous)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: KEY }),
  })

  const remove = useMutation({
    mutationFn: (voiceId: string) => api.removeFavourite(voiceId),
    onMutate: async (voiceId) => {
      await qc.cancelQueries({ queryKey: KEY })
      const previous = qc.getQueryData<{ favourites: FavouriteVoice[] }>(KEY)
      qc.setQueryData<{ favourites: FavouriteVoice[] }>(KEY, {
        favourites: (previous?.favourites ?? []).filter((f) => f.voice_id !== voiceId),
      })
      return { previous }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.previous) qc.setQueryData(KEY, ctx.previous)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: KEY }),
  })

  const pins = rows.map((r) => r.voice_id)

  const isPinned = useCallback(
    (voiceId: string | null) => Boolean(voiceId) && pins.includes(voiceId as string),
    [pins],
  )

  const toggle = useCallback(
    (voiceId: string, meta?: { provider?: string; name?: string }) => {
      if (pins.includes(voiceId)) {
        remove.mutate(voiceId)
        return
      }
      if (pins.length >= MAX_PINS) return
      add.mutate({ voiceId, provider: meta?.provider, name: meta?.name })
    },
    // `add`/`remove` are stable mutation objects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pins],
  )

  return {
    pins,
    rows,
    isPinned,
    toggle,
    remove,
    atLimit: pins.length >= MAX_PINS,
    max: MAX_PINS,
    isLoading,
  }
}
