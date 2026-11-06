/**
 * Pinned voices — the handful a user actually reaches for.
 *
 * Stored in localStorage rather than on the server on purpose: this is a personal
 * convenience, not shared configuration. Putting it in the database would mean one user's
 * favourites reorder everyone else's list, and would need an auth story to be meaningful.
 *
 * A pinned id may stop existing (provider disabled, worker evicted, catalogue changed) —
 * readers must therefore tolerate unknown ids and never assume a pin resolves to a voice.
 */

const KEY = 'ttsgrab.pinnedVoices'

/** Hard cap. Beyond this the "pinned" section stops being a shortcut and becomes noise. */
export const MAX_PINS = 12

export function loadPins(): string[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Defensive: a hand-edited localStorage entry must not crash the picker.
    return parsed.filter((v): v is string => typeof v === 'string').slice(0, MAX_PINS)
  } catch {
    return []
  }
}

export function savePins(ids: string[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(ids.slice(0, MAX_PINS)))
  } catch {
    /* storage full or blocked — pins are a convenience, not correctness */
  }
}

export function togglePin(ids: string[], voiceId: string): string[] {
  if (ids.includes(voiceId)) return ids.filter((v) => v !== voiceId)
  // Newest first: the thing you just pinned is the thing you are about to use.
  return [voiceId, ...ids].slice(0, MAX_PINS)
}
