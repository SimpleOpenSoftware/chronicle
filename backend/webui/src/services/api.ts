import axios from 'axios'
import { getStorageKey } from '../utils/storage'
import { createBrowserSession } from './browserSession'

// Get backend URL from environment or auto-detect based on current location
const getBackendUrl = () => {
  const { protocol, hostname, port } = window.location
  console.log('Protocol:', protocol)
  console.log('Hostname:', hostname)
  console.log('Port:', port)

  const isStandardPort = (protocol === 'https:' && (port === '' || port === '443')) ||
                         (protocol === 'http:' && (port === '' || port === '80'))

  // Check if we have a base path (Caddy path-based routing)
  const basePath = import.meta.env.BASE_URL
  console.log('Base path from Vite:', basePath)

  if (isStandardPort && basePath && basePath !== '/') {
    // We're using Caddy path-based routing - use the base path
    console.log('Using Caddy path-based routing with base path')
    return basePath.replace(/\/$/, '')
  }

  // If explicitly set in environment, use that (for direct backend access)
  if (import.meta.env.VITE_BACKEND_URL !== undefined && import.meta.env.VITE_BACKEND_URL !== '') {
    console.log('Using explicit VITE_BACKEND_URL')
    return import.meta.env.VITE_BACKEND_URL
  }

  if (isStandardPort) {
    // We're being accessed through nginx proxy or standard proxy
    console.log('Using standard proxy - relative URLs')
    return ''
  }

  // Development mode - direct access to dev server
  if (port === '5173') {
    console.log('Development mode - using localhost:8000')
    return 'http://localhost:8000'
  }

  // Fallback
  console.log('Fallback - using hostname:8000')
  return `${protocol}//${hostname}:8000`
}

const BACKEND_URL = getBackendUrl()
console.log('VITE_BACKEND_URL:', import.meta.env.VITE_BACKEND_URL)

console.log('🌐 API: Backend URL configured as:', BACKEND_URL || 'Same origin (relative URLs)')

// Export BACKEND_URL for use in other components
export { BACKEND_URL }

export const api = axios.create({
  baseURL: BACKEND_URL,
  timeout: 60000,  // Increased to 60 seconds for heavy processing scenarios
})

// Add request interceptor to include auth token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem(getStorageKey('token'))
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

export const browserSession = createBrowserSession(api, BACKEND_URL)

export interface CurrentUser {
  id: string
  email: string
  display_name: string | null
  assistant_name: string | null
  timezone: string | null
  is_superuser: boolean
}

export interface AuthToken {
  access_token: string
  token_type: string
}

export interface MemorySpace {
  space_id: string
  name: string
  state: 'active' | 'merging' | 'archived'
  seed_notes: Array<{ note_path: string; content_hash: string; byte_size: number }>
  sync_state: 'unpaired' | 'syncing' | 'healthy' | 'frozen' | 'error'
  sync_error: string | null
  merge_checkpoint: string | null
  created_at: string
  updated_at: string
  archived_at: string | null
}

export interface MemorySpaceNote {
  note_path: string
  content: string
  updated_at: string
}

export interface SpaceMergeChange {
  change_id: string
  note_path: string
  operation: 'create' | 'update' | 'delete'
  before_hash: string | null
  before_text: string | null
  after_text: string | null
  conflict: string | null
  source_refs: Array<{ kind: 'conversation' | 'chat' | 'manual' | 'obsidian'; source_id: string }>
  validation_findings: Array<{ rule: string; detail: string; severity: 'semantic' | 'conflict' }>
}

export interface SpaceMergeProposal {
  proposal_id: string
  space_id: string
  state: 'generating' | 'pending' | 'applying' | 'applied' | 'cancelled' | 'stale' | 'failed'
  changes: SpaceMergeChange[]
  accepted_change_ids: string[]
  rejected_change_ids: string[]
  deferred_event_count: number
  error: string | null
}

export interface DeferredSpaceEvent {
  event_id: string
  source_kind: 'conversation' | 'chat' | 'manual' | 'obsidian'
  source_id: string
  event_type: string
  causal_order: number
  idempotency_key: string
  state: 'pending' | 'dispatching' | 'dispatched' | 'failed'
  attempts: number
  error: string | null
  created_at: string
  dispatched_at: string | null
}

export interface SpaceNoteReviewFrame {
  key: string
  source_id: string
  frame_id: number
  captured_at: string | null
  content_type: string
}

export interface SpaceNoteReview {
  conversation_id: string
  title: string | null
  transcript: string
  created_at: string
  started_at: string
  ended_at: string | null
  review_state: 'automatic' | 'awaiting_context' | 'context_requested' | 'ready' | 'extracting' | 'extracted' | 'failed'
  review_error: string | null
  selected_frame_keys: string[]
  context_description: string | null
  sources: Array<{
    source_id: string
    name: string
    platform: string
    status: 'pairing' | 'online' | 'offline' | 'error'
    last_seen_at: string | null
    health: Record<string, unknown>
  }>
  jobs: Array<{
    job_id: string
    source_id: string
    status: 'pending' | 'claimed' | 'complete' | 'failed'
    error: string | null
    created_at: string
    completed_at: string | null
  }>
  frames: SpaceNoteReviewFrame[]
  memory_job_id?: string
}

export const memorySpacesApi = {
  list: () => api.get<MemorySpace[]>('/api/spaces'),
  mainNotes: (query = '') => api.get<Array<{ note_path: string; byte_size: number; excerpt: string }>>(
    '/api/spaces/main-notes', { params: { query } },
  ),
  get: (spaceId: string) => api.get<MemorySpace>(`/api/spaces/${spaceId}`),
  previewSeed: (notePaths: string[]) => api.post('/api/spaces/seed-preview', { note_paths: notePaths }),
  create: (name: string, seedNotePaths: string[]) => api.post<MemorySpace>('/api/spaces', {
    name,
    seed_note_paths: seedNotePaths,
  }),
  rename: (spaceId: string, name: string) => api.patch<MemorySpace>(`/api/spaces/${spaceId}`, { name }),
  reopen: (spaceId: string) => api.post<MemorySpace>(`/api/spaces/${spaceId}/reopen`),
  notes: (spaceId: string) => api.get<MemorySpaceNote[]>(`/api/spaces/${spaceId}/notes`),
  writeNote: (spaceId: string, notePath: string, content: string | null) => api.put(
    `/api/spaces/${spaceId}/notes/${encodeURIComponent(notePath).replace(/%2F/g, '/')}`,
    { content },
  ),
  search: (spaceId: string, query: string) => api.get(`/api/spaces/${spaceId}/search`, { params: { query } }),
  recordings: (spaceId: string) => api.get(`/api/spaces/${spaceId}/recordings`),
  recordingAudio: (conversationId: string) => api.get<Blob>(
    `/api/audio/get_audio/${conversationId}`,
    { params: { format: 'opus' }, responseType: 'blob' },
  ),
  noteReview: (spaceId: string, conversationId: string) => api.get<SpaceNoteReview>(
    `/api/spaces/${spaceId}/recordings/${conversationId}/note-review`,
  ),
  requestNoteContext: (spaceId: string, conversationId: string, sourceId: string) => api.post<SpaceNoteReview>(
    `/api/spaces/${spaceId}/recordings/${conversationId}/note-review/context`,
    { source_id: sourceId },
  ),
  noteReviewFrame: (spaceId: string, conversationId: string, sourceId: string, frameId: number) => api.get<Blob>(
    `/api/spaces/${spaceId}/recordings/${conversationId}/note-review/frames/${encodeURIComponent(sourceId)}/${frameId}`,
    { responseType: 'blob' },
  ),
  extractReviewedNote: (spaceId: string, conversationId: string, selectedFrameKeys: string[]) => api.post<SpaceNoteReview>(
    `/api/spaces/${spaceId}/recordings/${conversationId}/note-review/extract`,
    { selected_frame_keys: selectedFrameKeys },
  ),
  createChatSession: (spaceId: string, title?: string) => api.post(`/api/spaces/${spaceId}/chat/sessions`, { title }),
  chat: (spaceId: string, message: string, sessionId?: string) => api.post(`/api/spaces/${spaceId}/chat/completions`, {
    session_id: sessionId,
    message,
  }),
  prepareMerge: (spaceId: string, acknowledgeSyncWarnings = false) => api.post<SpaceMergeProposal>(
    `/api/spaces/${spaceId}/merge-proposals`,
    { acknowledge_sync_warnings: acknowledgeSyncWarnings },
    { timeout: 300_000 },
  ),
  latestMergeProposal: (spaceId: string) => api.get<SpaceMergeProposal | null>(
    `/api/spaces/${spaceId}/merge-proposals/latest`,
  ),
  resolveMerge: (proposalId: string, acceptedChangeIds: string[]) => api.post<SpaceMergeProposal>(
    `/api/spaces/merge-proposals/${proposalId}/resolve`,
    { accepted_change_ids: acceptedChangeIds },
  ),
  cancelMerge: (proposalId: string) => api.post<SpaceMergeProposal>(
    `/api/spaces/merge-proposals/${proposalId}/cancel`,
  ),
  sync: (spaceId: string) => api.get(`/api/spaces/${spaceId}/sync`),
  rescanSync: (spaceId: string) => api.post(`/api/spaces/${spaceId}/sync/rescan`),
  deferredEvents: (spaceId: string) => api.get<DeferredSpaceEvent[]>(`/api/spaces/${spaceId}/deferred-events`),
  retryDeferredEvent: (spaceId: string, eventId: string) => api.post<DeferredSpaceEvent>(
    `/api/spaces/${spaceId}/deferred-events/${eventId}/retry`,
  ),
}

// API endpoints
export const authApi = {
  login: browserSession.login,
  getMe: () => api.get<CurrentUser>('/users/me'),
  updateMe: (data: { display_name?: string; assistant_name?: string }) =>
    api.patch<CurrentUser>('/users/me', data),
}

export const conversationsApi = {
  getAll: (includeDeleted?: boolean, includeUnprocessed?: boolean, limit?: number, offset?: number, starredOnly?: boolean, sortBy?: string, sortOrder?: string) => api.get('/api/conversations', {
    params: {
      ...(includeDeleted !== undefined && { include_deleted: includeDeleted }),
      ...(includeUnprocessed !== undefined && { include_unprocessed: includeUnprocessed }),
      ...(starredOnly !== undefined && { starred_only: starredOnly }),
      ...(limit !== undefined && { limit }),
      ...(offset !== undefined && { offset }),
      ...(sortBy !== undefined && { sort_by: sortBy }),
      ...(sortOrder !== undefined && { sort_order: sortOrder }),
    }
  }),
  getById: (id: string) => api.get(`/api/conversations/${id}`),
  search: (
    query: string,
    limit?: number,
    offset?: number,
    fields?: Array<'id' | 'title' | 'summary' | 'speakers'>,
  ) =>
    api.get('/api/conversations/search', {
      params: { q: query, limit, offset, fields },
      paramsSerializer: { indexes: null },
    }),
  star: (id: string) => api.post(`/api/conversations/${id}/star`),
  delete: (id: string) => api.delete(`/api/conversations/${id}`),
  restore: (id: string) => api.post(`/api/conversations/${id}/restore`),
  permanentDelete: (id: string) => api.delete(`/api/conversations/${id}`, {
    params: { permanent: true }
  }),

  // Reprocessing endpoints
  reprocessOrphan: (conversationId: string) => api.post(`/api/conversations/${conversationId}/reprocess-orphan`),
  reprocessTranscript: (conversationId: string) => api.post(`/api/conversations/${conversationId}/reprocess-transcript`),
  reprocessMemory: (conversationId: string, transcriptVersionId: string = 'active') => api.post(`/api/conversations/${conversationId}/reprocess-memory`, null, {
    params: { transcript_version_id: transcriptVersionId }
  }),
  reprocessSpeakers: (
    conversationId: string,
    transcriptVersionId: string = 'active',
    diarizationSource?: 'provider' | 'pyannote'
  ) =>
    api.post(`/api/conversations/${conversationId}/reprocess-speakers`, null, {
      params: {
        transcript_version_id: transcriptVersionId,
        diarization_source: diarizationSource
      }
    }),

  // Conversations whose speaker labels would change under the current gallery (admin).
  // Fingerprint-cached: returns { status: 'cached', report } instantly when nothing
  // relevant changed; otherwise { job_id } — poll progress via dataAuditApi.
  scanDrift: (force = false) =>
    api.post<{ job_id?: string; status: string; report?: unknown }>(
      '/api/conversations/drift/scan',
      undefined,
      { params: force ? { force: true } : undefined }
    ),
  backfillDriftClusterEmbeddings: () =>
    api.post<{ job_id: string; status: string }>(
      '/api/conversations/drift/backfill-cluster-embeddings'
    ),

  // Version management (transcript only — memory is no longer versioned)
  activateTranscriptVersion: (conversationId: string, versionId: string) => api.post(`/api/conversations/${conversationId}/activate-transcript/${versionId}`),
  getVersionHistory: (conversationId: string) => api.get(`/api/conversations/${conversationId}/versions`),

  // Memory vault change history (audit ledger)
  getMemoryAudit: (conversationId: string, limit: number = 100) => api.get(`/api/conversations/${conversationId}/memory-audit`, { params: { limit } }),

  // Active conversation management
  closeActiveConversation: (clientId: string) => api.post(`/api/conversations/${clientId}/close`),
}

