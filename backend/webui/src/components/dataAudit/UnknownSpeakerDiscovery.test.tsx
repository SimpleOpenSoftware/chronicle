// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import UnknownSpeakerDiscovery from './UnknownSpeakerDiscovery'

vi.mock('../../hooks/useGaplessPlayer', () => ({
  useGaplessPlayer: () => ({ playingSegmentId: null, playSegment: vi.fn(), stop: vi.fn() }),
}))

vi.mock('../../services/api', () => ({
  dataAuditApi: {
    getUnknownSpeakerClusters: vi.fn().mockResolvedValue({
      data: {
        clusters: [{
          cluster_id: 'cluster-1',
          run_fingerprint: 'run-1',
          conversation_count: 2,
          segment_count: 2,
          members: [
            {
              identity_key: 'conversation-a:Unknown Speaker 1',
              conversation_id: 'conversation-a',
              conversation_title: 'Lunch',
              conversation_date: '',
              local_label: 'Unknown Speaker 1',
              segments: [{ segment_index: 0, start: 0, end: 4, duration: 4, text: 'hello' }],
            },
            {
              identity_key: 'conversation-b:Unknown Speaker 7',
              conversation_id: 'conversation-b',
              conversation_title: 'Dinner',
              conversation_date: '',
              local_label: 'Unknown Speaker 7',
              segments: [{ segment_index: 2, start: 5, end: 9, duration: 4, text: 'again' }],
            },
          ],
        }],
      },
    }),
    discoverUnknownSpeakers: vi.fn(),
    getJobResult: vi.fn(),
    decideUnknownSpeakerCluster: vi.fn(),
  },
}))

afterEach(cleanup)

describe('unknown speaker discovery', () => {
  it('reviews local identities and enrollment clips before confirmation', async () => {
    render(<UnknownSpeakerDiscovery />)

    expect(await screen.findByText(/Possible same person/)).toHaveTextContent('2 conversations')
    expect(screen.getByText('Unknown Speaker 1')).toBeInTheDocument()
    expect(screen.getByText('Unknown Speaker 7')).toBeInTheDocument()
    // Conversations start collapsed; the summary still shows what will be enrolled.
    expect(screen.queryAllByTitle('Use this clip for enrollment')).toHaveLength(0)
    expect(screen.getAllByText('1 clip · 1 selected')).toHaveLength(2)

    const lunch = screen.getByRole('button', { name: /Lunch/ })
    expect(lunch).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(lunch)
    expect(lunch).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByTitle('Use this clip for enrollment')).toHaveLength(1)
    fireEvent.click(lunch)
    expect(screen.queryAllByTitle('Use this clip for enrollment')).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }))
    expect(screen.getAllByTitle('Use this clip for enrollment')).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: /Play clip from/ })).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Relabel + enroll' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }))
    expect(screen.queryAllByTitle('Use this clip for enrollment')).toHaveLength(0)
  })
})
