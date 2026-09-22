// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import AutomationSettings from './AutomationSettings'
const state = vi.hoisted(() => ({ next: '2026-09-14T04:30:00+00:00' as string | null, update: vi.fn() }))
vi.mock('../hooks/useFinetuning', () => ({
  useCronJobs: () => ({ data: [{ job_id: 'daily', description: '', enabled: true, schedule: '30 4 * * *', last_run: null, next_run: state.next }], isLoading: false, refetch: vi.fn() }),
  useToggleCronJob: () => ({ mutateAsync: vi.fn() }), useRunCronJob: () => ({ mutateAsync: vi.fn() }),
  useUpdateCronSchedule: () => ({ mutateAsync: state.update }),
}))
beforeEach(() => { state.next = '2026-09-14T04:30:00+00:00'; state.update.mockReset().mockResolvedValue({}) })
afterEach(cleanup)
it('shows the UTC next-run instant in IST while preserving the UTC expression when edited', async () => {
  render(<AutomationSettings isAdmin />)
  expect(screen.getByText(/Next:/)).toHaveTextContent(/10:00 am IST/i)
  fireEvent.click(screen.getByText('Schedule and last run'))
  expect(screen.getByText(/At 04:30 AM/)).toHaveTextContent('(UTC)')
  fireEvent.click(screen.getByRole('button', { name: 'Edit schedule' }))
  expect(screen.getByLabelText('Cron schedule (UTC)')).toHaveValue('30 4 * * *')
  fireEvent.change(screen.getByLabelText('Cron schedule (UTC)'), { target: { value: '0 5 * * *' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save schedule' }))
  await waitFor(() => expect(state.update).toHaveBeenCalledWith({ jobId: 'daily', schedule: '0 5 * * *' }))
})
it('does not describe an enabled job without a next-run instant as scheduled or never run', () => {
  state.next = null
  render(<AutomationSettings isAdmin />)
  expect(screen.getByText(/Next:/)).toHaveTextContent('Not scheduled')
})
