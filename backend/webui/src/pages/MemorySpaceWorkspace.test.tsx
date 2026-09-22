// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { memorySpacesApi } from '../services/api'
import MemorySpaceWorkspace from './MemorySpaceWorkspace'

vi.mock('./LiveRecord', () => ({
  default: ({ memorySpaceId, destinationLabel }: { memorySpaceId?: string; destinationLabel?: string }) => (
    <div data-testid="scoped-recorder">{memorySpaceId}:{destinationLabel}</div>
  ),
}))

const SPACE_ID = '9f3523c8-af75-469d-995a-7179531f3fc8'

function renderWorkspace(path = `/spaces/${SPACE_ID}/record`) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path="/spaces/:spaceId/:tab?" element={<MemorySpaceWorkspace />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MemorySpaceWorkspace scope', () => {
  it('keeps the seal and recording destination visible before capture', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Launch brainstorm',
        state: 'active',
        seed_notes: [],
        sync_state: 'healthy',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-29T00:00:00Z',
        updated_at: '2026-08-29T00:00:00Z',
        archived_at: null,
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({
      data: { conversations: [], total: 0 },
    } as never)

    renderWorkspace()

    expect(await screen.findByText('Main sealed')).toBeVisible()
    expect(screen.getByText('Recordings land here')).toBeVisible()
    expect(screen.getByTestId('scoped-recorder')).toHaveTextContent(`${SPACE_ID}:Launch brainstorm`)
    expect(screen.getByRole('link', { name: 'Notes' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Chat' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Merge' })).toBeVisible()
  })

  it('shows an archived vault as read-only with an explicit reopen action', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Past cycle',
        state: 'archived',
        seed_notes: [],
        sync_state: 'frozen',
        sync_error: null,
        merge_checkpoint: 'hash',
        created_at: '2026-08-29T00:00:00Z',
        updated_at: '2026-08-29T00:00:00Z',
        archived_at: '2026-08-29T01:00:00Z',
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({ data: { conversations: [] } } as never)
    vi.spyOn(memorySpacesApi, 'notes').mockResolvedValue({ data: [] } as never)

    renderWorkspace(`/spaces/${SPACE_ID}/notes`)

    expect(await screen.findByRole('button', { name: 'Reopen new cycle' })).toBeVisible()
    expect(screen.getByText(/Archived spaces are read-only/i)).toBeVisible()
  })

  it('loads authenticated audio on demand for a space recording', async () => {
    const conversationId = 'conversation-audio'
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Playback notebook',
        state: 'active',
        seed_notes: [],
        sync_state: 'healthy',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-30T05:00:00Z',
        updated_at: '2026-08-30T05:00:00Z',
        archived_at: null,
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({
      data: { conversations: [{ conversation_id: conversationId, title: 'Cat interruption', processing_status: 'completed', memory_review_state: 'automatic' }] },
    } as never)
    const recordingAudio = vi.spyOn(memorySpacesApi, 'recordingAudio').mockResolvedValue({
      data: new Blob(['audio'], { type: 'audio/ogg' }),
    } as never)
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:recording'), revokeObjectURL: vi.fn() })

    renderWorkspace()

    fireEvent.click(await screen.findByRole('button', { name: 'Load recording' }))
    expect(await screen.findByLabelText('Playback for Cat interruption')).toHaveAttribute('src', 'blob:recording')
    expect(recordingAudio).toHaveBeenCalledWith(conversationId)
  })

  it('sends only checked ScreenPipe frames into explicit note extraction', async () => {
    const conversationId = 'conversation-1'
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Rainbow ramble',
        state: 'active',
        seed_notes: [],
        sync_state: 'healthy',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-30T05:00:00Z',
        updated_at: '2026-08-30T05:00:00Z',
        archived_at: null,
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'recordings').mockResolvedValue({
      data: { conversations: [{ conversation_id: conversationId, title: 'Career notebook', processing_status: 'completed', memory_review_state: 'ready' }] },
    } as never)
    vi.spyOn(memorySpacesApi, 'noteReview').mockResolvedValue({
      data: {
        conversation_id: conversationId,
        title: 'Career notebook',
        transcript: 'Let me show you the four aspects in my notebook.',
        created_at: '2026-08-30T05:15:00Z',
        started_at: '2026-08-30T05:15:00Z',
        ended_at: '2026-08-30T05:18:00Z',
        review_state: 'ready',
        review_error: null,
        selected_frame_keys: [],
        context_description: null,
        sources: [{ source_id: 'screenpipe-rainbow', name: 'Rainbow', platform: 'linux', status: 'online', last_seen_at: '2026-08-30T05:19:00Z', health: {} }],
        jobs: [],
        frames: [
          { key: 'screenpipe-rainbow:41', source_id: 'screenpipe-rainbow', frame_id: 41, captured_at: '2026-08-30T05:16:00Z', content_type: 'image/jpeg' },
          { key: 'screenpipe-rainbow:42', source_id: 'screenpipe-rainbow', frame_id: 42, captured_at: '2026-08-30T05:17:00Z', content_type: 'image/jpeg' },
        ],
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'noteReviewFrame').mockResolvedValue({ data: new Blob(['frame'], { type: 'image/jpeg' }) } as never)
    vi.spyOn(memorySpacesApi, 'notes').mockResolvedValue({ data: [] } as never)
    const extract = vi.spyOn(memorySpacesApi, 'extractReviewedNote').mockResolvedValue({ data: {} } as never)
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:frame'), revokeObjectURL: vi.fn() })

    renderWorkspace()

    expect(await screen.findByText('Choose what the note extractor can see')).toBeVisible()
    expect(screen.getByText(/Only checked frames are sent as pixels/i)).toBeVisible()
    const checks = screen.getAllByRole('checkbox')
    fireEvent.click(checks[1])
    fireEvent.click(screen.getByRole('button', { name: 'Extract note with 1 image' }))

    await waitFor(() => expect(extract).toHaveBeenCalledWith(
      SPACE_ID,
      conversationId,
      ['screenpipe-rainbow:42'],
    ))
    // Let the mutation's post-success refetches settle before afterEach restores
    // the API spies; otherwise an in-flight query can fall through to localhost.
    await waitFor(() => {
      expect(memorySpacesApi.noteReview).toHaveBeenCalledTimes(2)
      expect(memorySpacesApi.recordings).toHaveBeenCalledTimes(2)
    })
  })

  it('recovers a persisted proposal while the prepare request is still pending', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Slow review',
        state: 'active',
        seed_notes: [],
        sync_state: 'healthy',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-30T16:45:00Z',
        updated_at: '2026-08-30T16:45:00Z',
        archived_at: null,
      },
    } as never)
    vi.spyOn(memorySpacesApi, 'prepareMerge').mockReturnValue(new Promise(() => {}))
    const latest = vi.spyOn(memorySpacesApi, 'latestMergeProposal').mockResolvedValue({
      data: {
        proposal_id: 'proposal-1',
        space_id: SPACE_ID,
        state: 'pending',
        changes: [{
          change_id: 'change-1',
          note_path: 'Topics/Slow review.md',
          operation: 'create',
          before_hash: null,
          before_text: null,
          after_text: '# Slow review',
          conflict: null,
          source_refs: [],
          validation_findings: [],
        }],
        accepted_change_ids: [],
        rejected_change_ids: [],
        deferred_event_count: 0,
        error: null,
      },
    } as never)

    renderWorkspace(`/spaces/${SPACE_ID}/merge`)

    fireEvent.click(await screen.findByRole('button', { name: 'Prepare merge' }))

    expect(await screen.findByText('Topics/Slow review.md')).toBeVisible()
    expect(latest).toHaveBeenCalledWith(SPACE_ID)
  })

  it('returns a pending proposal to editing without publishing it', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({
      data: {
        space_id: SPACE_ID,
        name: 'Sync before review',
        state: 'merging',
        seed_notes: [],
        sync_state: 'unpaired',
        sync_error: null,
        merge_checkpoint: null,
        created_at: '2026-08-30T17:00:00Z',
        updated_at: '2026-08-30T17:00:00Z',
        archived_at: null,
      },
    } as never)
    const pending = {
      proposal_id: 'proposal-cancel',
      space_id: SPACE_ID,
      state: 'pending',
      changes: [],
      accepted_change_ids: [],
      rejected_change_ids: [],
      deferred_event_count: 0,
      error: null,
    }
    vi.spyOn(memorySpacesApi, 'latestMergeProposal').mockResolvedValue({ data: pending } as never)
    const cancel = vi.spyOn(memorySpacesApi, 'cancelMerge').mockResolvedValue({
      data: { ...pending, state: 'cancelled' },
    } as never)
    const resolve = vi.spyOn(memorySpacesApi, 'resolveMerge')

    renderWorkspace(`/spaces/${SPACE_ID}/merge`)

    fireEvent.click(await screen.findByRole('button', { name: 'Return to editing' }))

    await waitFor(() => expect(cancel).toHaveBeenCalledWith('proposal-cancel'))
    expect(resolve).not.toHaveBeenCalled()
  })
})