export interface DeviceInputSource {
  source_id: string
  name: string
  provider: 'screenpipe' | 'immich' | 'mobile'
  platform: string
  status: 'pairing' | 'online' | 'offline' | 'error'
  health: Record<string, unknown>
  last_seen_at: string | null
  capabilities: string[]
  privacy_enabled_from?: string | null
  privacy_waiting_from?: string | null
  privacy_revision?: number
}

export interface DeviceInputItem {
  id: string
  source_id: string
  kind: 'audio' | 'activity' | 'observation' | 'screen_context' | 'immich_memory'
  source_item_id?: string
  captured_at: string
  ended_at: string | null
  metadata: Record<string, any>
  state: 'received' | 'linked' | 'promoted' | 'rejected'
  lifecycle?: 'open' | 'closed' | null
  curation?: 'pending' | 'curating' | 'discarded' | 'duplicate' | 'linked' | 'promoted' | 'failed' | null
  samples?: Array<Record<string, any> & { text?: string; text_source?: string | null }>
  frame_candidates?: Array<Record<string, any>>
  related_conversation_ids?: string[]
  duplicate_of?: string | null
  agent_reason?: string | null
  vault_paths?: string[]
}

export const deviceInputApi = {
  overridePrivacy: (interval: PrivacyInterval, decision: 'allowed' | 'excluded') =>
    api.post('/api/device-input/privacy/override', {
      source_id: interval.source_id, started_at: interval.started_at,
      ended_at: interval.ended_at, revision: interval.revision, decision,
    }),
  getSources: () => api.get<{ sources: DeviceInputSource[] }>('/api/device-input/sources'),
  createPairingCode: () => api.post<{ code: string; expires_at: string }>('/api/device-input/pairing-codes'),
  getTimeline: (startAt: string, endAt: string) =>
    api.get<{ items: DeviceInputItem[] }>('/api/device-input/timeline', {
      params: { start_at: startAt, end_at: endAt },
    }),
  getThumbnail: (itemId: string) =>
    api.get<Blob>(`/api/device-input/items/${itemId}/thumbnail`, {
      responseType: 'blob',
    }),
  requestThumbnail: (itemId: string) =>
    api.post(`/api/device-input/items/${itemId}/request-thumbnail`),
  getConversationContext: (conversationId: string) =>
    api.get<{ items: DeviceInputItem[] }>(`/api/device-input/conversations/${conversationId}/context`),
  requestConversationContext: (conversationId: string) =>
    api.post(`/api/device-input/conversations/${conversationId}/request-context`),
  clearConversationContext: (conversationId: string) =>
    api.delete(`/api/device-input/conversations/${conversationId}/context`),
  promoteItem: (itemId: string) =>
    api.post(`/api/device-input/items/${itemId}/promote`),
}

export interface ManualMemoryAttachment {
  attachment_id: string
  media_type: 'image'
  content_type: string
  original_filename: string
  content_hash: string
  byte_size: number
  description: string | null
  extracted_text: string | null
  enrichments: Record<string, { state: 'pending' | 'processing' | 'complete' | 'failed' }>
}

export interface ManualMemory {
  memory_id: string
  note: string | null
  source: { kind: string; application?: string | null }
  shared_at: string
  memory_at: string | null
  vault_path: string
  attachments: ManualMemoryAttachment[]
}

export const manualMemoriesApi = {
  list: () => api.get<{ items: ManualMemory[]; next_before: string | null }>('/api/manual-memories'),
  get: (memoryId: string) => api.get<ManualMemory>(`/api/manual-memories/${memoryId}`),
  getThumbnail: (memoryId: string, attachmentId: string) =>
    api.get<Blob>(`/api/manual-memories/${memoryId}/attachments/${attachmentId}/thumbnail`, {
      responseType: 'blob',
    }),
}

export interface TimelineEvidenceLocator {
  capture_source_id: string
  modality: 'screen' | 'audio' | 'transcript' | 'photo' | 'context'
  track_id: string | null
}

export interface TimelineBoundarySupport {
  anchor_id: string
  evidence_id: string
  locator: TimelineEvidenceLocator
  support_type: 'sample' | 'frame' | 'transcript_edge' | 'source_edge' | 'transition' | 'capture_gap_edge' | 'user_edit'
  resolved_source_position: string | number | null
  earliest_at: string
  latest_at: string
  resolved_at: string
  uncertainty_seconds: number
  separation_artifact_hash: string
}

export interface TimelineEvidenceRef {
  evidence_id: string
  kind: 'audio_span' | 'transcript' | 'observation' | 'frame' | 'meeting' | 'immich' | 'capture_gap'
  source_id: string | null
  source_item_id: string | null
  started_at: string
  ended_at: string | null
  role: string
  excerpt: string | null
  ephemeral: boolean
  locator: TimelineEvidenceLocator
  start_boundary_support: TimelineBoundarySupport[]
  end_boundary_support: TimelineBoundarySupport[]
  /** Assembly-time context. `conversation_id` is what lets an episode link to a recording. */
  metadata: Record<string, unknown>
}

export interface TimelineEpisode {
  episode_id: string
  /** Durable identity across analysis runs; `episode_id` names one generation's row. */
  episode_key: string
  /** Immutable revision inside the current snapshot; mutations must fence on it. */
  revision: number
  started_at: string
  ended_at: string
  kind: string
  title: string
  summary: string
  detailed_summary?: string | null
  status: 'open' | 'provisional' | 'settled' | 'superseded'
  confirmed_at: string | null
  /** Fields a person has edited; later runs must not regenerate them. */
  confirmed_fields: string[]
  memory_policy: 'auto' | 'reference' | 'remember'
  requires_activity_review: boolean
  memory_eligible: boolean
  salience: 'background' | 'routine' | 'notable' | 'highlight'
  confidence: number
  activity_mode: 'foreground' | 'background' | 'ambient' | 'idle'
  entities: string[]
  attributes: Record<string, unknown>
  assertions: Array<{ claim: string; role: string; confidence: number; evidence_ids: string[] }>
  evidence: TimelineEvidenceRef[]
  related_episode_ids: string[]
  related_conversation_ids: string[]
  /** Authoritative audio claim; stable across conversation split/merge/trim. */
  audio_ranges: Array<{
    range_id: string
    chunk_ids: string[]
    started_at: string
    ended_at: string
    source_stream: string | null
    /** Operational lineage/debug context only; not the range identity. */
    conversation_ids: string[]
  }>
  // Every live recording overlapping the episode, in wall-clock order. Wider than
  // the cited set, which is only the evidence the agent reasoned over. Detail
  // endpoint only.
  audio_recording_ids?: string[]
  /** Current playback coordinates resolved from authoritative audio_ranges. */
  audio_playback_ranges?: Array<{
    range_id: string
    conversation_id: string
    start: number
    end: number
    started_at: string
    ended_at: string
  }>
  parent_episode_id: string | null
  has_thumbnail: boolean
}

export interface TimelineAnalysis {
  run_id: string
  state: 'pending' | 'preparing' | 'awaiting_evidence' | 'running' | 'validating' | 'complete' | 'failed' | 'quota_deferred'
  attempts: number
  retry_after: string | null
  error: string | null
  requested_evidence_revision?: string
  processed_evidence_revision?: string | null
  created_at: string
  completed_at: string | null
}

export interface PrivacyInterval {
  source_id: string
  source_name: string
  started_at: string
  ended_at: string
  revision: number
  state: 'pending' | 'excluded' | 'needs_review'
  label: string
  reason?: string
}

export interface TimelineDay {
  latest_reconciliation?: TimelineReconciliationRequest | null
  date: string
  timezone: string
  current_snapshot_id: string | null
  reviewed_snapshot_id: string | null
  applied_snapshot_id: string | null
  snapshot_state: 'dirty' | 'ready' | 'reviewed' | 'applied' | 'correction_required'
  coverage: {
    privacy_intervals?: PrivacyInterval[]
    started_at?: string
    ended_at?: string
    window_count?: number
    evidence_count?: number
    recording_intervals?: Array<{ started_at: string; ended_at: string; source: string; state: string; covered_seconds: number; missing_seconds: number; acoustic_active_seconds: number }>
    unassigned_intervals?: Array<{
      started_at: string
      ended_at: string
      reason: string
      cause?: 'no_capture' | 'unexplained'
    }>
  }
  analysis: TimelineAnalysis | null
  consolidation: TimelineConsolidationProposal | null
  semantic_groups: TimelineSemanticGroupRevision[]
  review_decision_count: number
  review_projection: DayReviewProjection
  review: TimelineDayReview | null
  reconciliation: {
    ranges: TimelineDirtyRange[]
  }
  episodes: TimelineEpisode[]
}

export interface TimelineDirtyRangeResolution {
  resolution_id: string
  action: 'dismissed'
  actor_user_id: string
  reason: string
  prior_state: 'failed'
  created_at: string
}

export interface TimelineDirtyRange {
  dirty_range_id: string
  started_at: string
  ended_at: string
  state: 'pending' | 'authorized_pending' | 'leased' | 'waiting' | 'awaiting_context' | 'context_pending' | 'completed' | 'failed' | 'dismissed' | 'superseded'
  trigger_reasons: string[]
  attempts: number
  error: string | null
  resolution_history: TimelineDirtyRangeResolution[]
}

export interface DayReviewGroup {
  group_id: string
  started_at: string
  ended_at: string
  title: string
  summary: string
  semantic: boolean
  lane: 'conversation' | 'foreground' | 'background'
  episode_ids: string[]
  episode_count: number
  conversational_count: number
  confirmed_count: number
  duration_seconds: number
  span_seconds: number
  gap_seconds: number
  intervals: Array<{
    lane: DayReviewGroup['lane']
    episode_id: string
    started_at: string
    ended_at: string
  }>
  entities: string[]
  salience: TimelineEpisode['salience']
  attention_reasons: Array<'low_confidence' | 'missing_evidence' | 'missing_audio' | 'long_episode'>
  needs_attention: boolean
}

export interface DayReviewProjection {
  version: string
  day_started_at: string
  day_ended_at: string
  episode_count: number
  group_count: number
  needs_attention_count: number
  confirmed_count: number
  groups: DayReviewGroup[]
}

export interface TimelineConsolidationSuggestion {
  suggestion_id: string
  episode_ids: string[]
  title: string
  reason: string
  confidence: number
}

export interface TimelineConsolidationProposal {
  state: 'queued' | 'generating' | 'ready' | 'resolved' | 'failed' | 'stale' | ''
  snapshot_id: string | null
  model: string | null
  suggestions: TimelineConsolidationSuggestion[]
  error?: string | null
  generated_at?: string | null
}

export interface TimelineEpisodeRevisionRef {
  episode_key: string
  revision: number
}

export interface TimelineGroupRevisionRef {
  owner_local_date: string
  group_key: string
  revision: number
}

export interface TimelineSemanticGroupRevision {
  group_key: string
  revision: number
  relation_type: 'same_activity'
  member_revisions: TimelineEpisodeRevisionRef[]
  episode_ids: string[]
  source_snapshot_id: string
  predecessor_revisions: TimelineGroupRevisionRef[]
  status: 'active' | 'tombstone'
  title: string
  summary: string
  started_at: string
  ended_at: string
  suggestion_id: string | null
  reason: string
  confidence: number | null
  model: string | null
  created_at: string
}

export interface TimelineReviewDecision {
  decision_id: string
  run_id: string
  action: 'grouping_accept' | 'grouping_reject' | 'grouping_remove' | 'episode_update' | 'episode_structure_confirm' | 'episode_split' | 'episode_merge' | 'episode_delete'
  episode_ids: string[]
  suggestion_id: string | null
  model: string | null
  before: Record<string, unknown>
  after: Record<string, unknown>
  created_at: string
}

export type TimelineReviewState =
  | 'episodes_pending'
  | 'memory_queued'
  | 'memory_generating'
  | 'memory_pending'
  | 'memory_applying'
  | 'finalized'
  | 'failed'

export interface PotentialMemoryChange {
  change_id: string
  note_path: string
  operation: 'create' | 'update' | 'delete'
  before_hash: string | null
  after_hash: string | null
  before_text: string | null
  after_text: string | null
  summary: string
  source_episode_keys: string[]
  source_evidence_keys?: string[]
}

export interface EpisodeRevisionRef { episode_key: string; revision: number }

export interface MemoryReviewProposal {
  proposal_id: string
  request_id: string
  generation: number
  selected_episodes: EpisodeRevisionRef[]
  local_date: string
  timezone: string
  active: boolean
  replacement_proposal_id: string | null
  supersedes_proposal_id: string | null
  freshness: { verdict: 'unaffected' | 'affected' | 'uncertain'; reason: string; relevant_paths: string[] } | null
  state: 'queued' | 'generating' | 'paused' | 'pending' | 'checking' | 'applying' | 'applied' | 'rejected' | 'no_changes' | 'excluded' | 'stale' | 'failed' | 'regenerating' | 'correction_required' | 'corrected' | 'needs_attention' | 'deferred'
  snapshot_id: string
  change_count: number
  accepted_change_ids: string[]
  rejected_change_ids: string[]
  error: string | null
  created_at: string
  generated_at: string | null
  resolved_at: string | null
  changes?: PotentialMemoryChange[]
  account?: { claims: { text: string; source_keys: string[]; citations: { source_key: string; quote: string }[] }[] } | null
}

