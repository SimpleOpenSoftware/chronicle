import { useState, useEffect, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import SourceSearch from '../components/SourceSearch'
import { useAuth } from '../contexts/AuthContext'
import { sourceDate } from '../utils/sourceTime'
import { MessageSquare, RefreshCw, Calendar, User, Play, Pause, MoreVertical, RotateCcw, Zap, ChevronDown, ChevronUp, ChevronLeft, ChevronRight, Trash2, Save, X, AlertTriangle, Pencil, Star, Clock, Mic } from 'lucide-react'
import { conversationsApi, annotationsApi, speakerApi } from '../services/api'
import { useConversations, useDeleteConversation, useReprocessTranscript, useReprocessMemory, useReprocessSpeakers, useReprocessOrphan, useToggleStar } from '../hooks/useConversations'
import ConversationVersionHeader from '../components/ConversationVersionHeader'
import { PlayheadTimeLabel } from '../components/audio/PlayheadWaveform'
import { useGaplessPlayer } from '../hooks/useGaplessPlayer'
import TranscriptEditor from '../components/transcript/TranscriptEditor'
import { Button, Checkbox, IconButton } from '../components/ui'
import { TITLE_NOT_GENERATED } from '../lib/constants'

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  detailed_summary?: string
  created_at?: string
  client_id: string
  segment_count?: number  // From list endpoint
  speakers?: string[]  // Unique speakers of the active version (from list endpoint, at a glance)
  audio_chunks_count?: number  // Number of MongoDB audio chunks
  audio_total_duration?: number  // Total duration in seconds
  duration_seconds?: number
  transcript?: string  // From detail endpoint
  segments?: Array<{
    text: string
    speaker: string
    segment_type?: string  // "speech" | "event" | "note"
    start: number
    end: number
    confidence?: number
  }>  // From detail endpoint (loaded on expand)
  active_transcript_version?: string
  transcript_version_count?: number
  active_transcript_version_number?: number
  deleted?: boolean
  deletion_reason?: string
  deleted_at?: string
  origin?: 'deliberate' | 'detected'
  processing_status?: string
  failure_stage?: string
  is_orphan?: boolean
  starred?: boolean
  starred_at?: string
}


// Unknown and background labels are metadata, not enrolled people.
const isUnknownLabel = (name?: string): boolean => {
  if (!name || !name.trim()) return true
  const n = name.trim().toLowerCase()
  return ['noise', 'background speech'].includes(n) || /^(?:unknown(?:[ _]speaker)?|speaker)(?:[ _]*\d+)?$/.test(n)
}

const PAGE_SIZE = 20

const SORT_OPTIONS = [
  { label: 'Date (newest)', sortBy: 'created_at', sortOrder: 'desc' },
  { label: 'Date (oldest)', sortBy: 'created_at', sortOrder: 'asc' },
  { label: 'Duration (longest)', sortBy: 'audio_total_duration', sortOrder: 'desc' },
  { label: 'Title (A-Z)', sortBy: 'title', sortOrder: 'asc' },
] as const

