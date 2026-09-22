// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { chatApi } from '../services/api';
import DialogueTasks from './DialogueTasks';

const task = { id: 'task', title: 'Choose room', status: 'awaiting_input', revision: 7,
  input_wait: { after_utterance_id: 'prompt', choices: [{ id: 'study', label: 'Study' }], expires_at: null } };
const snapshot = { revision: 10, tasks: [task], foreground_task_id: 'task', audio_client_id: null };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><DialogueTasks sessionId="thread" /></QueryClientProvider>);
  return client;
}
it('targets the advertised choice and task revision and keeps free text available', async () => {
  vi.spyOn(chatApi, 'getDialogue').mockResolvedValue({ data: snapshot } as never);
  const send = vi.spyOn(chatApi, 'commandDialogueTask').mockResolvedValue({ data: { ...snapshot, revision: 11, tasks: [] } } as never);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Study' }));
  await waitFor(() => expect(send).toHaveBeenCalledWith('thread', 'task', expect.objectContaining({ action: 'reply', revision: 7, choice_id: 'study' })));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Study' })).not.toBeInTheDocument());
});
it('offers resume and cancel for a paused task without repeating choices', async () => {
  vi.spyOn(chatApi, 'getDialogue').mockResolvedValue({ data: { ...snapshot, tasks: [{ ...task, status: 'paused' }] } } as never);
  const send = vi.spyOn(chatApi, 'commandDialogueTask').mockResolvedValue({ data: snapshot } as never);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
  await waitFor(() => expect(send).toHaveBeenCalledWith('thread', 'task', expect.objectContaining({ action: 'resume', revision: 7 })));
});
it('hides cached task contents when a privacy refresh rejects the thread', async () => {
  const get = vi.spyOn(chatApi, 'getDialogue').mockResolvedValue({ data: snapshot } as never);
  const client = mount();
  await screen.findByText('Choose room');
  get.mockRejectedValue(new Error('held'));
  await client.invalidateQueries({ queryKey: ['chat', 'dialogue', 'thread'] });
  await screen.findByRole('alert');
  expect(screen.queryByText('Choose room')).not.toBeInTheDocument();
});
