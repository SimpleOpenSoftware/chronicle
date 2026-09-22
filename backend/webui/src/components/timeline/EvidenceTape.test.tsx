// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DayReviewProjection, TimelineEpisode } from '../../services/api'
import EvidenceTape, { evidenceChannel } from './EvidenceTape'

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
  has_thumbnail: true,
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

const projection = {
  version: 'test',
  day_started_at: '2026-02-19T00:00:00.000Z',
  day_ended_at: '2026-02-20T00:00:00.000Z',
  episode_count: 2,
  group_count: 2,
  needs_attention_count: 0,
  confirmed_count: 0,
  groups: [
    {
      group_id: 'media-group',
      started_at: mediaEpisode.started_at,
      ended_at: mediaEpisode.ended_at,
      title: mediaEpisode.title,
      summary: '',
      semantic: false,
      lane: 'foreground',
      episode_ids: [mediaEpisode.episode_id],
      episode_count: 1,
      conversational_count: 0,
      confirmed_count: 0,
      duration_seconds: 3600,
      span_seconds: 3600,
      gap_seconds: 0,
      intervals: [{ lane: 'foreground', episode_id: mediaEpisode.episode_id, started_at: mediaEpisode.started_at, ended_at: mediaEpisode.ended_at }],
      entities: [],
      salience: 'routine',
      attention_reasons: [],
      needs_attention: false,
    },
    {
      group_id: 'conversation-group',
      started_at: conversationEpisode.started_at,
      ended_at: conversationEpisode.ended_at,
      title: conversationEpisode.title,
      summary: '',
      semantic: false,
      lane: 'conversation',
      episode_ids: [conversationEpisode.episode_id],
      episode_count: 1,
      conversational_count: 1,
      confirmed_count: 0,
      duration_seconds: 1800,
      span_seconds: 1800,
      gap_seconds: 0,
      intervals: [{ lane: 'conversation', episode_id: conversationEpisode.episode_id, started_at: conversationEpisode.started_at, ended_at: conversationEpisode.ended_at }],
      entities: [],
      salience: 'routine',
      attention_reasons: [],
      needs_attention: false,
    },
  ],
} satisfies DayReviewProjection

