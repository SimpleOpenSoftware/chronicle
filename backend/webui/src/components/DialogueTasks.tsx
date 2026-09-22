import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { chatApi } from '../services/api';

export interface DialogueTask {
  id: string;
  title: string;
  status: 'running' | 'awaiting_input' | 'paused' | 'completed' | 'cancelled' | 'failed' | 'stale';
  revision: number;
  input_wait: { after_utterance_id: string; choices: { id: string; label: string }[]; expires_at: string | null } | null;
}
export interface DialogueView {
  revision: number;
  tasks: DialogueTask[];
  foreground_task_id: string | null;
  audio_client_id: string | null;
}

const control = 'min-h-10 rounded px-3 py-2 text-sm hover:bg-[var(--tape-chip)] disabled:opacity-50';

export default function DialogueTasks({ sessionId }: { sessionId: string }) {
  const cache = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const query = useQuery<DialogueView>({
    queryKey: ['chat', 'dialogue', sessionId],
    queryFn: () => chatApi.getDialogue(sessionId).then(response => response.data),
    refetchInterval: 1500,
    retry: false,
  });
  useEffect(() => {
    if (query.data) void cache.invalidateQueries({ queryKey: ['chat', 'messages', sessionId] });
  }, [query.data?.revision, sessionId, cache]);

  async function command(task: DialogueTask, action: 'reply' | 'pause' | 'resume' | 'cancel', choiceId?: string) {
    setBusy(task.id); setError('');
    try {
      const response = await chatApi.commandDialogueTask(sessionId, task.id, {
        id: crypto.randomUUID(), revision: task.revision, action, choice_id: choiceId,
      });
      cache.setQueryData(['chat', 'dialogue', sessionId], response.data);
      await cache.invalidateQueries({ queryKey: ['chat', 'messages', sessionId] });
    } catch (cause: any) {
      setError(cause.response?.data?.detail || 'Could not update this task. Reload and try again.');
      void query.refetch();
    } finally { setBusy(null); }
  }

  const tasks = query.isError ? [] : query.data?.tasks || [];
  if (!tasks.length && !error && !query.isError) return null;
  return <section aria-label="Dialogue tasks" className="my-4 border-t border-[var(--tape-line)] pt-3">
    {(error || query.isError) && <p role="alert" className="text-sm">{error || 'Task state could not load.'}</p>}
    {tasks.map(task => <div key={task.id} className="mb-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm">{task.title}</span>
        <span className="text-xs text-[var(--tape-activity)]">{{ awaiting_input: 'Waiting for you', paused: 'Paused', running: 'Working', completed: 'Completed', cancelled: 'Cancelled', failed: 'Failed', stale: 'Expired' }[task.status]}</span>
        {task.status === 'paused' && <button className={control} disabled={!!busy} onClick={() => void command(task, 'resume')}>Resume</button>}
        {task.status === 'awaiting_input' && <button className={control} disabled={!!busy} onClick={() => void command(task, 'pause')}>Pause</button>}
        {['running', 'awaiting_input', 'paused'].includes(task.status) && <button className={control} disabled={!!busy} onClick={() => void command(task, 'cancel')}>Cancel task</button>}
      </div>
      {task.status === 'awaiting_input' && <div className="flex flex-wrap gap-2">
        {task.input_wait?.choices.map(choice => <button key={choice.id} className={`${control} bg-[var(--tape-chip)]`} disabled={!!busy} onClick={() => void command(task, 'reply', choice.id)}>{choice.label}</button>)}
        <span className="self-center text-xs text-[var(--tape-activity)]">You can also reply in the chat.</span>
      </div>}
    </div>)}
  </section>;
}
