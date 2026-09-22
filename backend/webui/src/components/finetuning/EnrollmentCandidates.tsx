import { useState, useEffect, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, Pause, RefreshCw, ShieldCheck, Check, AlertTriangle } from 'lucide-react'
import { finetuningApi } from '../../services/api'
import { useGaplessPlayer } from '../../hooks/useGaplessPlayer'
import { Alert, Button, IconButton } from '../ui'

interface Clip {
  conversation_id: string
  conversation_title: string
  segment_index: number
  start: number
  end: number
  duration: number
  text: string
  gated_in: boolean
  default_selected: boolean
  reasons: string[]
  auto_identified?: boolean
}
interface SpeakerGroup {
  speaker: string
  clips: Clip[]
  selected_count: number
}
interface CandidatesResponse {
  candidates: SpeakerGroup[]
  min_duration: number
  default_per_speaker: number
  conversation_count: number
}

const clipKey = (c: Clip) => `${c.conversation_id}:${c.segment_index}`

/**
 * Curated speaker enrollment: shows quality-gated candidate clips (per speaker)
 * built from each conversation's ACTIVE transcript version, pre-ticks the clean
 * ones (>= min duration, no cross-talk, deduped), and enrolls ONLY what you
 * confirm. Replaces the old "process every applied annotation" blast that
 * mismatched audio↔label and enrolled overlap/short scraps.
 */
