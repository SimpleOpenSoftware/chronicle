// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import VoiceLatencyReport, { VoiceTimingCard, type VoiceReport } from './VoiceLatencyReport'
import { api } from '../services/api'
vi.mock('../services/api', () => ({ api: { get: vi.fn() } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const metric = (value_ms: number) => ({ value_ms, quality: 'estimated' })
const report: VoiceReport = {
  turn_id: 'turn-1', turn_revision: 0, audio_session_id: 'capture', client_id: 'phone', started_at_ms: 100000,
  status: 'complete', metrics: { speaking: metric(3000), waiting: metric(2000), reply: metric(5000), total: metric(10000), stt: metric(800), stt_wait: metric(200), stt_batch: metric(600) },
  missing: [], invalid: [], events: [], alignment: 'Wall-clock alignment is approximate.',
}
describe('Voice interaction timings', () => {
  it('accounts for the full interaction while identifying estimates and nested STT', () => {
    render(<VoiceTimingCard report={report} />)
    expect(screen.getByText('3.00 s')).toBeInTheDocument()
    expect(screen.getByText('2.00 s')).toBeInTheDocument()
    expect(screen.getByText('5.00 s')).toBeInTheDocument()
    expect(screen.getByText(/10.00 s total/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('STT, agent, TTS and delivery timings'))
    expect(screen.getByText('Streaming transcript wait')).toBeInTheDocument()
    expect(screen.getByText('Batch STT request')).toBeInTheDocument()
    expect(screen.getByText(/Nested timings overlap/)).toBeInTheDocument()
  })
  it('never presents a missing playback acknowledgement as zero latency', () => {
    render(<VoiceTimingCard report={{ ...report, status: 'incomplete', metrics: { speaking: metric(3000) }, missing: ['waiting', 'reply', 'total'] }} />)
    expect(screen.getAllByText('Not measured').length).toBeGreaterThan(1)
    expect(screen.queryByText('0.00 s')).not.toBeInTheDocument()
    expect(screen.getByText(/Missing measurements:/)).toBeInTheDocument()
  })
  it('loads the authenticated report and preserves failed/incomplete sample counts', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { reports: [report], summary: { sample_count: 3, complete_count: 1, failed_count: 1, wait_sample_count: 1, wait_p50_ms: 2000, wait_p95_ms: 2000 } } })
    render(<VoiceLatencyReport />)
    await waitFor(() => expect(screen.getByText(/1\/3 complete · 1 failed/)).toBeInTheDocument())
    expect(api.get).toHaveBeenCalledWith('/api/wakeword/latency', { params: { limit: 20 } })
  })
  it('shows a recoverable load failure', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('network'))
    render(<VoiceLatencyReport />)
    expect(await screen.findByText('Could not load voice timings. Try refreshing.')).toBeInTheDocument()
  })
})
