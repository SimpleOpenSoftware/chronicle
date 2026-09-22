// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AxiosHeaders } from 'axios'
import { deviceInputApi, PrivacyInterval } from '../../services/api'
import PrivacyIntervals from './PrivacyIntervals'

vi.mock('../../services/api', () => ({ deviceInputApi: { overridePrivacy: vi.fn() } }))

const interval: PrivacyInterval = {
  source_id: 'synthetic-source', source_name: 'Test computer', revision: 7,
  started_at: '2026-01-01T10:00:00Z', ended_at: '2026-01-01T10:01:00Z',
  state: 'excluded', label: 'Private activity · excluded',
}

function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
    <PrivacyIntervals intervals={[interval]} timezone="Asia/Kolkata" />
  </QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('Privacy interval review', () => {
  it.each([['Allow processing', 'allowed'], ['Keep excluded', 'excluded']] as const)(
    'submits %s with the displayed revision only after an explicit click', async (button, decision) => {
      vi.mocked(deviceInputApi.overridePrivacy).mockResolvedValue({
        data: { revision: 8, decision }, status: 200, statusText: 'OK',
        headers: {}, config: { headers: new AxiosHeaders() },
      })
      const view = show()
      expect(screen.getByText('Private activity · excluded')).toBeTruthy()
      expect(view.container.querySelector('img, video, audio')).toBeNull()
      expect(deviceInputApi.overridePrivacy).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: button }))
      await waitFor(() => expect(deviceInputApi.overridePrivacy).toHaveBeenCalledWith(interval, decision))
    },
  )

  it('keeps the exclusion visible when a stale decision is rejected', async () => {
    vi.mocked(deviceInputApi.overridePrivacy).mockRejectedValue(new Error('Synthetic stale revision'))
    show()
    fireEvent.click(screen.getByRole('button', { name: 'Allow processing' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Reload the timeline')
    expect(screen.getByText('Private activity · excluded')).toBeTruthy()
  })
})
