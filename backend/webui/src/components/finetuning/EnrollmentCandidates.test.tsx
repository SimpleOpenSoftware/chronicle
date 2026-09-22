// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import EnrollmentCandidates from './EnrollmentCandidates'

const fixture = vi.hoisted(() => ({ error: false, enroll: vi.fn(), refetch: vi.fn() }))
vi.mock('../../services/api', () => ({ finetuningApi: { enrollSelectedClips: fixture.enroll } }))
vi.mock('../../hooks/useGaplessPlayer', () => ({ useGaplessPlayer: () => ({ playingSegmentId: null }) }))
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: () => ({ isLoading: false, isFetching: false, isError: fixture.error,
    refetch: fixture.refetch, data: candidates }),
}))
const candidates = { candidates: [{ speaker: 'Synthetic speaker', selected_count: 1, clips: [{
  conversation_id: 'synthetic-recording', conversation_title: 'Synthetic recording',
  segment_index: 0, start: 0, end: 10, duration: 10, text: 'Synthetic text',
  gated_in: true, default_selected: true, reasons: [],
}] }], conversation_count: 1, min_duration: 3, default_per_speaker: 5 }

beforeEach(() => { fixture.error = false; fixture.enroll.mockReset(); fixture.refetch.mockReset() })
afterEach(cleanup)
it('shows privacy holds after enrollment without claiming every selected clip was enrolled', async () => {
  fixture.enroll.mockResolvedValue({ data: { total_enrolled: 0, enrolled_new: 0, appended: 0, privacy_held: 1 } })
  render(<EnrollmentCandidates />)
  fireEvent.click(await screen.findByRole('button', { name: 'Enroll 1 selected' }))
  expect(await screen.findByText(/1 held by privacy screening; review these periods in Timeline/)).toBeVisible()
  expect(screen.getByRole('status')).toHaveClass('bg-amber-100')
  expect(fixture.refetch).toHaveBeenCalled()
})
it('hides stale candidate content and enrollment controls after a failed privacy refresh', () => {
  fixture.error = true
  render(<EnrollmentCandidates />)
  expect(screen.getByText(/Candidate list is unavailable/)).toBeVisible()
  expect(screen.queryByText('Synthetic text')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Enroll .* selected/ })).not.toBeInTheDocument()
})