export interface EpisodeMemoryOutcome {
  episode_key: string
  revision: number
  state: string
  proposal_id: string
  accepted_changes: number
  rejected_changes: number
  daily_recorded: boolean
}

export interface TimelineDayReview {
  state: TimelineReviewState
  review_snapshot_id: string | null
  episodes_reviewed_at: string | null
  resolved_at: string | null
  outcome: 'applied' | 'rejected' | 'no_changes' | null
  error: string | null
  proposal: MemoryReviewProposal | null
}

export interface TimelineReviewQueueItem {
  date: string
  state: TimelineReviewState
  outcome: TimelineDayReview['outcome']
  episode_count: number
  unexplained_count: number
  capture_gap_count: number
  proposal: MemoryReviewProposal | null
}

/**
 * The answer to a stable-key lookup: either the episode that currently covers the
 * key, or — when a split or merge replaced it — the keys that did.
 */
export type TimelineEpisodeByKey =
  | ({ resolved: true } & TimelineEpisode)
  | { resolved: false; episode_key: string; successor_keys: string[] }

export interface TimelineReconciliationRequest {
  progress?: import("../components/timeline/ReconciliationProgress").JobProgress | null
  request_id: string
  date: string
  timezone: string
  state: 'blocked' | 'queued' | 'running' | 'completed' | 'failed'
  reason: 'assets_on_day' | 'later_asset_watermark' | 'no_immich_evidence' | 'immich_unconfigured' | 'immich_unreachable' | 'user_bypassed_immich'
  target_asset_count: number
  latest_eligible_asset_date: string | null
  evidence_cutoff: string
  checked_at: string
  notification_id: string | null
  notification_status: string | null
  job_id: string | null
  dirty_range_id: string | null
  run_id: string | null
  immich_visual: {
    state: 'pending' | 'running' | 'not_needed' | 'complete' | 'partial' | 'failed'
    candidate_count: number
    analyzed_count: number
    newly_analyzed_count: number
    helpful_count: number
    unhelpful_count: number
    failed_count: number
    uninspected_count: number
    exploration_rounds: number
    stop_reason: string
    artifact_id: string
  } | null
  immich_evidence: {
    evidence_count: number
    helpful_evidence_count: number
    window_count: number
    windows: Array<{
      started_at: string
      ended_at: string
      asset_count: number
      helpful_asset_count: number
    }>
  } | null
  last_error: string | null
  created_at: string
  updated_at: string
}

export interface SessionSource {
  key: string; evidence_id: string; content_hash: string | null
  locator: TimelineEvidenceLocator; started_at: string; ended_at: string
  kind: string; role: string; direction: string; excerpt: string
  episode_keys: string[]; participation: 'supporting' | 'background' | 'excluded' | 'uncertain'
  disposition: string
  metadata?: { text_source?: string }
}
export interface MemorySession {
  refresh_assessment?: { verdict: string; reason: string } | null
  session_key: string; revision: number; owner_local_date: string
  origin: 'human' | 'automatic'; title: string; summary: string
  started_at: string; ended_at: string; episodes: EpisodeRevisionRef[]; episode_ids: string[]
  source_keys: string[]; sources: SessionSource[]; scope_hash: string; state: string; questions: string[]; waiting_reason?: string
  proposal_id: string | null; change_count: number; stage: string | null
  completed_sources: number; total_sources: number; error: string | null
  failure_kind?: string | null
  investigation_activity?: { event: string; tool?: string | null; store?: string | null; source?: string | null; tool_calls: number; rounds: number; checkpoint_saved: boolean; resumed: boolean; updated_at: string; runtime_version: string; policy: string }
}

export const timelineApi = {
  getSessions: (day: string, timezone: string) => api.get<{ sessions: MemorySession[]; preparation?: { state: string; attempts: number; job_id: string | null; error: string | null; waiting_sessions: Record<string, string>; completed_queries: number } | null }>(`/api/timeline/sessions/${day}`, { params: { timezone } }),
  getSessionOrganizationExchanges: (day: string, timezone: string) => api.get<{ runs: { request: unknown; stdout: string; stderr: string; metadata?: { model_input?: unknown; tool_calls?: unknown[] } }[] }>(`/api/timeline/sessions/${day}/organization-exchanges`, { params: { timezone } }),
  getSessionSources: (day: string, timezone: string, session: MemorySession) => api.get<{ sources: SessionSource[]; scope_hash: string }>(`/api/timeline/sessions/${day}/${session.session_key}/sources`, { params: { timezone, revision: session.revision } }),
  prepareSessions: (day: string, timezone: string, snapshot_id: string) => api.post(`/api/timeline/sessions/${day}/prepare`, { timezone, snapshot_id }),
  generateSessionMemory: (day: string, timezone: string, session: MemorySession, excluded_source_keys: string[] = []) => api.post(`/api/timeline/sessions/${day}/memory`, { timezone, session_key: session.session_key, revision: session.revision, excluded_source_keys }),
  decideSessionMemory: (day: string, timezone: string, session: MemorySession, action: 'exclude' | 'defer' | 'resume' | 'include' | 'attribute' | 'clarify', source_keys: string[], role?: 'user_statement' | 'third_party' | 'media_content', clarification?: string) => api.post<{ correction_required: boolean }>(`/api/timeline/sessions/${day}/disposition`, { timezone, session_key: session.session_key, revision: session.revision, scope_hash: session.scope_hash, action, source_keys, role, clarification }),

  getPhotoExploration: (requestId: string) => api.get<PhotoExplorationDetails>(`/api/timeline/reconciliation/${requestId}/photos`),
  getPhotoExplorationGrid: (requestId: string, round: number) => api.get<Blob>(`/api/timeline/reconciliation/${requestId}/photos/${round}/grid`, { responseType: 'blob' }),

  getDay: (date: string, timezone: string) =>
    api.get<TimelineDay>('/api/timeline/day', { params: { date, timezone } }),
  reconcileDay: (date: string, timezone: string, skipImmich = false) =>
    api.post<TimelineReconciliationRequest>(`/api/timeline/reconciliation/day/${date}`, { timezone, skip_immich: skipImmich }),
  getReconciliation: (requestId: string) =>
    api.get<TimelineReconciliationRequest>(`/api/timeline/reconciliation/${requestId}`),
  dismissFailedRange: (dirtyRangeId: string, reason: string) =>
    api.post<TimelineDirtyRange>(`/api/timeline/reconciliation/ranges/${encodeURIComponent(dirtyRangeId)}/dismiss`, { reason }),
  getAnalysis: (runId: string) =>
    api.get<TimelineAnalysis>(`/api/timeline/analysis/${runId}`),
  setTimezone: (timezone: string) =>
    api.put<{ timezone: string }>('/api/timeline/timezone', { timezone }),
  getThumbnail: (episodeId: string) =>
    api.get<Blob>(`/api/timeline/episodes/${episodeId}/thumbnail`, { responseType: 'blob' }),
  getEpisode: (episodeId: string) =>
    api.get<TimelineEpisode>(`/api/timeline/episodes/${episodeId}`),
  /**
   * Resolve a durable episode key to the claim that currently covers it.
   *
   * The key outlives reanalysis, editing, split and merge, so a bookmark or vault
   * link uses it instead of the per-row `episode_id`. A key whose own row was
   * superseded resolves to its successors rather than to nothing.
   */
  resolveEpisodeKey: (episodeKey: string) =>
    api.get<TimelineEpisodeByKey>(`/api/timeline/key/${episodeKey}`),
  /** Any edit confirms the episode, pinning it against future reanalysis. */
  updateEpisode: (episodeId: string, changes: TimelineEpisodeUpdate) =>
    api.patch<TimelineEpisode>(`/api/timeline/episodes/${episodeId}`, changes),
  /** Cut an episode in two at `at`; evidence is repartitioned by overlap. */
  splitEpisode: (episodeId: string, at: string) =>
    api.post<{ episodes: TimelineEpisode[] }>(`/api/timeline/episodes/${episodeId}/split`, { at }),
  /** Collapse episodes into the earliest one, unioning their evidence. */
  mergeEpisodes: (episodeIds: string[]) =>
    api.post<TimelineEpisode>('/api/timeline/episodes/merge', { episode_ids: episodeIds }),
  /** Relate episodes semantically while preserving every exact interval. */
  groupEpisodes: (date: string, timezone: string, snapshotId: string, episodeIds: string[]) =>
    api.post<TimelineSemanticGroupRevision>(`/api/timeline/review/day/${date}/groups`, {
      timezone,
      snapshot_id: snapshotId,
      episode_ids: episodeIds,
    }),
  removeSemanticGroup: (date: string, timezone: string, snapshotId: string, groupId: string) =>
    api.delete<void>(`/api/timeline/review/day/${date}/groups/${groupId}`, { params: { timezone, snapshot_id: snapshotId } }),
  getReviewDecisions: (date: string, timezone: string) =>
    api.get<{
      date: string
      timezone: string
      timeline_decisions: TimelineReviewDecision[]
      memory_proposals: MemoryReviewProposal[]
    }>(`/api/timeline/review/day/${date}/decisions`, { params: { timezone } }),
  deleteEpisode: (episodeId: string) =>
    api.delete<void>(`/api/timeline/episodes/${episodeId}`),
  getReviewQueue: (timezone: string) =>
    api.get<{ items: TimelineReviewQueueItem[] }>('/api/timeline/review/queue', { params: { timezone } }),
  finalizeEpisodes: (date: string, timezone: string, snapshotId: string) =>
    api.post<TimelineDayReview>(`/api/timeline/review/day/${date}/episodes`, { timezone, snapshot_id: snapshotId }),
  rejectActivity: (episode: TimelineEpisode, snapshotId: string, date: string, timezone: string) =>
    api.post<void>(`/api/timeline/episodes/${episode.episode_id}/not-activity`, { revision: episode.revision, snapshot_id: snapshotId, local_date: date, timezone }),
  confirmSessionStructures: (date: string, timezone: string, snapshotId: string, episodes: Array<{ episode_key: string; revision: number }>) =>
    api.post<{ episodes: TimelineEpisode[] }>(`/api/timeline/review/day/${date}/confirm-session-structures`, { timezone, snapshot_id: snapshotId, episodes }),
  confirmEpisodeStructure: (date: string, timezone: string, snapshotId: string, episodeKey: string, revision: number) =>
    api.post<TimelineEpisode>(`/api/timeline/review/day/${date}/episodes/${encodeURIComponent(episodeKey)}/revisions/${revision}/confirm-structure`, {
      timezone,
      snapshot_id: snapshotId,
    }),
  suggestConsolidation: (date: string, timezone: string, snapshotId: string) =>
    api.post<TimelineConsolidationProposal>(`/api/timeline/review/day/${date}/consolidation`, { timezone, snapshot_id: snapshotId }),
  resolveConsolidation: (date: string, timezone: string, snapshotId: string, acceptedSuggestionIds: string[], finalize = true) =>
    api.post<{ groups: TimelineSemanticGroupRevision[] }>(`/api/timeline/review/day/${date}/consolidation/resolve`, {
      timezone,
      snapshot_id: snapshotId,
      accepted_suggestion_ids: acceptedSuggestionIds,
      finalize,
    }),
  getMemoryExchanges: (proposalId: string) => api.get<{ accepted_context?: { notes?: { path: string; hash: string; passage: string }[]; consulted_notes?: { path: string; hash: string }[]; unresolved_lookups?: string[]; review?: { verdict: string; reason: string }; review_context?: { notes?: { path: string; hash: string; passage: string }[] } }; source_digest: string; source_scope: SessionSource[]; writer_exchanges: { operation: string; request: unknown; stdout: string; stderr: string }[]; inference_runs: { operation: string; request: unknown; exchanges?: unknown; model_input?: unknown; tool_calls?: unknown[]; context?: unknown; output: string; result?: unknown; error?: string; cached?: boolean }[] }>(`/api/timeline/review/proposals/${proposalId}/exchanges`),
  getMemorySelections: (date: string, timezone: string) =>
    api.get<{ proposals: MemoryReviewProposal[]; outcomes: Record<string, EpisodeMemoryOutcome> }>(`/api/timeline/review/day/${date}/selections`, { params: { timezone } }),
  createMemorySelection: (date: string, timezone: string, snapshotId: string, episodes: EpisodeRevisionRef[]) =>
    api.post<{ proposals: MemoryReviewProposal[] }>(`/api/timeline/review/day/${date}/selections`, { timezone, snapshot_id: snapshotId, episodes }),
  excludeMemorySelection: (date: string, timezone: string, snapshotId: string, episodes: EpisodeRevisionRef[]) =>
    api.post<{ proposals: MemoryReviewProposal[] }>(`/api/timeline/review/day/${date}/exclusions`, { timezone, snapshot_id: snapshotId, episodes }),
  resolveMemoryProposal: (proposalId: string, generation: number, acceptedChangeIds: string[]) =>
    api.post<{ outcome: 'checking'; proposal: MemoryReviewProposal }>(
      `/api/timeline/review/proposals/${proposalId}/resolve`,
      { generation, accepted_change_ids: acceptedChangeIds },
    ),
  correctMemoryProposal: (proposalId: string) =>
    api.post<{ proposal: MemoryReviewProposal }>(`/api/timeline/review/proposals/${proposalId}/correct`),
  regenerateMemoryProposal: (proposalId: string) =>
    api.post<{ outcome: string; proposal: MemoryReviewProposal }>(`/api/timeline/review/proposals/${proposalId}/regenerate`),
}

