import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'
import { Alert, Button, Card } from './ui'

type Metric = { value_ms: number; quality: string; attempts?: number }
type TimingEvent = {
  event_id: string; stage: string; observed_at_ms: number; timestamp_ms: number
  duration_ms?: number; clock_domain: string; response_id: string; outcome: string; detail: string
}
export type VoiceReport = {
  turn_id: string; turn_revision: number; audio_session_id: string; client_id: string
  started_at_ms: number; status: string; metrics: Record<string, Metric>
  missing: string[]; invalid: string[]; events: TimingEvent[]; alignment: string
}
type Reports = { reports: VoiceReport[]; summary: {
  sample_count: number; complete_count: number; failed_count: number
  wait_sample_count: number; wait_p50_ms: number | null; wait_p95_ms: number | null
} }
const duration = (ms: number | null | undefined) => ms == null ? 'Not measured' : `${(ms / 1000).toFixed(2)} s`
const labels: Record<string, string> = {
  speech_to_tts_request: 'Speech end → TTS requested (clock estimate)',
  speech_to_audio_ready: 'Speech end → audio ready (clock estimate)',
  endpoint_and_ingress: 'Endpoint and ingress (clock estimate)',
  speaking: 'Speaking', waiting: 'Waiting', reply: 'Reply playback', total: 'Total',
  stt: 'STT total', stt_wait: 'Streaming transcript wait', stt_batch: 'Batch STT request',
  routing: 'Routing and plugins', agent: 'Agent reply', notification: 'Notification',
  tts: 'TTS synthesis', encoding: 'Audio encoding', downlink: 'Sending audio', mode_handler: 'Interaction handler',
  speech_started: 'Speech begins', speech_ended: 'Speech ends', turn_committed: 'Turn committed',
  turn_received: 'Turn received', response_queued: 'Response queued', response_ready: 'Audio ready',
  response_offered: 'Audio offered', response_started: 'Playback begins', response_done: 'Playback ends',
  response_failed: 'Playback failed', response_cancelled: 'Playback cancelled', turn_failed: 'Turn failed',
}

