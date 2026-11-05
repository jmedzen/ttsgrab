/** Typed client for the ttsgrab API. */

export type AudioFormat = 'mp3' | 'wav' | 'opus' | 'flac'
export type Policy =
  | 'cheapest'
  | 'fastest'
  | 'best_quality'
  | 'local_only'
  | 'free_only'
  | 'explicit'

export interface Voice {
  provider: string
  voice_id: string
  name: string
  languages: string[]
  gender: string
  styles: string[]
  cost_class: string
  tier: string | null
  streaming: boolean
  cloning: boolean
  ssml: boolean
  price_usd_per_mchar: number
  free_chars_per_month: number
}

export interface Provider {
  name: string
  enabled: boolean
  kind: string
  official: boolean
  stability: string
  cost_class: string
  reason: string | null
  note: string | null
  tos_url: string | null
  voices: number
  health: {
    ewma_ms?: number | null
    p50_ms?: number | null
    failures?: number
    circuit_open?: boolean
  }
}

export interface ApiError {
  code: string
  message: string
  retryable: boolean
  suggested_fallback: string[]
  param?: string | null
  details?: Record<string, unknown>
  request_id?: string | null
}

export class ApiRequestError extends Error {
  readonly error: ApiError
  readonly status: number
  constructor(status: number, error: ApiError) {
    super(error.message)
    this.status = status
    this.error = error
  }
}

export interface TTSResponse {
  id: string
  provider: string
  voice_id: string
  format: string
  mime_type: string
  audio_base64: string | null
  audio_url: string | null
  duration_ms: number | null
  latency_ms: number | null
  first_byte_ms: number | null
  characters: number
  cost_micro_usd: number
  cost_usd: number
  used_free_tier: boolean
  cached: boolean
  unsupported: string[]
  fallback_used: boolean
  requested_provider: string | null
  policy: string | null
  attempts: { provider: string; ok: boolean; error?: string; latency_ms?: number }[]
}

export interface TTSResult {
  historyId: string | null
  provider: string
  voiceId: string
  format: AudioFormat
  blobUrl: string
  bytes: number
  durationMs: number | null
  latencyMs: number | null
  cached: boolean
  costMicroUsd: number
  unsupported: string[]
  fallbackFrom: string | null
  characters: number
}

export interface TTSRequestBody {
  text: string
  voice: string
  provider?: string | null
  format?: AudioFormat
  sample_rate?: number | null
  speed?: number
  pitch?: number
  volume?: number
  ssml?: boolean
  style?: string | null
  prompt?: string | null
  language?: string | null
  policy?: Policy | null
  allow_fallback?: boolean
  strict?: boolean
  bypass_cache?: boolean
  response?: 'audio' | 'json'
}

export interface UsageSummary {
  period: { start: string; end: string }
  totals: {
    characters: number
    cost_micro_usd: number
    cost_usd: number
    requests: number
    cached_hits: number
  }
  by_provider: Record<
    string,
    {
      characters: number
      cost_micro_usd: number
      requests: number
      cached: number
      tiers: Record<string, { characters: number; cost_micro_usd: number }>
    }
  >
  free_tier: Record<
    string,
    { tier: string; limit: number; used: number; remaining: number; used_pct: number }[]
  >
  budgets: Record<string, { char_budget: number; chars_used: number; usd_budget: number; usd_used: number }>
}

export interface HistoryItem {
  id: string
  created_at: string
  text: string
  text_chars: number
  provider: string
  voice_id: string
  requested_provider: string | null
  format: string
  latency_ms: number | null
  duration_ms: number | null
  audio_bytes: number
  cost_micro_usd: number
  audio_url: string | null
  params: Record<string, unknown>
  from_cache: boolean
}

export interface WorkerNode {
  id: string
  name: string
  base_url: string
  kind: string
  enabled: boolean
  healthy: boolean
  consecutive_failures: number
  last_checked_at: string | null
  last_error: string | null
  latency_ms_ewma: number | null
  voices: number
  model_hint: string | null
}

