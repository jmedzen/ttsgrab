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
import Settings from './pages/Settings'
import { setLanguage } from './i18n'
import { api, type BuildInfo } from './lib/api'
import ThemePicker from './components/ThemePicker'
import { applyThemeToDocument, initialTheme } from './lib/theme'

const NAV = [
  { to: '/', key: 'playground', end: true },
  { to: '/compare', key: 'compare' },
  { to: '/voices', key: 'voices' },
  { to: '/providers', key: 'providers' },
  { to: '/history', key: 'history' },
  { to: '/usage', key: 'usage' },
  { to: '/keys', key: 'keys' },
  { to: '/settings', key: 'settings' },
] as const

function useTheme() {
  // The saved theme is read once, then owned by React. The inline script in index.html has
  // already applied it to <html> before paint, so this only has to stay in sync with it.
  const [theme, setTheme] = useState<string>(() => initialTheme())
  useEffect(() => {
    applyThemeToDocument(theme)
  }, [theme])
  return { theme, setTheme }
}

export default function App() {
  const { t, i18n } = useTranslation()
  const { theme, setTheme } = useTheme()
  const [menuOpen, setMenuOpen] = useState(false)
  const [build, setBuild] = useState<BuildInfo | null>(null)

  // Compare what this bundle was compiled from against what the server is running. A
  // mismatch means the browser loaded a cached index.html pointing at an older JS file —
  // worth saying, because otherwise a shipped feature looks absent.
  const stale =
    build?.build != null && __BUNDLE_BUILD__ !== 'dev' &&
    !__BUNDLE_BUILD__.startsWith(`${build.build}+`)

  useEffect(() => {
    // A failed lookup must not break the UI; the footer simply omits the build.
    api.buildInfo().then(setBuild).catch(() => setBuild(null))
  }, [])

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-panel/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-2.5">
          <NavLink to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-sm text-accent-on">
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
                      ? 'bg-panel2 font-medium text-text'
                      : 'text-muted hover:bg-panel2',
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
            <ThemePicker theme={theme} onPick={setTheme} />
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
          <nav className="flex flex-wrap gap-1 border-t border-line px-4 py-2 md:hidden">
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
                      ? 'bg-panel2 font-medium'
                      : 'text-muted',
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
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>

      <footer className="border-t border-line px-4 py-3 text-center text-xs text-muted">
        ttsgrab · unified TTS gateway ·{' '}
        <a className="underline hover:text-accent" href="/docs">
          API
        </a>{' '}
        ·{' '}
        <a className="underline hover:text-accent" href="/openapi.json">
          OpenAPI
        </a>
        {build && (
          <>
            {' '}
            ·{' '}
            {/* The footer is where someone looks when asked "which version are you on?" */}
            <span title={build.commit ?? undefined} className="tabular-nums">
              {build.version}
              {build.build !== null && ` · build ${build.build}`}
              {build.dirty && ' · dirty'}
            </span>
          </>
        )}
        {/* The server's build number can differ from the bundle's: a cached index.html
            keeps loading an old JS file while /healthz already reports the new deploy.
            Saying so is the difference between "the feature is missing" and "reload". */}
        {build && stale && (
          <span className="ml-2 rounded bg-warn/15 px-1.5 py-0.5 text-warn" data-testid="stale-bundle">
            {t('app.staleBundle', { theirs: __BUNDLE_BUILD__ })}
            <button type="button" className="ml-1 underline" onClick={() => window.location.reload()}>
              {t('app.reload')}
            </button>
          </span>
        )}
      </footer>
    </div>
  )
}
