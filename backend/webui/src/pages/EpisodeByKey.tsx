import { useEffect } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { timelineApi } from '../services/api'
import { Button } from '../components/ui'

/**
 * Follow a durable `episode_key` to whatever currently covers it.
 *
 * `episode_id` names one generation's row and is replaced by reanalysis, so links that
 * must survive — vault notes, bookmarks, shared URLs — carry the key instead. Usually
 * this resolves to a single episode and the page is only a redirect. When a split
 * replaced the key with several claims there is no single right answer, so the choice
 * is shown rather than guessed at.
 */
export default function EpisodeByKey() {
  const { episodeKey = '' } = useParams()
  const navigate = useNavigate()

  const query = useQuery({
    queryKey: ['timeline', 'key', episodeKey],
    queryFn: async () => (await timelineApi.resolveEpisodeKey(episodeKey)).data,
    enabled: !!episodeKey,
    retry: false,
  })

  const resolved = query.data?.resolved ? query.data : null
  const episodeId = resolved?.episode_id

  useEffect(() => {
    if (episodeId) navigate(`/timeline/${episodeId}`, { replace: true })
  }, [episodeId, navigate])

  if (query.isLoading || episodeId) {
    return <div className="text-sm text-gray-500 dark:text-gray-400">Resolving episode…</div>
  }

  const successors =
    query.data && !query.data.resolved ? query.data.successor_keys : []

  return (
    <div className="space-y-4">
      <Button
        variant="secondary"
        onClick={() => navigate('/timeline')}
        icon={<ArrowLeft className="h-4 w-4" />}
      >
        Back to Timeline
      </Button>
      {successors.length > 0 ? (
        <div className="space-y-3">
          <p className="text-sm text-gray-600 dark:text-gray-300">
            This episode was replaced. It now continues as {successors.length}{' '}
            {successors.length === 1 ? 'episode' : 'episodes'}.
          </p>
          <ul className="space-y-2">
            {successors.map(key => (
              <li key={key}>
                <Link
                  to={`/timeline/key/${key}`}
                  className="text-sm text-blue-600 hover:underline dark:text-blue-400"
                >
                  Episode {successors.indexOf(key) + 1}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="rounded-lg border border-dashed border-gray-300 p-6 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
          {query.isError
            ? (query.error as { response?: { status?: number } }).response?.status === 404
              ? 'No episode was found for this link.'
              : 'Could not resolve this episode link.'
            : 'This episode was removed and was not replaced.'}
          {query.isError && (query.error as { response?: { status?: number } }).response?.status !== 404 && <Button className="mt-3" variant="secondary" onClick={() => query.refetch()}>Retry</Button>}
        </div>
      )}
    </div>
  )
}
