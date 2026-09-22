import { useSearchParams } from 'react-router-dom'
import { Trash2, Mic } from 'lucide-react'
import Recordings from './Recordings'
import Archive from './Archive'

export default function RecordingsRouter() {
  const [params, setParams] = useSearchParams()
  const trash = params.get('view') === 'trash'
  const select = (showTrash: boolean) => {
    const next = new URLSearchParams(params)
    showTrash ? next.set('view', 'trash') : next.delete('view')
    setParams(next)
  }
  return <div>
    <nav aria-label="Recording views" className="mb-5 flex gap-2 border-b border-[var(--tape-line)] pb-3">
      {[{ label: 'Recordings', icon: Mic, selected: !trash, trash: false }, { label: 'Trash', icon: Trash2, selected: trash, trash: true }].map(view => <button
        key={view.label} type="button" aria-pressed={view.selected} onClick={() => select(view.trash)}
        className={`inline-flex min-h-11 items-center gap-2 rounded-md px-3 text-sm font-medium ${view.selected ? 'bg-[var(--tape-selected)] text-[var(--tape-focus)]' : 'text-[var(--tape-activity)] hover:bg-[var(--tape-chip)]'}`}>
        <view.icon className="h-4 w-4" />{view.label}
      </button>)}
    </nav>
    {trash ? <Archive /> : <Recordings />}
  </div>
}
