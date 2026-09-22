import { useCallback, useEffect, useRef, useState } from 'react'
import { Mic, Radio, Trash2, Check, X, RefreshCw, Target, AlertTriangle, Square, Volume2, Play, CopyX, ArrowRightLeft, ChevronLeft } from 'lucide-react'
import { Link } from 'react-router-dom'
import { wakewordApi, WakeStream, WakeSample, WakeWordConfig, WakeStats } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { Alert, Button, Card, IconButton, StatCard, Tabs } from '../components/ui'

import VoiceLatencyReport from '../components/VoiceLatencyReport'

type Bucket = 'pending' | 'positive' | 'negative'

// Native <audio> volume tops out at 1.0; route playback through a Web Audio gain
// node to amplify past that ceiling when clips are too faint to hear. The boost
// button cycles through these levels (1 = off).
const BOOST_LEVELS = [1, 4, 8]

const BUCKET_LABELS: Record<Bucket, string> = {
  pending: 'Pending review',
  positive: 'Positives (wake)',
  negative: 'Negatives (not wake)',
}

const pretty = (name: string) => name.replace(/_/g, ' ')

export default function WakeWordLab() {
  const { isAdmin } = useAuth()
  const [words, setWords] = useState<WakeWordConfig[]>([])
  const [streams, setStreams] = useState<WakeStream[]>([])
  const [stats, setStats] = useState<Record<string, WakeStats>>({})
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [modelsError, setModelsError] = useState(false)
  const [streamsKnown, setStreamsKnown] = useState(false)
  // Bumped after any mutation (label/move/delete/dedupe); every section re-fetches
  // its clips when it changes, so a moved clip appears in the target section with
  // no manual refresh.
  const [dataVersion, setDataVersion] = useState(0)
  const [gain, setGain] = useState(() => {
    const saved = Number(localStorage.getItem('wakewordVolumeBoost'))
    return BOOST_LEVELS.includes(saved) ? saved : 1
  })

  const cycleBoost = () => setGain((g) => {
    const next = BOOST_LEVELS[(BOOST_LEVELS.indexOf(g) + 1) % BOOST_LEVELS.length]
    localStorage.setItem('wakewordVolumeBoost', String(next))
    return next
  })

  const refreshMeta = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([wakewordApi.getStreams(), wakewordApi.getStats()])
      setStreams(s.data.streams)
      setStreamsKnown(true)
      setStats(st.data)
      setError(null)
    } catch (e: any) {
      setStreamsKnown(false)
      setError(e?.response?.data?.detail || 'Wake-word service unreachable')
    }
  }, [])

  // Passed to every section: refresh shared meta AND signal all sections to
  // re-fetch their clip lists (so cross-section moves show up automatically).
  const onSectionChanged = useCallback(() => {
    refreshMeta()
    setDataVersion((v) => v + 1)
  }, [refreshMeta])

  // Flip a word between normal dispatch and collect-only (shadow) mode. The
  // service returns the refreshed per-word config, which we splice straight into
  // local state so the badge/toggle updates without a round-trip refresh.
  const toggleCollectOnly = useCallback(async (name: string, value: boolean) => {
    try {
      const { data } = await wakewordApi.setCollectOnly(name, value)
      setWords(data.wakewords)
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not change collect-only mode')
    }
  }, [])

  // Flip a word's second-stage verifier on/off (the verifier stays loaded; off
  // falls back to the stage-1 model). Same splice-in-place refresh as above.
  const toggleVerifier = useCallback(async (name: string, value: boolean) => {
    try {
      const { data } = await wakewordApi.setVerifierEnabled(name, value)
      setWords(data.wakewords)
      setError(null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not change verifier mode')
    }
  }, [])

  const refreshAll = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await wakewordApi.getModels()
      setWords(data.wakewords)
      setModelsError(false)
    } catch {
      setModelsError(true)
    }
    await refreshMeta()
    setDataVersion((v) => v + 1)
    setLoading(false)
  }, [refreshMeta])

  useEffect(() => { refreshAll() }, [refreshAll])
  // Poll streams/stats so a freshly-started recording shows up to prime, and a
  // just-finished prime's clip-count bumps without a manual refresh.
  useEffect(() => {
    const t = setInterval(refreshMeta, 4000)
    return () => clearInterval(t)
  }, [refreshMeta])

  return (
    <div>
      {/* Breadcrumb — the lab has no nav row; Data Audit's hub is the way in and back */}
      <Link
        to="/data-audit"
        className="mb-3 inline-flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400"
      >
        <ChevronLeft className="h-4 w-4" /> Data Audit
      </Link>

      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-2">
          <Target className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Wake-Word Lab</h1>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            icon={<RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />}
            onClick={refreshAll}
          >
            Refresh
          </Button>
        </div>
      </div>

      {/* Volume boost stays reachable while scrolling the long per-word clip lists */}
      <button
        onClick={cycleBoost}
        title={gain > 1 ? `Volume boost ${gain}× — tap to cycle` : 'Boost playback volume for faint clips — tap to cycle 4× / 8×'}
        className={`fixed bottom-6 right-6 z-50 flex items-center gap-2 px-4 py-2.5 rounded-full text-sm shadow-lg ${
          gain > 1
            ? 'bg-blue-600 text-white hover:bg-blue-700'
            : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
        }`}
      >
        <Volume2 className="h-4 w-4" />
        Volume boost {gain > 1 ? `${gain}×` : 'off'}
      </button>

      <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
        Listen to clips and label the exact wake word.
      </p>

      {error && (
        <Alert tone="danger" icon={<AlertTriangle className="h-4 w-4" />} className="mb-4">
          {error}
        </Alert>
      )}

      <VoiceLatencyReport />

      {/* Shared active-streams indicator */}
      <Card className="mb-6">
        <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
          <Radio className="h-4 w-4" /> Active streams
        </h2>
        {!streamsKnown ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">Stream status unavailable.</p>
        ) : streams.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            No live streams. Start Live Record to capture an example.
          </p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {streams.map((s) => (
              <li key={s.client_id} className="flex items-center gap-2 rounded-md bg-gray-50 dark:bg-gray-800 px-3 py-1.5 text-sm font-mono text-gray-700 dark:text-gray-300">
                <span className={`h-2 w-2 rounded-full ${s.armed ? 'bg-amber-500' : 'bg-green-500'}`} />
                {s.client_id}
                {s.priming && (
                  <span className="rounded bg-green-100 dark:bg-green-900 px-1.5 py-0.5 text-xs text-green-700 dark:text-green-300">
                    enrolling “{pretty(s.prime_wakeword || '')}”…
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* One section per wake word */}
      {modelsError ? (
        <Alert tone="danger">Wake-word configuration could not be loaded. Refresh to try again.</Alert>
      ) : loading && words.length === 0 ? (
        <p role="status">Loading wake words…</p>
      ) : words.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-500 dark:text-gray-400">
          No wake words configured.
        </p>
      ) : (
        <div className="space-y-8">
          {words.map((w) => (
            <WakeWordSection
              key={w.name}
              word={w}
              allWords={words.map((x) => x.name)}
              stats={stats[w.name]}
              streams={streams}
              gain={gain}
              dataVersion={dataVersion}
              isAdmin={isAdmin}
              onChanged={onSectionChanged}
              onToggleCollectOnly={toggleCollectOnly}
              onToggleVerifier={toggleVerifier}
              onError={setError}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function WakeWordSection({
  word,
  allWords,
  stats,
  streams,
  gain,
  dataVersion,
  isAdmin,
  onChanged,
  onToggleCollectOnly,
  onToggleVerifier,
  onError,
}: {
  word: WakeWordConfig
  allWords: string[]
  stats?: WakeStats
  streams: WakeStream[]
  gain: number
  dataVersion: number
  isAdmin: boolean
  onChanged: () => void
  onToggleCollectOnly: (name: string, value: boolean) => void
  onToggleVerifier: (name: string, value: boolean) => void
  onError: (msg: string | null) => void
}) {
  const [bucket, setBucket] = useState<Bucket>('pending')
  const [sampleList, setSampleList] = useState<{ bucket: Bucket; samples: WakeSample[] } | null>(null)
  const [samplesLoading, setSamplesLoading] = useState(false)
  const [samplesError, setSamplesError] = useState<string | null>(null)
  const [visibleLimit, setVisibleLimit] = useState(20)
  const sampleRequest = useRef(0)
  const activeBucket = useRef(bucket)
  activeBucket.current = bucket
  const samples = sampleList?.bucket === bucket ? sampleList.samples : []
  const [primedMsg, setPrimedMsg] = useState<string | null>(null)

  // Is any stream currently being primed/enrolled for THIS word?
  const primingStream = streams.find((s) => s.priming && s.prime_wakeword === word.name)

  const refreshSamples = useCallback(async (b: Bucket) => {
    if (b !== activeBucket.current) return
    const request = ++sampleRequest.current
    setSamplesLoading(true)
    setSamplesError(null)
    try {
      const { data } = await wakewordApi.getSamples(word.name, b)
      if (request === sampleRequest.current && b === activeBucket.current) setSampleList({ bucket: b, samples: data.samples })
    } catch {
      if (request === sampleRequest.current && b === activeBucket.current) setSamplesError('Clips could not be loaded.')
    } finally {
      if (request === sampleRequest.current) setSamplesLoading(false)
    }
  }, [word.name])

  useEffect(() => {
    refreshSamples(bucket)
    return () => { sampleRequest.current += 1 }
  }, [bucket, refreshSamples, dataVersion])
  useEffect(() => { setVisibleLimit(20) }, [bucket])

  // When this word's prime session ends, pull pending so the clip shows up.
  const wasPriming = useRef(false)
  useEffect(() => {
    const now = !!primingStream
    if (wasPriming.current && !now) {
      setPrimedMsg(null)
      refreshSamples(bucket)
      onChanged()
    }
    wasPriming.current = now
  }, [primingStream, bucket, refreshSamples, onChanged])

  const prime = async () => {
    try {
      const { data } = await wakewordApi.prime(word.name)
      setBucket('pending')
      setPrimedMsg(`Primed ${data.client_id} — say “${pretty(word.name)}” now (auto-stops in 10s)…`)
      setTimeout(() => setPrimedMsg(null), 10000)
      onChanged()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Could not prime stream')
    }
  }

  const stopPrime = async () => {
    if (!primingStream) return
    try {
      await wakewordApi.unprime(primingStream.client_id)
      setPrimedMsg(null)
      setTimeout(() => { refreshSamples('pending'); onChanged() }, 500)
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Could not stop priming')
    }
  }

  const label = async (id: string, lbl: 'wake' | 'not_wake') => {
    await wakewordApi.label(id, lbl)
    await refreshSamples(bucket)
    onChanged()
  }

  const remove = async (id: string) => {
    await wakewordApi.remove(id)
    await refreshSamples(bucket)
    onChanged()
  }

  // Move a clip to another wake word's PENDING. The common case is acoustic
  // overlap (a bare "hermes" that armed hey_hermes by priority), but the picker
  // lists EVERY word — so a clip that's a false-negative for the right word (and
  // therefore never co-fired it) can still be re-homed there as training data.
  const moveTo = async (id: string, target: string) => {
    try {
      await wakewordApi.move(id, target, 'pending')
      onChanged()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Could not move clip')
    }
  }

  // Copy a clip into another word's PENDING, source stays (shared-FP fan-out: a
  // false positive that fired several words is a hard negative for each).
  const copyTo = async (id: string, target: string) => {
    try {
      await wakewordApi.copy(id, target, 'pending')
      onChanged()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Could not copy clip')
    }
  }

  const [deduping, setDeduping] = useState(false)
  const dedupe = async () => {
    setDeduping(true)
    try {
      const { data } = await wakewordApi.dedupe(word.name)
      const r = data[word.name]
      const conflictMsg = r?.conflicts ? ` — ⚠ ${r.conflicts} clip(s) labeled both wake & not` : ''
      setPrimedMsg(
        r ? `Removed ${r.removed} duplicate clip(s); ${r.kept_unique} unique remain${conflictMsg}` : 'Dedupe complete'
      )
      setTimeout(() => setPrimedMsg(null), 7000)
      await refreshSamples(bucket)
      onChanged()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Could not remove duplicates')
    } finally {
      setDeduping(false)
    }
  }

  return (
    <section className="rounded-xl border border-gray-200 dark:border-gray-700">
      {/* Section header: word + config */}
      <div className="flex flex-wrap items-center gap-3 border-b border-gray-200 dark:border-gray-700 px-4 py-3">
        <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">“{pretty(word.name)}”</h2>
        <span className="rounded bg-gray-100 dark:bg-gray-800 px-2 py-0.5 font-mono text-xs text-gray-600 dark:text-gray-400">
          {word.model}
        </span>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {word.collect_only ? 'Review only' : 'Assistant dispatch enabled'} · {word.verifier && word.verifier_enabled ? 'Verifier enabled' : 'Verifier disabled'}
        </span>
        {isAdmin && <details className="text-sm text-gray-700 dark:text-gray-200">
          <summary className="cursor-pointer">Detection settings</summary>
          <div className="space-y-3 py-3">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={!word.collect_only} onChange={(e) => onToggleCollectOnly(word.name, !e.target.checked)} />
              Allow assistant dispatch
            </label>
            <p className="text-xs text-gray-500 dark:text-gray-400">When off, detections are collected for review only.</p>
            {word.verifier && <label className="flex items-center gap-2">
              <input type="checkbox" checked={word.verifier_enabled} onChange={(e) => onToggleVerifier(word.name, e.target.checked)} />
              Verify detections before dispatch
            </label>}
            <p className="text-xs text-gray-500 dark:text-gray-400">Threshold {word.threshold} · Patience {word.patience}</p>
          </div>
        </details>}
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={dedupe}
            disabled={deduping}
            title="Remove exact-duplicate clips (keeps one per group, across pending + labeled)"
            className="flex items-center gap-1.5 rounded-md bg-gray-100 dark:bg-gray-700 px-3 py-1.5 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50"
          >
            <CopyX className="h-3.5 w-3.5" /> {deduping ? 'Removing…' : 'Remove duplicates'}
          </button>
          {primingStream ? (
            <button
              onClick={stopPrime}
              className="flex items-center gap-1.5 rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700"
            >
              <Square className="h-3.5 w-3.5" /> Stop &amp; save
            </button>
          ) : (
            <button
              onClick={prime}
              disabled={streams.length === 0}
              title={streams.length === 0 ? 'Start a recording first' : ''}
              className="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Target className="h-3.5 w-3.5" /> I'll say “{pretty(word.name)}” now
            </button>
          )}
        </div>
      </div>

      <div className="p-4">
        {primedMsg && (
          <Alert tone="success" icon={<Mic className="h-4 w-4" />} className="mb-4 font-medium animate-pulse">
            {primedMsg}
          </Alert>
        )}

        {/* Stats */}
        <div className="mb-4 grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatCard label="Pending" value={stats?.pending ?? "Unknown"} tone="amber" />
          <StatCard label="Positives" value={stats?.positive ?? "Unknown"} tone="green" />
          <StatCard label="Negatives" value={stats?.negative ?? "Unknown"} tone="red" />
          <StatCard label="Captured examples" value={stats?.false_negatives ?? "Unknown"} />
        </div>

        {/* Bucket tabs + labeling help */}
        <div className="mb-3 flex items-center gap-2">
          <Tabs
            variant="pill"
            value={bucket}
            onChange={setBucket}
            tabs={(Object.keys(BUCKET_LABELS) as Bucket[]).map((b) => ({ value: b, label: BUCKET_LABELS[b] }))}
          />
          <div className="ml-auto">
            <LabelGuide word={pretty(word.name)} />
          </div>
        </div>

        {/* Clip list */}
        {samplesLoading || (!samplesError && sampleList?.bucket !== bucket) ? (
          <p role="status" className="py-6 text-sm text-gray-500 dark:text-gray-400">Loading clips…</p>
        ) : samplesError ? (
          <Alert tone="danger">{samplesError} <button onClick={() => refreshSamples(bucket)} className="underline">Retry clips</button></Alert>
        ) : samples.length === 0 ? (
          <p className="py-6 text-center text-sm text-gray-500 dark:text-gray-400">
            No clips in “{BUCKET_LABELS[bucket]}” for “{pretty(word.name)}”.
          </p>
        ) : (
          <ul className="space-y-2">
            {samples.slice(0, visibleLimit).map((s) => (
              <ClipRow
                key={s.id}
                sample={s}
                allWords={allWords}
                onLabel={label}
                onDelete={remove}
                onMove={moveTo}
                onCopy={copyTo}
                gain={gain}
              />
            ))}
          </ul>
        )}
        {!samplesLoading && !samplesError && samples.length > visibleLimit && (
          <Button variant="secondary" onClick={() => setVisibleLimit((limit) => limit + 20)} className="mt-3">
            Show more clips ({visibleLimit} of {samples.length})
          </Button>
        )}
      </div>
    </section>
  )
}

// Move/Copy a clip to another wake word. The picker lists EVERY configured word
// except the one the clip is filed under — so the false-negative case works too:
// a clip that's really "hermes" but armed "hey hermes" (and never co-fired hermes,
// so it's nowhere in `alsoFired`) can still be re-homed into hermes. Words that
// DID co-fire at capture (acoustic overlap) are surfaced first and badged as
// suggestions, and turn the button purple as a hint that an obvious target exists.
//   Move = it's really that word's (re-homes the clip, re-enters review there).
//   Copy = a false positive that applies to both (keep here too; hard negative for each).
function MoveCopyMenu({
  targets,
  alsoFired,
  onMove,
  onCopy,
}: {
  targets: string[]
  alsoFired: string[]
  onMove: (w: string) => void
  onCopy: (w: string) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  // Close on click-outside (NOT on mouse-leave — the menu sits in a gap below the
  // badge and outside its hover box, so leaving the badge to reach a button would
  // close it before you could click).
  useEffect(() => {
    if (!open) return
    const onDocMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [open])
  if (!targets.length) return null
  const fired = new Set(alsoFired)
  // Co-firers first (suggested), original order otherwise (stable sort).
  const ordered = [...targets].sort((a, b) => Number(fired.has(b)) - Number(fired.has(a)))
  const hasSuggestion = alsoFired.length > 0
  const label = !hasSuggestion
    ? 'Move / Copy'
    : alsoFired.length === 1
      ? `also: ${pretty(alsoFired[0])}`
      : 'also matches other wake words'
  return (
    <div ref={ref} className="relative inline-flex">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Move (it's really another word's) or Copy (a shared false positive) to another wake word"
        className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-xs ${
          hasSuggestion
            ? 'bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300 hover:bg-purple-200 dark:hover:bg-purple-900'
            : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
        }`}
      >
        <ArrowRightLeft className="h-3 w-3" /> {label}
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-64 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-1.5 shadow-lg">
          <div className="px-1 pb-1.5 text-[11px] leading-tight text-gray-500 dark:text-gray-400">
            <b>Move</b> = it's really that word's (a real wake).<br />
            <b>Copy</b> = a false positive that fired both (keep here too).
          </div>
          {ordered.map((w) => (
            <div key={w} className="flex items-center justify-between gap-1 px-1 py-0.5">
              <span className="flex items-center gap-1 text-xs font-medium text-gray-700 dark:text-gray-200">
                “{pretty(w)}”
                {fired.has(w) && (
                  <span className="rounded bg-purple-100 dark:bg-purple-900/40 px-1 py-px text-[10px] text-purple-700 dark:text-purple-300">
                    also fired
                  </span>
                )}
              </span>
              <span className="flex gap-1">
                <button
                  onClick={() => { onMove(w); setOpen(false) }}
                  title={`Move — this clip is really "${pretty(w)}"`}
                  className="rounded bg-blue-100 dark:bg-blue-900/40 px-2 py-0.5 text-xs text-blue-700 dark:text-blue-300 hover:bg-blue-200 dark:hover:bg-blue-900"
                >
                  Move
                </button>
                <button
                  onClick={() => { onCopy(w); setOpen(false) }}
                  title={`Copy — a false positive that also fires "${pretty(w)}" (hard negative for both)`}
                  className="rounded bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
                >
                  Copy
                </button>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function LabelGuide({ word }: { word: string }) {
  return (
    <details className="text-xs text-gray-500 dark:text-gray-400">
      <summary className="cursor-pointer">Labeling guide</summary>
      <p className="mt-2">Wake: exactly “{word}”. Not: the word was absent. Leave ambiguous or overlapping phrases unlabelled; do not mark them Not.</p>
    </details>
  )
}

function ClipRow({
  sample,
  allWords,
  onLabel,
  onDelete,
  onMove,
  onCopy,
  gain,
}: {
  sample: WakeSample
  allWords: string[]
  onLabel: (id: string, label: 'wake' | 'not_wake') => void
  onDelete: (id: string) => void
  onMove: (id: string, wakeword: string) => void
  onCopy: (id: string, wakeword: string) => void
  gain: number
}) {
  const [audioUrl, setAudioUrl] = useState<string | null>(null)
  const urlRef = useRef<string | null>(null)
  const audioElRef = useRef<HTMLAudioElement | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const gainRef = useRef<GainNode | null>(null)

  const [audioLoading, setAudioLoading] = useState(false)
  const [audioError, setAudioError] = useState(false)
  const mounted = useRef(true)

  const loadAudio = async () => {
    if (audioLoading) return
    setAudioLoading(true)
    setAudioError(false)
    try {
      const response = await wakewordApi.getAudioBlob(sample.id)
      if (!mounted.current) return
      const url = URL.createObjectURL(response.data)
      urlRef.current = url
      setAudioUrl(url)
    } catch {
      if (mounted.current) setAudioError(true)
    } finally {
      if (mounted.current) setAudioLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    }
  }, [])

  // Lazily route the <audio> element through a gain node — createMediaElementSource
  // requires a user gesture, so we build the graph on first play.
  const handlePlay = useCallback(() => {
    const el = audioElRef.current
    if (!el) return
    if (!ctxRef.current) {
      const Ctx = window.AudioContext || (window as any).webkitAudioContext
      const ctx = new Ctx()
      const source = ctx.createMediaElementSource(el)
      const gainNode = ctx.createGain()
      source.connect(gainNode).connect(ctx.destination)
      ctxRef.current = ctx
      gainRef.current = gainNode
    }
    if (gainRef.current) gainRef.current.gain.value = gain
    ctxRef.current.resume()
  }, [gain])

  // Reflect live toggle changes on an already-playing clip.
  useEffect(() => {
    if (gainRef.current) gainRef.current.gain.value = gain
  }, [gain])

  // Release the AudioContext when the row unmounts.
  useEffect(() => () => { ctxRef.current?.close() }, [])

  const when = new Date(sample.created_at_ms).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })

  return (
    <li className="flex flex-wrap items-center gap-3 rounded-md border border-gray-200 dark:border-gray-700 px-3 py-2">
      <span className="font-mono text-sm font-semibold text-gray-800 dark:text-gray-200 w-16">
        {sample.score.toFixed(3)}
      </span>
      {sample.false_negative && (
        <span className="rounded bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 text-xs text-blue-700 dark:text-blue-300">
          captured example
        </span>
      )}
      <MoveCopyMenu
        targets={allWords.filter((w) => w !== sample.wakeword)}
        alsoFired={(sample.also_fired ?? []).filter((w) => w !== sample.wakeword)}
        onMove={(w) => onMove(sample.id, w)}
        onCopy={(w) => onCopy(sample.id, w)}
      />
      <span className="text-xs text-gray-500 dark:text-gray-400">{when}</span>
      <span className="text-xs text-gray-400 dark:text-gray-500">
        {sample.duration_secs}s · {sample.reason}
      </span>
      {audioUrl ? (
        <audio ref={audioElRef} onPlay={handlePlay} controls src={audioUrl} className="h-8 max-w-[220px]" />
      ) : (
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" disabled={audioLoading} onClick={loadAudio} icon={<Play className="h-3.5 w-3.5" />}>
            {audioLoading ? 'Loading audio…' : audioError ? 'Retry audio' : 'Load audio'}
          </Button>
          {audioError && <span role="alert" className="text-xs text-red-600 dark:text-red-400">Audio unavailable</span>}
        </div>
      )}
      <div className="ml-auto flex items-center gap-1.5">
        <button
          onClick={() => onLabel(sample.id, 'wake')}
          title="Mark as a real wake word (positive)"
          className="flex items-center gap-1 rounded-md bg-green-100 dark:bg-green-900/40 px-2 py-1 text-xs font-medium text-green-700 dark:text-green-300 hover:bg-green-200 dark:hover:bg-green-900"
        >
          <Check className="h-3.5 w-3.5" /> Wake
        </button>
        <button
          onClick={() => onLabel(sample.id, 'not_wake')}
          title="Mark as NOT the wake word (hard negative)"
          className="flex items-center gap-1 rounded-md bg-red-100 dark:bg-red-900/40 px-2 py-1 text-xs font-medium text-red-700 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-900"
        >
          <X className="h-3.5 w-3.5" /> Not
        </button>
        <IconButton label="Delete clip" danger onClick={() => onDelete(sample.id)}>
          <Trash2 className="h-3.5 w-3.5" />
        </IconButton>
      </div>
    </li>
  )
}
