import { useTranslation } from 'react-i18next'

interface Props {
  /** The error thrown by TanStack Query. Rendered as a message, never as a stack. */
  error: unknown
  onRetry?: () => void
  className?: string
}

/**
 * Shared failure state.
 *
 * Pages that render `data?.foo ?? []` on error show an empty list, which reads as "there
 * is nothing here" rather than "the request failed". Those are opposite facts and the user
 * acts differently on each, so every query needs a visible failure branch.
 */
export default function QueryError({ error, onRetry, className }: Props) {
  const { t } = useTranslation()
  const message = error instanceof Error ? error.message : String(error)

  return (
    <div
      role="alert"
      className={
        'rounded-lg border border-bad/45 bg-bad/10 px-3 py-2 text-sm text-bad bg-bad/15' +
        (className ?? '')
      }
    >
      <p className="font-medium">{t('common.loadFailed')}</p>
      <p className="mt-0.5 break-words text-xs opacity-80">{message}</p>
      {onRetry && (
        <button type="button" className="mt-2 text-xs underline" onClick={onRetry}>
          {t('common.retry')}
        </button>
      )}
    </div>
  )
}
