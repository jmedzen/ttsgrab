import { useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import clsx from 'clsx'
import Playground from './pages/Playground'
import Compare from './pages/Compare'
import Voices from './pages/Voices'
import Providers from './pages/Providers'
import History from './pages/History'
import Usage from './pages/Usage'
import Keys from './pages/Keys'
import { setLanguage } from './i18n'

const NAV = [
  { to: '/', key: 'playground', end: true },
  { to: '/compare', key: 'compare' },
  { to: '/voices', key: 'voices' },
  { to: '/providers', key: 'providers' },
  { to: '/history', key: 'history' },
  { to: '/usage', key: 'usage' },
  { to: '/keys', key: 'keys' },
] as const

function useTheme() {
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem('ttsgrab.theme')
    if (stored) return stored === 'dark'
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  })
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('ttsgrab.theme', dark ? 'dark' : 'light')
  }, [dark])
  return { dark, toggle: () => setDark((d) => !d) }
}

export default function App() {
  const { t, i18n } = useTranslation()
  const { dark, toggle } = useTheme()
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-ink-200 bg-white/85 backdrop-blur dark:border-ink-800 dark:bg-ink-950/85">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-2.5">
          <NavLink to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-accent-600 text-sm text-white">
              t
            </span>
            ttsgrab
          </NavLink>

          <nav className="hidden flex-1 items-center gap-0.5 md:flex">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={'end' in item ? item.end : false}
                className={({ isActive }) =>
                  clsx(
                    'rounded-lg px-3 py-1.5 text-sm transition-colors',
                    isActive
                      ? 'bg-ink-100 font-medium text-ink-900 dark:bg-ink-800 dark:text-white'
                      : 'text-ink-600 hover:bg-ink-100 dark:text-ink-300 dark:hover:bg-ink-800',
                  )
                }
              >
                {t(`nav.${item.key}`)}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              className="btn-ghost px-2 py-1 text-xs"
              onClick={() => setLanguage(i18n.language === 'zh-Hant' ? 'en' : 'zh-Hant')}
              aria-label={t('settings.language')}
              data-testid="lang-toggle"
            >
              {i18n.language === 'zh-Hant' ? 'English' : '繁體中文'}
            </button>
            <button
              type="button"
              className="btn-ghost px-2 py-1 text-xs"
              onClick={toggle}
              aria-label={t('settings.theme')}
              data-testid="theme-toggle"
            >
              {dark ? '☾' : '☀'}
            </button>
            <button
              type="button"
              className="btn-ghost px-2 py-1 text-xs md:hidden"
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="Menu"
            >
              ☰
            </button>
          </div>
        </div>

        {menuOpen && (
          <nav className="flex flex-wrap gap-1 border-t border-ink-200 px-4 py-2 md:hidden dark:border-ink-800">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={'end' in item ? item.end : false}
                onClick={() => setMenuOpen(false)}
                className={({ isActive }) =>
                  clsx(
                    'rounded-lg px-2.5 py-1 text-sm',
                    isActive
                      ? 'bg-ink-100 font-medium dark:bg-ink-800'
                      : 'text-ink-600 dark:text-ink-300',
                  )
                }
              >
                {t(`nav.${item.key}`)}
              </NavLink>
            ))}
          </nav>
        )}
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        <Routes>
          <Route path="/" element={<Playground />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/voices" element={<Voices />} />
          <Route path="/providers" element={<Providers />} />
          <Route path="/history" element={<History />} />
          <Route path="/usage" element={<Usage />} />
          <Route path="/keys" element={<Keys />} />
        </Routes>
      </main>

      <footer className="border-t border-ink-200 px-4 py-3 text-center text-xs text-ink-500 dark:border-ink-800 dark:text-ink-500">
        ttsgrab · unified TTS gateway ·{' '}
        <a className="underline hover:text-accent-500" href="/docs">
          API
        </a>{' '}
        ·{' '}
        <a className="underline hover:text-accent-500" href="/openapi.json">
          OpenAPI
        </a>
      </footer>
    </div>
  )
}
