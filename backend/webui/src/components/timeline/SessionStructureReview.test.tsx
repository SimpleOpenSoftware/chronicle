// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DayReviewProjection, TimelineEpisode } from '../../services/api'
import SessionStructureReview from './SessionStructureReview'
afterEach(cleanup)
const mediaEpisode = {
  episode_id: 'media-1',
  episode_key: 'media-key',
  revision: 1,
  started_at: '2026-02-19T04:00:00.000Z',
  ended_at: '2026-02-19T05:00:00.000Z',
  kind: 'media',
  title: 'Watching YouTube',
  summary: 'A long-form video was playing.',
  status: 'provisional',
  confirmed_at: null,
  confirmed_fields: ['title', 'kind'],
  requires_activity_review: true, memory_eligible: true, memory_policy: 'auto',
  salience: 'routine',
  confidence: 0.91,
  activity_mode: 'foreground',
  entities: [],
  attributes: {},
  assertions: [],
  evidence: [
    {
      evidence_id: 'screen-1',
      kind: 'observation',
      source_id: 'screenpipe-laptop',
      source_item_id: 'observation-1',
      started_at: '2026-02-19T04:00:00.000Z',
      ended_at: '2026-02-19T04:15:00.000Z',
      role: 'application_state',
      excerpt: 'YouTube was visible.',
      ephemeral: false,
      locator: { capture_source_id: 'screenpipe-laptop', modality: 'screen', track_id: 'display-1' },
      start_boundary_support: [],
      end_boundary_support: [],
      metadata: {},
    },
    {
      evidence_id: 'audio-1',
      kind: 'audio_span',
      source_id: 'screenpipe-laptop',
      source_item_id: 'audio-1',
      started_at: '2026-02-19T04:45:00.000Z',
      ended_at: '2026-02-19T05:00:00.000Z',
      role: 'media_content',
      excerpt: 'Video audio.',
      ephemeral: false,
      locator: { capture_source_id: 'screenpipe-laptop', modality: 'audio', track_id: 'output' },
      start_boundary_support: [],
      end_boundary_support: [],
      metadata: {},
    },
  ],
  related_episode_ids: [],
  related_conversation_ids: [],
  audio_ranges: [],
  parent_episode_id: null,
  has_thumbnail: false,
} satisfies TimelineEpisode

const conversationEpisode = {
  ...mediaEpisode,
  episode_id: 'conversation-1',
  episode_key: 'conversation-key',
  kind: 'communication',
  title: 'Planning Chronicle',
  summary: 'A project discussion.',
  started_at: '2026-02-19T06:00:00.000Z',
  ended_at: '2026-02-19T06:30:00.000Z',
} satisfies TimelineEpisode


it('shows evidence for the selected interval and confirms only unsettled session members', () => {
  const members = [{ ...mediaEpisode, evidence: [...mediaEpisode.evidence, { ...mediaEpisode.evidence[0], evidence_id: 'photo-1', kind: 'immich' as const, excerpt: 'A photo of a park.' }] }, conversationEpisode]
  const projection = { version: 'test', day_started_at: mediaEpisode.started_at, day_ended_at: conversationEpisode.ended_at,
    episode_count: 2, group_count: 1, needs_attention_count: 0, confirmed_count: 1,
    groups: [{ group_id: 'accepted', title: 'Playback across devices', summary: 'One activity captured twice', semantic: true, lane: 'foreground',
      started_at: mediaEpisode.started_at, ended_at: conversationEpisode.ended_at, episode_ids: members.map(e => e.episode_id), episode_count: 2,
      conversational_count: 1, confirmed_count: 1, duration_seconds: 100, span_seconds: 100, gap_seconds: 0,
      intervals: members.map(e => ({ episode_id: e.episode_id, started_at: e.started_at, ended_at: e.ended_at, lane: 'foreground' as const })),
      entities: [], salience: 'routine', attention_reasons: [], needs_attention: false }],
  } satisfies DayReviewProjection
  const confirm = vi.fn(), edit = vi.fn(), reject = vi.fn()
  render(<QueryClientProvider client={new QueryClient()}><SessionStructureReview projection={projection} episodes={members} unstableEpisodes={[members[0]]} timezone="Asia/Kolkata" onConfirm={confirm} onEdit={edit} onNotActivity={reject} /></QueryClientProvider>)
  fireEvent.click(screen.getByText('Why these episodes were grouped'))
  expect(screen.getByText('One activity captured twice')).toBeVisible()
  expect(screen.getByText(/Accepted grouping/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Screen OCR' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Photo' }))
  expect(screen.getByText('A photo of a park.', { selector: 'p.mt-1' })).toBeVisible()
  expect(screen.getByText(/Photo description/)).toBeVisible()
  expect(screen.queryByText('YouTube was visible.')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Screen OCR' }))
  expect(screen.getByText('YouTube was visible.', { selector: 'p.mt-1' })).toBeVisible()
  const rows = screen.getByLabelText('Episode intervals for Playback across devices')
  fireEvent.click(within(rows).getAllByRole('button')[1])
  expect(within(screen.getByLabelText('Selected episode evidence')).getByRole('heading')).toHaveTextContent(conversationEpisode.title)
  fireEvent.click(screen.getByRole('button', { name: 'Edit episode 2' }))
  expect(edit).toHaveBeenCalledWith(conversationEpisode)
  fireEvent.click(screen.getByRole('button', { name: 'Not an activity' }))
  expect(reject).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Confirm session (1)' })).toBeDisabled()
  expect(screen.getByText(/Recordings stay/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Remove episode' }))
  expect(reject).toHaveBeenCalledWith(conversationEpisode)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm session (1)' }))
  expect(confirm).toHaveBeenCalledWith('accepted', [members[0]])
  expect(screen.queryByText(/Revision 2/)).not.toBeInTheDocument()
})