export interface TimelineEpisodeUpdate {
  title?: string
  summary?: string
  kind?: string
  memory_policy?: TimelineEpisode['memory_policy']
  entities?: string[]
  salience?: TimelineEpisode['salience']
  started_at?: string
  ended_at?: string
}

// One recorded change to the memory vault (the audit ledger). Content lives in
// the per-entry diff endpoint, not the list, so the list stays light.
export interface MemoryAuditEntry {
  id: string
  user_id: string
  conversation_id: string | null
  operation: 'create' | 'update' | 'delete' | 'rename' | 'delete_all'
  note_path: string | null
  // Provenance: `cause` is why the memory changed, `strategy` is how the vault
  // was updated (control flow). `source_kind`/`source_label`/`actor` are the
  // backend-classified taxonomy the UI renders directly.
  cause: string | null
  strategy: string | null
  source_kind: 'extraction' | 'reprocess' | 'human' | 'agent' | 'bulk' | 'other'
  source_label: string
  actor: 'system' | 'user' | 'human_external' | 'agent'
  provider: string
  agent_mode: boolean
  before_hash: string | null
  after_hash: string | null
  after_bytes: number | null
  summary: string | null
  extra: Record<string, unknown>
  created_at: string | null
  has_diff: boolean
}

export interface MemoryAuditDiff {
  id: string
  note_path: string | null
  operation: string
  cause: string | null
  created_at: string | null
  before_text: string | null
  after_text: string | null
  diff: string
  diff_available: boolean
  reason?: string
}

export const memoryApi = {
  // Memory vault change ledger (newest first). `user_id` honored for admins only.
  getAudit: (params?: { limit?: number; conversation_id?: string; user_id?: string }) =>
    api.get<{ user_id: string; count: number; entries: MemoryAuditEntry[] }>(
      '/api/memories/audit',
      { params }
    ),
  // Lazily-fetched before→after diff for one ledger entry.
  getAuditDiff: (entryId: string) =>
    api.get<MemoryAuditDiff>(`/api/memories/audit/${entryId}/diff`),
}

export const annotationsApi = {
  // Create annotations
  createMemoryAnnotation: (data: {
    memory_id: string
    original_text: string
    corrected_text: string
  }) => api.post('/api/annotations/memory', data),

  createTranscriptAnnotation: (data: {
    conversation_id: string
    segment_index: number
    original_text: string
    corrected_text: string
  }) => api.post('/api/annotations/transcript', data),

  // Retrieve annotations
  getMemoryAnnotations: (memory_id: string) =>
    api.get(`/api/annotations/memory/${memory_id}`),

  getTranscriptAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/transcript/${conversation_id}`),

  // Handle suggestions
  acceptSuggestion: (annotation_id: string) =>
    api.patch(`/api/annotations/${annotation_id}/status`, { status: 'accepted' }),

  rejectSuggestion: (annotation_id: string) =>
    api.patch(`/api/annotations/${annotation_id}/status`, { status: 'rejected' }),

  // Diarization annotations
  createDiarizationAnnotation: (data: {
    conversation_id: string
    segment_index: number
    original_speaker: string
    corrected_speaker: string
    segment_start_time?: number
  }) => api.post('/api/annotations/diarization', data),

  getDiarizationAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/diarization/${conversation_id}`),

  // Apply diarization annotations (creates new version)
  applyDiarizationAnnotations: (conversation_id: string) =>
    api.post(`/api/annotations/diarization/${conversation_id}/apply`),

  // Apply ALL pending annotations (diarization + transcript + insert) - creates single new version
  applyAllAnnotations: (conversation_id: string) =>
    api.post(`/api/annotations/${conversation_id}/apply`),

  // Title annotations (instantly applied)
  createTitleAnnotation: (data: {
    conversation_id: string
    original_text: string
    corrected_text: string
  }) => api.post('/api/annotations/title', data),

  getTitleAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/title/${conversation_id}`),

  // Generic annotation management
  deleteAnnotation: (annotationId: string) =>
    api.delete(`/api/annotations/${annotationId}`),

  updateAnnotation: (annotationId: string, data: {
    corrected_text?: string
    corrected_speaker?: string
    insert_text?: string
    insert_segment_type?: string
    insert_speaker?: string
  }) => api.patch(`/api/annotations/${annotationId}`, data),

  // Insert annotations
  createInsertAnnotation: (data: {
    conversation_id: string
    insert_after_index: number
    insert_text: string
    insert_segment_type: string
    insert_speaker?: string
    insert_start?: number
    insert_end?: number
  }) => api.post('/api/annotations/insert', data),

  getInsertAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/insert/${conversation_id}`),

  // Timing annotations (waveform region move/resize of an existing segment)
  createTimingAnnotation: (data: {
    conversation_id: string
    segment_index: number
    new_start: number
    new_end: number
  }) => api.post('/api/annotations/timing', data),

  getTimingAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/timing/${conversation_id}`),

  // Deletion annotations (remove an existing segment)
  createDeletionAnnotation: (data: {
    conversation_id: string
    segment_index: number
  }) => api.post('/api/annotations/deletion', data),

  getDeletionAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/deletion/${conversation_id}`),
}

export const finetuningApi = {
  // Get fine-tuning status
  getStatus: () => api.get('/api/finetuning/status'),

  // Curated enrollment: quality-gated candidate clips + enroll only selected.
  // includeIdentified surfaces auto-labelled (identification) segments too.
  getEnrollmentCandidates: (includeIdentified = false, minDuration?: number) =>
    api.get('/api/finetuning/enrollment-candidates', {
      params: {
        include_identified: includeIdentified,
        ...(minDuration != null ? { min_duration: minDuration } : {}),
      },
    }),
  enrollSelectedClips: (clips: Array<{
    conversation_id: string
    segment_index: number
    start: number
    end: number
    speaker: string
  }>) => api.post('/api/finetuning/enroll-selected', { clips }),

  // Orphaned annotation management
  deleteOrphanedAnnotations: (annotationType?: string) =>
    api.delete('/api/finetuning/orphaned-annotations', {
      params: annotationType ? { annotation_type: annotationType } : {}
    }),
  reattachOrphanedAnnotations: () =>
    api.post('/api/finetuning/orphaned-annotations/reattach'),

  // Failed (stuck) annotation management
  retryFailedAnnotations: (annotationType?: string) =>
    api.post('/api/finetuning/failed-annotations/retry', null, {
      params: annotationType ? { annotation_type: annotationType } : {}
    }),
  deleteFailedAnnotations: (annotationType?: string) =>
    api.delete('/api/finetuning/failed-annotations', {
      params: annotationType ? { annotation_type: annotationType } : {}
    }),

  // Cron job management
  getCronJobs: () => api.get('/api/finetuning/cron-jobs'),
  updateCronJob: (jobId: string, data: { enabled?: boolean; schedule?: string }) =>
    api.put(`/api/finetuning/cron-jobs/${jobId}`, data),
  runCronJob: (jobId: string) =>
    api.post(`/api/finetuning/cron-jobs/${jobId}/run`),
}

export const usersApi = {
  getAll: () => api.get('/api/users'),
  create: (userData: any) => api.post('/api/users', userData),
  update: (id: string, userData: any) => api.put(`/api/users/${id}`, userData),
  delete: (id: string) => api.delete(`/api/users/${id}`),
}

// userId targets another user's keys (admin only); omit for your own.
export const apiKeysApi = {
  list: (userId?: string) =>
    api.get('/api/api-keys', { params: userId ? { user_id: userId } : undefined }),
  create: (name: string, expiresInDays?: number, userId?: string) =>
    api.post(
      '/api/api-keys',
      { name, expires_in_days: expiresInDays ?? null },
      { params: userId ? { user_id: userId } : undefined },
    ),
  revoke: (id: string) => api.delete(`/api/api-keys/${id}`),
}

export const clientsApi = {
  list: () => api.get('/api/clients'),
  rename: (clientId: string, name: string) => api.patch(`/api/clients/${clientId}`, { name }),
  forget: (clientId: string) => api.delete(`/api/clients/${clientId}`),
}

