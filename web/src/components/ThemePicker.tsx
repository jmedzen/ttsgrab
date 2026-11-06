import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import clsx from 'clsx'
import { THEMES, findTheme, kindIcon, type ThemeKind } from '../lib/theme'

interface Props {
  theme: string
  onPick: (id: string) => void
}

/**
 * Theme switcher: six themes in two groups (dark / light).
 *
 * The swatch shows the theme's own page colour with the accent as a ring, so the choice is
 * legible without applying it first — the point of a picker is to decide before committing.
 */
export default function ThemePicker({ theme, onPick }: Props) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const active = findTheme(theme)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const groups: ThemeKind[] = ['dark', 'light']

  return (
    <div className="relative flex-none" ref={boxRef}>
      <button
        type="button"
        className="btn-ghost h-8 w-8 !px-0"
        title={t('theme.change')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('theme.change')}
        onClick={() => setOpen((v) => !v)}
        data-testid="theme-picker"
      >
        <span aria-hidden="true">{kindIcon(active.kind)}</span>
      </button>

      {open && (
        <div
          className="card absolute right-0 top-10 z-50 w-52 p-1.5"
          role="menu"
          aria-label={t('theme.change')}
        >
          {groups.map((kind) => (
            <div key={kind}>
              <div className="px-2.5 pb-1 pt-2 text-[10.5px] uppercase tracking-widest text-muted">
                {t(kind === 'dark' ? 'theme.dark' : 'theme.light')}
              </div>
              {THEMES.filter((th) => th.kind === kind).map((th) => (
                <button
                  key={th.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={th.id === theme}
                  className={clsx(
                    'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors',
                    th.id === theme ? 'bg-accent/15 text-text' : 'text-muted hover:bg-panel2',
                  )}
                  onClick={() => {
                    onPick(th.id)
                    setOpen(false)
                  }}
                  data-testid={`theme-${th.id}`}
                >
                  <span
                    className="h-3.5 w-3.5 flex-none rounded-full border border-line-strong"
                    style={{ background: th.dot, boxShadow: `inset 0 0 0 2px ${th.accent}` }}
                    aria-hidden="true"
                  />
                  <span>{th.name}</span>
                  {th.id === theme && (
                    <span className="ml-auto text-xs text-accent" aria-hidden="true">
                      ✓
                    </span>
                  )}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
