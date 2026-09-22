import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { conversationsApi, api } from '../services/api'

interface ConversationListOpts {
  includeDeleted?: boolean
  includeUnprocessed?: boolean
  starredOnly?: boolean
  limit?: number
  offset?: number
  sortBy?: string
  sortOrder?: string
}

export function useConversations(opts: ConversationListOpts = {}) {
  return useQuery({
    queryKey: ['conversations', opts],
    queryFn: () => conversationsApi.getAll(
      opts.includeDeleted, opts.includeUnprocessed,
      opts.limit, opts.offset, opts.starredOnly,
      opts.sortBy, opts.sortOrder,
    ).then(r => r.data),
  })
}

export function useConversationDetail(conversationId: string | null, dataset = false) {
  return useQuery({
    queryKey: ['conversation', conversationId, dataset ? 'dataset' : 'personal'],
    queryFn: () => (dataset
      ? api.get(`/api/data-audit/recordings/${conversationId}`)
      : conversationsApi.getById(conversationId!)).then(r => r.data.conversation),
    enabled: !!conversationId,
  })
}

export function useDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function usePermanentDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.permanentDelete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useRestoreConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.restore(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessTranscript() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (conversationId: string) => conversationsApi.reprocessTranscript(conversationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ conversationId, transcriptVersionId }: { conversationId: string; transcriptVersionId?: string }) =>
      conversationsApi.reprocessMemory(conversationId, transcriptVersionId || 'active'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useReprocessSpeakers() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ conversationId, transcriptVersionId, diarizationSource }: {
      conversationId: string
      transcriptVersionId?: string
      diarizationSource?: 'provider' | 'pyannote'
    }) => conversationsApi.reprocessSpeakers(
      conversationId,
      transcriptVersionId || 'active',
      diarizationSource
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}

export function useToggleStar() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => conversationsApi.star(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
      queryClient.invalidateQueries({ queryKey: ['conversation'] })
    },
  })
}

export function useReprocessOrphan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (conversationId: string) => conversationsApi.reprocessOrphan(conversationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
}