describe('EvidenceTape', () => {
  it('keeps conversation and background lanes inside a foreground semantic group', () => {
    const background = { ...mediaEpisode, episode_id: 'ambient-1', kind: 'ambient_audio', activity_mode: 'ambient' as const }
    const merged = { ...projection, groups: [{ ...projection.groups[0], semantic: true,
      episode_ids: [background.episode_id, conversationEpisode.episode_id],
      intervals: [
        { lane: 'background' as const, episode_id: background.episode_id, started_at: background.started_at, ended_at: background.ended_at },
        { lane: 'conversation' as const, episode_id: conversationEpisode.episode_id, started_at: conversationEpisode.started_at, ended_at: conversationEpisode.ended_at },
      ],
    }] }
    render(<EvidenceTape coverage={[]} projection={merged} episodes={[background, conversationEpisode]} lens="conversation" selectedEpisodeId={null} onLensChange={vi.fn()} onSelectEpisode={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Conversation 1' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Background 1' })).toBeVisible()
    expect(screen.getAllByRole('button', { name: /Conversation overlap lane/ })[0]).toHaveClass('opacity-100')
    expect(screen.getAllByRole('button', { name: /Background overlap lane/ })[0]).toHaveClass('opacity-15')
  })

  it('identifies media without relying on color and synchronizes selection and lenses', () => {
    const onSelectEpisode = vi.fn()
    const onLensChange = vi.fn()
    render(
      <EvidenceTape
        projection={projection}
        episodes={[mediaEpisode, conversationEpisode]}
        coverage={[{
          started_at: '2026-02-19T08:00:00.000Z',
          ended_at: '2026-02-19T09:00:00.000Z',
          kind: 'no_capture',
          label: 'No capture',
        }]}
        lens="all"
        selectedEpisodeId={null}
        onLensChange={onLensChange}
        onSelectEpisode={onSelectEpisode}
      />,
    )

    const mediaIntervals = screen.getAllByRole('button', { name: /Media.*YouTube/i })
    fireEvent.focus(mediaIntervals[0])
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('Media')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('A long-form video was playing.')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('provisional')
    expect(screen.getByTestId('tape-preview')).toHaveClass('min-h-[8.5rem]', 'sm:min-h-[7.25rem]')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('Screen · screenpipe-laptop / display-1')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('Audio · screenpipe-laptop / output')
    expect(screen.getByTestId('tape-preview')).toHaveTextContent('Pinned: title · kind')
    expect(screen.getByLabelText('2 supported evidence intervals; 1 unsupported interior intervals')).toBeVisible()

    fireEvent.click(mediaIntervals[0])
    expect(onSelectEpisode).toHaveBeenCalledWith('media-1')

    fireEvent.click(screen.getByRole('button', { name: /^Media 1$/ }))
    expect(onLensChange).toHaveBeenCalledWith('media')
    expect(screen.getAllByRole('button', { name: /No capture/i }).length).toBeGreaterThan(0)
  })

  it('uses media as the primary channel and conversation as a separate non-media channel', () => {
    expect(evidenceChannel(mediaEpisode, 'conversation')).toBe('media')
    expect(evidenceChannel(conversationEpisode, 'conversation')).toBe('conversation')
  })

  it('does not clamp evidence wholly before or after an episode into false support', () => {
    const outsideEpisode = {
      ...mediaEpisode,
      evidence: [
        { ...mediaEpisode.evidence[0], evidence_id: 'before', started_at: '2026-02-19T03:00:00.000Z', ended_at: '2026-02-19T03:30:00.000Z' },
        { ...mediaEpisode.evidence[1], evidence_id: 'after', started_at: '2026-02-19T05:30:00.000Z', ended_at: '2026-02-19T06:00:00.000Z' },
      ],
    } satisfies TimelineEpisode

    render(
      <EvidenceTape
        projection={projection}
        episodes={[outsideEpisode, conversationEpisode]}
        coverage={[]}
        lens="all"
        selectedEpisodeId={null}
        onLensChange={vi.fn()}
        onSelectEpisode={vi.fn()}
      />,
    )

    fireEvent.focus(screen.getAllByRole('button', { name: /Media.*YouTube/i })[0])
    expect(screen.getByLabelText('0 supported evidence intervals; 1 unsupported interior intervals')).toBeVisible()
  })

  it('packs concurrent episodes into separate overlap lanes and shows snapshot review state', () => {
    const concurrent = {
      ...mediaEpisode,
      episode_id: 'work-1',
      episode_key: 'work-key',
      kind: 'work',
      title: 'Editing Chronicle',
      started_at: '2026-02-19T04:30:00.000Z',
      ended_at: '2026-02-19T05:30:00.000Z',
      confirmed_fields: [],
      evidence: [],
    } satisfies TimelineEpisode
    const concurrentProjection = {
      ...projection,
      episode_count: 2,
      group_count: 2,
      groups: [
        projection.groups[0],
        {
          ...projection.groups[0],
          group_id: 'work-group',
          started_at: concurrent.started_at,
          ended_at: concurrent.ended_at,
          title: concurrent.title,
          episode_ids: [concurrent.episode_id],
          intervals: [{ lane: 'foreground', episode_id: concurrent.episode_id, started_at: concurrent.started_at, ended_at: concurrent.ended_at }],
        },
      ],
    } satisfies DayReviewProjection

    render(
      <EvidenceTape
        projection={concurrentProjection}
        episodes={[mediaEpisode, concurrent]}
        coverage={[]}
        lens="all"
        selectedEpisodeId={null}
        snapshot={{
          snapshot_state: 'ready',
          current_snapshot_id: 'abcdef0'.padEnd(64, '1'),
          reviewed_snapshot_id: null,
          applied_snapshot_id: null,
        }}
        onLensChange={vi.fn()}
        onSelectEpisode={vi.fn()}
      />,
    )

    expect(screen.getAllByText('Activity ×2').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Snapshot: Ready for review')).toHaveTextContent('abcdef0')
    expect(screen.getAllByRole('button', { name: /Activity overlap lane 1.*YouTube/i }).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('button', { name: /Activity overlap lane 2.*Editing Chronicle/i }).length).toBeGreaterThan(0)
  })
})
