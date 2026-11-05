import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, formatUsd } from '../lib/api'
import QueryError from '../components/QueryError'

export default function Usage() {
  const { t } = useTranslation()
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['usage'], queryFn: () => api.usage(30) })

  if (isLoading) return <p className="text-sm text-ink-500">{t('common.loading')}</p>
  // `return null` here would render a blank page, which looks like a working page with no
  // data. The user cannot tell the difference, so say what happened.
  if (isError) return <QueryError error={error} onRetry={() => void refetch()} />
  if (!data) return <p className="text-sm text-ink-500">{t('usage.noData')}</p>

  const freeTiers = Object.entries(data.free_tier)
  const providers = Object.entries(data.by_provider)

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold">{t('usage.title')}</h1>
        <p className="text-sm text-ink-500">
          {t('usage.subtitle')} · {data.period.start.slice(0, 10)} → {data.period.end.slice(0, 10)}
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label={t('usage.characters')} value={data.totals.characters.toLocaleString()} />
        <Stat label={t('usage.requests')} value={data.totals.requests.toLocaleString()} />
        <Stat label={t('usage.cost')} value={formatUsd(data.totals.cost_micro_usd)} />
        <Stat label={t('usage.cacheHits')} value={data.totals.cached_hits.toLocaleString()} />
      </div>

      <section className="card p-4">
        <h2 className="mb-3 font-medium">{t('usage.freeTierRemaining')}</h2>
        {freeTiers.length === 0 ? (
          <p className="text-sm text-ink-500">{t('usage.noData')}</p>
        ) : (
          <div className="space-y-3">
            {freeTiers.map(([provider, tiers]) => (
              <div key={provider}>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-500">
                  {provider}
                </div>
                {tiers.map((tier) => (
                  <div key={tier.tier} className="mb-2">
                    <div className="flex justify-between text-xs">
                      <span>{tier.tier}</span>
                      <span className="font-mono text-ink-500">
                        {tier.used.toLocaleString()} / {tier.limit.toLocaleString()} (
                        {tier.used_pct}%)
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 w-full rounded-full bg-ink-200 dark:bg-ink-700">
                      <div
                        className={
                          tier.used_pct > 90
                            ? 'h-1.5 rounded-full bg-red-500'
                            : tier.used_pct > 70
                              ? 'h-1.5 rounded-full bg-amber-500'
                              : 'h-1.5 rounded-full bg-emerald-500'
                        }
                        style={{ width: `${Math.min(100, tier.used_pct)}%` }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="card overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-ink-200 text-left text-xs uppercase tracking-wide text-ink-500 dark:border-ink-800">
            <tr>
              <th className="px-4 py-2">{t('usage.byProvider')}</th>
              <th className="px-4 py-2 text-right">{t('usage.characters')}</th>
              <th className="px-4 py-2 text-right">{t('usage.requests')}</th>
              <th className="px-4 py-2 text-right">{t('usage.cacheHits')}</th>
              <th className="px-4 py-2 text-right">{t('usage.cost')}</th>
            </tr>
          </thead>
          <tbody>
            {providers.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-ink-500">
                  {t('usage.noData')}
                </td>
              </tr>
            )}
            {providers.map(([provider, row]) => (
              <tr key={provider} className="border-b border-ink-100 last:border-0 dark:border-ink-800/60">
                <td className="px-4 py-2 font-medium">{provider}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">
                  {row.characters.toLocaleString()}
                </td>
                <td className="px-4 py-2 text-right font-mono text-xs">{row.requests}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">{row.cached}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">
                  {formatUsd(row.cost_micro_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card p-4">
        <h2 className="mb-3 font-medium">{t('usage.budgets')}</h2>
        <div className="space-y-3">
          {Object.entries(data.budgets).map(([provider, b]) => (
            <div key={provider}>
              <div className="flex justify-between text-xs">
                <span>{provider}</span>
                <span className="font-mono text-ink-500">
                  {b.chars_used.toLocaleString()} /{' '}
                  {b.char_budget ? b.char_budget.toLocaleString() : t('usage.budgetUnlimited')}
                </span>
              </div>
              <div className="mt-1 h-1.5 w-full rounded-full bg-ink-200 dark:bg-ink-700">
                <div
                  className="h-1.5 rounded-full bg-accent-500"
                  style={{
                    width: b.char_budget
                      ? `${Math.min(100, (b.chars_used / b.char_budget) * 100)}%`
                      : '0%',
                  }}
                />
              </div>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-ink-500">{t('usage.estimateNote')}</p>
      </section>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card p-4">
      <div className="text-xs uppercase tracking-wide text-ink-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  )
}
