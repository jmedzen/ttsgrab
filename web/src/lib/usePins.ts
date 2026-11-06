/**
 * Shared favourites state.
 *
 * The pins live in localStorage (see `lib/pins.ts` for why) but two components need to see
 * the same list: the voice picker, and the Playground's own star button and quick-switch
 * row. Keeping a `useState` in each would let them drift — you star a voice in the
 * Playground and the picker's pinned group does not notice until a reload.
 *
 * A module-level listener set is used rather than React context so the state survives
 * components that are mounted independently of any provider, and so a second tab's
 * change (via the `storage` event) also lands.
 */

import { useCallback, useEffect, useState } from 'react'
import { loadPins, MAX_PINS, savePins, togglePin } from './pins'

type Listener = (ids: string[]) => void

const listeners = new Set<Listener>()

function publish(ids: string[]): void {
  for (const fn of listeners) fn(ids)
}

export interface PinsApi {
  pins: string[]
  isPinned: (voiceId: string | null) => boolean
  toggle: (voiceId: string) => void
  atLimit: boolean
  max: number
}

export function usePins(): PinsApi {
  const [pins, setPins] = useState<string[]>(() => loadPins())

  useEffect(() => {
    const fn: Listener = (ids) => setPins(ids)
    listeners.add(fn)
    // Another tab pinning something must show up here too, without a refresh.
    const onStorage = (e: StorageEvent) => {
      if (e.key === null || e.key === 'ttsgrab.pinnedVoices') setPins(loadPins())
    }
    window.addEventListener('storage', onStorage)
    return () => {
      listeners.delete(fn)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  const toggle = useCallback((voiceId: string) => {
    // Read the canonical list rather than the local copy: two components toggling in the
    // same tick would otherwise each apply their stale snapshot and lose one change.
    const next = togglePin(loadPins(), voiceId)
    savePins(next)
    publish(next)
  }, [])

  const isPinned = useCallback((voiceId: string | null) => {
    if (!voiceId) return false
    return pins.includes(voiceId)
  }, [pins])

  return { pins, isPinned, toggle, atLimit: pins.length >= MAX_PINS, max: MAX_PINS }
}