export const systemApi = {
  getHealth: () => api.get('/health'),
  getReadiness: () => api.get('/readiness'),
  getMetrics: () => api.get('/api/metrics'),
  getConfigDiagnostics: () => api.get('/api/config/diagnostics'),
  getActiveClients: () => api.get('/api/clients/active'),
  getDiarizationSettings: () => api.get('/api/diarization-settings'),
  saveDiarizationSettings: (settings: any) => api.post('/api/diarization-settings', settings),

  // ASR hint mechanism (keyword boosting vs LLM context prompt) + per-provider context
  getAsrContext: () => api.get('/api/asr-context'),
  saveAsrContext: (model_name: string, context: string) =>
    api.post('/api/asr-context', { model_name, context }),

  // Miscellaneous Configuration Settings
  getMiscSettings: () => api.get('/api/misc-settings'),
  saveMiscSettings: (settings: {
    per_segment_speaker_id?: boolean;
    streaming_fallback_timeout_seconds?: number;
    always_batch_retranscribe?: boolean;
    audio_filtering_require_speech?: boolean;
    live_segmentation?: 'streaming_stt' | 'windowed_batch' | 'off';
  }) => api.post('/api/misc-settings', settings),
  getTimelineGroupingSettings: () => api.get('/api/timeline-grouping-settings'),
  saveTimelineGroupingSettings: (settings: { pregenerate: boolean; prefetch_days: number }) =>
    api.post('/api/timeline-grouping-settings', settings),

  // Plugin Configuration Management (YAML-based)
  getPluginsConfigRaw: () => api.get('/api/admin/plugins/config'),
  updatePluginsConfigRaw: (configYaml: string) =>
    api.post('/api/admin/plugins/config', configYaml, {
      headers: { 'Content-Type': 'text/plain' }
    }),
  validatePluginsConfig: (configYaml: string) =>
    api.post('/api/admin/plugins/config/validate', configYaml, {
      headers: { 'Content-Type': 'text/plain' }
    }),

  // Plugin Configuration Management (Structured/Form-based)
  getPluginsMetadata: () => api.get('/api/admin/plugins/metadata'),
  updatePluginConfigStructured: (pluginId: string, config: {
    orchestration?: {
      enabled: boolean
      events: string[]
      condition: { type: string; wake_words?: string[]; keywords?: string[]; threshold?: number }
    }
    settings?: Record<string, any>
    env_vars?: Record<string, string>
  }) => api.post(`/api/admin/plugins/config/structured/${pluginId}`, config),
  testPluginConnection: (pluginId: string, config: {
    orchestration?: {
      enabled: boolean
      events: string[]
      condition: { type: string; wake_words?: string[]; keywords?: string[]; threshold?: number }
    }
    settings?: Record<string, any>
    env_vars?: Record<string, string>
  }) => api.post(`/api/admin/plugins/test-connection/${pluginId}`, config),

  // Plugin CRUD
  createPlugin: (data: { plugin_name: string; description: string; events: string[]; plugin_code?: string }) =>
    api.post('/api/admin/plugins/create', data),
  deletePlugin: (pluginId: string, removeFiles: boolean = false) =>
    api.delete(`/api/admin/plugins/${pluginId}`, { params: { remove_files: removeFiles } }),
  writePluginCode: (pluginId: string, data: { code: string; config_yml?: string }) =>
    api.put(`/api/admin/plugins/${pluginId}/code`, data),

  // Plugin AI Assistant (SSE streaming)
  pluginAssistantChat: (messages: Array<{ role: string; content: string }>) => {
    return fetch(`${BACKEND_URL}/api/admin/plugins/assistant`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem(getStorageKey('token'))}`
      },
      body: JSON.stringify({ messages })
    })
  },

  // Plugin Connectivity
  getPluginsConnectivity: () => api.get('/api/admin/plugins/connectivity'),

  // Memory Provider Management
  getMemoryProvider: () => api.get('/api/admin/memory/provider'),
  setMemoryProvider: (provider: string) => api.post('/api/admin/memory/provider', { provider }),

  // LLM Operations Settings
  getLLMOperations: () => api.get('/api/admin/llm-operations'),
  saveLLMOperations: (operations: Record<string, any>) =>
    api.post('/api/admin/llm-operations', operations),
  testLLMModel: (modelName: string | null) =>
    api.post('/api/admin/llm-operations/test', { model_name: modelName }),

  // Model registry + active defaults (Chronicle model configuration)
  getModels: () => api.get('/api/admin/models'),
  setActiveDefaults: (defaults: Record<string, string>) =>
    api.post('/api/admin/defaults', defaults),
  upsertModel: (model: Record<string, any>) => api.post('/api/admin/models', model),
  deleteModel: (name: string) =>
    api.delete(`/api/admin/models/${encodeURIComponent(name)}`),
  testModel: (modelName: string | null) =>
    api.post('/api/admin/models/test', { model_name: modelName }),

  // Network discovery
  getNetworkDiscovery: () => api.get('/api/system/network'),

  // System restart operations
  restartWorkers: () => api.post('/api/admin/system/restart-workers'),
  restartBackend: () => api.post('/api/admin/system/restart-backend'),

  // External service management (host service-manager agent)
  getExternalServices: () => api.get('/api/admin/services'),
  externalServiceAction: (
    name: string,
    action: 'start' | 'stop' | 'restart',
    options?: { build?: boolean; force?: boolean; node?: string | null },
  ) =>
    api.post(`/api/admin/services/${name}/${action}`, options ?? {}),
  setExternalServiceProvider: (
    name: string,
    provider: string,
    build: boolean = false,
    lane: 'batch' | 'streaming' = 'batch',
    node?: string | null,
  ) =>
    api.post(`/api/admin/services/${name}/provider`, { provider, build, lane, node }),
  getExternalServiceOperation: (operationId: string, node?: string | null) =>
    api.get(`/api/admin/services/operations/${operationId}`, { params: node ? { node } : undefined }),

  // Node code-version updates (git fetch + rebuild/restart via the node agent).
  // The check does a `git fetch` on the node and can take up to ~90s, so it needs
  // a longer timeout than the default 60s axios instance.
  checkNodeUpdate: (node?: string | null, target?: string) =>
    api.get('/api/admin/update', {
      params: {
        ...(node ? { node } : {}),
        ...(target ? { target } : {}),
      },
      timeout: 100_000,
    }),
  startNodeUpdate: (body: { target?: string; prebuilt?: string; node?: string | null }) =>
    api.post('/api/admin/update', body),
  // Backend build version (unauthenticated). Uses the /api-prefixed mount:
  // Caddy only routes /api/* (+ a few named paths) to the backend; a bare
  // /version would fall through to the webui-dev catch-all and 404.
  getVersion: () => api.get('/api/version'),

  // Claude remote-control session (control Claude Code from your phone)
  getRemoteControl: () => api.get('/api/admin/remote-control'),
  remoteControlAction: (action: 'start' | 'stop' | 'restart') =>
    api.post(`/api/admin/remote-control/${action}`),

  // Observability
  getObservabilityConfig: () => api.get('/api/observability'),
}

// ---- System events (admin "System Errors" page) ---------------------------

export interface SystemEvent {
  id: string
  severity: 'info' | 'warning' | 'error' | 'critical'
  category: string
  source: string
  title: string
  detail: string | null
  traceback: string | null
  user_id: string | null
  client_id: string | null
  conversation_id: string | null
  count: number
  acked: boolean
  metadata: Record<string, unknown>
  created_at: string | null
  last_seen_at: string | null
  occurrence_times: string[]
}

export interface SystemEventsList {
  events: SystemEvent[]
  total: number
  limit: number
  offset: number
}

export interface MemoryFallbackAgentPath {
  primary_backend: string
  recovery_backend: string
  occurrences: number
}

export interface MemoryFallbackSummary {
  occurrences: number
  affected_conversations: number
  by_reason: Record<string, number>
  by_user: Record<string, number>
  by_primary_backend: Record<string, number>
  by_recovery_backend: Record<string, number>
  agent_paths: MemoryFallbackAgentPath[]
  latest_at: string | null
}

export interface SystemEventsSummary {
  window_hours: number
  total: number
  unacked: number
  by_severity: Record<string, number>
  by_category: Record<string, number>
  by_source: Record<string, number>
  memory_fallbacks: MemoryFallbackSummary
}

export interface SystemEventsFilter {
  severity?: string
  category?: string
  source?: string
  client_id?: string
  user_id?: string
  acked?: boolean
  since_hours?: number
  limit?: number
  offset?: number
}

export const systemEventsApi = {
  list: (params: SystemEventsFilter = {}) =>
    api.get<SystemEventsList>('/api/admin/system-events', { params }),
  summary: (windowHours = 24) =>
    api.get<SystemEventsSummary>('/api/admin/system-events/summary', {
      params: { window_hours: windowHours },
    }),
  ack: (id: string) => api.post(`/api/admin/system-events/${id}/ack`),
  ackSelected: (eventIds: string[]) =>
    api.post('/api/admin/system-events/ack-selected', { event_ids: eventIds }),
  ackAll: (params: Omit<SystemEventsFilter, 'acked' | 'limit' | 'offset'> = {}) =>
    api.post('/api/admin/system-events/ack-all', null, { params }),
  clear: (ackedOnly = false) =>
    api.post('/api/admin/system-events/clear', null, {
      params: { acked_only: ackedOnly },
    }),
}

export const queueApi = {
  // Consolidated dashboard endpoint - replaces individual getJobs, getStats, getStreamingStatus calls
  getDashboard: (expandedSessions: string[] = []) => api.get('/api/queue/dashboard', {
    params: { expanded_sessions: expandedSessions.join(',') }
  }),

  // Individual endpoints (kept for debugging and specific use cases)
  getJob: (jobId: string) => api.get(`/api/queue/jobs/${jobId}`),
  retryJob: (jobId: string, force: boolean = false) =>
    api.post(`/api/queue/jobs/${jobId}/retry`, { force }),
  cancelJob: (jobId: string) => api.delete(`/api/queue/jobs/${jobId}`),

  // Cleanup operations
  // Runs the same sweep as the audio_stream_reclaim cron. Replaced cleanupStuckWorkers
  // and cleanupOldSessions, which were two overlapping implementations behind three
  // buttons whose names described neither.
  reclaimStreams: () => api.post('/api/streaming/reclaim'),

  // Job flush operations
  flushJobs: (flushAll: boolean, body: any) => {
    const endpoint = flushAll ? '/api/queue/flush-all' : '/api/queue/flush'
    return api.post(endpoint, body)
  },

  // Clear jobs
  clearJobs: () => api.delete('/api/queue/jobs'),


  // Plugin events
  getEvents: (limit: number = 50, eventType?: string) => api.get('/api/queue/events', {
    params: { limit, ...(eventType && { event_type: eventType }) }
  }),
  clearEvents: () => api.delete('/api/queue/events'),

  // Legacy endpoints - kept for backward compatibility but not used in Queue page
  // getJobs: (params: URLSearchParams) => api.get(`/api/queue/jobs?${params}`),
  // getJobsBySession: (sessionId: string) => api.get(`/api/queue/jobs/by-session/${sessionId}`),
  // getStats: () => api.get('/api/queue/stats'),
  // getStreamingStatus: () => api.get('/api/streaming/status'),
}

export const uploadApi = {
  uploadAudioFiles: (
    files: FormData,
    onProgress?: (progress: number) => void,
    options?: { annotationOnly?: boolean }
  ) =>
    api.post(options?.annotationOnly ? '/api/audio/upload/annotation' : '/api/audio/upload', files, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000, // 5 minutes
      onUploadProgress: (progressEvent) => {
        if (onProgress && progressEvent.total) {
          const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total)
          onProgress(progress)
        }
      },
    }),

  uploadFromGDriveFolder: (payload: { gdrive_folder_id: string; device_name?: string; annotation_only?: boolean }) =>
    api.post(payload.annotation_only ? '/api/audio/upload_audio_from_gdrive/annotation' : '/api/audio/upload_audio_from_gdrive', null, {
      params: {
        gdrive_folder_id: payload.gdrive_folder_id,
        device_name: payload.device_name,
      },
      timeout: 300000,
    }),
}



export interface ChatSourceRef {
  kind: 'recording' | 'session' | 'episode'
  key: string
  local_date?: string | null
  timezone?: string
}
export interface SourcePassage {
  id: string
  text: string
  url: string
  label: string
  revision: string
}
export interface ChatSourceContext {
  ref: ChatSourceRef
  title: string
  url: string
  started_at: string | null
  revision: string
  passages: SourcePassage[]
  coverage: string
  total_passages: number
}

export const chatApi = {
  getDialogue: (sessionId: string) => api.get(`/api/chat/sessions/${sessionId}/dialogue`),
  commandDialogueTask: (sessionId: string, taskId: string, command: {
    id: string; revision: number; action: 'reply' | 'pause' | 'resume' | 'cancel'; text?: string; choice_id?: string;
  }) => api.post(`/api/chat/sessions/${sessionId}/dialogue/tasks/${taskId}/commands`, command),
  getRuns: (sessionId: string) => api.get(`/api/chat/sessions/${sessionId}/runs`),
  getRun: (sessionId: string, runId: string) => api.get(`/api/chat/sessions/${sessionId}/runs/${runId}`),
  deleteRuns: (sessionId: string) => api.delete(`/api/chat/sessions/${sessionId}/runs`),
  getSources: (sessionId: string) => api.get<{ sources: ChatSourceContext[] }>(`/api/chat/sessions/${sessionId}/sources`),
  setSources: (sessionId: string, sources: ChatSourceRef[]) => api.put(`/api/chat/sessions/${sessionId}`, { sources }),
  createSaveProposal: (sessionId: string) => api.post(`/api/chat/sessions/${sessionId}/save-proposals`),
  getSaveProposal: (sessionId: string) => api.get(`/api/chat/sessions/${sessionId}/save-proposals/latest`),
  decideSaveProposal: (sessionId: string, proposalId: string, generation: string, selected: string[], action: 'approve' | 'discard' | 'retry') => api.post(`/api/chat/sessions/${sessionId}/save-proposals/${proposalId}/${action}`, { generation, selected_change_ids: selected }),
  // Session management
  createSession: (title?: string, sources: ChatSourceRef[] = [], memorySpaceId?: string) => api.post('/api/chat/sessions', { title, sources, memory_space_id: memorySpaceId }),
  getSource: (sessionId: string) => api.get<ChatSourceContext>(`/api/chat/sessions/${sessionId}/source`),
  getSessions: (limit = 50, memorySpaceId?: string, signal?: AbortSignal) => api.get('/api/chat/sessions', { params: { limit, memory_space_id: memorySpaceId }, signal }),
  getSession: (sessionId: string) => api.get(`/api/chat/sessions/${sessionId}`),
  updateSession: (sessionId: string, title: string) => api.put(`/api/chat/sessions/${sessionId}`, { title }),
  deleteSession: (sessionId: string) => api.delete(`/api/chat/sessions/${sessionId}`),

  // Messages
  getMessages: (sessionId: string, limit = 100, offset = 0) => api.get(`/api/chat/sessions/${sessionId}/messages`, { params: { limit, offset } }),

  // Statistics
  getStatistics: () => api.get('/api/chat/statistics'),

  // Health check
  getHealth: () => api.get('/api/chat/health'),

  // Streaming chat — OpenAI-compatible completions endpoint.
  // Memory is always agentic: the backend's chat agent calls the search_memories
  // tool (which runs the agentic vault search) when a question needs context.
  sendMessage: (message: string, sessionId?: string, signal?: AbortSignal) => {
    const requestBody: Record<string, unknown> = {
      messages: [{ role: 'user', content: message }],
      stream: true,
    }
    if (sessionId) {
      requestBody.session_id = sessionId
    }


    return fetch(`${BACKEND_URL}/api/chat/completions`, {
      method: 'POST',
      signal,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem(getStorageKey('token'))}`
      },
      body: JSON.stringify(requestBody)
    })
  }
}

export const speakerApi = {
  // Get current user's speaker configuration
  getSpeakerConfiguration: () => api.get('/api/speaker-configuration'),

  // Update current user's speaker configuration
  updateSpeakerConfiguration: (primarySpeakers: Array<{speaker_id: string, name: string, user_id: number}>) =>
    api.post('/api/speaker-configuration', primarySpeakers),

  // Get enrolled speakers from speaker recognition service
  getEnrolledSpeakers: () => api.get('/api/enrolled-speakers'),

  // Check speaker service status (admin only)
  getSpeakerServiceStatus: () => api.get('/api/speaker-service-status'),

  // Get current user's wake-word speaker gate (only fire for selected speakers)
  getWakewordSpeakerGate: () => api.get('/api/wakeword-speaker-gate'),

  // Update the wake-word speaker gate
  updateWakewordSpeakerGate: (
    enabled: boolean,
    speakers: Array<{ speaker_id: string; name: string }>
  ) => api.post('/api/wakeword-speaker-gate', { enabled, speakers }),
}

export interface AuditConversation {
  conversation_id: string
  title: string | null
  client_id: string
  created_at: string | null
  duration_seconds: number
  speakers: string[]
  // Speech segments not matched to an enrolled speaker — the "needs triage" count.
  unknown_speech_segments: number
  // Speech segments identified as a speaker but at low confidence (within the
  // margin of the match threshold) — likely-wrong labels to review.
  marginal_identified_segments: number
  // Pipeline processing status: 'active' | 'completed' | 'failed' | null (legacy).
  processing_status: string | null
  failure_stage: string | null
  analyzed: boolean
  speech_fraction: number | null
  derived_operation: 'split' | 'merge' | null
  // Most recent annotation export that shipped this conversation (null = never
  // exported) — lets curation sessions skip audio already sent to annotators.
  last_export: { export_id: string; created_at: string } | null
  audio_archived: boolean
  audio_archived_at: string | null
  archive_reason: string | null
}

