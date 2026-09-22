import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Archive as ArchiveIcon, RefreshCw, Calendar, User, RotateCcw, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import { conversationsApi, authApi } from '../services/api'
import { useConversations, useRestoreConversation, usePermanentDeleteConversation } from '../hooks/useConversations'
import { Button } from '../components/ui'
import { TITLE_NOT_GENERATED } from '../lib/constants'

interface Conversation {
  conversation_id: string
  title?: string
  summary?: string
  created_at?: string
  client_id: string
  segment_count?: number
  deleted?: boolean
  deletion_reason?: string
  deleted_at?: string
  audio_archived?: boolean
  archive_reason?: string
  transcript?: string
  segments?: Array<{
    text: string
    speaker: string
    start: number
    end: number
    confidence?: number
  }>
}

export default function Archive() {
  const queryClient = useQueryClient()
  const [expandedTranscripts, setExpandedTranscripts] = useState<Set<string>>(new Set())
  const [restoringConversation, setRestoringConversation] = useState<Set<string>>(new Set())
  const [deletingConversation, setDeletingConversation] = useState<Set<string>>(new Set())
  const [isAdmin, setIsAdmin] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const {
    data: conversationsData,
    isLoading: loading,
    error: queryError,
    refetch,
  } = useConversations({ includeDeleted: true })

  // Filter to show only deleted conversations
  const conversations = (conversationsData?.conversations ?? []).filter((conv: Conversation) => conv.deleted === true)
  const error = queryError?.message ?? actionError ?? null

  const checkAdminStatus = async () => {
    try {
      const response = await authApi.getMe()
      setIsAdmin(response.data.is_superuser || false)
    } catch {
      setIsAdmin(false)
    }
  }

  useEffect(() => {
    checkAdminStatus()
  }, [])

  const formatDate = (timestamp: number | string) => {
    if (typeof timestamp === 'string') {
      const isoString = timestamp.endsWith('Z') || timestamp.includes('+') || timestamp.includes('T') && timestamp.split('T')[1].includes('-')
        ? timestamp
        : timestamp + 'Z'
      return new Date(isoString).toLocaleString()
    }
    if (timestamp === 0) {
      return 'Unknown date'
    }
    return new Date(timestamp * 1000).toLocaleString()
  }

  const restoreConversationMutation = useRestoreConversation()

  const handleRestoreConversation = async (conversationId: string) => {
    setRestoringConversation(prev => new Set(prev).add(conversationId))

    try {
      await restoreConversationMutation.mutateAsync(conversationId)
    } catch (err: any) {
      setActionError(`Error restoring conversation: ${err.message || 'Unknown error'}`)
    } finally {
      setRestoringConversation(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  const permanentDeleteMutation = usePermanentDeleteConversation()

  const handlePermanentDelete = async (conversationId: string) => {
    const confirmed = window.confirm(
      'Are you sure you want to permanently delete this recording? This action cannot be undone.'
    )
    if (!confirmed) return

    setDeletingConversation(prev => new Set(prev).add(conversationId))

    try {
      await permanentDeleteMutation.mutateAsync(conversationId)
    } catch (err: any) {
      setActionError(`Error permanently deleting conversation: ${err.message || 'Unknown error'}`)
    } finally {
      setDeletingConversation(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
    }
  }

  const toggleTranscriptExpansion = async (conversationId: string) => {
    if (expandedTranscripts.has(conversationId)) {
      setExpandedTranscripts(prev => {
        const newSet = new Set(prev)
        newSet.delete(conversationId)
        return newSet
      })
      return
    }

    const conversation = conversations.find(c => c.conversation_id === conversationId)
    if (!conversation || !conversation.conversation_id) {
      return
    }

    if (conversation.segments && conversation.segments.length > 0) {
      setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      return
    }

    try {
      const response = await conversationsApi.getById(conversation.conversation_id)
      if (response.status === 200 && response.data.conversation) {
        queryClient.setQueryData(['conversations', { includeDeleted: true }], (old: any) => {
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
        setExpandedTranscripts(prev => new Set(prev).add(conversationId))
      }
    } catch (err: any) {
      console.error('Failed to fetch conversation details:', err)
      setActionError(`Failed to load transcript: ${err.message || 'Unknown error'}`)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        <span className="ml-2 text-gray-600 dark:text-gray-400">Loading Trash…</span>
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
      {/* Header */}
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center space-x-2">
          <ArchiveIcon className="h-6 w-6 text-orange-600" />
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100 sm:text-2xl">
            Trash
          </h1>
        </div>
        <Button
          variant="primary"
          size="md"
          onClick={() => refetch()}
          icon={<RefreshCw className="h-4 w-4" />}
          className="self-start sm:self-auto"
        >
          Refresh
        </Button>
      </div>

      {/* Archive Info */}
      <div className="mb-4 p-3 bg-orange-50 dark:bg-orange-900/20 rounded-lg border border-orange-300 dark:border-orange-700">
        <p className="text-sm text-orange-800 dark:text-orange-300">
          <strong>Trash:</strong> Deleted recordings are listed here. Restore them to Recordings or permanently delete them {isAdmin && '(admin only)'}.
        </p>
      </div>

      {/* Trash List */}
      <div className="space-y-6">
        {conversations.length === 0 ? (
          <div className="text-center text-gray-500 dark:text-gray-400 py-12">
            <ArchiveIcon className="h-12 w-12 mx-auto mb-4 opacity-50" />
            <p>Trash is empty</p>
          </div>
        ) : (
          conversations.map((conversation) => (
            <div
              key={conversation.conversation_id}
              className="rounded-lg border border-red-300 bg-red-50 p-4 dark:border-red-700 dark:bg-red-900/20 sm:p-6"
            >
              {/* Deleted Conversation Banner */}
              <div className="mb-4 p-3 bg-red-100 dark:bg-red-900/40 rounded-lg border border-red-300 dark:border-red-700">
                <div className="flex items-start space-x-2">
                  <ArchiveIcon className="h-5 w-5 text-red-600 dark:text-red-400 mt-0.5 flex-shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="font-semibold text-red-800 dark:text-red-300 text-sm">Deleted recording</p>
                    <p className="text-xs text-red-700 dark:text-red-400 mt-1">
                      Reason: {conversation.deletion_reason === 'user_deleted'
                        ? 'User deleted'
                        : conversation.deletion_reason === 'audio_archived'
                        ? `Audio archived${conversation.archive_reason ? ` (${conversation.archive_reason.replace(/_/g, ' ')})` : ''}`
                        : conversation.deletion_reason === 'no_meaningful_speech'
                        ? 'No meaningful speech detected'
                        : conversation.deletion_reason === 'audio_file_not_ready'
                        ? 'Audio file not saved (possible Bluetooth disconnect)'
                        : conversation.deletion_reason || 'Unknown'}
                    </p>
                    {conversation.audio_archived && (
                      <p className="text-xs text-red-700 dark:text-red-400 mt-1">
                        Audio bytes were permanently deleted to reclaim storage. Restoring brings back metadata only — the audio cannot be recovered.
                      </p>
                    )}
                    {conversation.deleted_at && (
                      <p className="text-xs text-red-600 dark:text-red-500 mt-1">
                        {conversation.audio_archived ? 'Archived at: ' : 'Deleted at: '}{formatDate(conversation.deleted_at)}
                      </p>
                    )}
                  </div>
                </div>
              </div>

              {/* Conversation Header */}
              <div className="mb-4 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="flex min-w-0 flex-col space-y-2">
                  <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
                    {conversation.title || TITLE_NOT_GENERATED}
                  </h2>

                  {conversation.summary && (
                    <p className="text-sm text-gray-600 dark:text-gray-400 italic">
                      {conversation.summary}
                    </p>
                  )}

                  {/* Metadata */}
                  <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                      <Calendar className="h-4 w-4" />
                      <span>{formatDate(conversation.created_at || '')}</span>
                    </div>
                    <div className="flex items-center space-x-2 text-sm text-gray-600 dark:text-gray-400">
                      <User className="h-4 w-4" />
                      <span>{conversation.client_id}</span>
                    </div>
                  </div>
                </div>

                {/* Action Buttons */}
                <div className="grid w-full grid-cols-1 gap-2 min-[380px]:grid-cols-2 sm:flex sm:w-auto sm:flex-shrink-0 sm:items-center">
                  {conversation.conversation_id && (
                    <>
                      <button
                        onClick={() => handleRestoreConversation(conversation.conversation_id!)}
                        disabled={restoringConversation.has(conversation.conversation_id)}
                        className="flex min-w-0 items-center justify-center space-x-2 rounded-lg bg-green-600 px-3 py-2 text-white transition-colors hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
                        title="Restore conversation to active view"
                      >
                        {restoringConversation.has(conversation.conversation_id) ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="h-4 w-4" />
                        )}
                        <span>Restore</span>
                      </button>

                      {isAdmin && (
                        <button
                          onClick={() => handlePermanentDelete(conversation.conversation_id!)}
                          disabled={deletingConversation.has(conversation.conversation_id)}
                          className="flex min-w-0 items-center justify-center space-x-2 rounded-lg bg-red-600 px-3 py-2 text-white transition-colors hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
                          title="Permanently delete (admin only)"
                        >
                          {deletingConversation.has(conversation.conversation_id) ? (
                            <RefreshCw className="h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="h-4 w-4" />
                          )}
                          <span>Permanent Delete</span>
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>

              {/* Transcript */}
              <div className="space-y-2">
                {(() => {
                  const segments = conversation.segments || []

                  return (
                    <>
                      {/* Transcript Header with Expand/Collapse */}
                      <div
                        className="flex items-center justify-between cursor-pointer p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors"
                        onClick={() => conversation.conversation_id && toggleTranscriptExpansion(conversation.conversation_id)}
                      >
                        <h3 className="font-medium text-gray-900 dark:text-gray-100">
                          Transcript {(segments.length > 0 || conversation.segment_count) && (
                            <span className="text-sm text-gray-500 dark:text-gray-400 ml-1">
                              ({segments.length || conversation.segment_count || 0} segments)
                            </span>
                          )}
                        </h3>
                        <div className="flex items-center space-x-2">
                          {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) ? (
                            <ChevronUp className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          ) : (
                            <ChevronDown className="h-5 w-5 text-gray-500 dark:text-gray-400 transition-transform duration-200" />
                          )}
                        </div>
                      </div>

                      {/* Transcript Content - Conditionally Rendered */}
                      {conversation.conversation_id && expandedTranscripts.has(conversation.conversation_id) && (
                        <div className="animate-in slide-in-from-top-2 duration-300 ease-out space-y-4">
                          {segments.length > 0 ? (
                            <div className="p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-600">
                              <div className="space-y-1">
                                {segments.map((segment, index) => {
                                  const speaker = segment.speaker || 'Unknown'
                                  return (
                                    <div
                                      key={index}
                                      className="text-sm leading-relaxed flex items-start space-x-2 py-1 px-2 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
                                    >
                                      <div className="flex-1 min-w-0">
                                        <span className="font-medium text-blue-600 dark:text-blue-400">
                                          {speaker}:
                                        </span>
                                        <span className="text-gray-900 dark:text-gray-100 ml-1">
                                          {segment.text}
                                        </span>
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          ) : (
                            <div className="text-sm text-gray-500 dark:text-gray-400 italic p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-600">
                              No transcript available
                            </div>
                          )}
                        </div>
                      )}
                    </>
                  )
                })()}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
