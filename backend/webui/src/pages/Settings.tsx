import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Settings as SettingsIcon, CheckCircle, AlertCircle, RefreshCw, Volume2, Sliders, Mic, Users, Cpu, Play, Loader2, X, Check, UserCircle, Database, Plus, Trash2, Pencil } from 'lucide-react'
import { systemApi, speakerApi, authApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { useDiarizationSettings, useLLMOperations, useMiscSettings, useModels, useTimelineGroupingSettings, ModelView, ModelType } from '../hooks/useSystem'
import ApiKeysCard from '../components/ApiKeysCard'
import ExternalServices from '../components/ExternalServices'
import AsrContextSettings from '../components/AsrContextSettings'
import AutomationSettings from '../components/AutomationSettings'
import { Alert, Button, IconButton, Input, Modal, Select, Textarea } from '../components/ui'

interface DiarizationSettings {
  diarization_source: 'provider' | 'pyannote'
  similarity_threshold: number
  min_duration: number
  collar: number
  min_duration_off: number
  min_speakers: number | null
  max_speakers: number | null
}

export default function Settings() {
  const { isAdmin, user } = useAuth()

  // TanStack Query hooks
  const { data: diarizationData } = useDiarizationSettings()
  const { data: miscSettingsData } = useMiscSettings()
  const { data: llmOpsData, refetch: refetchLLMOps } = useLLMOperations()
  const { data: timelineGroupingData } = useTimelineGroupingSettings()

  // Local state for editable settings
  const [diarizationSettings, setDiarizationSettings] = useState<DiarizationSettings>({
    diarization_source: 'provider',
    similarity_threshold: 0.15,
    min_duration: 0.5,
    collar: 2.0,
    min_duration_off: 1.5,
    min_speakers: null,
    max_speakers: null
  })
  const [diarizationLoading, setDiarizationLoading] = useState(false)

  const [miscSettings, setMiscSettings] = useState({
    per_segment_speaker_id: false,
    streaming_fallback_timeout_seconds: 120,
    always_batch_retranscribe: false,
    audio_filtering_require_speech: true,
    live_segmentation: 'streaming_stt' as 'streaming_stt' | 'windowed_batch' | 'off',
  })
  const [miscLoading, setMiscLoading] = useState(false)
  const [miscMessage, setMiscMessage] = useState('')
  const [audioFilterLoading, setAudioFilterLoading] = useState(false)
  const [audioFilterMessage, setAudioFilterMessage] = useState('')
  const [timelineGrouping, setTimelineGrouping] = useState({ pregenerate: true, prefetch_days: 5 })
  const [timelineGroupingLoading, setTimelineGroupingLoading] = useState(false)
  const [timelineGroupingMessage, setTimelineGroupingMessage] = useState('')

  // Identity settings (how the user/assistant are labeled when extracting chat memories)
  const [displayName, setDisplayName] = useState('')
  const [assistantName, setAssistantName] = useState('')
  const [identityLoading, setIdentityLoading] = useState(false)
  const [identityMessage, setIdentityMessage] = useState('')

  // Sync query data into local editable state
  useEffect(() => {
    if (diarizationData) setDiarizationSettings(diarizationData)
  }, [diarizationData])


  useEffect(() => {
    if (miscSettingsData) setMiscSettings(miscSettingsData)
  }, [miscSettingsData])

  useEffect(() => {
    if (timelineGroupingData) setTimelineGrouping(timelineGroupingData)
  }, [timelineGroupingData])

  const saveTimelineGrouping = async () => {
    try {
      setTimelineGroupingLoading(true)
      setTimelineGroupingMessage('')
      await systemApi.saveTimelineGroupingSettings(timelineGrouping)
      setTimelineGroupingMessage('Review buffer saved')
      setTimeout(() => setTimelineGroupingMessage(''), 3000)
    } catch (err: any) {
      setTimelineGroupingMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setTimelineGroupingLoading(false)
    }
  }

  // Load current identity from the user's profile
  useEffect(() => {
    authApi.getMe()
      .then((res) => {
        setDisplayName(res.data.display_name || '')
        setAssistantName(res.data.assistant_name || '')
      })
      .catch(() => {
        // Non-fatal: leave fields blank if profile can't be loaded
      })
  }, [])

  const saveIdentity = async () => {
    try {
      setIdentityLoading(true)
      setIdentityMessage('')
      await authApi.updateMe({
        display_name: displayName.trim(),
        assistant_name: assistantName.trim(),
      })
      setIdentityMessage('Identity saved successfully')
      setTimeout(() => setIdentityMessage(''), 3000)
    } catch (err: any) {
      setIdentityMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setIdentityLoading(false)
    }
  }

  const saveMiscSettings = async () => {
    try {
      setMiscLoading(true)
      setMiscMessage('')
      const response = await systemApi.saveMiscSettings(miscSettings)
      if (response.data.status === 'success') {
        setMiscMessage('Settings saved successfully')
        setTimeout(() => setMiscMessage(''), 3000)
      } else {
        setMiscMessage('Failed to save settings')
      }
    } catch (err: any) {
      setMiscMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setMiscLoading(false)
    }
  }

  const saveAudioFiltering = async () => {
    try {
      setAudioFilterLoading(true)
      setAudioFilterMessage('')
      const response = await systemApi.saveMiscSettings({
        audio_filtering_require_speech: miscSettings.audio_filtering_require_speech,
      })
      if (response.data.status === 'success') {
        setAudioFilterMessage(
          response.data.requires_worker_restart
            ? 'Saved — workers are restarting to apply the change'
            : 'Audio filtering settings saved successfully'
        )
        setTimeout(() => setAudioFilterMessage(''), 4000)
      } else {
        setAudioFilterMessage('Failed to save settings')
      }
    } catch (err: any) {
      setAudioFilterMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setAudioFilterLoading(false)
    }
  }

  const saveDiarizationSettings = async () => {
    try {
      setDiarizationLoading(true)
      const response = await systemApi.saveDiarizationSettings(diarizationSettings)
      if (response.data.status === 'success') {
        alert('Diarization settings saved successfully!')
      } else {
        alert(`Failed to save settings: ${response.data.error || 'Unknown error'}`)
      }
    } catch (err: any) {
      alert(`Error saving settings: ${err.message}`)
    } finally {
      setDiarizationLoading(false)
    }
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <SettingsIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view settings.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center space-x-2 mb-6">
        <SettingsIcon className="h-6 w-6 text-blue-600" />
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Settings
        </h1>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Identity */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <UserCircle className="h-5 w-5 mr-2 text-blue-600" />
            Identity
          </h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
            Names used in chat memories. Blank names appear as “User” and “Assistant”.
          </p>
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Your name
              </label>
              <Input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="e.g. Alex"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Assistant name
              </label>
              <Input
                type="text"
                value={assistantName}
                onChange={(e) => setAssistantName(e.target.value)}
                placeholder="e.g. Chronicle"
              />
            </div>
            <Button variant="primary" size="md" className="w-full" onClick={saveIdentity} disabled={identityLoading}>
              {identityLoading ? 'Saving...' : 'Save Identity'}
            </Button>
            {identityMessage && (
              <Alert tone={identityMessage.includes('Error') ? 'danger' : 'success'} className="text-xs">
                {identityMessage}
              </Alert>
            )}
          </div>
        </div>

        {/* Diarization Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Volume2 className="h-5 w-5 mr-2 text-blue-600" />
            Diarization Settings
          </h3>

          <div className="space-y-4">
            {/* Diarization Source Selector */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
                Diarization Source
              </label>
              <div className="space-y-2">
                <label className="flex items-center">
                  <input
                    type="radio"
                    name="diarization_source"
                    value="provider"
                    checked={diarizationSettings.diarization_source === 'provider'}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      diarization_source: e.target.value as 'provider' | 'pyannote'
                    }))}
                    className="mr-2"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    <strong>Provider</strong> - Trust speaker segments from the transcription provider when available
                  </span>
                </label>
                <label className="flex items-center">
                  <input
                    type="radio"
                    name="diarization_source"
                    value="pyannote"
                    checked={diarizationSettings.diarization_source === 'pyannote'}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      diarization_source: e.target.value as 'provider' | 'pyannote'
                    }))}
                    className="mr-2"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    <strong>Pyannote</strong> - Always re-diarize locally with configurable parameters
                  </span>
                </label>
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                {diarizationSettings.diarization_source === 'provider'
                  ? 'Diarized segments from the transcription provider are used as-is; Pyannote runs as a fallback when the provider does not diarize. The parameters below apply to the fallback and speaker identification.'
                  : 'Pyannote re-diarizes every transcript locally with full parameter control.'
                }
              </div>
            </div>

            {/* Similarity Threshold (always shown) */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Similarity Threshold: {diarizationSettings.similarity_threshold}
              </label>
              <input
                type="range"
                min="0.05"
                max="0.5"
                step="0.01"
                value={diarizationSettings.similarity_threshold}
                onChange={(e) => setDiarizationSettings(prev => ({
                  ...prev,
                  similarity_threshold: parseFloat(e.target.value)
                }))}
                className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
              />
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Lower values = more sensitive speaker identification
              </div>
            </div>

            {/* Pyannote parameters (apply when pyannote diarizes, including provider fallback) */}
            <>
                {/* Min Duration */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Min Duration: {diarizationSettings.min_duration}s
                  </label>
                  <input
                    type="range"
                    min="0.1"
                    max="2.0"
                    step="0.1"
                    value={diarizationSettings.min_duration}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      min_duration: parseFloat(e.target.value)
                    }))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Minimum speech segment duration
                  </div>
                </div>

                {/* Collar */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Collar: {diarizationSettings.collar}s
                  </label>
                  <input
                    type="range"
                    min="0.5"
                    max="5.0"
                    step="0.1"
                    value={diarizationSettings.collar}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      collar: parseFloat(e.target.value)
                    }))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Buffer around speaker segments
                  </div>
                </div>

                {/* Min Duration Off */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    Min Duration Off: {diarizationSettings.min_duration_off}s
                  </label>
                  <input
                    type="range"
                    min="0.5"
                    max="3.0"
                    step="0.1"
                    value={diarizationSettings.min_duration_off}
                    onChange={(e) => setDiarizationSettings(prev => ({
                      ...prev,
                      min_duration_off: parseFloat(e.target.value)
                    }))}
                    className="w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                  />
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Minimum silence between speakers
                  </div>
                </div>

                {/* Optional speaker-count constraints */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                      Min Speakers
                    </label>
                    <input
                      type="number"
                      min="1"
                      max="20"
                      step="1"
                      placeholder="Automatic"
                      value={diarizationSettings.min_speakers ?? ''}
                      onChange={(e) => setDiarizationSettings(prev => ({
                        ...prev,
                        min_speakers: e.target.value === '' ? null : parseInt(e.target.value)
                      }))}
                      className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                      Max Speakers
                    </label>
                    <input
                      type="number"
                      min="1"
                      max="20"
                      step="1"
                      placeholder="Automatic"
                      value={diarizationSettings.max_speakers ?? ''}
                      onChange={(e) => setDiarizationSettings(prev => ({
                        ...prev,
                        max_speakers: e.target.value === '' ? null : parseInt(e.target.value)
                      }))}
                      className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
                    />
                  </div>
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 -mt-2">
                  Leave both blank to let pyannote infer the speaker count. Constraints can materially change segmentation.
                </div>
            </>

            {/* Save Button */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
              <Button
                variant="primary"
                size="md"
                className="w-full"
                onClick={saveDiarizationSettings}
                disabled={diarizationLoading}
              >
                {diarizationLoading ? 'Saving...' : 'Save Diarization Settings'}
              </Button>
            </div>
          </div>
        </div>

        {/* Processing Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Sliders className="h-5 w-5 mr-2 text-blue-600" />
            Processing Settings
          </h3>

          <div className="space-y-4">
            {/* Always Batch Re-Transcribe Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Always Batch Re-Transcribe
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  After each streaming conversation, re-transcribe with the batch provider for higher quality. Streaming transcript is shown immediately as a preview; memories and summaries are only generated from the batch result.
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  checked={miscSettings.always_batch_retranscribe}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    always_batch_retranscribe: e.target.checked
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
              </label>
            </div>

            {/* Pseudo-streaming via batch Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Pseudo-streaming via batch
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  {miscSettings.live_segmentation === 'streaming_stt'
                    ? 'A real streaming STT provider is configured (stt_stream), so live transcripts come from it directly. This batch-window preview does not apply.'
                    : 'Show a live transcript preview during recording by batch-transcribing fixed audio windows -- for batch ASR (e.g. VibeVoice) that has no true streaming. When off, no live transcript is shown; the full transcript is produced by batch transcription when the conversation ends. Changing this restarts the workers.'}
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  disabled={miscSettings.live_segmentation === 'streaming_stt'}
                  checked={miscSettings.live_segmentation === 'windowed_batch'}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    live_segmentation: e.target.checked ? 'windowed_batch' : 'off'
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600 peer-disabled:opacity-40 peer-disabled:cursor-not-allowed"></div>
              </label>
            </div>

            {/* Speaker Identification Mode Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Speaker Identification Mode
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  {miscSettings.per_segment_speaker_id
                    ? 'Identify each segment individually -- better accuracy after fine-tuning'
                    : 'Majority vote per speaker label -- faster, groups segments by label'}
                </div>
              </div>
              <div className="flex items-center ml-4 gap-2">
                <span className={`text-xs font-medium ${!miscSettings.per_segment_speaker_id ? 'text-blue-600 dark:text-blue-400' : 'text-gray-400 dark:text-gray-500'}`}>
                  Voting
                </span>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={miscSettings.per_segment_speaker_id}
                    onChange={(e) => setMiscSettings(prev => ({
                      ...prev,
                      per_segment_speaker_id: e.target.checked
                    }))}
                    className="sr-only peer"
                  />
                  <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
                </label>
                <span className={`text-xs font-medium ${miscSettings.per_segment_speaker_id ? 'text-blue-600 dark:text-blue-400' : 'text-gray-400 dark:text-gray-500'}`}>
                  Per Segment
                </span>
              </div>
            </div>

            {/* Transcription Job Timeout */}
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  Streaming Fallback Timeout
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  Max seconds for streaming fallback check ({Math.round(miscSettings.streaming_fallback_timeout_seconds / 60)} min). How long to wait before giving up on batch transcription fallback.
                </div>
              </div>
              <input
                type="number"
                min={60}
                max={7200}
                step={60}
                value={miscSettings.streaming_fallback_timeout_seconds}
                onChange={(e) => setMiscSettings(prev => ({
                  ...prev,
                  streaming_fallback_timeout_seconds: Math.max(60, Math.min(7200, parseInt(e.target.value) || 60))
                }))}
                className="ml-4 w-24 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>


            {/* Status Message */}
            {miscMessage && (
              <Alert
                tone={miscMessage.includes('Error') || miscMessage.includes('Failed') ? 'danger' : 'success'}
              >
                {miscMessage}
              </Alert>
            )}

            {/* Save Button */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
              <Button
                variant="primary"
                size="md"
                className="w-full"
                onClick={saveMiscSettings}
                disabled={miscLoading}
              >
                {miscLoading ? 'Saving...' : 'Save Processing Settings'}
              </Button>
            </div>
          </div>
        </div>

        {/* Audio Filtering */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Volume2 className="h-5 w-5 mr-2 text-blue-600" />
            Audio Filtering
          </h3>

          <div className="space-y-4">
            {/* Require Speech Toggle */}
            <div className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
              <div className="flex-1">
                <div className="font-medium text-gray-900 dark:text-gray-100">
                  Require Speech Before Transcription
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  Run local voice activity detection on incoming audio and skip transcription entirely when no speech is found. Applies to ScreenPipe sessions (rejected at ingest), file uploads, batch and fallback transcription, and windowed live previews. Saves provider cost on silent recordings; live streaming ASR is not affected. Saving restarts the workers.
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer ml-4">
                <input
                  type="checkbox"
                  checked={miscSettings.audio_filtering_require_speech}
                  onChange={(e) => setMiscSettings(prev => ({
                    ...prev,
                    audio_filtering_require_speech: e.target.checked
                  }))}
                  className="sr-only peer"
                />
                <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-600 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
              </label>
            </div>

            {/* Status Message */}
            {audioFilterMessage && (
              <Alert
                tone={audioFilterMessage.includes('Error') || audioFilterMessage.includes('Failed') ? 'danger' : 'success'}
              >
                {audioFilterMessage}
              </Alert>
            )}

            {/* Save Button */}
            <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
              <Button
                variant="primary"
                size="md"
                className="w-full"
                onClick={saveAudioFiltering}
                disabled={audioFilterLoading}
              >
                {audioFilterLoading ? 'Saving...' : 'Save Audio Filtering'}
              </Button>
            </div>
          </div>
        </div>

        {/* Speaker Configuration */}
        <SpeakerConfiguration user={user} />

        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2 flex items-center">
            <Sliders className="h-5 w-5 mr-2 text-blue-600" /> Timeline review buffer
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
            Prepare grouping suggestions ahead of the day you are reviewing, so the next days are ready without waiting.
          </p>
          <label className="flex items-start gap-3 mb-4">
            <input type="checkbox" checked={timelineGrouping.pregenerate}
              onChange={(event) => setTimelineGrouping(current => ({ ...current, pregenerate: event.target.checked }))}
              className="mt-1" />
            <span><span className="block text-sm font-medium text-gray-900 dark:text-gray-100">Prepare suggestions automatically</span>
              <span className="block text-xs text-gray-500 dark:text-gray-400">Runs in the background for reviewable Timeline days.</span></span>
          </label>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1" htmlFor="timeline-prefetch-days">Days ready ahead</label>
          <div className="flex items-center gap-3">
            <Input id="timeline-prefetch-days" type="number" min={1} max={30}
              value={timelineGrouping.prefetch_days} disabled={!timelineGrouping.pregenerate}
              onChange={(event) => setTimelineGrouping(current => ({ ...current, prefetch_days: Number(event.target.value) }))}
              className="w-24" />
            <span className="text-sm text-gray-500 dark:text-gray-400">oldest pending review days</span>
          </div>
          <Button className="mt-4" onClick={saveTimelineGrouping} disabled={timelineGroupingLoading}>
            {timelineGroupingLoading ? 'Saving…' : 'Save review buffer'}
          </Button>
          {timelineGroupingMessage && <Alert tone={timelineGroupingMessage.startsWith('Error') ? 'danger' : 'success'} className="mt-3 text-xs">{timelineGroupingMessage}</Alert>}
        </div>

        {/* AI Model Settings */}
        {llmOpsData && (
          <LLMOperationsCard
            data={llmOpsData}
            onSaved={refetchLLMOps}
          />
        )}

        {/* Active Models — repoint which registry model each role uses */}
        <ActiveModelsCard isAdmin={isAdmin} />

        {/* Automation & schedules — when background jobs run (run-now lives on Training) */}
        <AutomationSettings isAdmin={isAdmin} />

        {/* ASR recognition hints (keyword boosting vs LLM context prompt) */}
        <div className="lg:col-span-2">
          <AsrContextSettings isAdmin={isAdmin} />
        </div>

        {/* ASR / TTS Providers (host service-manager agent) — switch the running service + its model */}
        <div className="lg:col-span-2">
          <ExternalServices isAdmin={isAdmin} mode="providers" />
        </div>

        {/* Model Registry — add/edit/delete model definitions and API keys */}
        <ModelRegistryCard isAdmin={isAdmin} />

        {/* API keys — long-lived credentials for clients that can't re-login */}
        <ApiKeysCard />
      </div>
    </div>
  )
}

const OPERATION_LABELS: Record<string, string> = {
  memory_extraction: 'Memory Extraction',
  memory_update: 'Memory Update',
  memory_reprocess: 'Memory Reprocess',
  conversation_title: 'Conversation Title',
  short_summary: 'Short Summary',
  detailed_summary: 'Detailed Summary',
  entity_extraction: 'Entity Extraction',
  chat: 'Chat',
  prompt_optimization: 'Prompt Optimization',
  plugin_assistant: 'Plugin Assistant',
  timeline_consolidation: 'Timeline Grouping Suggestions',
  timeline_segmentation: 'Timeline Segmentation',
  timeline_thumbnail: 'Timeline Thumbnail Selection',
  immich_visual_evidence: 'Immich Visual Evidence',
  manual_memory_image: 'Manual Memory Image',
}

interface LLMOpsData {
  operations: Record<string, { model: string | null; temperature: number | null; max_tokens: number | null; response_format: string | null }>
  available_models: Array<{ name: string; description: string; provider: string }>
  default_llm: string | null
  effective_routing: Record<string, { model: string; model_name: string; provider: string; endpoint: string; location: string; source: string }>
  operation_route_audit: { total: number; self_hosted: number; external: number; unknown: number }
  runtime_routes: Array<{
    workload: string
    adapter: string
    operation: string
    model: string
    model_name: string
    provider: string
    endpoint: string
    location: string
    source: string
    max_tokens: number | null
    reasoning_effort: string | null
    response_format: string
  }>
}

function LLMOperationsCard({ data, onSaved }: { data: LLMOpsData; onSaved: () => void }) {
  const [ops, setOps] = useState<Record<string, any>>({})
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [testResults, setTestResults] = useState<Record<string, { loading: boolean; success?: boolean; latency?: number; error?: string }>>({})

  useEffect(() => {
    if (data?.operations) {
      setOps({ ...data.operations })
    }
  }, [data])

  const updateOp = (opName: string, field: string, value: any) => {
    setOps(prev => ({
      ...prev,
      [opName]: { ...prev[opName], [field]: value },
    }))
  }

  const handleSave = async () => {
    try {
      setSaving(true)
      setMessage('')
      const response = await systemApi.saveLLMOperations(ops)
      if (response.data.status === 'success') {
        setMessage('Settings saved successfully')
        onSaved()
        setTimeout(() => setMessage(''), 3000)
      } else {
        setMessage('Failed to save settings')
      }
    } catch (err: any) {
      setMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (opName: string) => {
    const modelName = ops[opName]?.model || null
    setTestResults(prev => ({ ...prev, [opName]: { loading: true } }))
    try {
      const response = await systemApi.testLLMModel(modelName)
      const d = response.data
      setTestResults(prev => ({
        ...prev,
        [opName]: { loading: false, success: d.success, latency: d.latency_ms, error: d.error },
      }))
    } catch (err: any) {
      setTestResults(prev => ({
        ...prev,
        [opName]: { loading: false, success: false, error: err.message },
      }))
    }
  }

  const operationNames = Object.keys(ops)
  if (operationNames.length === 0) return null

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 lg:col-span-2">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Cpu className="h-5 w-5 mr-2 text-blue-600" />
        AI Model Settings
      </h3>

      {data.runtime_routes?.length > 0 && (
        <details className="mb-6 overflow-x-auto rounded-md border border-gray-200 dark:border-gray-700">
          <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-gray-700 dark:text-gray-300">
            Runtime route details
          </summary>
          <table className="w-full text-xs">
            <thead className="text-gray-700 dark:text-gray-300">
              <tr className="border-b border-gray-200 dark:border-gray-700">
                <th className="px-3 py-2 text-left">Workload</th>
                <th className="px-3 py-2 text-left">Adapter</th>
                <th className="px-3 py-2 text-left">Effective model</th>
                <th className="px-3 py-2 text-left">Location</th>
                <th className="px-3 py-2 text-left">Endpoint</th>
                <th className="px-3 py-2 text-left">Selected by</th>
              </tr>
            </thead>
            <tbody>
              {data.runtime_routes.map(route => (
                <tr key={route.workload} className="border-b border-gray-100 last:border-0 dark:border-gray-700">
                  <td className="whitespace-nowrap px-3 py-2 font-medium text-gray-900 dark:text-gray-100">{route.workload}</td>
                  <td className="px-3 py-2 text-gray-600 dark:text-gray-300">{route.adapter}</td>
                  <td className="px-3 py-2">
                    <div className="text-gray-900 dark:text-gray-100">{route.model}</div>
                    <div className="text-[10px] text-gray-500 dark:text-gray-400">
                      {route.provider} · {route.model_name}
                      {route.max_tokens ? ` · ${route.max_tokens.toLocaleString()} tokens` : ''}
                      {route.reasoning_effort ? ` · reasoning ${route.reasoning_effort}` : ''}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-gray-600 dark:text-gray-300">{route.location}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-[10px] text-gray-500 dark:text-gray-400">{route.endpoint}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-[10px] text-gray-500 dark:text-gray-400">{route.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      <div className="overflow-x-auto">
        {data.operation_route_audit && (
          <div className="mb-3 text-xs text-gray-600 dark:text-gray-300">
            Named operation audit: {data.operation_route_audit.self_hosted}/{data.operation_route_audit.total} self-hosted
            {data.operation_route_audit.external > 0 ? ` · ${data.operation_route_audit.external} external` : ''}
            {data.operation_route_audit.unknown > 0 ? ` · ${data.operation_route_audit.unknown} unknown` : ''}
          </div>
        )}
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-600">
              <th className="text-left py-2 pr-3 font-medium text-gray-700 dark:text-gray-300">Operation</th>
              <th className="text-left py-2 px-3 font-medium text-gray-700 dark:text-gray-300">Model</th>
              <th className="text-left py-2 px-3 font-medium text-gray-700 dark:text-gray-300 w-48">Temperature</th>
              <th className="text-left py-2 px-3 font-medium text-gray-700 dark:text-gray-300">Max Tokens</th>
              <th className="text-center py-2 px-3 font-medium text-gray-700 dark:text-gray-300">JSON</th>
              <th className="text-center py-2 pl-3 font-medium text-gray-700 dark:text-gray-300">Test</th>
            </tr>
          </thead>
          <tbody>
            {operationNames.map(opName => {
              const op = ops[opName] || {}
              const test = testResults[opName]
              return (
                <tr key={opName} className="border-b border-gray-100 dark:border-gray-700">
                  <td className="py-2 pr-3 font-medium text-gray-900 dark:text-gray-100 whitespace-nowrap">
                    {OPERATION_LABELS[opName] || opName}
                  </td>
                  <td className="py-2 px-3">
                    <select
                      value={op.model || ''}
                      onChange={e => updateOp(opName, 'model', e.target.value || null)}
                      className="w-full px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    >
                      <option value="">Default{data.default_llm ? ` (${data.default_llm})` : ''}</option>
                      {data.available_models.map(m => (
                        <option key={m.name} value={m.name}>{m.name} — {m.provider}</option>
                      ))}
                    </select>
                    {data.effective_routing?.[opName] && (
                      <div className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
                        Effective: {data.effective_routing[opName].model}
                        {' · '}{data.effective_routing[opName].provider}
                        {' · '}{data.effective_routing[opName].location}
                        {' · '}{data.effective_routing[opName].source}
                      </div>
                    )}
                  </td>
                  <td className="py-2 px-3">
                    <div className="flex items-center gap-2">
                      <input
                        type="range"
                        min="0"
                        max="2"
                        step="0.05"
                        value={op.temperature ?? 0.2}
                        onChange={e => updateOp(opName, 'temperature', parseFloat(e.target.value))}
                        className="flex-1 h-1.5 bg-gray-200 dark:bg-gray-600 rounded-lg appearance-none cursor-pointer"
                      />
                      <span className="text-xs text-gray-500 dark:text-gray-400 w-8 text-right">
                        {(op.temperature ?? 0.2).toFixed(2)}
                      </span>
                    </div>
                  </td>
                  <td className="py-2 px-3">
                    <input
                      type="number"
                      min="1"
                      placeholder="—"
                      value={op.max_tokens ?? ''}
                      onChange={e => updateOp(opName, 'max_tokens', e.target.value ? parseInt(e.target.value) : null)}
                      className="w-20 px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    />
                  </td>
                  <td className="py-2 px-3 text-center">
                    <input
                      type="checkbox"
                      checked={op.response_format === 'json'}
                      onChange={e => updateOp(opName, 'response_format', e.target.checked ? 'json' : null)}
                      className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                    />
                  </td>
                  <td className="py-2 pl-3 text-center">
                    <button
                      onClick={() => handleTest(opName)}
                      disabled={test?.loading}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                      title="Test model connection"
                    >
                      {test?.loading ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : test?.success === true ? (
                        <Check className="h-3 w-3 text-green-500" />
                      ) : test?.success === false ? (
                        <X className="h-3 w-3 text-red-500" />
                      ) : (
                        <Play className="h-3 w-3" />
                      )}
                      {test?.latency ? `${test.latency}ms` : 'Test'}
                    </button>
                    {test?.error && (
                      <div className="text-xs text-red-500 mt-1 max-w-[150px] truncate" title={test.error}>
                        {test.error}
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Status Message */}
      {message && (
        <Alert
          tone={message.includes('Error') || message.includes('Failed') ? 'danger' : 'success'}
          className="mt-4"
        >
          {message}
        </Alert>
      )}

      {/* Save Button */}
      <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
        <Button variant="primary" size="md" className="w-full" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save AI Model Settings'}
        </Button>
      </div>
    </div>
  )
}

// Speaker Configuration Component
function SpeakerConfiguration({ user }: { user: any }) {
  const [speakerServiceStatus, setSpeakerServiceStatus] = useState<any>(null)
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<any[]>([])
  const [speakersUnavailable, setSpeakersUnavailable] = useState(false)
  const [primarySpeakers, setPrimarySpeakers] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  // Wake-word speaker gate: only let a wake word fire for selected speakers.
  const [gateEnabled, setGateEnabled] = useState(false)
  const [gateSpeakers, setGateSpeakers] = useState<any[]>([])
  const [gateSaving, setGateSaving] = useState(false)
  const [gateMessage, setGateMessage] = useState('')

  useEffect(() => {
    loadSpeakerData()
  }, [])

  const loadSpeakerData = async () => {
    setLoading(true)
    try {
      const [configResponse, speakersResponse, statusResponse, gateResponse] = await Promise.allSettled([
        speakerApi.getSpeakerConfiguration(),
        speakerApi.getEnrolledSpeakers(),
        user?.is_superuser ? speakerApi.getSpeakerServiceStatus() : Promise.resolve({ data: null }),
        speakerApi.getWakewordSpeakerGate()
      ])

      if (configResponse.status === 'fulfilled') {
        setPrimarySpeakers(configResponse.value.data.primary_speakers || [])
      }

      setSpeakersUnavailable(speakersResponse.status === 'rejected'
        || speakersResponse.value.data.service_available === false
        || (statusResponse.status === 'fulfilled' && statusResponse.value.data?.healthy === false))

      if (speakersResponse.status === 'fulfilled') {
        setEnrolledSpeakers(speakersResponse.value.data.speakers || [])
      }

      if (statusResponse.status === 'fulfilled' && statusResponse.value.data) {
        setSpeakerServiceStatus(statusResponse.value.data)
      }

      if (gateResponse.status === 'fulfilled') {
        setGateEnabled(!!gateResponse.value.data.enabled)
        setGateSpeakers(gateResponse.value.data.speakers || [])
      }

    } catch (error) {
      console.error('Error loading speaker data:', error)
      setMessage('Failed to load speaker configuration')
    } finally {
      setLoading(false)
    }
  }

  const togglePrimarySpeaker = (speaker: any) => {
    const isSelected = primarySpeakers.some(ps => ps.speaker_id === speaker.id)

    if (isSelected) {
      setPrimarySpeakers(prev => prev.filter(ps => ps.speaker_id !== speaker.id))
    } else {
      setPrimarySpeakers(prev => [...prev, {
        speaker_id: speaker.id,
        name: speaker.name,
        user_id: speaker.user_id
      }])
    }
  }

  const saveSpeakerConfiguration = async () => {
    setSaving(true)
    setMessage('')

    try {
      await speakerApi.updateSpeakerConfiguration(primarySpeakers)
      setMessage(`Saved! ${primarySpeakers.length} primary speakers configured.`)
      setTimeout(() => setMessage(''), 3000)
    } catch (error: any) {
      console.error('Error saving speaker configuration:', error)
      setMessage(`Failed to save: ${error.response?.data?.error || error.message}`)
    } finally {
      setSaving(false)
    }
  }

  const resetConfiguration = () => {
    setPrimarySpeakers([])
    setMessage('Configuration reset. Click Save to apply changes.')
  }

  const toggleGateSpeaker = (speaker: any) => {
    const isSelected = gateSpeakers.some(gs => gs.speaker_id === speaker.id)
    if (isSelected) {
      setGateSpeakers(prev => prev.filter(gs => gs.speaker_id !== speaker.id))
    } else {
      setGateSpeakers(prev => [...prev, { speaker_id: speaker.id, name: speaker.name }])
    }
  }

  const saveWakewordGate = async () => {
    setGateSaving(true)
    setGateMessage('')
    try {
      await speakerApi.updateWakewordSpeakerGate(gateEnabled, gateSpeakers)
      setGateMessage(
        gateEnabled
          ? `Saved! Wake word will only fire for ${gateSpeakers.length} selected speaker(s).`
          : 'Saved! Wake word speaker gate disabled.'
      )
      setTimeout(() => setGateMessage(''), 3000)
    } catch (error: any) {
      console.error('Error saving wake-word speaker gate:', error)
      setGateMessage(`Failed to save: ${error.response?.data?.error || error.message}`)
    } finally {
      setGateSaving(false)
    }
  }

  // ↓ SpeakerConfiguration body continues below; new model-config cards live at file end.
  // Don't show the section if speaker service is explicitly disabled or unavailable
  const shouldShowSection = speakerServiceStatus !== null || enrolledSpeakers.length > 0 || loading || speakersUnavailable

  if (!shouldShowSection) {
    return null
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Mic className="h-5 w-5 mr-2 text-blue-600" />
        Speaker Processing Filter
        {speakerServiceStatus && (
          <span className={`ml-2 px-2 py-1 text-xs rounded-full ${
            speakerServiceStatus.healthy
              ? 'bg-green-100 text-green-800'
              : 'bg-red-100 text-red-800'
          }`}>
            {speakerServiceStatus.healthy ? 'Service Available' : 'Service Unavailable'}
          </span>
        )}
      </h3>

      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Select primary speakers for memory processing. Only conversations where these speakers are detected will have memories extracted.
        Leave empty to process all conversations.
      </p>

      {/* Service Status Info */}
      {speakerServiceStatus && !speakerServiceStatus.healthy && (
        <div className="mb-4 p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-700 rounded-md">
          <div className="flex">
            <AlertCircle className="h-5 w-5 text-yellow-400 mr-2 flex-shrink-0" />
            <div>
              <h4 className="text-sm font-medium text-yellow-800 dark:text-yellow-300">Speaker Service Unavailable</h4>
              <p className="text-sm text-yellow-700 dark:text-yellow-400 mt-1">
                {speakerServiceStatus.message}. Enrolled speakers cannot be verified.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div role="status" aria-label="Loading speaker data" aria-busy="true">
          <div className="mb-3 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <RefreshCw className="h-3.5 w-3.5 animate-spin text-blue-600" />
            Checking enrolled speakers
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map(index => (
              <div
                key={index}
                className="flex animate-pulse items-center gap-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700"
              >
                <div className="h-9 w-9 flex-shrink-0 rounded-full bg-gray-200 dark:bg-gray-700" />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="h-3.5 w-24 rounded bg-gray-200 dark:bg-gray-700" />
                  <div className="h-2.5 w-16 rounded bg-gray-100 dark:bg-gray-700/70" />
                </div>
              </div>
            ))}
          </div>
          <span className="sr-only">Loading speaker configuration and enrolled speakers.</span>
        </div>
      )}

      {!loading && speakersUnavailable && !speakerServiceStatus && <Alert tone="warning" className="mb-4">Enrolled speakers could not be loaded.</Alert>}

      {/* No Speakers Available */}
      {!loading && !speakersUnavailable && enrolledSpeakers.length === 0 && (
        <div className="text-center py-8">
          <Users className="h-12 w-12 text-gray-400 mx-auto mb-4" />
          <p className="text-gray-500 dark:text-gray-400">
            No enrolled speakers found. Enroll speakers in the speaker recognition service to configure primary users.
          </p>
        </div>
      )}

      {/* Speaker Selection */}
      {!loading && enrolledSpeakers.length > 0 && (
        <div className="space-y-4">
          {/* Current Configuration */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Primary speakers selected: {primarySpeakers.length}
            </span>
            <button
              onClick={resetConfiguration}
              className="text-sm text-red-600 hover:text-red-800 font-medium"
            >
              Reset
            </button>
          </div>

          {/* Speaker List */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 max-h-60 overflow-y-auto">
            {enrolledSpeakers.map((speaker) => {
              const isSelected = primarySpeakers.some(ps => ps.speaker_id === speaker.id)
              return (
                <div
                  key={speaker.id}
                  className={`p-3 border rounded-lg cursor-pointer transition-colors ${
                    isSelected
                      ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 text-blue-900 dark:text-blue-300'
                      : 'border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:border-gray-300 dark:hover:border-gray-500'
                  }`}
                  onClick={() => togglePrimarySpeaker(speaker)}
                >
                  <div className="flex items-center">
                    <div className={`w-4 h-4 mr-3 rounded border-2 flex items-center justify-center ${
                      isSelected ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-500'
                    }`}>
                      {isSelected && <CheckCircle className="h-3 w-3 text-white" />}
                    </div>
                    <div>
                      <div className="font-medium">{speaker.name}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        {speaker.audio_sample_count || 0} samples
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Save Button */}
          <div className="flex items-center justify-between pt-4 border-t border-gray-200 dark:border-gray-600">
            <div className="flex-1">
              {message && (
                <p className={`text-sm ${
                  message.includes('Failed') ? 'text-red-600' : 'text-green-600'
                }`}>
                  {message}
                </p>
              )}
            </div>
            <Button variant="primary" size="md" onClick={saveSpeakerConfiguration} disabled={saving}>
              {saving ? 'Saving...' : 'Save Configuration'}
            </Button>
          </div>

          {/* Wake Word Speaker Gate */}
          <div className="pt-6 mt-2 border-t border-gray-200 dark:border-gray-600 space-y-4">
            <div>
              <h4 className="text-base font-semibold text-gray-900 dark:text-gray-100">
                Wake word access
              </h4>
              <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                When on, a wake word only triggers a command if one of the selected
                people is recognized in what was said. Others saying the wake word are
                ignored. If the speaker service is unavailable, the wake word still fires.
              </p>
            </div>

            {/* Enable toggle */}
            <label className="flex items-center cursor-pointer select-none">
              <input
                type="checkbox"
                checked={gateEnabled}
                onChange={(e) => setGateEnabled(e.target.checked)}
                className="h-4 w-4 text-blue-600 rounded border-gray-300"
              />
              <span className="ml-2 text-sm font-medium text-gray-700 dark:text-gray-300">
                Only fire for selected people
              </span>
            </label>

            {/* Speaker allowlist (only when gating) */}
            {gateEnabled && (
              <>
                {gateSpeakers.length === 0 && (
                  <div className="flex items-start p-3 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-700 rounded-md">
                    <AlertCircle className="h-5 w-5 text-yellow-400 mr-2 flex-shrink-0" />
                    <p className="text-sm text-yellow-700 dark:text-yellow-400">
                      No one selected yet — the gate stays inert (all commands fire) until you pick at least one person.
                    </p>
                  </div>
                )}
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 max-h-60 overflow-y-auto">
                  {enrolledSpeakers.map((speaker) => {
                    const isSelected = gateSpeakers.some(gs => gs.speaker_id === speaker.id)
                    return (
                      <div
                        key={speaker.id}
                        className={`p-3 border rounded-lg cursor-pointer transition-colors ${
                          isSelected
                            ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 text-blue-900 dark:text-blue-300'
                            : 'border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:border-gray-300 dark:hover:border-gray-500'
                        }`}
                        onClick={() => toggleGateSpeaker(speaker)}
                      >
                        <div className="flex items-center">
                          <div className={`w-4 h-4 mr-3 rounded border-2 flex items-center justify-center ${
                            isSelected ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-500'
                          }`}>
                            {isSelected && <CheckCircle className="h-3 w-3 text-white" />}
                          </div>
                          <div className="font-medium">{speaker.name}</div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </>
            )}

            {/* Save */}
            <div className="flex items-center justify-between pt-2">
              <div className="flex-1">
                {gateMessage && (
                  <p className={`text-sm ${gateMessage.includes('Failed') ? 'text-red-600' : 'text-green-600'}`}>
                    {gateMessage}
                  </p>
                )}
              </div>
              <Button variant="primary" size="md" onClick={saveWakewordGate} disabled={gateSaving}>
                {gateSaving ? 'Saving...' : 'Save Wake Word Access'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Active Models — repoint which registry model each role (llm/stt/...) uses.
// ─────────────────────────────────────────────────────────────────────────────

const ACTIVE_MODEL_ROLES: { key: string; type: ModelType; label: string; hint: string }[] = [
  { key: 'llm', type: 'llm', label: 'LLM', hint: 'Memory, summaries, chat' },
  { key: 'fast_llm', type: 'llm', label: 'Fast LLM', hint: 'Lightweight / quick tasks' },
  { key: 'embedding', type: 'embedding', label: 'Embedding', hint: 'Vector search' },
  { key: 'stt', type: 'stt', label: 'Batch STT', hint: 'File / full-audio transcription' },
  { key: 'stt_stream', type: 'stt_stream', label: 'Streaming STT', hint: 'Live transcription' },
  { key: 'tts', type: 'tts', label: 'TTS', hint: 'Text-to-speech' },
]

function ActiveModelsCard({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient()
  const { data } = useModels(isAdmin)
  const [selected, setSelected] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (data?.defaults) {
      const next: Record<string, string> = {}
      for (const r of ACTIVE_MODEL_ROLES) next[r.key] = data.defaults[r.key] ?? ''
      setSelected(next)
    }
  }, [data])

  if (!isAdmin || !data) return null

  const dirty = ACTIVE_MODEL_ROLES.some(
    r => (selected[r.key] ?? '') !== (data.defaults[r.key] ?? '')
  )

  const handleSave = async () => {
    const updates: Record<string, string> = {}
    for (const r of ACTIVE_MODEL_ROLES) {
      const v = selected[r.key]
      if (v && v !== (data.defaults[r.key] ?? '')) updates[r.key] = v
    }
    if (Object.keys(updates).length === 0) return
    try {
      setSaving(true)
      setMessage('')
      const res = await systemApi.setActiveDefaults(updates)
      if (res.data.status === 'success') {
        setMessage('Saved — restart workers (System page) so in-flight jobs pick up the change')
        queryClient.invalidateQueries({ queryKey: ['system'] })
        setTimeout(() => setMessage(''), 6000)
      } else {
        setMessage(res.data.message || 'Failed to save')
      }
    } catch (err: any) {
      setMessage('Error: ' + (err.response?.data?.detail || err.message))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2 flex items-center">
        <Cpu className="h-5 w-5 mr-2 text-blue-600" />
        Active Models
      </h3>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
        Default model for each role. Saving changes routing; it does not start or stop provider services.
      </p>
      <div className="space-y-3">
        {ACTIVE_MODEL_ROLES.map(r => {
          const opts = data.models[r.type] || []
          return (
            <div key={r.key} className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-gray-700 dark:text-gray-300">{r.label}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400">{r.hint}</div>
              </div>
              <select
                value={selected[r.key] ?? ''}
                onChange={e => setSelected(p => ({ ...p, [r.key]: e.target.value }))}
                className="w-56 shrink-0 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              >
                {!data.defaults[r.key] && <option value="">(not set)</option>}
                {opts.length === 0 && <option value="">(none available)</option>}
                {opts.map(m => (
                  <option key={m.name} value={m.name}>
                    {m.name} — {m.model_provider}
                  </option>
                ))}
              </select>
            </div>
          )
        })}
      </div>
      {message && (
        <Alert
          tone={message.startsWith('Error') || message.includes('Failed') ? 'danger' : 'success'}
          className="mt-4"
        >
          {message}
        </Alert>
      )}
      <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
        <Button variant="primary" size="md" className="w-full" onClick={handleSave} disabled={saving || !dirty}>
          {saving ? 'Saving...' : 'Save Active Models'}
        </Button>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Model Registry — add / edit / delete model definitions (incl. API keys/URLs).
// ─────────────────────────────────────────────────────────────────────────────

const MODEL_TYPE_LABELS: Record<ModelType, string> = {
  llm: 'LLM',
  embedding: 'Embedding',
  stt: 'Batch STT',
  stt_stream: 'Streaming STT',
  tts: 'TTS',
}
const MODEL_TYPE_ORDER: ModelType[] = ['llm', 'embedding', 'stt', 'stt_stream', 'tts']
const API_KEY_MASK = '••••••••'

interface ModelForm {
  name: string
  model_type: ModelType
  model_provider: string
  api_family: string
  model_name: string
  model_url: string
  deployment: string
  deployment_endpoint: string
  api_key: string
  description: string
  capabilities: string
  embedding_dimensions: string
  model_params: string
}

function emptyModelForm(): ModelForm {
  return {
    name: '', model_type: 'llm', model_provider: 'openai', api_family: 'openai',
    model_name: '', model_url: '', deployment: '', deployment_endpoint: '', api_key: '', description: '',
    capabilities: '', embedding_dimensions: '', model_params: '',
  }
}

function modelToForm(m: ModelView): ModelForm {
  return {
    name: m.name,
    model_type: m.model_type,
    model_provider: m.model_provider,
    api_family: m.api_family || 'openai',
    model_name: m.model_name,
    model_url: m.model_url,
    deployment: m.deployment || '',
    deployment_endpoint: m.deployment_endpoint || '',
    api_key: m.api_key || '',
    description: m.description || '',
    capabilities: (m.capabilities || []).join(', '),
    embedding_dimensions: m.embedding_dimensions != null ? String(m.embedding_dimensions) : '',
    model_params: m.model_params && Object.keys(m.model_params).length
      ? JSON.stringify(m.model_params, null, 2)
      : '',
  }
}

function ModelRegistryCard({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient()
  const { data } = useModels(isAdmin)
  const [form, setForm] = useState<ModelForm | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [tests, setTests] = useState<Record<string, { loading: boolean; success?: boolean; latency?: number; error?: string }>>({})

  if (!isAdmin || !data) return null

  const openAdd = () => { setForm(emptyModelForm()); setIsNew(true); setError('') }
  const openEdit = (m: ModelView) => { setForm(modelToForm(m)); setIsNew(false); setError('') }

  const handleDelete = async (m: ModelView) => {
    if (!window.confirm(`Delete model '${m.name}'? This cannot be undone.`)) return
    try {
      await systemApi.deleteModel(m.name)
      queryClient.invalidateQueries({ queryKey: ['system'] })
    } catch (err: any) {
      alert(err.response?.data?.detail || err.message)
    }
  }

  const handleTest = async (m: ModelView) => {
    setTests(prev => ({ ...prev, [m.name]: { loading: true } }))
    try {
      const res = await systemApi.testModel(m.name)
      const d = res.data
      setTests(prev => ({ ...prev, [m.name]: { loading: false, success: d.success, latency: d.latency_ms, error: d.error } }))
    } catch (err: any) {
      setTests(prev => ({ ...prev, [m.name]: { loading: false, success: false, error: err.message } }))
    }
  }

  const handleSubmit = async () => {
    if (!form) return
    if (!form.name.trim()) { setError('Name is required'); return }
    const payload: Record<string, any> = {
      name: form.name.trim(),
      model_type: form.model_type,
      model_provider: form.model_provider.trim() || 'unknown',
      api_family: form.api_family.trim() || 'openai',
      model_name: form.model_name.trim(),
      model_url: form.deployment ? '' : form.model_url.trim(),
      deployment: form.deployment.trim() || null,
      deployment_endpoint: form.deployment_endpoint.trim() || null,
      description: form.description.trim() || null,
    }
    // api_key: '' clears it; the mask sentinel is preserved by the backend.
    payload.api_key = form.api_key
    if (form.capabilities.trim()) {
      payload.capabilities = form.capabilities.split(',').map(s => s.trim()).filter(Boolean)
    }
    if (form.embedding_dimensions.trim()) {
      const dim = parseInt(form.embedding_dimensions)
      if (!Number.isNaN(dim)) payload.embedding_dimensions = dim
    }
    if (form.model_params.trim()) {
      try {
        payload.model_params = JSON.parse(form.model_params)
      } catch {
        setError('Model params must be valid JSON')
        return
      }
    }
    try {
      setSaving(true)
      setError('')
      await systemApi.upsertModel(payload)
      queryClient.invalidateQueries({ queryKey: ['system'] })
      setForm(null)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message)
    } finally {
      setSaving(false)
    }
  }

  const keyState = (m: ModelView) =>
    !m.api_key_is_set ? '—' : m.api_key_is_ref ? 'Set (env)' : 'Set (inline)'

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 lg:col-span-2">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
          <Database className="h-5 w-5 mr-2 text-blue-600" />
          Model Registry
        </h3>
        <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={openAdd}>
          Add Model
        </Button>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
        Provider/model definitions. Built-in templates (defaults.yml) are read-only; models
        defined here can be edited or deleted. Store shared secrets as <code className="px-1 bg-gray-100 dark:bg-gray-700 rounded">{'${oc.env:VAR}'}</code> references.
      </p>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-600 text-left text-gray-700 dark:text-gray-300">
              <th className="py-2 pr-3 font-medium">Name</th>
              <th className="py-2 px-3 font-medium">Provider</th>
              <th className="py-2 px-3 font-medium">Model</th>
              <th className="py-2 px-3 font-medium">API key</th>
              <th className="py-2 px-3 font-medium">Source</th>
              <th className="py-2 pl-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {MODEL_TYPE_ORDER.flatMap(type => {
              const rows = data.models[type] || []
              if (rows.length === 0) return []
              return [
                <tr key={`hdr-${type}`} className="bg-gray-50 dark:bg-gray-700/40">
                  <td colSpan={6} className="py-1.5 px-3 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                    {MODEL_TYPE_LABELS[type]}
                  </td>
                </tr>,
                ...rows.map(m => {
                  const test = tests[m.name]
                  const isBuiltin = m.source === 'default'
                  return (
                    <tr key={m.name} className="border-b border-gray-100 dark:border-gray-700">
                      <td className="py-2 pr-3">
                        <span className="font-medium text-gray-900 dark:text-gray-100">{m.name}</span>
                        {m.is_default && (
                          <span className="ml-2 px-1.5 py-0.5 text-[10px] rounded bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300">default</span>
                        )}
                      </td>
                      <td className="py-2 px-3 text-gray-600 dark:text-gray-400">{m.model_provider}</td>
                      <td className="py-2 px-3 text-gray-600 dark:text-gray-400 truncate max-w-[180px]" title={m.model_url || m.model_name}>{m.model_name}</td>
                      <td className="py-2 px-3 text-gray-600 dark:text-gray-400">{keyState(m)}</td>
                      <td className="py-2 px-3">
                        <span className={`px-1.5 py-0.5 text-[10px] rounded ${isBuiltin ? 'bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300' : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'}`}>
                          {isBuiltin ? 'built-in' : 'config'}
                        </span>
                      </td>
                      <td className="py-2 pl-3">
                        <div className="flex items-center justify-end gap-1">
                          {(m.model_type === 'llm' || m.model_type === 'embedding') && (
                            <button
                              onClick={() => handleTest(m)}
                              disabled={test?.loading}
                              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                              title={test?.error || 'Test connection'}
                            >
                              {test?.loading ? <Loader2 className="h-3 w-3 animate-spin" />
                                : test?.success === true ? <Check className="h-3 w-3 text-green-500" />
                                : test?.success === false ? <X className="h-3 w-3 text-red-500" />
                                : <Play className="h-3 w-3" />}
                              {test?.latency ? `${test.latency}ms` : 'Test'}
                            </button>
                          )}
                          <IconButton label="Edit model" onClick={() => openEdit(m)}>
                            <Pencil className="h-3.5 w-3.5" />
                          </IconButton>
                          <IconButton
                            label={m.is_default ? 'Active default — repoint first' : isBuiltin ? 'Built-in template (defaults.yml)' : 'Delete model'}
                            danger
                            onClick={() => handleDelete(m)}
                            disabled={m.is_default || isBuiltin}
                          >
                            <Trash2 className="h-3.5 w-3.5 text-red-600" />
                          </IconButton>
                        </div>
                      </td>
                    </tr>
                  )
                }),
              ]
            })}
          </tbody>
        </table>
      </div>

      {form && (
        <ModelEditModal
          form={form}
          setForm={setForm}
          isNew={isNew}
          saving={saving}
          error={error}
          onCancel={() => setForm(null)}
          onSubmit={handleSubmit}
        />
      )}
    </div>
  )
}

function ModelEditModal({
  form, setForm, isNew, saving, error, onCancel, onSubmit,
}: {
  form: ModelForm
  setForm: (f: ModelForm) => void
  isNew: boolean
  saving: boolean
  error: string
  onCancel: () => void
  onSubmit: () => void
}) {
  const set = (field: keyof ModelForm, value: string) => setForm({ ...form, [field]: value } as ModelForm)
  const label = 'block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1'

  return (
    <Modal
      open
      onClose={onCancel}
      title={isNew ? 'Add Model' : `Edit ${form.name}`}
      maxWidthClassName="max-w-lg"
      className="max-h-[90vh] overflow-y-auto"
      closeOnEscape={false}
      footer={
        <>
          <Button variant="secondary" size="md" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="primary" size="md" onClick={onSubmit} disabled={saving}>
            {saving ? 'Saving…' : 'Save Model'}
          </Button>
        </>
      }
    >
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={label}>Name {isNew && <span className="text-red-500">*</span>}</label>
            <Input value={form.name} onChange={e => set('name', e.target.value)} disabled={!isNew} placeholder="e.g. openai-llm" />
          </div>
          <div>
            <label className={label}>Type</label>
            <Select value={form.model_type} onChange={e => set('model_type', e.target.value)} disabled={!isNew}>
              {MODEL_TYPE_ORDER.map(t => <option key={t} value={t}>{MODEL_TYPE_LABELS[t]}</option>)}
            </Select>
          </div>
          <div>
            <label className={label}>Provider</label>
            <Input value={form.model_provider} onChange={e => set('model_provider', e.target.value)} placeholder="openai, ollama, deepgram…" />
          </div>
          <div>
            <label className={label}>API family</label>
            <Input value={form.api_family} onChange={e => set('api_family', e.target.value)} placeholder="openai, http, websocket" />
          </div>
          <div>
            <label className={label}>Model name</label>
            <Input value={form.model_name} onChange={e => set('model_name', e.target.value)} placeholder="provider-specific id" />
          </div>
          <div>
            <label className={label}>Embedding dims</label>
            <Input value={form.embedding_dimensions} onChange={e => set('embedding_dimensions', e.target.value)} placeholder="e.g. 1536" />
          </div>
          <div>
            <label className={label}>Managed deployment (optional)</label>
            <Input value={form.deployment} onChange={e => set('deployment', e.target.value)} placeholder="e.g. llm-services" />
          </div>
          <div>
            <label className={label}>Deployment endpoint</label>
            <Input value={form.deployment_endpoint} onChange={e => set('deployment_endpoint', e.target.value)} placeholder="e.g. chat or embeddings" disabled={!form.deployment} />
          </div>
          <div className="col-span-2">
            <label className={label}>Base URL</label>
            <Input disabled={!!form.deployment} value={form.model_url} onChange={e => set('model_url', e.target.value)} placeholder="https://api.openai.com/v1 (blank = Tailnet discovery)" />
          </div>
          <div className="col-span-2">
            <label className={label}>API key</label>
            <Input
              type={form.api_key === API_KEY_MASK ? 'text' : 'password'}
              value={form.api_key}
              onChange={e => set('api_key', e.target.value)}
              placeholder="inline key or ${oc.env:OPENAI_API_KEY}"
            />
            <p className="text-[10px] text-gray-400 mt-1">
              Leave the dots ({API_KEY_MASK}) to keep the stored secret. Clear to remove. Prefer <code>{'${oc.env:VAR}'}</code> for shared keys.
            </p>
          </div>
          <div className="col-span-2">
            <label className={label}>Capabilities (comma-separated)</label>
            <Input value={form.capabilities} onChange={e => set('capabilities', e.target.value)} placeholder="word_timestamps, segments, keyword_boosting…" />
          </div>
          <div className="col-span-2">
            <label className={label}>Description</label>
            <Input value={form.description} onChange={e => set('description', e.target.value)} />
          </div>
          <div className="col-span-2">
            <label className={label}>Model params (JSON)</label>
            <Textarea className="font-mono text-xs" rows={3} value={form.model_params} onChange={e => set('model_params', e.target.value)} placeholder='{"temperature": 0.2, "max_tokens": 2000}' />
          </div>
        </div>

        {error && (
          <Alert tone="danger" className="mt-3">
            {error}
          </Alert>
        )}
    </Modal>
  )
}