export interface AuditSegment {
  index: number
  start: number
  end: number
  segment_start_time: number
  text: string
  speaker: string
  identified_as: string | null
  confidence: number | null
  segment_type: string
}

export interface AuditSegmentsResponse {
  conversation_id: string
  duration_seconds: number
  audio_available: boolean
  segments: AuditSegment[]
}

export interface SegmentIdentifyResponse {
  found: boolean
  speaker_id: string | null
  speaker_name: string | null
  confidence: number | null
  threshold?: number
  status: string | null
}

export type SpeakerLabelReviewVerdict =
  | 'correct'
  | 'relabel'
  | 'unknown'
  | 'background'
  | 'mixed'
  | 'bad_audio'

export interface SpeakerLabelReviewClip {
  review_key: string
  conversation_id: string
  conversation_title: string | null
  conversation_date: string
  segment_index: number
  segment_start_time: number
  start: number
  end: number
  text: string
  claimed_speaker: string
  raw_speaker: string | null
  confidence: number | null
  selection_lane: 'boundary' | 'control'
  context: Array<{
    position: 'before' | 'current' | 'after'
    speaker: string | null
    text: string
    start: number
    end: number
  }>
}

export interface SpeakerLabelReviewBatch {
  batch: SpeakerLabelReviewClip[]
  reviewed_total: number
  conversations_scanned: number
  candidate_claims: number
  threshold: number
}

export interface SpeakerLabelReviewSummary {
  reviewed: number
  evaluable: number
  correct: number
  precision: number | null
  excluded: number
  errors: Record<string, number>
}

export interface SpeakerLabelReviewMetrics {
  overall: SpeakerLabelReviewSummary
  boundary: SpeakerLabelReviewSummary
  control: SpeakerLabelReviewSummary
  speakers: Array<SpeakerLabelReviewSummary & { speaker: string }>
}

export interface TriageApplyResponse {
  applied_count: number
  conversation_count: number
  apply_errors?: string[]
  enrolled: boolean | null
}

export interface AuditListResponse {
  conversations: AuditConversation[]
  total: number
  limit: number
  offset: number
  scan_capped: boolean
  speech_threshold: number
  // Live speaker-match threshold + comfort margin used to compute the
  // per-conversation "low-confidence" review counts.
  similarity_threshold: number
  marginal_margin: number
  // Conversations the Analyze button would process (user's own live audio
  // without cached VAD analysis) — 0 means there is nothing to analyze.
  unanalyzed_count: number
  // Distinct speaker labels present in the scanned working set, so the
  // speaker filter offers only labels that exist in the current view.
  speakers: string[]
  has_unknown_speakers: boolean
  // Annotation dataset IDs available to the dataset filter.
  datasets: string[]
}

export interface AnnotationImportResponse {
  dataset_id: string
  schema_version: number
  message: string
  results: Array<{
    clip_id: string
    status: 'imported' | 'skipped' | 'error'
    conversation_id?: string
    error?: string
  }>
  summary: {
    total: number
    imported: number
    skipped: number
    failed: number
  }
}

export interface UnknownSpeakerSegment {
  segment_index: number
  start: number
  end: number
  duration: number
  text: string
}

export interface UnknownSpeakerIdentity {
  identity_key: string
  conversation_id: string
  conversation_title: string
  conversation_date: string
  local_label: string
  segments: UnknownSpeakerSegment[]
}

export interface UnknownSpeakerCluster {
  cluster_id: string
  run_fingerprint: string
  conversation_count: number
  segment_count: number
  members: UnknownSpeakerIdentity[]
}

export interface SpeakerConfidenceRow {
  name: string
  nseg: number
  nconv: number
  mean: number
  median: number
  min: number
  max: number
  // % of this speaker's identifications below threshold+margin (noise-magnet signal).
  marginal_pct: number
  // % that would still clear the live threshold.
  keep_pct: number
  // Enrolled but zero stored identifications (e.g. just enrolled); stats are null.
  never_identified?: boolean
}

export interface SpeakerConfidenceOverview {
  threshold: number
  margin: number
  total_identified: number
  conversations_with_ids: number
  conversations_scanned: number
  scan_capped: boolean
  marginal_count: number
  marginal_fraction: number
  histogram: { start: number; bin_width: number; counts: number[] }
  survival: { threshold: number; keep: number; drop: number }[]
  recommended_threshold: number | null
  speakers: SpeakerConfidenceRow[]
}

// In-flight progress published by batch jobs via job.meta "batch_progress".
export interface BatchProgress {
  percent?: number
  message?: string
  done?: number
  total?: number
}

export interface SilenceGap {
  start_seconds: number
  end_seconds: number
  duration_seconds: number
  start_chunk: number
  end_chunk: number
  split_point_seconds: number
}

export interface SilenceGapsResponse {
  analyzed: boolean
  needs_analysis: boolean
  duration_seconds: number
  chunk_duration_seconds: number
  speech_threshold: number
  min_gap_seconds: number
  gaps: SilenceGap[]
}

export interface SpeechRegion {
  start: number
  end: number
}

export interface SpeechRegionsResponse {
  analyzed: boolean
  needs_analysis: boolean
  duration_seconds: number
  speech_seconds: number
  speakers?: string[]
  regions: SpeechRegion[]
}

export interface SplitChild {
  conversation_id: string
  start_seconds: number
  end_seconds: number
  duration_seconds: number
  chunk_count: number
  has_transcript: boolean
  jobs: Record<string, string> | null
}

export interface SplitResponse {
  parent_conversation_id: string
  children: SplitChild[]
}

export interface MergeResponse {
  merged_conversation_id: string
  source_conversation_ids: string[]
  duration_seconds: number
  chunk_count: number
  has_transcript: boolean
  jobs: Record<string, string> | null
}

export interface ExportConversationSummary {
  conversation_id: string
  title: string | null
  client_id: string | null
  clip_count?: number
  clip_seconds?: number
  skipped_reason?: string
}

export interface ExportRecord {
  export_id: string
  created_at: string
  created_by: string
  params: {
    mode?: 'clips' | 'full'
    pad_seconds: number
    speech_threshold: number
    merge_gap_seconds: number
    screened?: boolean
    sensitivity_policy?: string | null
  }
  conversations: ExportConversationSummary[]
  totals: {
    conversation_count: number
    exported_conversations: number
    clip_count: number
    total_clip_seconds: number
    excluded_seconds?: number
    zip_bytes?: number
  }
  zip_ready: boolean
}

// One clip the export would produce, as returned by the preview dry-run.
export interface ExportPreviewClip {
  clip_index: number
  clip_id: string
  start: number
  end: number
  duration_seconds: number
  // The sliced transcript this clip's manifest record would carry ('' = no
  // transcript covers the clip).
  text: string
  segment_count: number
}

export interface ExportPreviewConversation {
  conversation_id: string
  title: string | null
  client_id: string | null
  created_at: string | null
  skipped_reason?: string
  clips?: ExportPreviewClip[]
  sample_rate?: number
  clip_seconds?: number
  excluded_seconds?: number
}

export interface ExportPreviewResult {
  conversations: ExportPreviewConversation[]
  totals: {
    conversation_count: number
    exported_conversations: number
    clip_count: number
    total_clip_seconds: number
    excluded_seconds: number
  }
}

// One transcript segment flagged by the privacy screen as too sensitive to share.
export interface ScreenFlaggedSegment {
  index: number
  start: number
  end: number
  category: string
  reason: string
  speaker?: string | null
  text?: string | null
}

export interface ScreenConversationReport {
  conversation_id: string
  title?: string | null
  client_id?: string | null
  segment_count?: number
  flagged?: ScreenFlaggedSegment[]
  flagged_seconds?: number
  skipped_reason?: string
  error?: string
}

export interface ScreenResult {
  success: boolean
  policy: string
  conversations: ScreenConversationReport[]
  totals: { conversation_count: number; flagged_segments: number }
}

export interface GuidedEnrollmentClip {
  conversation_id: string
  conversation_title: string | null
  conversation_date: string
  conversation_duration: number
  segment_index: number
  start: number
  end: number
  duration: number
  text: string
  current_label: string | null
  stored_confidence: number | null
  scores: {
    duration: number | null
    sim_centroid: number
    max_clip_sim: number | null
    n_gallery_clips: number
    best_other: { speaker_id: string; name: string; score: number } | null
  }
  info_score: number
  novelty: number
  uncertainty: number
  reasons: string[]
}

export interface GuidedEnrollmentSpeaker {
  speaker_id: string
  speaker_name: string
  n_clips: number | null
  total_duration_s: number | null
}

export interface GuidedEnrollmentSuggestResponse {
  speaker: GuidedEnrollmentSpeaker
  threshold: number
  batch: GuidedEnrollmentClip[]
  scanned: number
  gated_out: number
  pool_remaining: number
  reviewed_total: number
  discovery_indexed: boolean
  discovery_candidates: number
}

export interface GuidedEnrollmentDecideResponse {
  speaker: GuidedEnrollmentSpeaker | null
  enrolled: number
  reassigned: number
  rejected: number
  skipped: number
  multiple_speakers: number
  bad_clips: number
  health_before: GuidedEnrollmentHealth | null
  health_after: GuidedEnrollmentHealth | null
  coverage: { accepted_novelty_mean: number | null }
  benchmark_job_id: string | null
  discovery_job_id: string | null
  errors: { clip: unknown; error: string }[]
  status: string
}

export interface GuidedEnrollmentHealth {
  n_clips: number
  median_self: number | null
  n_flagged: number
  flagged_rate: number
  verdict: string
}

export interface GuidedEnrollmentSession {
  created_at: string
  health_before: GuidedEnrollmentHealth | null
  health_after: GuidedEnrollmentHealth | null
  coverage: { accepted_novelty_mean: number | null }
  decisions: {
    enrolled: number
    reassigned: number
    rejected: number
    skipped: number
    multiple_speakers: number
    bad_clips: number
  }
}

export interface SpeakerBenchmarkReport {
  created_at: string
  protocol: string
  threshold: number
  embedding_model: string
  conversation_groups: number
  dataset: {
    labeled_clips: number
    embedded_clips: number
    speakers: number
    cache_hits: number
    embedding_failures: number
    exclusions: Record<string, number>
  }
  learning_curve: Array<{
    fraction: number
    train_clips_mean: number
    top1_accuracy_mean: number | null
    macro_recall_mean: number | null
    false_accept_rate_mean: number | null
    eer_mean: number | null
  }>
  folds: Array<{
    fold: number
    train_clips: number
    test_clips: number
    top1_accuracy: number | null
    macro_recall: number | null
    false_accept_rate: number | null
    eer: number | null
    per_speaker_recall: Record<string, number>
    confusion: Record<string, Record<string, number>>
  }>
}

export interface SpeakerGalleryBaseline {
  cutoff: string | null
  status: string
  limitations?: string
  speakers: Array<{
    speaker_id: string
    name: string
    baseline: { n_clips: number; median_self: number | null; n_flagged: number; verdict: string }
    current: { n_clips: number; median_self: number | null; n_flagged: number; verdict: string } | null
  }>
}

export interface GuidedEnrollmentGalleryClip {
  segment_id: number
  filename: string
  duration: number
  self_score: number | null
  best_other: { speaker_id: string; name: string; score: number } | null
  flags: string[]
  suggested: { speaker_id: string; name: string; score: number } | null
}

export interface GuidedEnrollmentGalleryResponse {
  speaker: {
    speaker_id: string
    speaker_name: string
    n_clips: number | null
    total_duration_s: number | null
  }
  verdict: string | null
  median_self: number | null
  clips: GuidedEnrollmentGalleryClip[]
}

export interface GuidedEnrollmentResetResponse {
  speaker_name: string
  deleted: {
    reviews: number
    sessions: number
    discovery_matches: number
    discovery_runs: number
  }
  gallery_deleted: boolean
  status: string
}

export interface BackgroundCandidate {
  conversation_id: string
  title?: string
  segment_index: number
  segment_start_time: number
  start: number
  end: number
  text: string
  bucket_similarity: number
  snr_db: number | null
  background_likelihood: number
  candidate_type: 'noise' | 'background_speech'
  bucket_similarities: Partial<Record<'noise' | 'background_speech', number>>
}

export interface BackgroundSuggestResponse {
  conversation_id: string
  bucket_size: number
  candidates: BackgroundCandidate[]
}

export interface BackgroundScanResponse {
  bucket_sizes: Record<'noise' | 'background_speech', number>
  scanned_conversations: number
  candidates: BackgroundCandidate[]
}

export interface BackgroundSuppressionSegment {
  conversation_id: string
  segment_start: number
  segment_end: number
  text?: string
  cluster_signature?: string
  background_similarity: number
  foreground_similarity: number
  bucket_type?: 'noise' | 'background_speech'
  zone: 'confident_background' | 'unsure'
  status: 'applied' | 'shadow' | 'queued' | 'restored' | 'confirmed'
  source: string
  previous_identified_as?: string | null
}