describe('MemorySpaceWorkspace request recovery', () => {
  const activeSpace = { space_id: SPACE_ID, name: 'Notebook', state: 'active', sync_state: 'healthy', seed_notes: [] }

  it('keeps a failed notes request distinct from an empty notebook and retries it', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockResolvedValue({ data: activeSpace } as never)
    vi.spyOn(memorySpacesApi, 'notes').mockRejectedValueOnce(new Error('Offline')).mockResolvedValue({ data: [{ note_path: 'Notes/Idea.md', content: '# Idea', updated_at: '2026-09-13T00:00:00Z' }] } as never)
    renderWorkspace(`/spaces/${SPACE_ID}/notes`)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load notes.')
    expect(screen.queryByText('No notes yet.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: 'Notes/Idea.md' })).toBeVisible()
  })

  it('does not claim a space is missing when its request fails', async () => {
    vi.spyOn(memorySpacesApi, 'get').mockRejectedValueOnce({ response: { status: 503 } }).mockResolvedValue({ data: activeSpace } as never)
    vi.spyOn(memorySpacesApi, 'notes').mockResolvedValue({ data: [] } as never)
    renderWorkspace(`/spaces/${SPACE_ID}/notes`)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load this memory space.')
    expect(screen.queryByText('Memory space not found.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Notebook' })).toBeVisible()
  })
})
