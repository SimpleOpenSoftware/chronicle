// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BenchmarkPanel } from './GuidedEnrollment'
import { dataAuditApi } from '../../services/api'

vi.mock('../../hooks/useJobPolling', () => ({ useJobPolling: () => ({ pollJob: vi.fn().mockResolvedValue('finished') }) }))
vi.mock('../../services/api', () => ({
  BACKEND_URL: '', speakerApi: {},
  dataAuditApi: {
    getLatestSpeakerBenchmark: vi.fn(),
    getSpeakerGalleryBaseline: vi.fn().mockResolvedValue({ data: { speakers: [] } }),
    runSpeakerBenchmark: vi.fn().mockResolvedValue({ data: { job_id: 'synthetic-job' } }),
  },
}))
const held = { response: { status: 423, data: { detail: 'Private or unscreened evidence is held from processing' } } }
const report = { protocol: 'Synthetic evaluation', threshold: 0.5, conversation_groups: 2, embedding_model: 'synthetic', created_at: '2026-01-01T00:00:00Z', learning_curve: [{ fraction: 1, top1_accuracy_mean: 0.75, train_clips_mean: 10 }], dataset: { embedded_clips: 37, speakers: 2 } }
beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

describe('benchmark privacy', () => {
  it('explains an initial privacy hold without rendering a report', async () => {
    vi.mocked(dataAuditApi.getLatestSpeakerBenchmark).mockRejectedValue(held)
    render(<BenchmarkPanel speakerName="Synthetic speaker" />)
    fireEvent.click(screen.getByText(/Overall recognition benchmark/))
    expect(await screen.findByText(/This benchmark is held/)).toBeVisible()
    expect(screen.queryByText(/37 clips/)).not.toBeInTheDocument()
  })
  it('removes the old chart if the report becomes held after a run', async () => {
    vi.mocked(dataAuditApi.getLatestSpeakerBenchmark)
      .mockResolvedValueOnce({ data: { report } } as any).mockRejectedValueOnce(held)
    render(<BenchmarkPanel speakerName="Synthetic speaker" />)
    fireEvent.click(screen.getByText(/Overall recognition benchmark/))
    expect(await screen.findByText('37 clips · 2 speakers')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Run again' }))
    expect(await screen.findByText(/This benchmark is held/)).toBeVisible()
    await waitFor(() => expect(screen.queryByText('37 clips · 2 speakers')).not.toBeInTheDocument())
    expect(screen.queryByText('Queued')).not.toBeInTheDocument()
    expect(dataAuditApi.runSpeakerBenchmark).toHaveBeenCalledOnce()
  })
  it('shows an allowed rebuilt report and clears the hold', async () => {
    vi.mocked(dataAuditApi.getLatestSpeakerBenchmark)
      .mockRejectedValueOnce(held).mockResolvedValueOnce({ data: { report } } as any)
    render(<BenchmarkPanel speakerName="Synthetic speaker" />)
    fireEvent.click(screen.getByText(/Overall recognition benchmark/))
    await screen.findByText(/This benchmark is held/)
    fireEvent.click(screen.getByRole('button', { name: 'Run benchmark' }))
    expect(await screen.findByText('37 clips · 2 speakers')).toBeVisible()
    expect(screen.queryByText(/This benchmark is held/)).not.toBeInTheDocument()
  })
})
