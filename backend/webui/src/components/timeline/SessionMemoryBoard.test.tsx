// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SessionMemoryBoard, { sourceLabel } from './SessionMemoryBoard'
import { MemorySession, SessionSource, timelineApi } from '../../services/api'

const source = (key: string, role = 'user_statement'): SessionSource => ({ key, evidence_id: key, content_hash: `hash-${key}`, kind: 'transcript', role, locator: { capture_source_id: 'phone', modality: 'transcript', track_id: 'mic' }, direction: 'input', started_at: '2026-09-04T07:00:00Z', ended_at: '2026-09-04T08:15:00Z', episode_keys: ['ep1'], participation: role === 'uncertain' ? 'uncertain' : 'supporting', disposition: 'auto', excerpt: 'We agreed to meet next Friday.' })
const session = (id: string, overrides: Partial<MemorySession> = {}): MemorySession => ({ source_keys: [id], owner_local_date: '2026-09-04', session_key: id, revision: 1, origin: 'automatic', title: `Call ${id}`, summary: 'Agreed on a follow-up next Friday.', started_at: '2026-09-04T07:00:00Z', ended_at: '2026-09-04T08:15:00Z', episodes: [{ episode_key: 'ep1', revision: 1 }], episode_ids: ['ep1'], sources: [source(id)], scope_hash: id, state: 'available', questions: [], proposal_id: null, change_count: 0, stage: null, completed_sources: 0, total_sources: 0, error: null, ...overrides })
function show(rows: MemorySession[], url = "/") {
  vi.spyOn(timelineApi, 'getSessionSources').mockImplementation(async (_day, _timezone, row) => ({ data: { sources: rows.find(r => r.session_key === row.session_key)!.sources, scope_hash: row.scope_hash } }) as never)
  vi.spyOn(timelineApi, 'getSessions').mockResolvedValue({ data: { sessions: rows } } as never)
  render(<MemoryRouter initialEntries={[url]}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><SessionMemoryBoard day="2026-09-04" timezone="Asia/Kolkata" snapshotId="snapshot" /></QueryClientProvider></MemoryRouter>)
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })
describe('Session memory decisions', () => {
  it('restarts a paused investigation only after an explicit click', async () => {
    const restart = vi.spyOn(timelineApi, 'regenerateMemoryProposal').mockResolvedValue({ data: {} } as never)
    show([session('paused', { state: 'paused', proposal_id: 'saved-proposal', failure_kind: 'budget_exhausted', error: 'Limit' })])
    const button = await screen.findByRole('button', { name: 'Restart investigation' })
    expect(restart).not.toHaveBeenCalled()
    fireEvent.click(button)
    await waitFor(() => expect(restart).toHaveBeenCalledWith('saved-proposal'))
    expect(restart).toHaveBeenCalledTimes(1)
  })
  it('keeps another session actionable while a request is in flight', async () => {
    const generate = vi.spyOn(timelineApi, 'generateSessionMemory').mockImplementation(() => new Promise(() => {}))
    show([session('one'), session('two')])
    const buttons = await screen.findAllByRole('button', { name: 'Generate memory' })
    fireEvent.click(buttons[0])
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(1))
    expect(buttons[1]).toBeEnabled()
    fireEvent.click(buttons[1])
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(2))
  })
  it('excludes only checked evidence using the displayed revision and scope fence', async () => {
    const decide = vi.spyOn(timelineApi, 'decideSessionMemory').mockResolvedValue({ data: { correction_required: false } } as never)
    const row = session('one', { sources: [source('call'), { ...source('tv', 'media_content'), participation: 'background', direction: 'output' }] })
    show([row])
    fireEvent.click(await screen.findByRole('button', { name: /Call one/ }))
    fireEvent.click((await screen.findAllByText('Microphone audio'))[0].closest('summary')!)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Microphone audio phone' }))
    fireEvent.click(screen.getByRole('button', { name: 'Don’t remember selected sources' }))
    await waitFor(() => expect(decide).toHaveBeenCalledWith('2026-09-04', 'Asia/Kolkata', row, 'exclude', ['call'], undefined, undefined))
  })
  it('shows a material question and explicit attribution actions, without a confirmation gate', async () => {
    const decide = vi.spyOn(timelineApi, 'decideSessionMemory').mockResolvedValue({ data: { correction_required: false } } as never)
    show([session('one', { sources: [source('speech', 'uncertain')], state: 'needs_attention', questions: ['Whose speech is this?'] })])
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve question' }))
    expect(screen.getByText('Whose speech is this?')).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: 'Another person speaking' }))
    await waitFor(() => expect(decide.mock.calls[0].slice(3)).toEqual(['attribute', ['speech'], 'third_party', undefined]))
    expect(screen.queryByText('Confirm episode structure')).not.toBeInTheDocument()
  })
  it('keeps empty and background outcomes out of the decision list, and retries a failed generation', async () => {
    const retry = vi.spyOn(timelineApi, 'generateSessionMemory').mockResolvedValue({ data: {} } as never)
    show([session('quiet', { state: 'no_changes' }), session('failed', { state: 'failed', proposal_id: 'failed-proposal', error: 'Model output was incomplete' })])
    const quiet = await screen.findByRole('article', { name: 'Session: Call quiet', hidden: true })
    expect(quiet).not.toBeVisible()
    expect(await screen.findByRole('alert')).toHaveTextContent('Preparation stopped before a reviewed draft was completed')
    fireEvent.click(within(screen.getByRole('article', { name: 'Session: Call failed' })).getByRole('button', { name: 'Retry preparation' }))
    await waitFor(() => expect(retry).toHaveBeenCalledWith('2026-09-04', 'Asia/Kolkata', expect.objectContaining({ session_key: 'failed' })))
  })
  it('shows exhausted work as paused with completed groups and no retry spinner', async () => {
    show([session('paused', {state: 'paused', failure_kind: 'budget_exhausted', error: 'Limit', completed_sources: 2, total_sources: 5})])
    expect(await screen.findByText('Investigation limit reached')).toBeVisible()
    expect(screen.getByRole('progressbar', {name: 'Source groups complete'})).toHaveAttribute('aria-valuenow', '2')
    expect(screen.getByText(/no automatic retry is scheduled/)).toBeVisible()
    expect(screen.getByRole('button', {name: 'Inspect saved work'})).toBeEnabled()
    expect(screen.queryByRole('button', {name: 'Retry preparation'})).not.toBeInTheDocument()
    expect(screen.queryByText('Reading session sources')).not.toBeInTheDocument()
  })
  it('distinguishes a repeated-call stop from exhausting the work budget', async () => {
    show([session('paused', {state: 'paused', failure_kind: 'repeated_tool_call', error: 'Repeated call guard stopped execution', completed_sources: 1, total_sources: 3})])
    expect(await screen.findByText('Investigation stopped repeating a tool call')).toBeVisible()
    expect(screen.getByText(/Saved source reads and drafts remain inspectable/)).toBeVisible()
    expect(screen.queryByText('Investigation limit reached')).not.toBeInTheDocument()
    expect(screen.getByRole('button', {name: 'Inspect saved work'})).toBeEnabled()
  })
  it('refreshes a stale draft instead of asking an obsolete question', async () => {
    const generate = vi.spyOn(timelineApi, 'generateSessionMemory').mockResolvedValue({ data: {} } as never)
    show([session('stale', { state: 'stale', questions: ['Old attribution question'], error: 'SelectionChanged: internal diagnostic' })])
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh draft' }))
    await waitFor(() => expect(generate).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Resolve question' })).not.toBeInTheDocument()
    expect(screen.queryByText('SelectionChanged: internal diagnostic')).not.toBeInTheDocument()
  })
  it('makes retained knowledge, independent review and exact tool exchanges inspectable', async () => {
    vi.spyOn(timelineApi, 'getMemoryExchanges').mockResolvedValue({ data: {
      accepted_context: { notes: [{ path: 'Projects/Navigation.md', hash: 'h', passage: 'The next trial is in November.' }],
        review: { verdict: 'ready', reason: 'The source supports a new decision.' }, unresolved_lookups: ['expedition lead'] },
      inference_runs: [{ operation: 'pi_session_review', model_input: { prompt: 'Assess grounding' },
        tool_calls: [{ tool: 'search_material', arguments: { query: 'navigation' } }], output: 'review complete' }],
      writer_exchanges: [], source_digest: 'Grounded account', source_scope: [],
    } } as never)
    show([session('one', { proposal_id: 'proposal' })])
    fireEvent.click(await screen.findByRole('button', { name: /Call one/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Inspect model input and output' }))
    const context = await screen.findByRole('region', { name: 'Accepted knowledge consulted' })
    expect(within(context).getByText('The source supports a new decision.')).toBeVisible()
    fireEvent.click(within(context).getByText('Projects/Navigation.md'))
    expect(within(context).getByText('The next trial is in November.')).toBeVisible()
    fireEvent.click(screen.getByText(/Independent Pi review/))
    expect(screen.getByText(/Assess grounding/)).toBeVisible()
    fireEvent.click(screen.getByText('Evidence and vault tool calls · 1'))
    expect(screen.getByText(/search_material/)).toBeVisible()
  })
  it('retries publication contention without losing or changing the clarification', async () => {
    const decide = vi.spyOn(timelineApi, 'decideSessionMemory')
      .mockRejectedValueOnce({ response: { status: 503, data: { detail: 'Timeline is updating.' } } })
      .mockResolvedValueOnce({ data: { correction_required: false } } as never)
    show([session('one', { state: 'needs_attention', questions: ['Who made this decision?'] })])
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve question' }))
    const input = screen.getByRole('textbox', { name: 'Add the missing context' })
    fireEvent.change(input, { target: { value: 'The project owner made that decision.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save clarification' }))
    expect(await screen.findByText('Timeline is updating. Retrying your answer…')).toBeVisible()
    expect(input).toHaveValue('The project owner made that decision.')
    expect(screen.getByRole('button', { name: 'Save clarification' })).toBeDisabled()
    await waitFor(() => expect(decide).toHaveBeenCalledTimes(2), { timeout: 4000 })
    expect(decide.mock.calls[1]).toEqual(decide.mock.calls[0])
    expect(await screen.findByText('Decision saved. Source recordings remain available.')).toBeVisible()
    expect(input).toHaveValue('')
  })
  it('keeps the answer on a stale-scope error and does not retry it', async () => {
    const decide = vi.spyOn(timelineApi, 'decideSessionMemory').mockRejectedValue({ response: { status: 409, data: { detail: 'Source scope changed; refresh before deciding' } } })
    show([session('one', { state: 'needs_attention', questions: ['Who made this decision?'] })])
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve question' }))
    const input = screen.getByRole('textbox', { name: 'Add the missing context' })
    fireEvent.change(input, { target: { value: 'The project owner.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save clarification' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Source scope changed')
    expect(input).toHaveValue('The project owner.')
    expect(decide).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Save clarification' })).toBeEnabled()
  })
  it('shows a saved decision without a conflicting saving status while refreshing', async () => {
    vi.spyOn(timelineApi, 'decideSessionMemory').mockResolvedValue({ data: { correction_required: false } } as never)
    show([session('one', { state: 'needs_attention', questions: ['Who made this decision?'] })])
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve question' }))
    vi.mocked(timelineApi.getSessions).mockImplementationOnce(() => new Promise(() => {}))
    fireEvent.change(screen.getByRole('textbox', { name: 'Add the missing context' }), { target: { value: 'The project owner.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save clarification' }))
    expect(await screen.findByText('Decision saved. Source recordings remain available.')).toBeVisible()
    expect(screen.queryByText('Saving your decision…')).not.toBeInTheDocument()
    expect(screen.queryByText('Timeline is updating. Retrying your answer…')).not.toBeInTheDocument()
  })
  it('labels photos and screen OCR distinctly', () => {
    expect(sourceLabel({ ...source('photo'), locator: { capture_source_id: 'phone', modality: 'photo', track_id: null } })).toBe('Photo')
    const screen: SessionSource = { ...source('screen'), locator: { capture_source_id: 'macbook', modality: 'screen', track_id: null } }
    expect(sourceLabel(screen)).toBe('Screen text')
    expect(sourceLabel({ ...screen, metadata: { text_source: 'ocr' } })).toBe('Screen OCR')
    expect(sourceLabel({ ...screen, metadata: { text_source: 'accessibility' } })).toBe('Screen accessibility text')
  })
})


it('opens a completed session named by a search deep link', async () => {
  Element.prototype.scrollIntoView = vi.fn()
  show([session('completed-call', { state: 'applied' })], '/timeline?date=2026-09-04&session=completed-call')
  expect(await screen.findByRole('button', { name: 'Ask about this' })).toBeVisible()
})