export default function EnrollmentCandidates() {
  const qc = useQueryClient()
  const player = useGaplessPlayer()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [enrolling, setEnrolling] = useState(false)
  const [privacyHeld, setPrivacyHeld] = useState(false)
  const [resultMsg, setResultMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Off by default: only clips you relabelled by hand are candidates. When on,
  // segments auto-labelled by identification are also shown (never pre-ticked).
  const [includeIdentified, setIncludeIdentified] = useState(false)

  const { data, isLoading, isError: candidatesError, refetch, isFetching } = useQuery<CandidatesResponse>({
    queryKey: ['finetuning', 'enrollmentCandidates', includeIdentified],
    queryFn: () => finetuningApi.getEnrollmentCandidates(includeIdentified).then((r) => r.data),
  })

  // Seed the selection from the gate's defaults whenever fresh data arrives.
  useEffect(() => {
    if (!data) return
    const s = new Set<string>()
    data.candidates.forEach((g) => g.clips.forEach((c) => c.default_selected && s.add(clipKey(c))))
    setSelected(s)
  }, [data])

  const allClips = useMemo(
    () => (data?.candidates || []).flatMap((g) => g.clips.map((c) => ({ ...c, speaker: g.speaker }))),
    [data]
  )
  const selectedClips = useMemo(
    () => allClips.filter((c) => selected.has(clipKey(c))),
    [allClips, selected]
  )

  const toggle = (c: Clip) => {
    setSelected((prev) => {
      const n = new Set(prev)
      const k = clipKey(c)
      n.has(k) ? n.delete(k) : n.add(k)
      return n
    })
  }

  const setSpeakerSelected = (group: SpeakerGroup, shouldSelect: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      group.clips.forEach((clip) => {
        const key = clipKey(clip)
        if (shouldSelect && clip.default_selected) next.add(key)
        else next.delete(key)
      })
      return next
    })
  }

  const selectAllSpeakers = () => {
    setSelected(
      new Set(
        (data?.candidates || []).flatMap((group) =>
          group.clips.filter((clip) => clip.default_selected).map(clipKey)
        )
      )
    )
  }

  const deselectAllSpeakers = () => setSelected(new Set())

  const playClip = (c: Clip) => {
    const segId = `enroll-${clipKey(c)}`
    if (player.playingSegmentId === segId) player.stop()
    else player.playSegment(c.conversation_id, segId, c.start, c.end)
  }

  const handleEnroll = async () => {
    if (selectedClips.length === 0) return
    setError(null)
    setResultMsg(null)
    setPrivacyHeld(false)
    setEnrolling(true)
    try {
      const payload = selectedClips.map((c) => ({
        conversation_id: c.conversation_id,
        segment_index: c.segment_index,
        start: c.start,
        end: c.end,
        speaker: (c as any).speaker,
      }))
      const { data: res } = await finetuningApi.enrollSelectedClips(payload)
      const parts = [
        `${res.total_enrolled} enrolled (${res.enrolled_new} new, ${res.appended} appended)`,
      ]
      if (res.privacy_held > 0) {
        setPrivacyHeld(true)
        parts.push(`${res.privacy_held} held by privacy screening; review these periods in Timeline`)
      }
      if (res.failed) parts.push(`${res.failed} failed`)
      if (res.skipped) parts.push(`${res.skipped} skipped`)
      setResultMsg(parts.join(', '))
      qc.invalidateQueries({ queryKey: ['finetuning'] })
      await refetch()
    } catch (e: any) {
      setError(e.response?.data?.detail || e.response?.data?.message || e.message || 'Enrollment failed')
    } finally {
      setEnrolling(false)
    }
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center space-x-2">
          <ShieldCheck className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Curated Speaker Enrollment</h2>
        </div>
        <Button
          variant="secondary"
          onClick={() => refetch()}
          disabled={isFetching}
          icon={<RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />}
        >
          Refresh
        </Button>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
        Review clips from <strong>your speaker corrections</strong>. Only checked clips are enrolled.
      </p>
      <label className="mb-4 inline-flex items-center gap-2 cursor-pointer select-none text-sm text-gray-600 dark:text-gray-400">
        <input
          type="checkbox"
          checked={includeIdentified}
          onChange={(e) => setIncludeIdentified(e.target.checked)}
          className="h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
        />
        Also show auto-identified segments
        <span className="text-xs text-gray-400">(review before selecting)</span>
      </label>

      {resultMsg && (
        <Alert tone={privacyHeld ? "warning" : "success"} className="mb-4" icon={privacyHeld ? <AlertTriangle className="h-4 w-4" /> : <Check className="h-4 w-4" />}>
          {resultMsg}
        </Alert>
      )}
      {error && (
        <Alert tone="danger" className="mb-4" icon={<AlertTriangle className="h-4 w-4" />}>
          {error}
        </Alert>
      )}

      {candidatesError ? (
        <Alert tone="warning">Candidate list is unavailable. Privacy screening may have changed; refresh to try again.</Alert>
      ) : isLoading ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">Loading candidates…</p>
      ) : !data || data.candidates.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400 italic">
          No candidates. Apply a speaker correction to add clips here.
        </p>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 border-y border-gray-200 py-3 dark:border-gray-700">
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Choose speakers to enroll
            </span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={selectAllSpeakers}
                className="rounded-md px-3 py-1.5 text-sm font-medium text-emerald-700 hover:bg-emerald-50 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2 dark:text-emerald-300 dark:hover:bg-emerald-900/20 dark:focus:ring-offset-gray-800"
              >
                Select all
              </button>
              <Button
                variant="ghost"
                onClick={deselectAllSpeakers}
                disabled={selectedClips.length === 0}
              >
                Deselect all
              </Button>
            </div>
          </div>
          <div className="space-y-5">
            {data.candidates.map((group) => {
              const selectedCount = group.clips.filter((c) => selected.has(clipKey(c))).length
              const defaultCount = group.clips.filter((c) => c.default_selected).length
              const speakerSelected = selectedCount > 0
              return (
                <div key={group.speaker}>
                  <div className="mb-1.5 flex items-center justify-between gap-3">
                    <label className="flex min-w-0 cursor-pointer items-center gap-2">
                      <input
                        type="checkbox"
                        checked={speakerSelected}
                        onChange={() => setSpeakerSelected(group, !speakerSelected)}
                        disabled={defaultCount === 0}
                        className="h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
                      />
                      <span className="truncate font-medium text-gray-900 dark:text-gray-100">{group.speaker}</span>
                    </label>
                    <span className="flex-shrink-0 text-xs text-gray-500 dark:text-gray-400">
                      {selectedCount} of {group.clips.length} clips selected
                    </span>
                  </div>
                  <div className="space-y-1">
                  {group.clips.map((c) => {
                    const isSel = selected.has(clipKey(c))
                    const segId = `enroll-${clipKey(c)}`
                    const playing = player.playingSegmentId === segId
                    return (
                      <div
                        key={clipKey(c)}
                        className={`flex items-center gap-2 px-2 py-1.5 rounded border ${
                          isSel
                            ? 'border-emerald-300 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-900/15'
                            : c.gated_in
                            ? 'border-gray-200 dark:border-gray-700'
                            : 'border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/40 opacity-70'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={isSel}
                          onChange={() => toggle(c)}
                          className="h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
                        />
                        <IconButton
                          onClick={() => playClip(c)}
                          className="flex-shrink-0"
                          label={`Play ${c.duration}s`}
                        >
                          {playing ? <Pause className="h-3.5 w-3.5 text-emerald-600" /> : <Play className="h-3.5 w-3.5 text-gray-500" />}
                        </IconButton>
                        <span className="text-xs tabular-nums text-gray-500 dark:text-gray-400 w-12 flex-shrink-0">{c.duration}s</span>
                        <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1" title={c.text}>
                          {c.text || <em className="text-gray-400">(no text)</em>}
                        </span>
                        {c.auto_identified && (
                          <span className="text-xs font-medium text-purple-600 dark:text-purple-400 flex-shrink-0" title="Labelled by speaker identification, not by you — review before enrolling">
                            auto-identified
                          </span>
                        )}
                        {!c.gated_in && (
                          <span className="text-xs text-amber-600 dark:text-amber-400 flex-shrink-0" title={c.reasons.join(', ')}>
                            {c.reasons.join(', ')}
                          </span>
                        )}
                        <span
                          className="text-xs text-gray-400 dark:text-gray-500 truncate max-w-[10rem] flex-shrink-0 hidden sm:inline"
                          title={c.conversation_title}
                        >
                          {c.conversation_title}
                        </span>
                      </div>
                    )
                  })}
                  </div>
                </div>
              )
            })}
          </div>

          <div className="mt-5 flex items-center justify-between">
            <span className="text-sm text-gray-600 dark:text-gray-400">
              {selectedClips.length} clip{selectedClips.length === 1 ? '' : 's'} selected across{' '}
              {new Set(selectedClips.map((c) => (c as any).speaker)).size} speaker(s)
            </span>
            <button
              onClick={handleEnroll}
              disabled={enrolling || selectedClips.length === 0}
              className="flex items-center space-x-2 px-6 py-2.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            >
              {enrolling ? <RefreshCw className="h-5 w-5 animate-spin" /> : <ShieldCheck className="h-5 w-5" />}
              <span>Enroll {selectedClips.length} selected</span>
            </button>
          </div>
        </>
      )}
    </div>
  )
}
