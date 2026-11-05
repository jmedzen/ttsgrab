import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import clsx from 'clsx'

interface Props {
  src: string
  format?: string
  className?: string
  compact?: boolean
  autoPlay?: boolean
  onEnded?: () => void
}

/**
 * Audio player with a waveform.
 *
 * We decode with WebAudio and draw the peaks ourselves rather than pulling in
 * wavesurfer: we only need a static envelope plus a playhead, and decoding also gives us
 * the true duration (which the server only estimates for compressed formats).
 */
export default function AudioPlayer({
  src,
  format,
  className,
  compact = false,
  autoPlay = false,
  onEnded,
}: Props) {
  const { t } = useTranslation()
  const audioRef = useRef<HTMLAudioElement>(null)
  const [peaks, setPeaks] = useState<number[]>([])
  const [duration, setDuration] = useState<number>(0)
  const [progress, setProgress] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setPeaks([])
    setProgress(0)

    async function decode() {
      try {
        const res = await fetch(src)
        const buf = await res.arrayBuffer()
        if (cancelled) return
        const AudioCtx =
          window.AudioContext ||
          (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
        const ctx = new AudioCtx()
        const decoded = await ctx.decodeAudioData(buf.slice(0))
        if (cancelled) return
        const channel = decoded.getChannelData(0)
        const buckets = compact ? 120 : 400
        const step = Math.max(1, Math.floor(channel.length / buckets))
        const out: number[] = []
        for (let i = 0; i < buckets; i++) {
          let peak = 0
          const start = i * step
          for (let j = 0; j < step; j += 8) {
            const v = Math.abs(channel[start + j] ?? 0)
            if (v > peak) peak = v
          }
          out.push(peak)
        }
        const max = Math.max(...out, 0.0001)
        setPeaks(out.map((v) => Math.max(0.02, v / max)))
        setDuration(decoded.duration)
        void ctx.close()
      } catch {
        setPeaks([])
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void decode()
    return () => {
      cancelled = true
    }
  }, [src, compact])

  useEffect(() => {
    const el = audioRef.current
    if (!el) return
    const onTime = () => setProgress(el.currentTime)
    const onMeta = () => {
      if (Number.isFinite(el.duration) && el.duration > 0) setDuration(el.duration)
    }
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    const onDone = () => {
      setPlaying(false)
      onEnded?.()
    }
    el.addEventListener('timeupdate', onTime)
    el.addEventListener('loadedmetadata', onMeta)
    el.addEventListener('play', onPlay)
    el.addEventListener('pause', onPause)
    el.addEventListener('ended', onDone)
    return () => {
      el.removeEventListener('timeupdate', onTime)
      el.removeEventListener('loadedmetadata', onMeta)
      el.removeEventListener('play', onPlay)
      el.removeEventListener('pause', onPause)
      el.removeEventListener('ended', onDone)
    }
  }, [onEnded])

  const toggle = () => {
    const el = audioRef.current
    if (!el) return
    if (el.paused) void el.play()
    else el.pause()
  }

  const seek = (event: React.MouseEvent<HTMLDivElement>) => {
    const el = audioRef.current
    if (!el || !duration) return
    const rect = event.currentTarget.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
    el.currentTime = ratio * duration
    setProgress(el.currentTime)
  }

  const ratio = duration ? Math.min(1, progress / duration) : 0

  return (
    <div className={clsx('flex items-center gap-3', className)}>
      <audio ref={audioRef} src={src} autoPlay={autoPlay} preload="auto" data-format={format} />
      <button
        type="button"
        onClick={toggle}
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-accent-600 text-white transition-colors hover:bg-accent-500"
        aria-label={playing ? t('common.pause') : t('common.play')}
        data-testid="play-button"
      >
        {playing ? '❚❚' : '▶'}
      </button>

      <div
        className="relative h-10 flex-1 cursor-pointer overflow-hidden rounded"
        onClick={seek}
        role="presentation"
      >
        {peaks.length > 0 ? (
          <div className="flex h-full w-full items-center gap-[1px]">
            {peaks.map((p, i) => {
              const played = i / peaks.length <= ratio
              return (
                <span
                  key={i}
                  className={clsx(
                    'flex-1 rounded-sm transition-colors',
                    played ? 'bg-accent-500' : 'bg-ink-300 dark:bg-ink-700',
                  )}
                  style={{ height: `${Math.max(6, p * 100)}%` }}
                />
              )
            })}
          </div>
        ) : (
          <div className="h-full w-full rounded bg-ink-200 dark:bg-ink-800">
            <div
              className="h-full rounded bg-accent-500/70 transition-[width]"
              style={{ width: `${ratio * 100}%` }}
            />
          </div>
        )}
      </div>

      <span className="shrink-0 font-mono text-xs tabular-nums text-ink-500">
        {loading && peaks.length === 0
          ? '…'
          : `${formatTime(progress)} / ${formatTime(duration)}`}
      </span>
    </div>
  )
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0:00'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}
