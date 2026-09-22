import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { MessageCircle } from 'lucide-react'
import { chatApi, ChatSourceRef } from '../services/api'
import { Button } from './ui'

/** All source entry points create the same persisted chat attachment. */
export default function AskAboutSource({ source }: { source: ChatSourceRef }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [params] = useSearchParams()
  const cache = useQueryClient()
  const space = params.get('memory_space_id') || undefined
  const create = useMutation({
    mutationFn: () => chatApi.createSession(undefined, [source], space),
    onSuccess: ({ data }) => {
      void cache.invalidateQueries({ queryKey: ['chat', 'sessions'] })
      const query = new URLSearchParams({ session: data.session_id })
      if (space) query.set('memory_space_id', space)
      navigate(`/chat?${query}`, { state: { from: location.pathname + location.search } })
    },
  })
  const detail = (create.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  return <div className="shrink-0">
    <Button size="sm" variant="secondary" disabled={create.isPending} onClick={() => create.mutate()} icon={<MessageCircle className="h-4 w-4" />}>
      {create.isPending ? 'Opening chat…' : 'Ask about this'}
    </Button>
    {create.isError && <p role="alert" className="mt-2 max-w-sm text-sm text-red-700 dark:text-red-300">{detail || 'Could not open this discussion. Try again.'}</p>}
  </div>
}
