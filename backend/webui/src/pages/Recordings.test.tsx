// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RecordingsRouter from './RecordingsRouter'

const mocks = vi.hoisted(() => ({
  play: vi.fn(), star: vi.fn(), list: vi.fn(), admin: true,
  recording: { conversation_id: 'r1', title: 'Pottery workshop', client_id: 'abc-phone',
    summary: 'Learning to shape clay.', segment_count: 1, audio_chunks_count: 2,
    audio_total_duration: 120, processing_status: 'completed', starred: false,
    segments: [{ text: 'Hello', speaker: 'Unknown Speaker 1', start: 0, end: 1 }] },
}))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ isAdmin: mocks.admin }) }))
vi.mock('../hooks/useGaplessPlayer', () => ({ useGaplessPlayer: () => ({ togglePlay: mocks.play, isActive: () => false, stop: vi.fn() }) }))
vi.mock('../hooks/useConversations', () => ({
  useConversations: (options: unknown) => { mocks.list(options); return { data: { conversations: [mocks.recording], total: 1 }, refetch: vi.fn() } },
  useDeleteConversation: () => ({}), useReprocessTranscript: () => ({}),
  useReprocessMemory: () => ({}), useReprocessSpeakers: () => ({}),
  useReprocessOrphan: () => ({}), useToggleStar: () => ({ mutateAsync: mocks.star }),
  useRestoreConversation: () => ({}), usePermanentDeleteConversation: () => ({}),
}))
vi.mock('../services/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ data: { items: [], total: 0, indexing: { initialized: true } } }) },
  conversationsApi: {}, annotationsApi: {},
  authApi: { getMe: vi.fn().mockResolvedValue({ data: { is_superuser: true } }) },
  speakerApi: { getEnrolledSpeakers: vi.fn().mockResolvedValue({ data: { speakers: [] } }) },
}))
vi.mock('../components/transcript/TranscriptEditor', () => ({ default: ({ hideUnknownSpeakers }: { hideUnknownSpeakers: boolean }) => <div>Transcript editor: {hideUnknownSpeakers ? 'known only' : 'all speakers'}</div> }))
vi.mock('../components/audio/PlayheadWaveform', () => ({ PlayheadTimeLabel: () => null }))
vi.mock('../components/ConversationVersionHeader', () => ({ default: () => null }))

function show() {
  render(<MemoryRouter initialEntries={['/recordings']}><QueryClientProvider client={new QueryClient()}><Routes>
    <Route path="/recordings" element={<RecordingsRouter />} />
    <Route path="/recordings/r1" element={<p>Recording detail</p>} />
  </Routes></QueryClientProvider></MemoryRouter>)
}
beforeEach(() => { mocks.admin = true; vi.clearAllMocks() })
afterEach(cleanup)

it('opens via a title link while playback, starring and rename stay in the list', async () => {
  show()
  fireEvent.click(screen.getByRole('button', { name: 'Play recording' }))
  expect(mocks.play).toHaveBeenCalledWith('r1', 120)
  expect(screen.getByRole('heading', { name: 'Recordings' })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Star recording' }))
  await waitFor(() => expect(mocks.star).toHaveBeenCalledWith('r1'))
  fireEvent.click(screen.getByRole('button', { name: 'Recording options' }))
  fireEvent.click(screen.getByRole('button', { name: 'Rename' }))
  expect(screen.getByDisplayValue('Pottery workshop')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  fireEvent.click(screen.getByRole('link', { name: 'Pottery workshop' }))
  expect(screen.getByText('Recording detail')).toBeVisible()
})

it('keeps transcript display options local and filters recording queries explicitly', async () => {
  show()
  expect(screen.queryByText('Hide unknown speakers in transcripts')).not.toBeInTheDocument()
  expect(screen.queryByText(/Detailed Summary/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Show transcript/ }))
  expect(await screen.findByText('Transcript editor: all speakers')).toBeVisible()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Hide unknown speakers in transcripts' }))
  expect(screen.getByText('Transcript editor: known only')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
  fireEvent.click(screen.getByRole('checkbox', { name: 'Starred recordings only' }))
  expect(mocks.list).toHaveBeenLastCalledWith(expect.objectContaining({ starredOnly: true, offset: 0 }))
  fireEvent.click(screen.getByRole('button', { name: /Clear filter/ }))
  expect(mocks.list).toHaveBeenLastCalledWith(expect.objectContaining({ starredOnly: undefined }))
})

it('keeps diagnostics out of regular-user browsing and gives deleted recordings a Trash view', async () => {
  mocks.admin = false
  show()
  expect(screen.queryByText('Diagnostics')).not.toBeInTheDocument()
  expect(screen.queryByText('Classic View')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Trash' }))
  expect(await screen.findByRole('heading', { name: 'Trash' })).toBeVisible()
  expect(screen.getByText('Trash is empty')).toBeVisible()
})