export default function Recordings() {
  const queryClient = useQueryClient()
  const { isAdmin } = useAuth()
  const [debugMode, setDebugMode] = useState(false)
  const [starredOnly, setStarredOnly] = useState(false)
  const [hideUnknownSpeakers, setHideUnknownSpeakers] = useState(false)
  const [sortIdx, setSortIdx] = useState(0)
  const [page, setPage] = useState(0)

  const sortOption = SORT_OPTIONS[sortIdx]

  const {
    data: conversationsData,
    isLoading: loading,
    error: queryError,
    refetch,
  } = useConversations({
    includeUnprocessed: debugMode || undefined,
    starredOnly: starredOnly || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    sortBy: sortOption.sortBy,
    sortOrder: sortOption.sortOrder,
  })

  const conversations: Conversation[] = conversationsData?.conversations ?? []
  const totalConversations: number = conversationsData?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalConversations / PAGE_SIZE))

  // Stable query key matching what useConversations uses, for setQueryData calls
  const conversationsQueryKey = useMemo(() => ['conversations', {
    includeUnprocessed: debugMode || undefined,
    starredOnly: starredOnly || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    sortBy: sortOption.sortBy,
    sortOrder: sortOption.sortOrder,
  }], [debugMode, starredOnly, page, sortOption])
  const [actionError, setActionError] = useState<string | null>(null)
  const error = queryError?.message ?? actionError ?? null

  // Transcript expand/collapse state
  const [expandedTranscripts, setExpandedTranscripts] = useState<Set<string>>(new Set())
  // Detailed summary expand/collapse state
  const [expandedDetailedSummaries, setExpandedDetailedSummaries] = useState<Set<string>>(new Set())
  // Audio playback is owned by the app-wide gapless scheduler (Web Audio).
  // Only one conversation plays at a time across the whole list.
  const player = useGaplessPlayer()

  // Reprocessing state
  const [openDropdown, setOpenDropdown] = useState<string | null>(null)
  const [reprocessingTranscript, setReprocessingTranscript] = useState<Set<string>>(new Set())
  const [reprocessingMemory, setReprocessingMemory] = useState<Set<string>>(new Set())
  const [reprocessingSpeakers, setReprocessingSpeakers] = useState<Set<string>>(new Set())
  const [reprocessingOrphan, setReprocessingOrphan] = useState<Set<string>>(new Set())
  const [deletingConversation, setDeletingConversation] = useState<Set<string>>(new Set())

  // Enrolled speakers (passed to the shared transcript editor)
  const [enrolledSpeakers, setEnrolledSpeakers] = useState<Array<{speaker_id: string, name: string}>>([])

  // Title editing state
  const [editingTitle, setEditingTitle] = useState<string | null>(null) // conversationId being edited
  const [editedTitle, setEditedTitle] = useState<string>('')
  const [savingTitle, setSavingTitle] = useState<boolean>(false)
  const [titleEditError, setTitleEditError] = useState<string | null>(null)

  const [searchParams] = useSearchParams()
  const searchActive = !!searchParams.get('q')?.trim()

  const loadEnrolledSpeakers = async () => {
    try {
      const response = await speakerApi.getEnrolledSpeakers()
      setEnrolledSpeakers(response.data.speakers || [])
    } catch (err: any) {
      console.error('Failed to load enrolled speakers:', err)
    }
  }


  useEffect(() => {
    loadEnrolledSpeakers()
  }, [])

  // Refetch conversations when debug mode toggles (to include/exclude orphans)
  useEffect(() => {
    refetch()
  }, [debugMode])

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = () => setOpenDropdown(null)
    const handleKey = (event: KeyboardEvent) => { if (event.key === 'Escape') { setOpenDropdown(null); (document.activeElement?.closest('.relative')?.querySelector('button') as HTMLElement | null)?.focus() } }
    document.addEventListener('keydown', handleKey)
    document.addEventListener('click', handleClickOutside)
    return () => { document.removeEventListener('click', handleClickOutside); document.removeEventListener('keydown', handleKey) }
  }, [])

  const formatDate = (timestamp: number | string) => {
    if (!timestamp) return 'Date unknown'
    return sourceDate(typeof timestamp === 'number' ? new Date(timestamp * 1000).toISOString() : timestamp)
      .toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }) + ' IST'
  }



  const reprocessTranscriptMutation = useReprocessTranscript()

  const handleReprocessTranscript = async (conversation: Conversation) => {
    if (!conversation.conversation_id) {
      setActionError('Cannot reprocess transcript: Conversation ID is missing. This conversation may be from an older format.')
      return
    }

    setReprocessingTranscript(prev => new Set(prev).add(conversation.conversation_id!))
    setOpenDropdown(null)

    try {
      await reprocessTranscriptMutation.mutateAsync(conversation.conversation_id)
    } catch (err: any) {
      setActionError(`Error starting transcript reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingTranscript(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const reprocessMemoryMutation = useReprocessMemory()

  const handleReprocessMemory = async (conversation: Conversation, transcriptVersionId?: string) => {
    if (!conversation.conversation_id) {
      setActionError('Cannot reprocess memory: Conversation ID is missing. This conversation may be from an older format.')
      return
    }

    setReprocessingMemory(prev => new Set(prev).add(conversation.conversation_id!))
    setOpenDropdown(null)

    try {
      await reprocessMemoryMutation.mutateAsync({
        conversationId: conversation.conversation_id,
        transcriptVersionId: transcriptVersionId,
      })
    } catch (err: any) {
      setActionError(`Error starting memory reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingMemory(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const reprocessSpeakersMutation = useReprocessSpeakers()

  const handleReprocessSpeakers = async (conversation: Conversation) => {
    if (!conversation.conversation_id) {
      setActionError('Cannot reprocess speakers: Conversation ID is missing. This conversation may be from an older format.')
      return
    }

    setReprocessingSpeakers(prev => new Set(prev).add(conversation.conversation_id!))
    setOpenDropdown(null)

    try {
      await reprocessSpeakersMutation.mutateAsync({
        conversationId: conversation.conversation_id,
        transcriptVersionId: 'active',
      })
    } catch (err: any) {
      setActionError(`Error starting speaker reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingSpeakers(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const reprocessOrphanMutation = useReprocessOrphan()

  const handleReprocessOrphan = async (conversation: Conversation) => {
    if (!conversation.conversation_id) return

    setReprocessingOrphan(prev => new Set(prev).add(conversation.conversation_id!))


    try {
      await reprocessOrphanMutation.mutateAsync(conversation.conversation_id)
    } catch (err: any) {
      setActionError(`Error starting orphan reprocessing: ${err.message || 'Unknown error'}`)
    } finally {
      setReprocessingOrphan(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversation.conversation_id!)
        return newSet
      })
    }
  }

  const deleteConversationMutation = useDeleteConversation()
  const toggleStarMutation = useToggleStar()

  const handleToggleStar = async (conversationId: string) => {
    try {
      await toggleStarMutation.mutateAsync(conversationId)
    } catch (err: any) {
      setActionError(err?.response?.data?.error || 'Failed to toggle star')
    }
  }

  const handleDeleteConversation = async (conversationId: string) => {
    const confirmed = window.confirm('Are you sure you want to delete this conversation? This action cannot be undone.')
    if (!confirmed) return

    setDeletingConversation(prev => new Set(prev).add(conversationId))
    setOpenDropdown(null)

    try {
      await deleteConversationMutation.mutateAsync(conversationId)
    } catch (err: any) {
      setActionError(`Error deleting conversation: ${err.message || 'Unknown error'}`)
    } finally {
      setDeletingConversation(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }


  // Title editing handlers
  const handleStartTitleEdit = (conversationId: string, currentTitle: string) => {
    setEditingTitle(conversationId)
    setEditedTitle(currentTitle)
    setTitleEditError(null)
  }

  const handleSaveTitleEdit = async (conversationId: string, originalTitle: string) => {
    if (!editedTitle.trim()) {
      setTitleEditError('Title cannot be empty')
      return
    }

    if (editedTitle === originalTitle) {
      handleCancelTitleEdit()
      return
    }

    try {
      setSavingTitle(true)
      setTitleEditError(null)

      await annotationsApi.createTitleAnnotation({
        conversation_id: conversationId,
        original_text: originalTitle,
        corrected_text: editedTitle.trim(),
      })

      // Optimistically update the title in local state
      queryClient.setQueryData(conversationsQueryKey, (old: any) => {
        if (!old) return old
        return {
          ...old,
          conversations: old.conversations.map((c: Conversation) =>
            c.conversation_id === conversationId
              ? { ...c, title: editedTitle.trim() }
              : c
          ),
        }
      })

      setEditingTitle(null)
      setEditedTitle('')
    } catch (err: any) {
      console.error('Error saving title edit:', err)
      setTitleEditError(err.response?.data?.detail || err.message || 'Failed to save title')
    } finally {
      setSavingTitle(false)
    }
  }

  const handleCancelTitleEdit = () => {
    setEditingTitle(null)
    setEditedTitle('')
    setTitleEditError(null)
  }

  const handleTitleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>, conversationId: string, originalTitle: string) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleSaveTitleEdit(conversationId, originalTitle)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      handleCancelTitleEdit()
    }
  }

  const toggleDetailedSummary = async (conversationId: string) => {
    // If already expanded, just collapse
    if (expandedDetailedSummaries.has(conversationId)) {
      setExpandedDetailedSummaries(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
      return
    }

    // Find the conversation by conversation_id
    const conversation = conversations.find(
      c => c.conversation_id === conversationId,
    )
    if (!conversation || !conversation.conversation_id) {
      console.error('Cannot expand detailed summary: conversation_id missing')
      return
    }

    // Check if detailed_summary is already loaded
    if (conversation.detailed_summary) {
      setExpandedDetailedSummaries(prev => new Set(prev).add(conversationId))
      return
    }

    // Fetch full conversation details to get detailed_summary
    try {
      const response = await conversationsApi.getById(conversation.conversation_id)
      if (response.status === 200 && response.data.conversation) {
        // Update the conversation in query cache with detailed_summary
        queryClient.setQueryData(conversationsQueryKey, (old: any) => {
          if (!old) return old
          return {
            ...old,
            conversations: old.conversations.map((c: Conversation) =>
              c.conversation_id === conversationId
                ? { ...c, detailed_summary: response.data.conversation.detailed_summary }
                : c
            ),
          }
        })
        // Expand the detailed summary
        setExpandedDetailedSummaries(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch detailed summary:', err)
      setActionError(`Failed to load detailed summary: ${err.message || 'Unknown error'}`)
    }
  }

  // Re-fetch a single conversation's full detail (segments) into the list cache — used
  // after the shared editor applies corrections (which create a new transcript version).
  const refreshConversationDetail = async (conversationId: string) => {
    try {
      const response = await conversationsApi.getById(conversationId)
      if (response.status === 200 && response.data.conversation) {
        queryClient.setQueryData(conversationsQueryKey, (old: any) => {
          if (!old) return old
          return {
            ...old,
            conversations: old.conversations.map((c: Conversation) =>
              c.conversation_id === conversationId ? { ...c, ...response.data.conversation } : c
            ),
          }
        })
      }
    } catch (err: any) {
      setActionError(`Failed to refresh conversation: ${err.message || 'Unknown error'}`)
    }
  }

  const toggleTranscriptExpansion = async (conversationId: string) => {
    // If already expanded, just collapse
    if (expandedTranscripts.has(conversationId)) {
      setExpandedTranscripts(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
      return
    }

    // Find the conversation by conversation_id
    const conversation = conversations.find(c => c.conversation_id === conversationId)
    if (!conversation || !conversation.conversation_id) {
      console.error('Cannot expand transcript: conversation_id missing')
      return
    }

    // If segments are already loaded, just expand
    if (conversation.segments && conversation.segments.length > 0) {
      setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      return
    }

    // Fetch full conversation details including segments
    try {
      const response = await conversationsApi.getById(conversation.conversation_id)
      if (response.status === 200 && response.data.conversation) {
        // Update the conversation in query cache with full data
        queryClient.setQueryData(conversationsQueryKey, (old: any) => {
          if (!old) return old
          return {
            ...old,
            conversations: old.conversations.map((c: Conversation) =>
              c.conversation_id === conversationId
                ? { ...c, ...response.data.conversation }
                : c
            ),
          }
        })
        // Expand the transcript (the editor loads its own annotations)
        setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch conversation details:', err)
      setActionError(`Failed to load transcript: ${err.message || 'Unknown error'}`)
    }
  }


  // Stop playback when leaving the list.
  useEffect(() => {
    return () => player.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])


  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600 dark:text-gray-400">Loading conversations...</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center">
        <div className="text-red-600 dark:text-red-400 mb-4">{error}</div>
        <Button variant="primary" size="md" onClick={() => { setActionError(null); refetch() }}>
          Try Again
        </Button>
      </div>
    )
  }

  return (
    <div>
      {/* Header with Search */}
      <div className="flex flex-col gap-4 mb-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center space-x-2">
            <MessageSquare className="h-6 w-6 text-blue-600 flex-shrink-0" />
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Recordings
            </h1>
          </div>
          {isAdmin && !searchActive && <details className="relative text-sm text-[var(--tape-activity)]">
            <summary className="min-h-11 cursor-pointer rounded-md px-3 py-3">Diagnostics{debugMode ? ' · On' : ''}</summary>
            <div className="absolute right-0 z-20 w-64 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-4">
              <Checkbox checked={debugMode} onChange={e => { setDebugMode(e.target.checked); setPage(0) }} label="Show unprocessed audio and details" />
            </div>
          </details>}
        </div>

        <SourceSearch
          activeFilterCount={!searchActive && starredOnly ? 1 : 0}
          browseActions={!searchActive && <>
            <select aria-label="Sort recordings" value={sortIdx} onChange={e => { setSortIdx(Number(e.target.value)); setPage(0) }} className="h-11 max-w-full rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 text-sm text-[var(--tape-ink)]">{SORT_OPTIONS.map((option, i) => <option key={option.label} value={i}>{option.label}</option>)}</select>
            <IconButton label="Refresh recordings" onClick={() => refetch()}><RefreshCw className="h-4 w-4" /></IconButton>
          </>}
          browseFilters={!searchActive && <div className="mt-3 border-t border-[var(--tape-line)] pt-3">
            <Checkbox checked={starredOnly} onChange={e => { setStarredOnly(e.target.checked); setPage(0) }} label="Starred recordings only" />
          </div>}
        />
        {!searchActive && starredOnly && <button className="self-start flex min-h-11 items-center gap-2 rounded-full bg-[var(--tape-chip)] px-3 text-sm text-[var(--tape-ink)]" onClick={() => { setStarredOnly(false); setPage(0) }}>Starred recordings <X className="h-4 w-4" /><span className="sr-only">Clear filter</span></button>}
        {searchActive && starredOnly && <p className="text-sm text-[var(--tape-activity)]">The starred filter is paused during search.</p>}

      </div>

      {/* Conversations List */}
      <div className={searchActive ? 'hidden' : 'space-y-3'}>
        {(() => {
          const displayConversations = conversations
          return displayConversations.length === 0 ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-12">
            <MessageSquare className="h-12 w-12 mx-auto mb-4 opacity-50" />
            <p>No recordings found</p>
          </div>
        ) : (
          displayConversations.map((conversation) => (
            <div
              key={conversation.conversation_id}
              className={`rounded-lg p-4 border recording-card ${
                conversation.is_orphan
                  ? 'bg-amber-50 dark:bg-amber-900/10 border-amber-300 dark:border-amber-700'
                  : 'bg-[var(--tape-paper-raised)] border-[var(--tape-line)]'
              }`}
            >
              {/* Orphan Audio Session Banner */}
              {conversation.is_orphan && (
                <div className="mb-4 p-3 bg-amber-100 dark:bg-amber-900/30 rounded-lg border border-amber-200 dark:border-amber-800 flex items-center justify-between">
                  <div className="flex items-center space-x-2">
                    <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400 flex-shrink-0" />
                    <div>
                      <span className="text-sm font-medium text-amber-800 dark:text-amber-200">
                        Unprocessed Audio Session
                      </span>
                      <span className="text-xs text-amber-600 dark:text-amber-400 ml-2">
                        {conversation.processing_status === 'failed'
                          ? (conversation.failure_stage === 'summarization' ? 'Summary generation failed' : 'Transcription failed') :
                         conversation.processing_status === 'active' ? 'Processing…' :
                         conversation.deleted ? `Deleted: ${conversation.deletion_reason}` :
                         conversation.processing_status || 'Pending'}
                        {conversation.audio_total_duration ? ` · ${Math.floor(conversation.audio_total_duration / 60)}:${Math.floor(conversation.audio_total_duration % 60).toString().padStart(2, '0')} audio` : ''}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => handleReprocessOrphan(conversation)}
                    disabled={reprocessingOrphan.has(conversation.conversation_id)}
                    className="flex items-center space-x-1 px-3 py-1.5 text-sm font-medium text-amber-700 dark:text-amber-300 bg-white dark:bg-transparent border border-amber-300 dark:border-amber-700 hover:bg-amber-50 dark:hover:bg-amber-900/20 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {reprocessingOrphan.has(conversation.conversation_id) ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RotateCcw className="h-3.5 w-3.5" />
                    )}
                    <span>{reprocessingOrphan.has(conversation.conversation_id) ? 'Reprocessing...' : 'Reprocess'}</span>
                  </button>
                </div>
              )}

              {/* Conversation Header */}
              <div className="flex justify-between items-start mb-2 gap-2">
                <div className="flex flex-col gap-1 min-w-0">
                  {/* Conversation Title - Editable */}
                  {editingTitle === conversation.conversation_id ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        type="text"
                        value={editedTitle}
                        onChange={(e) => setEditedTitle(e.target.value)}
                        onKeyDown={(e) => handleTitleKeyDown(e, conversation.conversation_id, conversation.title || TITLE_NOT_GENERATED)}
                        aria-label="Recording title" className="w-full min-w-0 text-lg font-semibold px-2 py-1 border border-blue-500 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                        autoFocus
                        disabled={savingTitle}
                      />
                      <button
                        onClick={() => handleSaveTitleEdit(conversation.conversation_id, conversation.title || TITLE_NOT_GENERATED)}
                        disabled={savingTitle || editedTitle === (conversation.title || TITLE_NOT_GENERATED)}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        <Save className="w-3 h-3" />
                        {savingTitle ? 'Saving...' : 'Save'}
                      </button>
                      <button
                        onClick={handleCancelTitleEdit}
                        disabled={savingTitle}
                        className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-200 dark:bg-gray-600 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        <X className="w-3 h-3" />
                        Cancel
                      </button>
                      {titleEditError && (
                        <span className="text-xs text-red-600 dark:text-red-400">{titleEditError}</span>
                      )}
                    </div>
                  ) : (
                    <h2 className="text-lg font-semibold leading-snug text-[var(--tape-ink)] break-words">
                      <Link className="rounded hover:underline underline-offset-4" to={`/recordings/${conversation.conversation_id}`} state={{ from: '/recordings' + (searchParams.size ? '?' + searchParams.toString() : '') }}>
                        {conversation.title || TITLE_NOT_GENERATED}
                      </Link>
                    </h2>
                  )}

                  {(conversation.processing_status === 'failed' || conversation.processing_status === 'active') && <div className="flex flex-wrap gap-2 text-xs text-[var(--tape-activity)]">
                    {conversation.processing_status === 'failed' ? <span className="rounded bg-red-100 px-2 py-1 text-red-800 dark:bg-red-900/30 dark:text-red-300">{conversation.failure_stage === 'summarization' ? 'Summary failed' : 'Processing failed'}</span>
                      : conversation.processing_status === 'active' ? <span className="rounded bg-[var(--tape-chip)] px-2 py-1">Processing</span>
                      : null}
                  </div>}
                  {/* Short Summary - Always visible */}
                  {conversation.summary && !/^Transcribing detected speech\.{0,3}$/.test(conversation.summary) && (
                    <p className="text-sm leading-relaxed text-[var(--tape-activity)] line-clamp-2">
                      {conversation.summary}
                    </p>
                  )}

                  {/* Detailed Summary Expand Button */}
                  {conversation.conversation_id && conversation.detailed_summary && (
                    <div>
                      <button
                        aria-expanded={expandedDetailedSummaries.has(conversation.conversation_id)}
                        onClick={() => toggleDetailedSummary(conversation.conversation_id!)}
                        className="min-h-11 text-sm text-[var(--tape-activity)] hover:underline flex items-center gap-2"
                      >
                        <span>
                          {expandedDetailedSummaries.has(conversation.conversation_id) ? '▼' : '▶'} Detailed Summary
                        </span>
                      </button>

                      {/* Detailed Summary Content */}
                      {expandedDetailedSummaries.has(conversation.conversation_id) && conversation.detailed_summary && (
                        <div className="mt-2 p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg border border-gray-200 dark:border-gray-700 animate-in slide-in-from-top-2 duration-200">
                          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                            {conversation.detailed_summary}
                          </p>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Metadata */}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                    {debugMode && <ConversationVersionHeader
                      conversationId={conversation.conversation_id}
                      versionInfo={{
                        transcript_count: conversation.transcript_version_count || 0,
                        active_transcript_version: conversation.active_transcript_version,
                        active_transcript_version_number: conversation.active_transcript_version_number,
                      }}
                      onVersionChange={async () => {
                        try {
                          const response = await conversationsApi.getById(conversation.conversation_id!)
                          if (response.status === 200 && response.data.conversation) {
                            queryClient.setQueryData(conversationsQueryKey, (old: any) => {
                              if (!old) return old
                              return {
                                ...old,
                                conversations: old.conversations.map((c: Conversation) =>
                                  c.conversation_id === conversation.conversation_id
                                    ? { ...c, ...response.data.conversation }
                                    : c
                                ),
                              }
                            })
                          }
                        } catch (err: any) {
                          console.error('Failed to refresh conversation:', err)
                          refetch()
                        }
                      }}
                    />}
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                      <Calendar className="h-4 w-4 flex-shrink-0" />
                      <span>{formatDate(conversation.created_at || '')}</span>
                    </div>
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400 min-w-0">
                      <User className="h-4 w-4 flex-shrink-0" />
                      <span className="truncate">{conversation.client_id.replace(/^[^-]+-/, '').replace(/[-_]/g, ' ')}</span>
                    </div>
                    {/* Play pill inline (doubles as the duration readout) when there's audio;
                        otherwise a static duration. */}
                    {(conversation.audio_chunks_count && conversation.audio_chunks_count > 0) ? (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          player.togglePlay(conversation.conversation_id!, conversation.audio_total_duration || 0)
                        }}
                        className="inline-flex min-h-11 items-center gap-2 px-3 rounded-md text-[var(--tape-ink)] bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                        aria-label={player.isActive(conversation.conversation_id) && player.isPlaying ? 'Pause recording' : 'Play recording'}
                      >
                        {player.isActive(conversation.conversation_id) && player.isPlaying
                          ? <Pause className="h-3.5 w-3.5 text-blue-600" />
                          : <Play className="h-3.5 w-3.5 text-blue-600" />}
                        <span className="text-sm">{player.isActive(conversation.conversation_id) && player.isPlaying ? 'Pause' : 'Play'}</span>
                        {player.isActive(conversation.conversation_id) ? (
                          <PlayheadTimeLabel
                            cid={conversation.conversation_id}
                            total={conversation.audio_total_duration}
                            className="text-xs font-mono tabular-nums text-gray-600 dark:text-gray-300"
                          />
                        ) : (
                          <span className="text-xs font-mono tabular-nums text-gray-600 dark:text-gray-300">
                            {conversation.audio_total_duration
                              ? `${Math.floor(conversation.audio_total_duration / 60)}:${Math.floor(conversation.audio_total_duration % 60).toString().padStart(2, '0')}`
                              : 'Audio'}
                          </span>
                        )}
                      </button>
                    ) : (() => {
                      const dur = conversation.duration_seconds || conversation.audio_total_duration
                      return dur && dur > 0 ? (
                        <div className="flex items-center space-x-1 text-sm text-gray-600 dark:text-gray-400">
                          <Clock className="h-3.5 w-3.5" />
                          <span>{Math.floor(dur / 60)}:{Math.floor(dur % 60).toString().padStart(2, '0')}</span>
                        </div>
                      ) : null
                    })()}
                  </div>

                  {/* Speakers at a glance (active version) */}
                  {conversation.speakers && conversation.speakers.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5 mt-1">
                      <Mic className="h-3.5 w-3.5 text-gray-400 flex-shrink-0" />
                      {conversation.speakers.map((sp, i) => (
                        <span
                          key={i}
                          className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                            isUnknownLabel(sp)
                              ? 'bg-gray-100 dark:bg-gray-700/60 text-[var(--tape-activity)]'
                              : 'bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-200'
                          }`}
                        >
                          {sp}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {/* Star + Hamburger Menu */}
                <div className="flex items-center space-x-1">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      handleToggleStar(conversation.conversation_id)
                    }}
                    className="min-h-11 min-w-11 inline-flex items-center justify-center rounded-md hover:bg-yellow-100 dark:hover:bg-yellow-900/30 transition-colors"
                    aria-label={conversation.starred ? 'Unstar recording' : 'Star recording'} aria-pressed={!!conversation.starred}
                  >
                    <Star className={`h-5 w-5 ${conversation.starred ? 'fill-yellow-400 text-yellow-400' : 'text-[var(--tape-activity)]'}`} />
                  </button>
                <div className="relative">
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      setOpenDropdown(openDropdown === conversation.conversation_id ? null : conversation.conversation_id)
                    }}
                    className="min-h-11 min-w-11 inline-flex items-center justify-center rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                    aria-label="Recording options" aria-expanded={openDropdown === conversation.conversation_id}
                  >
                    <MoreVertical className="h-5 w-5 text-gray-500 dark:text-gray-400" />
                  </button>

                  {/* Dropdown Menu */}
                  {openDropdown === conversation.conversation_id && (
                    <div className="absolute right-0 top-12 w-56 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-600 py-2 z-10">
                      <button onClick={() => { handleStartTitleEdit(conversation.conversation_id, conversation.title || TITLE_NOT_GENERATED); setOpenDropdown(null) }} className="min-h-11 w-full flex items-center gap-2 px-4 text-left text-sm text-[var(--tape-ink)] hover:bg-[var(--tape-chip)]"><Pencil className="h-4 w-4" />Rename</button>
                      <button
                        onClick={() => handleReprocessTranscript(conversation)}
                        disabled={!conversation.conversation_id || reprocessingTranscript.has(conversation.conversation_id)}
                        className="min-h-11 w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {conversation.conversation_id && reprocessingTranscript.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="h-4 w-4" />
                        )}
                        <span>Reprocess Transcript</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                      <button
                        onClick={() => handleReprocessMemory(conversation)}
                        disabled={!conversation.conversation_id || reprocessingMemory.has(conversation.conversation_id)}
                        className="min-h-11 w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {conversation.conversation_id && reprocessingMemory.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <Zap className="h-4 w-4" />
                        )}
                        <span>Reprocess Memory</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                      <button
                        onClick={() => handleReprocessSpeakers(conversation)}
                        disabled={!conversation.conversation_id || reprocessingSpeakers.has(conversation.conversation_id)}
                        className="min-h-11 w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Create new transcript version with re-identified speakers (automatically updates memories)"
                      >
                        {conversation.conversation_id && reprocessingSpeakers.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <User className="h-4 w-4" />
                        )}
                        <span>Reprocess Who Spoke</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                      <div className="border-t border-gray-200 dark:border-gray-600 my-1"></div>
                      <button
                        onClick={() => conversation.conversation_id && handleDeleteConversation(conversation.conversation_id)}
                        disabled={!conversation.conversation_id || (!!conversation.conversation_id && deletingConversation.has(conversation.conversation_id))}
                        className="min-h-11 w-full text-left px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {conversation.conversation_id && deletingConversation.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <Trash2 className="h-4 w-4" />
                        )}
                        <span>Move to Trash</span>
                        {!conversation.conversation_id && (
                          <span className="text-xs text-red-500 ml-1">(ID missing)</span>
                        )}
                      </button>
                    </div>
                  )}
                </div>
                </div>
              </div>

              {/* Transcript */}
              <div className="space-y-2" onClick={(event) => event.stopPropagation()}>
                {(() => {
                  // Get segments directly from conversation (returned by detail endpoint)
                  const segments = conversation.segments || []

                  return (
                    <>
                      {/* Transcript Header with Expand/Collapse */}
                      <button
                        type="button"
                        aria-expanded={expandedTranscripts.has(conversation.conversation_id)} aria-controls={`transcript-${conversation.conversation_id}`} className="flex min-h-11 w-full items-center justify-between gap-2 border-t border-[var(--tape-line)] px-2 py-2 rounded-md text-left hover:bg-[var(--tape-chip)] transition-colors"
                        onClick={() => conversation.conversation_id && toggleTranscriptExpansion(conversation.conversation_id)}
                      >
                        <span className="font-medium text-gray-900 dark:text-gray-100">
                          {expandedTranscripts.has(conversation.conversation_id) ? 'Hide transcript' : 'Show transcript'} {(segments.length > 0 || conversation.segment_count) && (
                            <span className="text-sm text-gray-500 dark:text-gray-400 ml-1">
                              ({segments.length || conversation.segment_count || 0} segments)
                            </span>
                          )}
                        </span>
                        <div className="flex items-center space-x-2">
                          {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) ? (
                            <ChevronUp className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          ) : (
                            <ChevronDown className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          )}
                        </div>
                      </button>

                      {/* Transcript Content - Conditionally Rendered */}
                      {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) && (
                        <div id={`transcript-${conversation.conversation_id}`}>
                          <div className="px-2 py-3"><Checkbox checked={hideUnknownSpeakers} onChange={e => setHideUnknownSpeakers(e.target.checked)} label="Hide unknown speakers in transcripts" /></div>
                          <TranscriptEditor
                            conversationId={conversation.conversation_id}
                            segments={segments}
                            duration={conversation.audio_total_duration}
                            hasAudio={!!conversation.audio_chunks_count && conversation.audio_chunks_count > 0}
                            showWaveform
                            enrolledSpeakers={enrolledSpeakers}
                            hideUnknownSpeakers={hideUnknownSpeakers}
                            onChanged={() => conversation.conversation_id && refreshConversationDetail(conversation.conversation_id)}
                          />
                        </div>
                      )}
                    </>
                  )
                })()}
              </div>

              {/* Debug info */}
              {debugMode && (
                <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
                  <h4 className="font-medium text-gray-900 dark:text-gray-100 mb-2">Processing details:</h4>
                  <div className="text-xs text-gray-600 dark:text-gray-400 space-y-1">
                    <div>Conversation ID: {conversation.conversation_id || 'N/A'}</div>
                    <div>Transcript Version Count: {conversation.transcript_version_count || 0}</div>
                    <div>Segment Count: {conversation.segment_count || 0}</div>
                    <div>Client ID: {conversation.client_id}</div>
                  </div>

                  {/* Raw Segments JSON */}
                  {conversation.segments && conversation.segments.length > 0 && (
                    <details className="mt-3 p-2 bg-gray-100 dark:bg-gray-800 rounded text-xs">
                      <summary className="cursor-pointer font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100">
                        Raw Segments ({conversation.segments.length})
                      </summary>
                      <pre className="mt-2 overflow-auto max-h-96 whitespace-pre-wrap text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 p-2 rounded border border-gray-200 dark:border-gray-700">
                        {JSON.stringify(conversation.segments, null, 2)}
                      </pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          ))
        )
        })()}
      </div>

      {/* Pagination */}
      {!searchActive && totalPages > 1 && (
        <div className="flex items-center justify-between mt-6 px-2">
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {totalConversations} recording{totalConversations !== 1 ? 's' : ''} total
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </button>
            <span className="text-sm text-gray-600 dark:text-gray-400 px-2">
              Page {page + 1} of {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