export interface BackgroundSuppressionCluster {
  cluster_signature: string
  segments: BackgroundSuppressionSegment[]
  statuses: Record<string, number>
  zones: Record<string, number>
  max_background_similarity: number
}

export interface BackgroundSuppressionsResponse {
  conversation_id: string
  total: number
  status_counts: Record<string, number>
  clusters: BackgroundSuppressionCluster[]
  subject_override: boolean
}

export interface BackgroundClusterSample {
  clip_key: string
  conversation_id: string
  conversation_title?: string
  segment_index: number
  start: number
  end: number
  text: string
  candidate_type: 'noise' | 'background_speech'
  current_label?: string
  review_role: 'typical' | 'edge'
}

export interface BackgroundCluster {
  cluster_id: string
  candidate_type: 'noise' | 'background_speech'
  conversation_id: string
  conversation_title?: string
  size: number
  member_keys: string[]
  samples: BackgroundClusterSample[]
  known_speaker_fraction?: number
  known_speaker_count?: number
  mean_foreground_confidence?: number
  mean_foreground_similarity?: number
  mean_background_similarity?: number
  suggestion_score?: number
  mined?: 'harvest' | 'novel' | null
}

export type BackgroundClusterLane = 'harvest' | 'novel' | 'similar'
// Review dial: how aggressively the queue surfaces candidates ("more" widens
// lane thresholds / smaller clusters; production suppression is unaffected).
export type BackgroundSurface = 'less' | 'default' | 'more'

export interface BackgroundClustersResponse {
  clusters: BackgroundCluster[]
  indexed: number
  remaining: number
  bucket_sizes: Record<'noise' | 'background_speech', number>
  review_focus?: 'bootstrap' | 'hard_speech' | 'discovery'
  lane?: BackgroundClusterLane | null
  lane_counts?: Partial<Record<BackgroundClusterLane, number>>
  // "quick_confirms" = clusters the system already believes are background
  // (sign-off only); "uncertain" is the number that shrinks as it learns.
  queue_summary?: { unreviewed: number; quick_confirms: number; uncertain: number }
}

export interface BackgroundClusterDecisionResponse {
  review_id: string
  reviewed: number
  exemplars_added: number
  duplicates_covered: number
  decision: 'noise' | 'background_speech' | 'not_background' | 'mixed' | 'dismissed'
}

export interface BackgroundLatestDecision {
  review_id?: string
  decision: 'noise' | 'background_speech' | 'not_background' | 'skip' | 'mixed' | 'dismissed' | null
  reviewed?: number
  reviewed_at?: string
}

export interface BackgroundDecisionHistoryItem {
  review_id: string
  cluster_id: string
  decision: 'noise' | 'background_speech' | 'not_background' | 'skip' | 'mixed' | 'dismissed'
  reviewed: number
  clips_affected: number
  reviewed_at: string
  samples_reconstructed: boolean
  samples: (Omit<BackgroundClusterSample, 'review_role'> & {
    decision?: 'noise' | 'background_speech' | 'not_background'
  })[]
}

export interface BackgroundCleanupSample {
  clip_key: string
  conversation_id: string
  conversation_title?: string
  start: number
  end: number
  text: string
  current_label?: string
  proposed_label: 'Noise' | 'Background Speech'
  background_score: number
  foreground_score: number
  margin: number
  tier: 'high' | 'ambiguous'
}

export interface BackgroundCleanupReport {
  ready: boolean
  reason?: string
  recommendation?: string
  report_id?: string
  reference_counts?: Record<'noise' | 'background_speech' | 'foreground', number>
  high_confidence?: number
  ambiguous?: number
  conversations_affected?: number
  proposed_counts?: Record<'noise' | 'background_speech', number>
  high_samples?: BackgroundCleanupSample[]
  ambiguous_samples?: BackgroundCleanupSample[]
}

export interface BackgroundAccuracyMetric {
  precision: number
  recall: number
  f1: number
  accuracy: number
  confusion: { tp: number; fp: number; fn: number; tn: number }
  samples: number
}

export interface BackgroundAccuracyReport {
  ready: boolean
  reason?: string
  method?: string
  reconstructed_review_samples?: boolean
  reviewed_clusters?: number
  reviewed_samples?: number
  decision_counts?: Record<string, number>
  background_speech_samples?: number
  baseline?: BackgroundAccuracyMetric
  adapted?: BackgroundAccuracyMetric
  f1_change?: number
  learning_curve?: { annotations: number; f1: number; samples: number }[]
  errors?: Array<{
    clip_key: string
    conversation_id: string
    conversation_title?: string
    start: number
    end: number
    text: string
    decision: string
    predicted: 'background' | 'foreground'
  }>
}

export const dataAuditApi = {
  // Enqueue batch VAD analysis. Returns { job_id, status }.
  analyze: (conversationIds?: string[], force: boolean = false) =>
    api.post('/api/data-audit/analyze', {
      conversation_ids: conversationIds ?? null,
      force,
    }),

  // Per-speaker identification-confidence overview (histogram, baselines,
  // noise magnets, recommended threshold). Computed from stored confidence.
  getSpeakerConfidence: () =>
    api.get<SpeakerConfidenceOverview>('/api/data-audit/speakers/confidence'),

  // Poll an analysis job's status (includes in-flight batch progress)
  getJobStatus: (jobId: string) =>
    api.get<{ job_id: string; status: string; batch_progress?: BatchProgress }>(
      `/api/queue/jobs/${jobId}/status`
    ),

  // Fetch a job's full record (status + meta progress + result)
  getJobResult: <T = unknown>(jobId: string) =>
    api.get<{
      status: string
      result: T | null
      meta?: { batch_progress?: BatchProgress }
    }>(`/api/queue/jobs/${jobId}`),

  // Filtered listing with VAD speech metrics + speaker labels.
  // Generic param handling so registry filters (filters.tsx) can contribute
  // params without touching this function: undefined/null and empty arrays
  // are dropped, arrays become comma-separated values.
  getConversations: (params: {
    speech_threshold?: number
    min_speech_fraction?: number
    max_speech_fraction?: number
    min_duration?: number
    max_duration?: number
    created_after?: string
    created_before?: string
    include_speakers?: string[]
    exclude_speakers?: string[]
    archived_only?: boolean
    limit?: number
    offset?: number
    [key: string]: unknown
  }) =>
    api.get<AuditListResponse>('/api/data-audit/conversations', {
      params: Object.fromEntries(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && !(Array.isArray(v) && v.length === 0))
          .map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : v])
      ),
    }),

  // Capture archival is disabled by the backend until a retention policy is available (409).
  archive: (conversationIds: string[], reason: string) =>
    api.post('/api/data-audit/archive', {
      conversation_ids: conversationIds,
      reason,
    }),

  // Detected silence gaps (candidate split points) from cached chunk VAD scores
  getSilenceGaps: (conversationId: string, params?: { speech_threshold?: number; min_gap_seconds?: number }) =>
    api.get<SilenceGapsResponse>(`/api/data-audit/conversations/${conversationId}/silence-gaps`, { params }),

  // Merged speech intervals for speech-skip playback
  getSpeechRegions: (conversationId: string, speakers?: string[]) =>
    api.get<SpeechRegionsResponse>(
      `/api/data-audit/conversations/${conversationId}/speech-regions`,
      { params: speakers?.length ? { speakers: speakers.join(',') } : undefined }
    ),

  // Active-version transcript segments (with speaker-recognition confidence)
  // for the speaker-triage panel.
  getSegments: (conversationId: string) =>
    api.get<AuditSegmentsResponse>(
      `/api/data-audit/conversations/${conversationId}/segments`
    ),

  // Live speaker suggestion for one segment: closest enrolled speaker + cosine.
  identifySegment: (conversationId: string, start: number, end: number) =>
    api.post<SegmentIdentifyResponse>(
      `/api/data-audit/conversations/${conversationId}/segments/identify`,
      { start, end }
    ),

  getNextSpeakerLabelReviews: (batchSize = 5) =>
    api.get<SpeakerLabelReviewBatch>('/api/data-audit/speaker-label-reviews/next', {
      params: { batch_size: batchSize },
    }),

  getSpeakerLabelReviewMetrics: () =>
    api.get<SpeakerLabelReviewMetrics>('/api/data-audit/speaker-label-reviews/metrics'),

  decideSpeakerLabelReviews: (
    decisions: Array<{
      conversation_id: string
      segment_index: number
      segment_start_time: number
      claimed_speaker: string
      confidence: number | null
      selection_lane: 'boundary' | 'control'
      verdict: SpeakerLabelReviewVerdict
      corrected_speaker?: string
    }>
  ) =>
    api.post<{
      status: 'ok' | 'partial'
      recorded: number
      corrections_pending: number
      errors: Array<{ error: string }>
    }>('/api/data-audit/speaker-label-reviews/decide', { decisions }),

  // Guided enrollment: next batch of highest-information clips for a speaker.
  guidedEnrollmentSuggest: (
    speakerName: string,
    order: 'informative' | 'confidence' = 'informative'
  ) =>
    api.post<GuidedEnrollmentSuggestResponse>(
      '/api/data-audit/enrollment/guided/suggest',
      { speaker_name: speakerName, batch_size: 5, order }
    ),

  // Guided enrollment: record accept/reject decisions; accepted clips enroll.
  guidedEnrollmentDecide: (
    speakerName: string,
    decisions: {
      conversation_id: string
      start: number
      end: number
      original_start: number
      original_end: number
      decision: 'accept' | 'reject' | 'skip' | 'bad_clip' | 'multiple_speakers' | 'another_speaker'
      actual_speaker?: string
      scores?: GuidedEnrollmentClip['scores']
    }[]
  ) =>
    api.post<GuidedEnrollmentDecideResponse>(
      '/api/data-audit/enrollment/guided/decide',
      { speaker_name: speakerName, decisions }
    ),

  guidedEnrollmentHistory: (speakerName: string) =>
    api.get<{ speaker_name: string; sessions: GuidedEnrollmentSession[] }>(
      '/api/data-audit/enrollment/guided/history',
      { params: { speaker_name: speakerName } }
    ),

  discoverSpeakerCorpus: (speakerName: string, includeDeleted = false) =>
    api.post<{ job_id: string; status: string; reused: boolean }>(
      '/api/data-audit/enrollment/guided/discover',
      { speaker_name: speakerName, batch_size: 5, include_deleted: includeDeleted }
    ),

  discoverUnknownSpeakers: () =>
    api.post<{ job_id: string; status: string; reused: boolean }>(
      '/api/data-audit/enrollment/unknown/discover'
    ),

  getUnknownSpeakerClusters: () =>
    api.get<{ clusters: UnknownSpeakerCluster[] }>(
      '/api/data-audit/enrollment/unknown/clusters'
    ),

  decideUnknownSpeakerCluster: (
    cluster: UnknownSpeakerCluster,
    action: 'confirm' | 'dismiss',
    speakerName: string | null,
    acceptedIdentityKeys: string[],
    enrollmentClips: Array<{ identity_key: string; segment_index: number }>
  ) =>
    api.post(`/api/data-audit/enrollment/unknown/clusters/${cluster.cluster_id}/decide`, {
      run_fingerprint: cluster.run_fingerprint,
      action,
      speaker_name: speakerName,
      accepted_identity_keys: acceptedIdentityKeys,
      enrollment_clips: enrollmentClips,
    }),

  // Upload an unlabelled audio corpus and mine it for one speaker: files become
  // annotation-only conversations; discovery is chained after transcription.
  mineCorpusAudio: (speakerName: string, files: File[]) => {
    const form = new FormData()
    form.append('speaker_name', speakerName)
    files.forEach((f) => form.append('files', f))
    return api.post<{
      speaker_name: string
      ingested: number
      failed: { filename: string; error: string }[]
      transcription_jobs: number
      transcription_available: boolean
      discovery_job_id: string | null
    }>('/api/data-audit/enrollment/guided/mine', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  getSpeakerCorpusDiscovery: (speakerName: string) =>
    api.get<{
      speaker_name: string
      job_id: string | null
      status: string | null
      matched_segments: number
    }>('/api/data-audit/enrollment/guided/discover', {
      params: { speaker_name: speakerName },
    }),

  runSpeakerBenchmark: () =>
    api.post<{ job_id: string; status: string }>('/api/data-audit/enrollment/benchmark'),

  getLatestSpeakerBenchmark: () =>
    api.get<{ report: SpeakerBenchmarkReport | null }>(
      '/api/data-audit/enrollment/benchmark/latest'
    ),

  getSpeakerGalleryBaseline: () =>
    api.get<SpeakerGalleryBaseline>('/api/data-audit/enrollment/baseline'),

  // Enrolled clips for one speaker with per-clip contamination flags.
  getEnrollmentGallery: (speakerName: string) =>
    api.get<GuidedEnrollmentGalleryResponse>(
      '/api/data-audit/enrollment/guided/gallery',
      { params: { speaker_name: speakerName } }
    ),

  // Remove one enrolled clip from the voiceprint (quarantined by default).
  deleteEnrollmentGalleryClip: (speakerName: string, segmentId: number, hard = false) =>
    api.post(
      `/api/data-audit/enrollment/guided/gallery/segments/${segmentId}/delete`,
      { speaker_name: speakerName, hard }
    ),

  // Forget all guided-enrollment state for a speaker name (reviews, history,
  // discovery); optionally also purge the voiceprint gallery.
  resetGuidedEnrollment: (speakerName: string, purgeGallery = false) =>
    api.post<GuidedEnrollmentResetResponse>(
      '/api/data-audit/enrollment/guided/reset',
      { speaker_name: speakerName, purge_gallery: purgeGallery }
    ),

  // Count of unapplied triage decisions and conversations they span.
  getTriagePending: () =>
    api.get<{ pending_count: number; conversation_count: number }>(
      '/api/data-audit/triage/pending'
    ),

  // Bulk-apply all pending speaker-triage decisions across every conversation.
  applyTriage: () =>
    api.post<TriageApplyResponse>('/api/data-audit/triage/apply'),

  // Rank a conversation's unknown segments as "potentially background" by
  // similarity to the confirmed-background bucket + low SNR.
  backgroundSuggest: (conversationId: string, limit = 10) =>
    api.get<BackgroundSuggestResponse>('/api/data-audit/background/suggest', {
      params: { conversation_id: conversationId, limit },
    }),

  // Corpus-wide "potentially background" feed across recent conversations.
  backgroundScan: (limit = 40, maxConversations = 8) =>
    api.get<BackgroundScanResponse>('/api/data-audit/background/scan', {
      params: { limit, max_conversations: maxConversations },
    }),

  backgroundAdd: (
    conversationId: string,
    start: number,
    end: number,
    bucketType: 'noise' | 'background_speech'
  ) =>
    api.post('/api/data-audit/background/add', {
      conversation_id: conversationId,
      start,
      end,
      bucket_type: bucketType,
      source: 'review',
    }),

  startBackgroundIndex: () =>
    api.post<{ job_id: string; status: string; reused: boolean }>(
      '/api/data-audit/background/index'
    ),

  getBackgroundIndex: () =>
    api.get<{ job_id: string | null; status: string | null; indexed: number }>(
      '/api/data-audit/background/index'
    ),

  getBackgroundClusters: (
    limit = 6,
    samplesPerCluster = 5,
    lane?: BackgroundClusterLane,
    surface: BackgroundSurface = 'default'
  ) =>
    api.get<BackgroundClustersResponse>('/api/data-audit/background/clusters', {
      params: { limit, samples_per_cluster: samplesPerCluster, lane, surface },
    }),

  decideBackgroundCluster: (
    cluster: BackgroundCluster,
    decision: 'noise' | 'background_speech' | 'not_background' | 'mixed' | 'dismissed',
    sampleDecisions: Record<string, 'noise' | 'background_speech' | 'not_background'> = {}
  ) =>
    api.post<BackgroundClusterDecisionResponse>('/api/data-audit/background/clusters/decide', {
      cluster_id: cluster.cluster_id,
      member_keys: cluster.member_keys,
      review_sample_keys: cluster.samples.map((sample) => sample.clip_key),
      decision,
      sample_decisions: sampleDecisions,
    }),

  getLatestBackgroundDecision: () =>
    api.get<BackgroundLatestDecision>('/api/data-audit/background/clusters/latest-decision'),

  getBackgroundDecisionHistory: (limit = 50) =>
    api.get<{ decisions: BackgroundDecisionHistoryItem[] }>(
      '/api/data-audit/background/clusters/decisions',
      { params: { limit } }
    ),

  undoBackgroundDecision: (reviewId: string) =>
    api.delete<{ undone: boolean; clips_restored: number; references_removed: number }>(
      `/api/data-audit/background/clusters/decisions/${reviewId}`
    ),

  getBackgroundCleanupReport: () =>
    api.get<BackgroundCleanupReport>('/api/data-audit/background/cleanup/report'),

  getBackgroundAccuracyReport: () =>
    api.get<BackgroundAccuracyReport>('/api/data-audit/background/accuracy/report'),

  applyBackgroundCleanup: (reportId: string) =>
    api.post<{ job_id: string; status: string; report_id: string }>(
      '/api/data-audit/background/cleanup/apply',
      { report_id: reportId }
    ),

  // Change a past cluster review's verdict in place (annotation-history edit).
  editBackgroundDecision: (
    reviewId: string,
    decision: 'noise' | 'background_speech' | 'not_background' | 'dismissed'
  ) =>
    api.post(`/api/data-audit/background/clusters/decisions/${reviewId}/edit`, {
      decision,
    }),

  // Suppression ledger: what was marked background in one conversation.
  backgroundSuppressions: (conversationId: string) =>
    api.get<BackgroundSuppressionsResponse>(
      `/api/data-audit/background/suppressions/${conversationId}`
    ),

  backgroundSuppressionDecide: (
    conversationId: string,
    clusterSignature: string,
    decision: 'restore' | 'confirm'
  ) =>
    api.post('/api/data-audit/background/suppressions/decide', {
      conversation_id: conversationId,
      cluster_signature: clusterSignature,
      decision,
    }),

  // Split a conversation at the given time points (seconds)
  split: (conversationId: string, splitPoints: number[]) =>
    api.post<SplitResponse>(`/api/data-audit/conversations/${conversationId}/split`, {
      split_points: splitPoints,
    }),

  // Merge adjacent conversations into a new one
  merge: (conversationIds: string[]) =>
    api.post<MergeResponse>('/api/data-audit/merge', {
      conversation_ids: conversationIds,
    }),

  // Default shareability policy (prefill for the privacy-screen editor)
  getSensitivityPolicy: () =>
    api.get<{ policy: string }>('/api/data-audit/sensitivity-policy'),

  // Enqueue a privacy screen over the selected conversations.
  // Returns { job_id, status }; fetch the result via getJobResult<ScreenResult>.
  screenExport: (conversationIds: string[], policy?: string) =>
    api.post<{ job_id: string; status: string }>('/api/data-audit/export/screen', {
      conversation_ids: conversationIds,
      policy: policy ?? null,
    }),

  // Dry-run of the export: the exact clips (boundaries + sliced transcripts)
  // the current settings would produce, without writing any audio. Runs the
  // same plan computation as the export job, so what it shows is what ships.
  previewExport: (
    conversationIds: string[],
    params?: {
      mode?: 'clips' | 'full'
      pad_seconds?: number
      speech_threshold?: number
      merge_gap_seconds?: number
      excluded_ranges?: Record<string, number[][]>
    }
  ) =>
    api.post<ExportPreviewResult>('/api/data-audit/export/preview', {
      conversation_ids: conversationIds,
      ...params,
    }),

  // Enqueue an annotation-dataset export (speech-cropped clips + manifest).
  // `excluded_ranges` (conversation_id → withheld [start,end] ranges from the
  // privacy screen) and `dropped_ranges` (clips unticked in the preview) are
  // carved out of the exported audio + transcript.
  // Returns { job_id, export_id, status }.
  startExport: (
    conversationIds: string[],
    params?: {
      mode?: 'clips' | 'full'
      pad_seconds?: number
      speech_threshold?: number
      merge_gap_seconds?: number
      excluded_ranges?: Record<string, number[][]>
      dropped_ranges?: Record<string, number[][]>
      sensitivity_policy?: string | null
    }
  ) =>
    api.post<{ job_id: string; export_id: string; status: string }>('/api/data-audit/export', {
      conversation_ids: conversationIds,
      ...params,
    }),

  // Import an export-compatible ZIP as memory-excluded conversations with the
  // manifest transcripts already active in the editor.
  importDataset: (dataset: FormData, onProgress?: (progress: number) => void) =>
    api.post<AnnotationImportResponse>('/api/data-audit/import', dataset, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
      onUploadProgress: (progressEvent) => {
        if (onProgress && progressEvent.total) {
          onProgress(Math.round((progressEvent.loaded * 100) / progressEvent.total))
        }
      },
    }),

  // List completed annotation exports
  listExports: () => api.get<{ exports: ExportRecord[] }>('/api/data-audit/exports'),

  // Direct download URL (token as query param so the browser streams to disk)
  exportDownloadUrl: (exportId: string) => {
    const token = localStorage.getItem(getStorageKey('token')) || ''
    return `${BACKEND_URL}/api/data-audit/exports/${exportId}/download?token=${token}`
  },

  // Delete an export (zip + metadata) from the server
  deleteExport: (exportId: string) => api.delete(`/api/data-audit/exports/${exportId}`),
}