export interface ApiKeyRecord {
  id: string
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  revoked: boolean
  provider_allowlist: string[]
  rate_limit_per_minute: number | null
  monthly_char_quota: number | null
}

export interface CompareRow {
  provider: string
  voice_id: string
  history_id?: string
  audio_url?: string
  format?: string
  bytes?: number
  duration_ms?: number | null
  latency_ms?: number | null
  first_byte_ms?: number | null
  rtf?: number | null
  characters?: number
  cost_micro_usd?: number
  cost_usd?: number
  cached?: boolean
  unsupported?: string[]
  error?: string
  error_type?: string
}

const TOKEN_KEY = 'ttsgrab.admin_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken()
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
}

async function parseError(res: Response): Promise<never> {
  let payload: { error?: ApiError } | null = null
  try {
    payload = await res.json()
  } catch {
    /* non-JSON error body */
  }
  const err: ApiError = payload?.error ?? {
    code: 'http_error',
    message: `HTTP ${res.status} ${res.statusText}`,
    retryable: res.status >= 500,
    suggested_fallback: [],
  }
  throw new ApiRequestError(res.status, err)
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: authHeaders({
      'Content-Type': 'application/json',
      ...(init?.headers as Record<string, string> | undefined),
    }),
  })
  if (!res.ok) await parseError(res)
  return (await res.json()) as T
}

