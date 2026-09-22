export default function PrivacyScreeningStatus({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const status = value as Record<string, unknown>
  const number = (key: string) => {
    const value = status[key]
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
  }
  const waiting = number('pending_jobs')
  const retrying = number('failed_jobs')
  const completed = number('completed_jobs')
  const live = number('pending_live_jobs')
  const background = number('pending_background_jobs')
  const oldestLive = number('oldest_live_pending_seconds')
  const timings = [
    ['Read frame', number('frame_read_ms')],
    ['Prepare image', number('prepare_ms')],
    ['Run detector', number('inference_ms')],
    ['Deliver result', number('delivery_ms')],
  ] as const
  const messages: Record<string, string> = {
    starting: 'Screening worker is starting.',
    ready: 'Screening worker is ready.',
    needs_review: 'Some captures need review; unresolved captures stay held.',
    unavailable: 'Screening or screen coverage is unavailable; unresolved captures stay held.',
    worker_already_running: 'Another screening worker owns this source.',
  }
  return <div aria-label="Privacy screening status" className="mt-1 space-y-1 text-xs text-gray-500 dark:text-gray-400">
    {typeof status.state === 'string' && messages[status.state] && <p>{messages[status.state]}</p>}
    {waiting !== null && <p>{waiting.toLocaleString()} screening jobs waiting{retrying !== null && ` (${retrying.toLocaleString()} retrying)`}.</p>}
    {live !== null && background !== null && <p>Live captures: {live.toLocaleString()} waiting · History and rechecks: {background.toLocaleString()} waiting.</p>}
    {oldestLive !== null && live !== null && live > 0 && <p>Oldest queued live capture: {Math.ceil(oldestLive).toLocaleString()} seconds.</p>}
    {completed !== null && <p>{completed.toLocaleString()} completed since worker restart.</p>}
    {timings.some(([, value]) => value !== null) && <details>
      <summary className="cursor-pointer">Latest screening timings</summary>
      <dl className="mt-1 grid max-w-sm grid-cols-2 gap-x-4 gap-y-1">
        {timings.map(([label, value]) => value !== null && <div key={label} className="contents">
          <dt>{label}</dt><dd className="text-right tabular-nums">{Math.round(value).toLocaleString()} ms</dd>
        </div>)}
        {number('cache_hits') !== null && <><dt>Cached predictions reused</dt><dd className="text-right tabular-nums">{number('cache_hits')!.toLocaleString()}</dd></>}
      </dl>
    </details>}
  </div>
}
