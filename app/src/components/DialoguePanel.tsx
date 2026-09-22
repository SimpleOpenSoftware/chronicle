import React, { useEffect, useState } from 'react';
import { View, Text, TextInput, ScrollView } from 'react-native';
import { createClientEventIdValue } from '@/protocol/audioV2Socket';
import { Button } from './ui';
import { useTheme } from '@/theme';
import { deriveBaseUrl, fetchAuthed } from '@/services/auth';
import { ConversationPhase, type ConversationState } from '@/protocol/audioV2';

type Thread = { session_id: string; title: string; memory_space_id?: string; interaction_version?: number };
type Utterance = { message_id: string; role: 'user' | 'assistant'; content: string };
type Task = { id: string; title: string; revision: number; status: string; input_wait?: { choices: { id: string; label: string }[] } };

/** Uses Chat's existing transcript; voice only binds the selected thread on explicit start. */
export default function DialoguePanel({ backendUrl, voice, captureReady, startVoice, endVoice }: {
  backendUrl: string; voice: ConversationState | null; captureReady: boolean;
  startVoice: (thread: string) => void; endVoice: () => void;
}) {
  const theme = useTheme();
  const [threads, setThreads] = useState<Thread[]>([]), [selected, select] = useState('');
  const [messages, setMessages] = useState<Utterance[]>([]), [tasks, setTasks] = useState<Task[]>([]);
  const [text, setText] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const base = deriveBaseUrl(backendUrl) + '/api/chat';
  const engaged = !!voice?.interactionId && voice.phase !== ConversationPhase.ENDED;
  const writable = threads.find(thread => thread.session_id === selected)?.interaction_version === 2;
  const request = async (path: string, body?: object) => {
    const response = await fetchAuthed(base + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Dialogue request failed');
    return value;
  };
  useEffect(() => { let active = true; request('/sessions').then(rows => { if (active) setThreads(rows); }).catch(cause => { if (active) setError(cause.message); }); return () => { active = false; }; }, [base]);
  useEffect(() => { if (voice?.threadId) select(voice.threadId); }, [voice?.threadId]);
  useEffect(() => {
    let active = true;
    setMessages([]); setTasks([]);
    if (!selected) return;
    const refresh = async () => {
      try {
        const [utterances, state] = await Promise.all([request(`/sessions/${selected}/messages`), request(`/sessions/${selected}/dialogue`)]);
        if (active) { setMessages(utterances); setTasks(state.tasks); }
      } catch (cause) { if (active) { setMessages([]); setTasks([]); setError(cause instanceof Error ? cause.message : String(cause)); } }
    };
    void refresh();
    const timer = setInterval(refresh, 1500);
    return () => { active = false; clearInterval(timer); };
  }, [selected, base]);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError('');
    try { await operation(); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const command = (task: Task, action: string, choice_id?: string) => act(async () => {
    const state = await request(`/sessions/${selected}/dialogue/tasks/${task.id}/commands`, { id: createClientEventIdValue(), revision: task.revision, action, choice_id });
    setTasks(state.tasks);
  });
  return <View style={{ gap: 10, marginTop: 16 }}>
    <Text style={{ color: theme.color.text.primary, fontWeight: '600' }}>Dialogue</Text>
    <ScrollView horizontal contentContainerStyle={{ gap: 8 }}>
      <Button children="New dialogue" variant="secondary" disabled={busy || engaged} onPress={() => void act(async () => {
        const thread = await request('/sessions', { title: 'Dialogue' }); setThreads(previous => [thread, ...previous]); select(thread.session_id);
      })} />
      {threads.map(thread => <Button key={thread.session_id} children={thread.title} variant={selected === thread.session_id ? 'primary' : 'secondary'} disabled={engaged || busy} onPress={() => select(thread.session_id)} />)}
    </ScrollView>
    {!!selected && <>
      <ScrollView style={{ maxHeight: 280 }} contentContainerStyle={{ gap: 12 }}>
        {messages.map(message => <Text key={message.message_id} selectable style={{ color: theme.color.text.primary }}>
          {message.role === 'user' ? 'You: ' : 'Chronicle: '}{message.content}
        </Text>)}
      </ScrollView>
      {tasks.map(task => <View key={task.id} style={{ gap: 6 }}>
        <Text style={{ color: theme.color.text.muted }}>{task.title} · {task.status.replaceAll('_', ' ')}</Text>
        <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
          {task.input_wait?.choices.map(choice => <Button key={choice.id} children={choice.label} disabled={busy} onPress={() => void command(task, 'reply', choice.id)} />)}
          {(task.status === 'paused' || task.status === 'awaiting_input' || task.status === 'running') && <>
            <Button children={task.status === 'paused' ? 'Resume' : 'Pause'} variant="secondary" disabled={busy} onPress={() => void command(task, task.status === 'paused' ? 'resume' : 'pause')} />
            <Button children="Cancel" variant="secondary" disabled={busy} onPress={() => void command(task, 'cancel')} />
          </>}
        </View>
      </View>)}
      <TextInput accessibilityLabel="Dialogue utterance" placeholder="Message Chronicle" placeholderTextColor={theme.color.text.muted} value={text} onChangeText={setText} multiline style={{ color: theme.color.text.primary, borderWidth: 1, borderColor: theme.color.text.muted, borderRadius: 8, padding: 10 }} />
      {!writable && <Text style={{ color: theme.color.text.muted }}>This historical dialogue is read-only.</Text>}
      <Button children="Send" disabled={!writable || busy || !text.trim()} onPress={() => void act(async () => {
        await request('/completions', { session_id: selected, messages: [{ role: 'user', content: text }], stream: false }); setText('');
      })} />
      <Button children={engaged ? 'End voice' : 'Continue by voice'} variant="secondary" disabled={(!writable && !engaged) || !captureReady || busy} onPress={() => engaged ? endVoice() : startVoice(selected)} />
      {!captureReady && <Text style={{ color: theme.color.text.muted }}>Start phone audio to continue this dialogue by voice.</Text>}
    </>}
    {!!error && <Text accessibilityRole="alert" style={{ color: theme.color.status.danger.fg }}>{error}</Text>}
  </View>;
}
