import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { chatApi } from '../services/api'

export interface ChatRun {
  run_id: string
  question: string
  status: string
  started_at: string
  error?: string
  recording_degraded?: boolean
}

interface RunStep {
  step_id: string
  parent_id?: string
  sequence: number
  kind: string
  name: string
  status: string
  duration_ms?: number
  request_payload?: unknown
  response_payload?: unknown
  request_unavailable?: boolean
  response_unavailable?: boolean
}

const stamp = (value: string) => new Date(value).toLocaleString('en-IN', {
  timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
}) + ' IST'

function Payload({ label, value }: { label: string; value: unknown }) {
  if (value === undefined) return null
  return <details className="mt-2">
    <summary className="cursor-pointer py-2 text-xs text-[var(--tape-focus)]">{label}</summary>
    <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded bg-[var(--tape-chip)] p-3 text-xs [overflow-wrap:anywhere]">{JSON.stringify(value, null, 2)}</pre>
  </details>
}

export default function ChatRunViewer({ sessionId, runId, onClose }: {
  sessionId: string; runId: string; onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const query = useQuery({
    queryKey: ['chat', 'run', sessionId, runId],
    queryFn: () => chatApi.getRun(sessionId, runId).then(r => r.data as ChatRun & { steps: RunStep[] }),
    refetchInterval: q => q.state.data?.status === 'running' ? 3000 : false,
    retry: false,
  })
  useEffect(() => {
    dialog.current?.showModal()
    return () => { dialog.current?.close() }
  }, [])
  const run = query.data
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(run, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `chronicle-run-${runId}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return <dialog ref={dialog} onCancel={onClose} aria-labelledby="chat-run-title" className="fixed inset-0 m-auto max-h-[90dvh] w-[min(52rem,calc(100%-1rem))] max-w-none overflow-y-auto rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-0 text-[var(--tape-ink)] backdrop:bg-black/40">
    <header className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-[var(--tape-line)] bg-[var(--tape-paper)] p-4">
      <div className="min-w-0"><h2 id="chat-run-title" className="font-semibold">Chat run</h2>
        <p className="mt-1 break-words text-sm text-[var(--tape-activity)]">{run?.question || 'Loading execution record…'}</p></div>
      <button autoFocus onClick={onClose} aria-label="Close chat run" className="shrink-0 rounded p-2 hover:bg-[var(--tape-chip)]"><X className="h-5 w-5" /></button>
    </header>
    <div className="p-4">
      {query.isPending && <p role="status">Loading run…</p>}
      {query.isError && <p role="alert">This run could not be loaded. <button className="underline" onClick={() => void query.refetch()}>Retry</button></p>}
      {run && <>
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <p><strong className="capitalize">{run.status}</strong> · {stamp(run.started_at)}</p>
          <button className="min-h-10 text-[var(--tape-focus)] underline" onClick={download}>Export JSON</button>
        </div>
        {run.error && <p role="alert" className="mt-3 text-sm text-red-700 dark:text-red-300">{run.error}</p>}
        {run.recording_degraded && <p role="alert" className="mt-3 text-sm text-amber-800 dark:text-amber-300">Some trace details could not be saved. This record is incomplete.</p>}
        <p className="my-4 text-xs text-[var(--tape-activity)]">Inputs and results retained with this run. Personal context is included in the export. Run history is kept until you delete it or its chat.</p>
        <ol className="divide-y divide-[var(--tape-line)]">
          {run.steps.map(step => <li key={step.step_id} className={`py-3 ${step.parent_id ? 'border-l border-[var(--tape-line)] pl-4' : ''}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium">{step.sequence}. {step.name}</h3>
              <span className="text-xs text-[var(--tape-activity)]">{step.kind} · {step.status}{step.duration_ms !== undefined ? ` · ${(step.duration_ms / 1000).toFixed(1)}s` : ''}</span>
            </div>
            {(step.request_unavailable || step.response_unavailable) && <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">A retained payload is unavailable.</p>}
            {step.parent_id && <p className="mt-1 text-xs text-[var(--tape-activity)]">Within {run.steps.find(parent => parent.step_id === step.parent_id)?.name || 'parent step'}</p>}
            <Payload label="Exact input" value={step.request_payload} />
            <Payload label="Exact result" value={step.response_payload} />
          </li>)}
        </ol>
      </>}
    </div>
  </dialog>
}
