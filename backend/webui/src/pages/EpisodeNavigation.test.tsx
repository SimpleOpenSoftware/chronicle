// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { speakerApi, timelineApi } from '../services/api'
import EpisodeDetail from './EpisodeDetail'
import EpisodeByKey from './EpisodeByKey'

vi.mock('../hooks/useTimelineTimezone', () => ({ useTimelineTimezone: () => ({ timezone: 'Asia/Kolkata' }) }))
vi.mock('../components/AskAboutSource', () => ({ default: () => null }))

const episode = {
  episode_id: 'episode-one', title: 'Evening work', summary: 'A focused work interval.',
  started_at: '2026-09-04T20:30:00', ended_at: '2026-09-04T20:45:00',
  kind: 'work_session', activity_mode: 'foreground', salience: 'routine',
  assertions: [], entities: [], evidence: [], attributes: {}, related_conversation_ids: [],
  has_thumbnail: false, audio_playback_ranges: [],
}
function Location() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}{location.search}</div>
}
function renderRoute(route: string) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[route]}><Routes>
      <Route path="/timeline/key/:episodeKey" element={<EpisodeByKey />} />
      <Route path="/timeline/:episodeId" element={<EpisodeDetail />} />
      <Route path="/timeline" element={<Location />} />
    </Routes><Location /></MemoryRouter>
  </QueryClientProvider>)
}
beforeEach(() => { vi.spyOn(speakerApi, 'getEnrolledSpeakers').mockResolvedValue({ data: { speakers: [] } } as never) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('Episode navigation and query failures', () => {
  it('returns to the episode day in the account timezone, including UTC timestamps without offsets', async () => {
    vi.spyOn(timelineApi, 'getEpisode').mockResolvedValue({ data: episode } as never)
    renderRoute('/timeline/episode-one')
    expect(await screen.findByRole('heading', { name: 'Evening work' })).toBeVisible()
    expect(screen.getByText(/5 Sept 2026.*Asia\/Kolkata/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Back to Timeline' }))
    expect(screen.getAllByTestId('location')[0]).toHaveTextContent('/timeline?date=2026-09-05')
  })

  it('retries a failed episode request without claiming the episode was removed', async () => {
    vi.spyOn(timelineApi, 'getEpisode').mockRejectedValueOnce({ response: { status: 503 } }).mockResolvedValue({ data: episode } as never)
    renderRoute('/timeline/episode-one')
    expect(await screen.findByText('Could not load this episode.')).toBeVisible()
    expect(screen.queryByText(/may have been removed/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Evening work' })).toBeVisible()
  })

  it('distinguishes an unavailable episode from a service error', async () => {
    vi.spyOn(timelineApi, 'getEpisode').mockRejectedValue({ response: { status: 404 } })
    renderRoute('/timeline/missing')
    expect(await screen.findByText(/This episode is unavailable/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('retries a durable-link service failure and resolves to the real episode', async () => {
    vi.spyOn(timelineApi, 'resolveEpisodeKey').mockRejectedValueOnce(new Error('Network unavailable')).mockResolvedValue({ data: { resolved: true, episode_id: 'episode-one' } } as never)
    vi.spyOn(timelineApi, 'getEpisode').mockResolvedValue({ data: episode } as never)
    renderRoute('/timeline/key/key-one')
    expect(await screen.findByText('Could not resolve this episode link.')).toBeVisible()
    expect(screen.queryByText(/No episode was found/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Evening work' })).toBeVisible()
    expect(screen.getByTestId('location')).toHaveTextContent('/timeline/episode-one')
  })

  it('keeps every successor addressable when an episode has split', async () => {
    vi.spyOn(timelineApi, 'resolveEpisodeKey').mockResolvedValue({ data: { resolved: false, successor_keys: ['key-a', 'key-b'] } } as never)
    renderRoute('/timeline/key/replaced')
    expect(await screen.findByRole('link', { name: 'Episode 1' })).toHaveAttribute('href', '/timeline/key/key-a')
    expect(screen.getByRole('link', { name: 'Episode 2' })).toHaveAttribute('href', '/timeline/key/key-b')
  })
})
