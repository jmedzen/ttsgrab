import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import QueryError from '../components/QueryError'
import { api, getToken, setToken } from '../lib/api'

const CURL_EXAMPLE = `curl -X POST http://127.0.0.1:8000/v1/tts \\
  -H "Authorization: Bearer $TTSGRAB_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"text":"你好，世界","voice":"zh-TW-HsiaoChenNeural","format":"mp3"}' \\
  --output hello.mp3`

const PY_EXAMPLE = `from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="ttsg_...")
with client.audio.speech.with_streaming_response.create(
    model="tts-1", voice="zh-TW-HsiaoChenNeural", input="你好，世界",
) as response:
    response.stream_to_file("hello.mp3")`

const MCP_EXAMPLE = `{
  "mcpServers": {
    "ttsgrab": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer ttsg_..." }
    }
  }
}`

export default function Keys() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const keysQuery = useQuery({ queryKey: ['keys'], queryFn: api.apiKeys })

  // Without this, a 401 renders as "No API keys yet" — the operator reads that as "they were
  // deleted" when the truth is "your token is wrong". Opposite facts, opposite actions.
  if (keysQuery.isError) {
    return (
      <div className="grid gap-4">
        <h1 className="text-xl font-semibold">{t('keys.title')}</h1>
        <QueryError error={keysQuery.error} onRetry={() => void keysQuery.refetch()} />
      </div>
    )
  }
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })

  const [name, setName] = useState('')
  const [allowlist, setAllowlist] = useState('')
  const [rateLimit, setRateLimit] = useState('')
  const [quota, setQuota] = useState('')
  const [newToken, setNewToken] = useState<string | null>(null)
  const [token, setTokenState] = useState(getToken() ?? '')
  const [copied, setCopied] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: () =>
      api.createApiKey({
        name,
        provider_allowlist: allowlist
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        rate_limit_per_minute: rateLimit ? Number(rateLimit) : null,
        monthly_char_quota: quota ? Number(quota) : null,
      }),
    onSuccess: (key) => {
      setNewToken(key.token)
      setName('')
      setAllowlist('')
      setRateLimit('')
      setQuota('')
      void qc.invalidateQueries({ queryKey: ['keys'] })
    },
  })

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeApiKey(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['keys'] }),
  })

  async function copy(text: string, tag: string) {
    await navigator.clipboard.writeText(text)
    setCopied(tag)
    setTimeout(() => setCopied(null), 1500)
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold">{t('keys.title')}</h1>
        <p className="text-sm text-muted">{t('keys.subtitle')}</p>
      </header>

      <section className="card p-4">
        <h2 className="mb-2 font-medium">{t('settings.token')}</h2>
        <div className="flex gap-2">
          <input
            className="input"
            type="password"
            placeholder="TTSGRAB_ADMIN_TOKEN"
            value={token}
            onChange={(e) => setTokenState(e.target.value)}
            data-testid="admin-token"
          />
          <button
            type="button"
            className="btn-primary shrink-0"
            onClick={() => {
              setToken(token || null)
              void qc.invalidateQueries()
            }}
          >
            {t('common.save')}
          </button>
        </div>
        <p className="mt-1 text-xs text-muted">{t('settings.tokenHint')}</p>
      </section>

      <section className="card p-4">
        <h2 className="mb-3 font-medium">{t('keys.create')}</h2>
        <form
          className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <input
            className="input"
            placeholder={t('keys.name')}
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            data-testid="key-name"
          />
          <input
            className="input"
            placeholder={t('keys.allowlist')}
            value={allowlist}
            onChange={(e) => setAllowlist(e.target.value)}
            list="provider-names"
          />
          <datalist id="provider-names">
            {(providersQuery.data?.providers ?? []).map((p) => (
              <option key={p.name} value={p.name} />
            ))}
          </datalist>
          <input
            className="input"
            placeholder={t('keys.rateLimit')}
            type="number"
            min={1}
            value={rateLimit}
            onChange={(e) => setRateLimit(e.target.value)}
          />
          <input
            className="input"
            placeholder={t('keys.quota')}
            type="number"
            min={1000}
            value={quota}
            onChange={(e) => setQuota(e.target.value)}
          />
          <button type="submit" className="btn-primary" disabled={create.isPending}>
            {t('keys.create')}
          </button>
        </form>

        {newToken && (
          <div className="mt-3 rounded-lg border border-ok/45 bg-ok/10 p-3 bg-ok/15">
            <p className="text-xs font-medium text-ok">
              {t('keys.tokenOnce')}
            </p>
            <div className="mt-1 flex gap-2">
              <code className="min-w-0 flex-1 overflow-x-auto rounded bg-panel px-2 py-1 font-mono text-xs">
                {newToken}
              </code>
              <button type="button" className="btn-ghost shrink-0 text-xs" onClick={() => copy(newToken, 'new')}>
                {copied === 'new' ? t('common.copied') : t('common.copy')}
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="card overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="px-4 py-2">{t('keys.name')}</th>
              <th className="px-4 py-2">{t('keys.prefix')}</th>
              <th className="px-4 py-2">{t('keys.allowlist')}</th>
              <th className="px-4 py-2 text-right">{t('keys.quota')}</th>
              <th className="px-4 py-2">{t('keys.lastUsed')}</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {(keysQuery.data?.keys ?? []).length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-muted">
                  {t('keys.noKeys')}
                </td>
              </tr>
            )}
            {(keysQuery.data?.keys ?? []).map((k) => (
              <tr key={k.id} className="border-b border-line last:border-0 border-line/60">
                <td className="px-4 py-2">{k.name}</td>
                <td className="px-4 py-2 font-mono text-xs">{k.prefix}…</td>
                <td className="px-4 py-2 text-xs text-muted">
                  {k.provider_allowlist.length ? k.provider_allowlist.join(', ') : t('common.all')}
                </td>
                <td className="px-4 py-2 text-right text-xs">
                  {k.monthly_char_quota?.toLocaleString() ?? '—'}
                </td>
                <td className="px-4 py-2 text-xs text-muted">
                  {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : '—'}
                </td>
                <td className="px-4 py-2 text-right">
                  <button
                    type="button"
                    className="btn-danger px-2 py-1 text-xs"
                    onClick={() => revoke.mutate(k.id)}
                  >
                    {t('keys.revoke')}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card p-4">
        <h2 className="mb-2 font-medium">{t('keys.examples')}</h2>
        <div className="space-y-3">
          <Example title="curl" code={CURL_EXAMPLE} onCopy={() => copy(CURL_EXAMPLE, 'curl')} copied={copied === 'curl'} />
          <Example title="Python (OpenAI SDK)" code={PY_EXAMPLE} onCopy={() => copy(PY_EXAMPLE, 'py')} copied={copied === 'py'} />
          <Example title="MCP (Claude)" code={MCP_EXAMPLE} onCopy={() => copy(MCP_EXAMPLE, 'mcp')} copied={copied === 'mcp'} />
        </div>
        <div className="mt-3 flex gap-3 text-xs">
          <a className="text-accent underline" href="/docs">
            {t('keys.swagger')}
          </a>
          <a className="text-accent underline" href="/openapi.json">
            openapi.json
          </a>
          <a className="text-accent underline" href="/llms.txt">
            llms.txt
          </a>
        </div>
      </section>
    </div>
  )
}

function Example({
  title,
  code,
  onCopy,
  copied,
}: {
  title: string
  code: string
  onCopy: () => void
  copied: boolean
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">{title}</span>
        <button type="button" className="btn-ghost px-2 py-0.5 text-xs" onClick={onCopy}>
          {copied ? '✓' : 'copy'}
        </button>
      </div>
      <pre className="overflow-x-auto rounded-lg bg-bg p-3 text-xs text-text">
        <code>{code}</code>
      </pre>
    </div>
  )
}
