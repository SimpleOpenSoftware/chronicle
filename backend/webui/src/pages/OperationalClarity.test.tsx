// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import DataAudit from './DataAudit'
import Finetuning from './Finetuning'
import Queue from './Queue'
import WakeWordLab from './WakeWordLab'
import { formatDate } from '../components/dataAudit/format'

const m = vi.hoisted(() => ({
  statusError: null as Error | null, dashboardError: null as Error | null,
  status: { annotation_counts: { transcript: { applied: 2, trained: 5 }, diarization: { applied: 3, trained: 8 }, timing: { applied: 99 } } },
  dashboard: {} as any, list: vi.fn(), archive: vi.fn(),
  models: vi.fn(), streams: vi.fn(), stats: vi.fn(), samples: vi.fn(), audio: vi.fn(), collect: vi.fn(), verifier: vi.fn(),
}))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }))
vi.mock('../hooks/useSystem', () => ({ useExternalServices: () => ({ data: { services: [] } }) }))
vi.mock('../hooks/useJobPolling', () => ({ useJobPolling: () => ({ pollJob: vi.fn() }) }))
vi.mock('../hooks/useQueue', () => ({ useQueueDashboard: () => ({ data: m.dashboard, error: m.dashboardError, isLoading: false, isFetching: false }) }))
vi.mock('../hooks/useFinetuning', () => ({
  useFinetuningStatus: () => ({ data: m.status, error: m.statusError, refetch: vi.fn() }),
  useCronJobs: () => ({ data: [{ job_id: 'asr_finetuning', enabled: false }, { job_id: 'prompt_optimization', enabled: false }], refetch: vi.fn() }),
  useRunCronJob: () => ({ mutateAsync: vi.fn() }), useDeleteOrphanedAnnotations: () => ({}), useRetryFailedAnnotations: () => ({}), useDeleteFailedAnnotations: () => ({}),
}))
vi.mock('../services/api', () => ({
  dataAuditApi: { getConversations: m.list, getTriagePending: vi.fn().mockResolvedValue({ data: { pending_count: 0, conversation_count: 0 } }), archive: m.archive }, queueApi: {}, conversationsApi: {},
  wakewordApi: { getModels: m.models, getStreams: m.streams, getStats: m.stats, getSamples: m.samples, getAudioBlob: m.audio, setCollectOnly: m.collect, setVerifierEnabled: m.verifier },
}))
vi.mock('../components/dataAudit/AuditFilterBar', () => ({ default: () => null }))
vi.mock('../components/dataAudit/AuditTable', () => ({ default: ({ onToggleSelect }: any) => <button onClick={() => onToggleSelect('r1')}>Select test recording</button> }))
vi.mock('../components/dataAudit/SpeakerConfidencePanel', () => ({ default: () => null }))
vi.mock('../components/dataAudit/DriftPanel', () => ({ default: () => null }))
vi.mock('../components/dataAudit/BackgroundReviewPanel', () => ({ default: () => <div>Background review content</div> }))
vi.mock('../components/dataAudit/SplitConversationModal', () => ({ default: () => null }))
vi.mock('../components/dataAudit/MergePreviewModal', () => ({ default: () => null }))
vi.mock('../components/dataAudit/ExportModal', () => ({ default: () => null }))
vi.mock('../components/dataAudit/GuidedEnrollment', () => ({ default: () => <div>Enrollment content</div> }))
vi.mock('../components/dataAudit/UnknownSpeakerDiscovery', () => ({ default: () => null }))
vi.mock('../components/dataAudit/SpeakerLabelReview', () => ({ default: () => null }))
vi.mock('../components/finetuning/EnrollmentCandidates', () => ({ default: () => null }))
vi.mock('../components/timeline/ReconciliationProgress', () => ({ default: () => null }))
const word = { name: 'hey_hermes', model: 'test.onnx', threshold: 0.9, patience: 2, verifier: 'test', verifier_enabled: true, collect_only: false }
const clip = (id: string, score = 0.901) => ({ id, score, wakeword: 'hey_hermes', created_at_ms: 1720000000000, duration_secs: 3, reason: 'trigger' })
function Location() { return <output data-testid="location">{useLocation().search}</output> }
function show(node: React.ReactNode, path = '/') {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[path]}>{node}<Location /></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear(); m.statusError = null; m.dashboardError = null
  m.list.mockResolvedValue({ data: { conversations: [{ conversation_id: 'r1', client_id: 'phone' }], total: 1, unanalyzed_count: 0, speakers: [], datasets: [] } })
  m.dashboard = { jobs: {}, conversation_jobs: {}, events: [], streaming_status: { stream_health: { 'audio:stream:phone': { stream_length: 10, consumer_groups: [] } }, active_sessions: [], completed_sessions: [], rq_queues: {} } }
  m.models.mockResolvedValue({ data: { wakewords: [word] } }); m.streams.mockResolvedValue({ data: { streams: [] } })
  m.stats.mockResolvedValue({ data: { hey_hermes: { pending: 1, positive: 1, negative: 0, false_negatives: 0 } } })
  m.samples.mockResolvedValue({ data: { samples: [clip('one')] } }); m.audio.mockResolvedValue({ data: new Blob(['audio']) })
  URL.createObjectURL = vi.fn().mockReturnValue('blob:clip'); URL.revokeObjectURL = vi.fn()
})
afterEach(cleanup)
it('normalizes naive UTC audit timestamps to explicit IST', () => {
  expect(formatDate('2026-09-13T00:07:18')).toBe(formatDate('2026-09-13T00:07:18Z'))
  expect(formatDate('2026-09-13T00:07:18')).toContain('5:37:18'); expect(formatDate(null)).toBe('—')
})
it('deep links to enrollment and preserves dataset when switching tasks', async () => {
  show(<DataAudit />, '/data-audit?view=enroll&dataset=actual-set')
  expect(screen.getByText('Enrollment content')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: 'Background & role' }))
  expect(screen.getByText('Background review content')).toBeVisible()
  expect(screen.getByTestId('location')).toHaveTextContent('view=background'); expect(screen.getByTestId('location')).toHaveTextContent('dataset=actual-set')
  await waitFor(() => expect(m.list).toHaveBeenCalled())
})
it('does not offer disabled backend audio deletion after selecting recordings', async () => {
  show(<DataAudit />, '/data-audit'); await waitFor(() => expect(m.list).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('button', { name: 'Select test recording' }))
  expect(screen.getByText('Audio archival unavailable')).toBeVisible()
  expect(screen.queryByRole('button', { name: /Archive|Delete audio/i })).not.toBeInTheDocument(); expect(m.archive).not.toHaveBeenCalled()
})
it('labels applied corrections without promising model training or deployment', () => {
  show(<Finetuning />)
  expect(screen.queryByText(/taught|ready to teach/i)).not.toBeInTheDocument()
  expect(screen.getAllByText('No recorded run')).toHaveLength(2)
  expect(screen.getByText('5')).toBeVisible(); expect(screen.queryByText('99')).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Review enrollment' })).toHaveAttribute('href', '/data-audit?view=enroll')
})
it('does not show verified-looking correction counts when status fails', () => {
  m.statusError = new Error('offline'); show(<Finetuning />)
  expect(screen.getByRole('alert')).toHaveTextContent('Training status could not be loaded')
  expect(screen.queryByText('applied corrections')).not.toBeInTheDocument()
})
it('deduplicates registry jobs and labels retained streams without invented activity or age', () => {
  const job = { job_id: 'duplicate-job', job_type: 'test_job', status: 'finished', created_at: '2026-09-13T00:00:00', meta: {} }
  m.dashboard.jobs = { finished: [job, job], scheduled: [job] }; show(<Queue />)
  expect(screen.getAllByText('duplicate-job')).toHaveLength(1); expect(screen.getByText('Retained')).toBeVisible()
  expect(screen.getAllByText('Unknown')).toHaveLength(2); expect(screen.queryByText('Active', { exact: true })).not.toBeInTheDocument()
  expect(screen.queryByText('0s', { exact: true })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Flush jobs…', hidden: true }).closest('details')).not.toHaveAttribute('open')
})
it('reports stale queue data on refresh failure', () => {
  m.dashboardError = new Error('offline'); show(<Queue />)
  expect(screen.getByRole('alert')).toHaveTextContent('Queue details are hidden until their privacy status can be checked')
})
it('bounds clip rows and downloads audio only on request with a retry state', async () => {
  m.samples.mockResolvedValue({ data: { samples: Array.from({ length: 25 }, (_, i) => clip(String(i))) } })
  m.audio.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ data: new Blob(['audio']) }); show(<WakeWordLab />)
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Load audio' })).toHaveLength(20)); expect(m.audio).not.toHaveBeenCalled()
  fireEvent.click(screen.getAllByRole('button', { name: 'Load audio' })[0])
  fireEvent.click(await screen.findByRole('button', { name: 'Retry audio' }))
  await waitFor(() => expect(document.querySelectorAll('audio')).toHaveLength(1))
  fireEvent.click(screen.getByRole('button', { name: 'Show more clips (20 of 25)' }))
  expect(screen.getAllByRole('button', { name: 'Load audio' })).toHaveLength(24); expect(m.audio).toHaveBeenCalledTimes(2)
})
it('rejects stale bucket responses after switching twice', async () => {
  let positive!: (v: any) => void; let negative!: (v: any) => void
  m.samples.mockImplementation((_word, bucket) => bucket === 'positive' ? new Promise((r) => { positive = r }) : bucket === 'negative' ? new Promise((r) => { negative = r }) : Promise.resolve({ data: { samples: [clip('pending', 0.901)] } }))
  show(<WakeWordLab />); expect(await screen.findByText('0.901')).toBeVisible()
  fireEvent.click(screen.getByRole('tab', { name: 'Positives (wake)' })); expect(screen.queryByText('0.901')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: 'Negatives (not wake)' }))
  await act(async () => { negative({ data: { samples: [clip('negative', 0.903)] } }) }); expect(await screen.findByText('0.903')).toBeVisible()
  await act(async () => { positive({ data: { samples: [clip('positive', 0.902)] } }) })
  expect(screen.queryByText('0.902')).not.toBeInTheDocument(); expect(screen.getByText('0.903')).toBeVisible()
})
it('makes dispatch controls explicit and distinguishes failed configuration from no words', async () => {
  const ui = show(<WakeWordLab />); expect(await screen.findByText('Detection settings')).toBeVisible()
  expect(await screen.findByText('3/7/2024, 3:16:40 pm IST')).toBeVisible()
  expect(screen.getByRole('checkbox', { name: 'Allow assistant dispatch', hidden: true }).closest('details')).not.toHaveAttribute('open'); expect(m.collect).not.toHaveBeenCalled()
  ui.unmount(); m.models.mockRejectedValue(new Error('offline')); show(<WakeWordLab />)
  expect(await screen.findByText('Wake-word configuration could not be loaded. Refresh to try again.')).toBeVisible()
  expect(screen.queryByText('No wake words configured.')).not.toBeInTheDocument()
})
