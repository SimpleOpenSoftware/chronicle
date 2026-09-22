// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import PluginSettingsForm from './PluginSettingsForm'
const api = vi.hoisted(() => ({ getPluginsMetadata: vi.fn(), getPluginsConnectivity: vi.fn(), updatePluginConfigStructured: vi.fn() }))
vi.mock('../services/api', () => ({ systemApi: api }))
vi.mock('./plugins/PluginConfigPanel', () => ({ default: ({ config, onChange, onSave, onReset }: any) => <>
  <span>{config.orchestration.enabled ? 'Enabled draft' : 'Disabled draft'}</span>
  <button onClick={() => onChange({ ...config, orchestration: { ...config.orchestration, enabled: !config.orchestration.enabled } })}>Change draft</button>
  <button onClick={onSave}>Save selected plugin</button><button onClick={onReset}>Reset draft</button>
</> }))
const plugin = { plugin_id: 'email', name: 'Email', description: '', enabled: true, status: 'active', supports_testing: false, orchestration: { enabled: true, events: ['conversation.complete'], condition: { type: 'always' } }, config_schema: { settings: {}, env_vars: {} } }
beforeEach(() => {
  api.getPluginsMetadata.mockReset().mockResolvedValue({ data: { plugins: [plugin] } })
  api.getPluginsConnectivity.mockReset().mockResolvedValue({ data: { plugins: {} } })
  api.updatePluginConfigStructured.mockReset()
})
afterEach(cleanup)
it('selects a plugin without writing configuration or claiming verified connectivity', async () => {
  render(<PluginSettingsForm />)
  fireEvent.click(await screen.findByRole('button', { name: 'Configure Email' }))
  expect(api.updatePluginConfigStructured).not.toHaveBeenCalled()
  expect(screen.getByText('Enabled · not checked')).toBeVisible()
  expect(screen.queryByRole('switch')).not.toBeInTheDocument()
})
it.each([
  [{ success: true, failed: [] }, 'Configuration saved and backend plugins reloaded.', 'bg-green-100'],
  [null, 'Plugin reload was not confirmed', 'bg-amber-100'],
  [{ success: true, failed: ['email'] }, '1 plugin(s) failed to initialize', 'bg-amber-100'],
])('preserves save/application feedback after refreshing metadata (%j)', async (reload, message, color) => {
  api.updatePluginConfigStructured.mockResolvedValue({ data: { reload } })
  render(<PluginSettingsForm />)
  fireEvent.click(await screen.findByRole('button', { name: 'Save selected plugin' }))
  await waitFor(() => expect(api.getPluginsMetadata).toHaveBeenCalledTimes(2))
  const banner = await screen.findByRole('status')
  expect(banner.textContent).toContain(message)
  expect(banner).toHaveClass(String(color))
  expect(api.updatePluginConfigStructured).toHaveBeenCalledWith('email', expect.objectContaining({ orchestration: plugin.orchestration }))
})

it('resets to the last saved configuration rather than the initial server snapshot', async () => {
  api.updatePluginConfigStructured.mockResolvedValue({ data: { reload: { success: true, failed: [] } } })
  render(<PluginSettingsForm />)
  fireEvent.click(await screen.findByRole('button', { name: 'Change draft' }))
  expect(screen.getByText('Disabled draft')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Save selected plugin' }))
  await waitFor(() => expect(api.getPluginsMetadata).toHaveBeenCalledTimes(2))
  fireEvent.click(screen.getByRole('button', { name: 'Change draft' }))
  expect(screen.getByText('Enabled draft')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Reset draft' }))
  expect(screen.getByText('Disabled draft')).toBeVisible()
})