export const api = {
  async voices(params: {
    provider?: string
    language?: string
    search?: string
    gender?: string
    freeOnly?: boolean
    cloning?: boolean
  } = {}): Promise<{ total: number; voices: Voice[]; languages: string[]; providers: string[] }> {
    const q = new URLSearchParams()
    if (params.provider) q.set('provider', params.provider)
    if (params.language) q.set('language', params.language)
    if (params.search) q.set('search', params.search)
    if (params.gender) q.set('gender', params.gender)
    if (params.freeOnly) q.set('free_only', 'true')
    if (params.cloning !== undefined) q.set('cloning', String(params.cloning))
    return json(`/v1/voices?${q.toString()}`)
  },

  async providers(): Promise<{
    providers: Provider[]
    errors: Record<string, string>
    pricing_version: number
    pricing_effective: string
  }> {
    return json('/v1/providers')
  },

  async synthesize(body: TTSRequestBody): Promise<TTSResult> {
    const res = await fetch('/v1/tts', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    })
    if (!res.ok) await parseError(res)
    const blob = await res.blob()
    return {
      historyId: res.headers.get('X-TTSGrab-History-Id'),
      provider: res.headers.get('X-TTSGrab-Provider') ?? '',
      voiceId: res.headers.get('X-TTSGrab-Voice') ?? '',
      format: (res.headers.get('X-TTSGrab-Format') as AudioFormat) ?? (body.format ?? 'mp3'),
      blobUrl: URL.createObjectURL(blob),
      bytes: blob.size,
      durationMs: null,
      latencyMs: Number(res.headers.get('X-TTSGrab-Latency-Ms') ?? 0) || null,
      cached: res.headers.get('X-TTSGrab-Cached') === 'true',
      costMicroUsd: Number(res.headers.get('X-TTSGrab-Cost-MicroUSD') ?? 0),
      unsupported: (res.headers.get('X-TTSGrab-Unsupported') ?? '')
        .split(',')
        .filter(Boolean),
      fallbackFrom: res.headers.get('X-TTSGrab-Fallback')?.split('->')[0] ?? null,
      characters: body.text.length,
    }
  },

  async compare(body: {
    text: string
    targets: { provider: string; voice_id: string }[]
    format?: AudioFormat
    speed?: number
    pitch?: number
  }): Promise<{ text_chars: number; results: CompareRow[] }> {
    return json('/v1/compare', { method: 'POST', body: JSON.stringify(body) })
  },

  async estimate(body: TTSRequestBody): Promise<{
    characters: number
    estimated_duration_ms: number
    provider: string | null
    tier: string | null
    estimated_cost_micro_usd: number
    estimated_cost_usd: number
    within_free_tier: boolean
  }> {
    return json('/v1/tts/estimate', { method: 'POST', body: JSON.stringify(body) })
  },

  async usage(days = 30): Promise<UsageSummary & { daily?: unknown[] }> {
    return json(`/v1/usage?days=${days}`)
  },

  async history(params: { limit?: number; offset?: number; provider?: string; q?: string } = {}): Promise<{
    total: number
    count: number
    items: HistoryItem[]
  }> {
    const q = new URLSearchParams()
    q.set('limit', String(params.limit ?? 50))
    q.set('offset', String(params.offset ?? 0))
    if (params.provider) q.set('provider', params.provider)
    if (params.q) q.set('q', params.q)
    return json(`/v1/history?${q.toString()}`)
  },

  async deleteHistory(id: string): Promise<void> {
    const res = await fetch(`/v1/history/${id}`, { method: 'DELETE', headers: authHeaders() })
    if (!res.ok) await parseError(res)
  },

  async stats(): Promise<{
    history_total: number
    history_last_7d: number
    avg_latency_ms: number | null
    cache: { entries: number; bytes: number; max_bytes: number; used_pct: number; enabled: boolean }
    voices: number
    providers_enabled: number
  }> {
    return json('/v1/stats')
  },

  async workers(): Promise<{ workers: WorkerNode[] }> {
    return json('/v1/workers')
  },

  async addWorker(body: {
    name: string
    base_url: string
    kind?: 'tts' | 'openai_compat'
    model_hint?: string
    auth_header?: string
  }): Promise<WorkerNode> {
    return json('/v1/workers', { method: 'POST', body: JSON.stringify(body) })
  },

  async deleteWorker(id: string): Promise<void> {
    const res = await fetch(`/v1/workers/${id}`, { method: 'DELETE', headers: authHeaders() })
    if (!res.ok) await parseError(res)
  },

  async checkWorker(id: string): Promise<WorkerNode> {
    return json(`/v1/workers/${id}/check`, { method: 'POST' })
  },

  async apiKeys(): Promise<{ keys: ApiKeyRecord[] }> {
    return json('/v1/keys')
  },

  async createApiKey(body: {
    name: string
    provider_allowlist?: string[]
    rate_limit_per_minute?: number | null
    monthly_char_quota?: number | null
  }): Promise<ApiKeyRecord & { token: string }> {
    return json('/v1/keys', { method: 'POST', body: JSON.stringify(body) })
  },

  async revokeApiKey(id: string): Promise<void> {
    const res = await fetch(`/v1/keys/${id}`, { method: 'DELETE', headers: authHeaders() })
    if (!res.ok) await parseError(res)
  },

  async references(): Promise<{ references: ReferenceAudio[] }> {
    return json('/v1/references')
  },

  async uploadReference(form: FormData): Promise<ReferenceAudio> {
    const res = await fetch('/v1/references', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    })
    if (!res.ok) await parseError(res)
    return res.json()
  },

  async deleteReference(id: string): Promise<void> {
    const res = await fetch(`/v1/references/${id}`, { method: 'DELETE', headers: authHeaders() })
    if (!res.ok) await parseError(res)
  },
}

export interface ReferenceAudio {
  id: string
  name: string
  provider: string
  mime: string
  bytes: number
  duration_ms: number | null
  transcript: string | null
  consent_note: string | null
  created_at: string
}

export function formatUsd(microUsd: number): string {
  if (microUsd === 0) return '$0'
  if (microUsd < 1000) return `$${(microUsd / 1_000_000).toFixed(6)}`
  return `$${(microUsd / 1_000_000).toFixed(4)}`
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`
}
