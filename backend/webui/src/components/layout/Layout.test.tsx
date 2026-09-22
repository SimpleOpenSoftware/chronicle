// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Layout from './Layout'

const state = vi.hoisted(() => ({ admin: true }))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ user: { display_name: 'Avery' }, isAdmin: state.admin }) }))
vi.mock('../../contexts/ThemeContext', () => ({ useTheme: () => ({ isDark: true, toggleTheme: vi.fn() }) }))
vi.mock('../../hooks/useSSE', () => ({ useSSE: () => 'connected' }))
vi.mock('../../hooks/useSystemEvents', () => ({ useSystemEventsSummary: () => ({ data: { unacked: 49 } }) }))
vi.mock('../../hooks/useSystem', () => ({ useSystemHealthSummary: () => ({ data: { services: { audio: { healthy: false } } } }) }))
vi.mock('./GlobalRecordingIndicator', () => ({ default: () => null }))
vi.mock('../UserLoopModal', () => ({ default: () => null }))
afterEach(() => { cleanup(); localStorage.clear(); state.admin = true })

it('groups navigation and keeps the parent recording destination active on detail pages', () => {
  render(<MemoryRouter initialEntries={['/recordings/r1']}><Layout /></MemoryRouter>)
  for (const group of ['Workspace', 'Memory', 'Administration', 'System']) expect(screen.getByText(group)).toBeVisible()
  expect(screen.getByRole('link', { name: 'Recordings' })).toHaveAttribute('aria-current', 'page')
  expect(screen.getByRole('link', { name: /System Status/ })).toHaveTextContent('1')
  expect(screen.getByRole('link', { name: /System Events/ })).toHaveTextContent('49')
})

it('preserves regular-user navigation permissions', () => {
  state.admin = false
  render(<MemoryRouter><Layout /></MemoryRouter>)
  expect(screen.getByRole('link', { name: 'User Management' })).toBeVisible()
  expect(screen.queryByRole('link', { name: /System Events/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Data Audit' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Upload Audio' })).not.toBeInTheDocument()
})