// Wake-word data-collection (the "Hermes" training flywheel). Proxied through
// the backend to the standalone wakeword-service.
export interface WakeStream {
  client_id: string
  priming: boolean
  prime_wakeword?: string | null
  armed: boolean
}

// Per-word config the service runs (one section per word in the Wake-Word Lab).
export interface WakeWordConfig {
  name: string
  model: string
  verifier: boolean          // a verifier file is loaded for this word (capability)
  verifier_enabled: boolean  // and it is currently consulted (the runtime toggle)
  threshold: number
  patience: number
  collect_only: boolean
}

export interface WakeStats {
  pending: number
  positive: number
  negative: number
  false_negatives: number
}

export interface WakeSample {
  id: string
  wakeword: string
  bucket: string
  client_id: string
  session_id: string
  score: number
  reason: string
  kind: string
  source: string
  also_fired?: string[]
  sample_rate: number
  created_at_ms: number
  duration_secs: number
  label?: string
  false_negative?: boolean
}

export const wakewordApi = {
  // Configured wake words (+ per-word config). available/active kept for the
  // acoustic-condition picker; wakewords drives the Lab's per-word sections.
  getModels: () =>
    api.get<{ available: string[]; active: string | null; wakewords: WakeWordConfig[] }>(
      '/api/wakeword/models'
    ),
  getStreams: () => api.get<{ streams: WakeStream[] }>('/api/wakeword/streams'),
  // Toggle a word between normal dispatch and collect-only (shadow) mode. Admin
  // only; effective live and persisted across restarts. Returns refreshed config.
  setCollectOnly: (wakeword: string, collect_only: boolean) =>
    api.post<{ wakewords: WakeWordConfig[] }>('/api/wakeword/collect_only', {
      wakeword,
      collect_only,
    }),
  // Toggle a word's second-stage verifier on/off. Admin only; the verifier stays
  // loaded (disabling falls back to stage-1). Effective live and persisted.
  setVerifierEnabled: (wakeword: string, enabled: boolean) =>
    api.post<{ wakewords: WakeWordConfig[] }>('/api/wakeword/verifier_enabled', {
      wakeword,
      enabled,
    }),
  // Enroll a specific word. No client_id -> backend primes the caller's recorder.
  prime: (wakeword: string, client_id?: string) =>
    api.post('/api/wakeword/prime', { wakeword, ...(client_id ? { client_id } : {}) }),
  // Manually end an in-progress prime; the captured attempt is saved to pending.
  unprime: (client_id?: string) =>
    api.post('/api/wakeword/unprime', client_id ? { client_id } : {}),
  getSamples: (wakeword: string, bucket: 'pending' | 'positive' | 'negative') =>
    api.get<{ wakeword: string; bucket: string; samples: WakeSample[] }>(
      '/api/wakeword/samples',
      { params: { wakeword, bucket } }
    ),
  // Per-word stats: { "hey_hermes": {pending, positive, negative, false_negatives}, ... }
  getStats: () => api.get<Record<string, WakeStats>>('/api/wakeword/samples/stats'),
  // Remove exact-duplicate clips within a wake word (keeps one per group).
  dedupe: (wakeword: string) =>
    api.post<Record<string, { duplicate_groups: number; removed: number; removed_by_bucket: Record<string, number>; kept_unique: number; conflicts: number }>>(
      '/api/wakeword/samples/dedupe', null, { params: { wakeword } }
    ),
  getAudioBlob: (id: string) =>
    api.get(`/api/wakeword/samples/${encodeURIComponent(id)}/audio`, {
      responseType: 'blob',
    }),
  label: (id: string, label: 'wake' | 'not_wake') =>
    api.post(`/api/wakeword/samples/${encodeURIComponent(id)}/label`, { label }),
  // Move a clip to a different wake word's bucket (default pending) — for the
  // overlap case (a bare "hermes" that armed hey_hermes by priority).
  move: (id: string, wakeword: string, bucket: 'pending' | 'positive' | 'negative' = 'pending') =>
    api.post(`/api/wakeword/samples/${encodeURIComponent(id)}/move`, null, {
      params: { wakeword, bucket },
    }),
  // Copy a clip into another word's bucket (source stays) — a shared FP that fired
  // multiple words is a hard negative for each.
  copy: (id: string, wakeword: string, bucket: 'pending' | 'positive' | 'negative' = 'pending') =>
    api.post(`/api/wakeword/samples/${encodeURIComponent(id)}/copy`, null, {
      params: { wakeword, bucket },
    }),
  remove: (id: string) => api.delete(`/api/wakeword/samples/${encodeURIComponent(id)}`),
}

export interface PhotoExplorationDetails {
  coverage: { inventory_count: number; inspected_count: number; observed_count: number; unseen_count: number; image_views: number; round_count: number; stop_reason: string; failures: Record<string, string> }
  rounds: Array<{ round: number; action: string; offered: string[]; question: string; request: { action: string; question: string } }>
}
