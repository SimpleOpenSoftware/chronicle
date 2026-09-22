import { useEffect, useState } from 'react'
import { Check, Loader2, Pause, Play, Search, X } from 'lucide-react'
import { dataAuditApi, UnknownSpeakerCluster } from '../../services/api'
import { useGaplessPlayer } from '../../hooks/useGaplessPlayer'
import { Alert, Button } from '../ui'

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null) {
    const candidate = error as { message?: string; response?: { data?: { error?: string } } }
    return candidate.response?.data?.error || candidate.message || 'Unknown-speaker operation failed'
  }
  return String(error)
}

export default function UnknownSpeakerDiscovery() {
  const [clusters, setClusters] = useState<UnknownSpeakerCluster[]>([])
  const [discovering, setDiscovering] = useState(false)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [accepted, setAccepted] = useState<Set<string>>(new Set())
  const [selectedClips, setSelectedClips] = useState<Set<string>>(new Set())
  const player = useGaplessPlayer()

  const refresh = async () => {
    const response = await dataAuditApi.getUnknownSpeakerClusters()
    setClusters(response.data.clusters)
  }
  useEffect(() => { void refresh() }, [])

  const discover = async () => {
    setDiscovering(true)
    setError(null)
    try {
      const response = await dataAuditApi.discoverUnknownSpeakers()
      const jobId = response.data.job_id
      for (;;) {
        const job = await dataAuditApi.getJobResult(jobId)
        if (job.data.status === 'finished') break
        if (job.data.status === 'failed') throw new Error('Unknown-speaker discovery failed')
        await new Promise((resolve) => setTimeout(resolve, 1500))
      }
      await refresh()
    } catch (error: unknown) {
      setError(errorMessage(error))
    } finally {
      setDiscovering(false)
    }
  }

  const decide = async (cluster: UnknownSpeakerCluster, action: 'confirm' | 'dismiss') => {
    if (action === 'confirm' && !name.trim()) return
    setBusy(true)
    setError(null)
    try {
      const acceptedKeys = cluster.members.map((member) => member.identity_key).filter((key) => accepted.has(key))
      const clips = cluster.members.filter((member) => accepted.has(member.identity_key)).flatMap((member) =>
        member.segments
          .filter((segment) => selectedClips.has(`${member.identity_key}:${segment.segment_index}`))
          .map((segment) => ({ identity_key: member.identity_key, segment_index: segment.segment_index }))
      )
      await dataAuditApi.decideUnknownSpeakerCluster(
        cluster, action, action === 'confirm' ? name.trim() : null, acceptedKeys, clips
      )
      setName('')
      await refresh()
    } catch (error: unknown) {
      setError(errorMessage(error))
    } finally {
      setBusy(false)
    }
  }

  const cluster = clusters[0]
  useEffect(() => {
    if (!cluster) return
    setAccepted(new Set(cluster.members.map((member) => member.identity_key)))
    setSelectedClips(new Set(cluster.members.flatMap((member) =>
      member.segments.filter((segment) => segment.duration >= 3).slice(0, 2)
        .map((segment) => `${member.identity_key}:${segment.segment_index}`)
    )))
  }, [cluster])

  const toggle = (values: Set<string>, value: string, setter: (next: Set<string>) => void) => {
    const next = new Set(values)
    next.has(value) ? next.delete(value) : next.add(value)
    setter(next)
  }
  return (
    <section className="rounded-lg bg-white p-6 shadow dark:bg-gray-800">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Discover unknown speakers</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Find matching voices among unidentified speakers.
          </p>
        </div>
        <Button onClick={discover} disabled={discovering} icon={discovering ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}>
          {discovering ? 'Scanning corpus…' : 'Scan corpus'}
        </Button>
      </div>
      {error && <Alert tone="danger" className="mt-4">{error}</Alert>}
      {!cluster && !discovering && <p className="mt-4 text-sm italic text-gray-400">No suggested voice matches to review.</p>}
      {cluster && (
        <div className="mt-5 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
          <p className="font-medium text-gray-900 dark:text-gray-100">
            Possible same person · {cluster.conversation_count} conversations · {cluster.segment_count} clips
          </p>
          <div className="mt-3 space-y-2">
            {cluster.members.map((member) => (
              <div key={member.identity_key} className="rounded bg-gray-50 px-3 py-2 text-sm dark:bg-gray-900/40">
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={accepted.has(member.identity_key)} onChange={() => toggle(accepted, member.identity_key, setAccepted)} />
                  <span className="font-medium">{member.conversation_title || 'Untitled'}</span>
                </label>
                <span className="ml-2 text-gray-400">{member.local_label}</span>
                <div className="mt-2 space-y-1 pl-6">
                  {member.segments.map((segment) => {
                    const key = `${member.identity_key}:${segment.segment_index}`
                    const playbackId = `unknown-discovery:${key}`
                    return <div key={key} className="flex items-center gap-2 text-xs text-gray-500">
                      <input type="checkbox" title="Use this clip for enrollment" disabled={!accepted.has(member.identity_key) || segment.duration < 3} checked={selectedClips.has(key) && accepted.has(member.identity_key)} onChange={() => toggle(selectedClips, key, setSelectedClips)} />
                      <button type="button" aria-label={`Play clip from ${member.conversation_title || 'conversation'}`} onClick={() => player.playingSegmentId === playbackId ? player.stop() : player.playSegment(member.conversation_id, playbackId, segment.start, segment.end)}>
                        {player.playingSegmentId === playbackId ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                      </button>
                      <span className="w-8 tabular-nums">{segment.duration.toFixed(1)}s</span>
                      <span className="truncate">{segment.text || '(no transcript)'}</span>
                    </div>
                  })}
                </div>
              </div>
            ))}
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Existing or new speaker name" className="min-w-64 rounded border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-900" />
            <Button variant="primary" disabled={busy || !name.trim() || accepted.size === 0 || selectedClips.size === 0} onClick={() => decide(cluster, 'confirm')} icon={<Check className="h-4 w-4" />}>Relabel + enroll</Button>
            <Button variant="ghost" disabled={busy} onClick={() => decide(cluster, 'dismiss')} icon={<X className="h-4 w-4" />}>Dismiss</Button>
          </div>
          <p className="mt-2 text-xs text-gray-400">Choose the local identities to relabel and the clean ≥3s clips to add to the voiceprint.</p>
        </div>
      )}
    </section>
  )
}
