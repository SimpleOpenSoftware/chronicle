// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { memorySpacesApi } from '../services/api'
import MemorySpaces from './MemorySpaces'

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><MemorySpaces /></MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MemorySpaces creation picker', () => {
  it('starts blank and treats first-hop links as optional suggestions', async () => {
    vi.spyOn(memorySpacesApi, 'list').mockResolvedValue({ data: [] } as never)
    vi.spyOn(memorySpacesApi, 'mainNotes').mockResolvedValue({
      data: [{ note_path: 'Topics/Idea.md', byte_size: 100, excerpt: 'Idea links to Ada' }],
    } as never)
    vi.spyOn(memorySpacesApi, 'previewSeed').mockResolvedValue({
      data: {
        selected: [{ note_path: 'Topics/Idea.md', byte_size: 100 }],
        suggestions: [{ note_path: 'People/Ada.md', byte_size: 80 }],
        total_bytes: 100,
      },
    } as never)

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'New space' }))

    expect(screen.getByText(/No notes will be copied from Main/i)).toBeVisible()
    fireEvent.click(await screen.findByRole('checkbox'))

    expect(await screen.findByText('+ People/Ada.md')).toBeVisible()
    expect(screen.getByText(/Only notes you check are copied/i)).toBeVisible()
    expect(memorySpacesApi.previewSeed).toHaveBeenCalledWith(['Topics/Idea.md'])
  })
})

describe('MemorySpaces request states', () => {
  it('retries a failed space list without reporting that no spaces exist', async () => {
    vi.spyOn(memorySpacesApi, 'list').mockRejectedValueOnce(new Error('Offline')).mockResolvedValue({ data: [{
      space_id: 'space-one', name: 'Research notebook', state: 'active', sync_state: 'healthy', seed_notes: [],
    }] } as never)
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load memory spaces.')
    expect(screen.queryByText(/No spaces yet/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('link', { name: /Research notebook/ })).toHaveAttribute('href', '/spaces/space-one/record')
  })
})