export function VoiceTimingCard({ report }: { report: VoiceReport }) {
  const values = ['speaking', 'waiting', 'reply']
  const total = report.metrics.total?.value_ms
  const eventStart = Math.min(...report.events.map(e => e.observed_at_ms))
  const eventEnd = Math.max(...report.events.map(e => e.observed_at_ms + (e.duration_ms ?? 0)))
  const range = Math.max(1, eventEnd - eventStart)
  return <article className="text-gray-900 dark:text-gray-100 border-t border-gray-200 dark:border-gray-700 py-4">
    <div className="flex flex-wrap justify-between gap-2 text-sm">
      <span className="font-medium">{report.client_id} · {new Date(report.started_at_ms).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST</span>
      <span>{report.status} · {duration(total)} total</span>
    </div>
    <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 my-3">
      {values.map(name => <div key={name}>
        <dt className="text-sm text-gray-500 dark:text-gray-400">{labels[name]}</dt>
        <dd className="font-mono tabular-nums text-lg">{duration(report.metrics[name]?.value_ms)}</dd>
      </div>)}
    </dl>
    {total != null && total > 0 && <div className="flex h-2 overflow-hidden rounded bg-gray-100 dark:bg-gray-800" aria-label="Interaction duration breakdown">
      {values.map((name, i) => <div key={name} title={`${labels[name]}: ${duration(report.metrics[name]?.value_ms)}`}
        className={['bg-gray-400', 'bg-blue-500', 'bg-gray-600'][i]}
        style={{ width: `${100 * (report.metrics[name]?.value_ms ?? 0) / total}%` }} />)}
    </div>}
    <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Speech boundaries and audible playback are estimates from the device audio clock. Reply playback includes pauses and any leading silence.</p>
    <details className="mt-3">
      <summary className="cursor-pointer text-sm font-medium text-blue-600 dark:text-blue-400">STT, agent, TTS and delivery timings</summary>
      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-2 my-3 text-sm">
        {['speech_to_tts_request', 'speech_to_audio_ready', 'endpoint_and_ingress', 'stt_wait', 'stt_batch', 'stt', 'routing', 'agent', 'notification', 'mode_handler', 'tts', 'encoding', 'downlink'].map(name => <div className="flex justify-between gap-3" key={name}>
          <dt>{labels[name]}</dt><dd className="font-mono tabular-nums">{duration(report.metrics[name]?.value_ms)}</dd>
        </div>)}
      </dl>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">STT total contains transcript wait and batch STT. Agent and notification time sit inside routing. Nested timings overlap and must not be added together.</p>
      {report.missing.length > 0 && <p className="text-sm mb-2">Missing measurements: {report.missing.map(n => labels[n] ?? n).join(', ')}.</p>}
      {report.invalid.length > 0 && <Alert tone="danger">Invalid clock order: {report.invalid.join(', ')}.</Alert>}
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">{report.alignment}</p>
      <ol className="space-y-2" aria-label="Interaction waterfall">
        {report.events.map(event => <li key={event.event_id} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3 text-xs items-center">
          <span className="break-words">{labels[event.stage] ?? event.stage}{event.outcome !== 'ok' ? ` · ${event.outcome === 'running' ? 'started' : event.outcome}` : ''}
            {event.duration_ms != null ? ` · ${duration(event.duration_ms)}` : ''}</span>
          <div className="h-2 bg-gray-100 dark:bg-gray-800 rounded" title={event.detail}>
            <div className="h-2 bg-blue-500 rounded" style={{ marginLeft: `${Math.min(99.5, 100 * (event.observed_at_ms - eventStart) / range)}%`,
              width: `${Math.max(0.5, 100 * (event.duration_ms ?? 0) / range)}%`, maxWidth: '100%' }} />
          </div>
        </li>)}
      </ol>
    </details>
  </article>
}

export default function VoiceLatencyReport() {
  const [data, setData] = useState<Reports | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [client, setClient] = useState('')
  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.get<Reports>('/api/wakeword/latency', { params: { limit: 20 } })
      setData(result.data); setError(null)
    } catch { setError('Could not load voice timings. Try refreshing.') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])
  const reports = data?.reports.filter(r => !client || r.client_id === client) ?? []
  return <Card className="mb-6 text-gray-900 dark:text-gray-100">
    <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
      <div><h2 className="font-semibold">Voice interaction timing</h2><p className="text-sm text-gray-500 dark:text-gray-400">From your first word to the end of the spoken reply.</p></div>
      <Button variant="secondary" onClick={refresh} disabled={loading}>{loading ? 'Loading…' : 'Refresh timings'}</Button>
    </div>
    {error && <Alert tone="danger">{error}</Alert>}
    {data && <p className="text-sm my-3">{data.summary.complete_count}/{data.summary.sample_count} complete · {data.summary.failed_count} failed.
      {' '}Wait p50: {duration(data.summary.wait_p50_ms)} · p95: {duration(data.summary.wait_p95_ms)}
      {' '}({data.summary.wait_sample_count} complete turns in the latest 20, within 30 days).</p>}
    {data && data.reports.length > 0 && <label className="block text-sm mb-3">Device{' '}
      <select className="rounded border border-gray-300 dark:border-gray-600 bg-transparent p-1" value={client} onChange={e => setClient(e.target.value)}>
        <option value="">All devices</option>{Array.from(new Set(data.reports.map(r => r.client_id))).map(id => <option key={id}>{id}</option>)}
      </select>
    </label>}
    {data && reports.length === 0 && <p className="text-sm py-4">No voice timing traces yet. Ask a question from a connected voice device, then refresh. A partial trace will show which measurements are missing.</p>}
    {reports.map(report => <VoiceTimingCard key={`${report.audio_session_id}:${report.turn_id}:${report.turn_revision}`} report={report} />)}
  </Card>
}
