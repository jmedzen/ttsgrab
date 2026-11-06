/**
 * The six themes, ported from vidgrab.
 *
 * Each theme is a set of CSS custom properties (see styles/index.css). Components never
 * name a colour directly — they use the semantic Tailwind scales (`bg-panel`, `text-muted`,
 * …) which resolve through those variables. That is what makes six themes possible without
 * six copies of every component.
 *
 * The hex values here are only for the picker swatches; the authoritative palette lives in
 * CSS so that the browser can apply a theme before React runs (no flash of the wrong colour).
 */

export type ThemeKind = 'dark' | 'light'

export interface ThemeDef {
  id: string
  name: string
  kind: ThemeKind
  /** The page background, shown as the swatch fill. */
  dot: string
  /** The accent, shown as the swatch ring. */
  accent: string
}

export const THEMES: readonly ThemeDef[] = [
  { id: 'midnight', name: 'Midnight', kind: 'dark', dot: '#0b0d12', accent: '#6cc0ff' },
  { id: 'ink', name: 'Ink', kind: 'dark', dot: '#08090b', accent: '#c9a227' },
  { id: 'forest', name: 'Forest', kind: 'dark', dot: '#0a1210', accent: '#6fd6a8' },
  { id: 'paper', name: 'Paper', kind: 'light', dot: '#f7f4ee', accent: '#b5651d' },
  { id: 'mist', name: 'Mist', kind: 'light', dot: '#eef2f7', accent: '#2f6fd0' },
  { id: 'sakura', name: 'Sakura', kind: 'light', dot: '#fdf4f5', accent: '#c25a7a' },
] as const

export const THEME_KEY = 'ttsgrab.theme'

/** What the pre-paint script in index.html evaluates. Kept in sync with `initialTheme`. */
export const THEME_BOOT_SCRIPT = `
(function () {
  try {
    var saved = localStorage.getItem('${THEME_KEY}');
    var known = ${JSON.stringify(THEMES.map((t) => t.id))};
    var dark = ['midnight', 'ink', 'forest'];
    var id = known.indexOf(saved) >= 0
      ? saved
      : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
          ? 'paper' : 'midnight');
    document.documentElement.dataset.theme = id;
    document.documentElement.style.colorScheme = dark.indexOf(id) >= 0 ? 'dark' : 'light';
  } catch (e) {
    document.documentElement.dataset.theme = 'midnight';
  }
})();
`

export function findTheme(id: string | null | undefined): ThemeDef {
  return THEMES.find((t) => t.id === id) ?? THEMES[0]!
}

export function initialTheme(): string {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    if (saved) return findTheme(saved).id
  } catch {
    /* localStorage can be blocked */
  }
  if (
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-color-scheme: light)').matches
  ) {
    return 'paper'
  }
  return 'midnight'
}

/**
 * Apply a theme to <html>.
 *
 * `colorScheme` matters as much as the palette: it is what makes scrollbars, form controls
 * and the caret follow the theme instead of staying light on a dark page.
 */
export function applyThemeToDocument(id: string): void {
  const theme = findTheme(id)
  const root = document.documentElement
  root.dataset.theme = theme.id
  root.style.colorScheme = theme.kind
  try {
    localStorage.setItem(THEME_KEY, theme.id)
  } catch {
    /* ignore */
  }
}

export function kindIcon(kind: ThemeKind): string {
  return kind === 'dark' ? '◐' : '◑'
}
