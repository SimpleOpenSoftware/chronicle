import { useEffect, useState } from 'react'
import { CheckCircle, Circle, Play, RefreshCw, Smartphone, Square } from 'lucide-react'
import { systemApi } from '../services/api'
import { Button, Card, MetadataChip } from './ui'

interface RemoteControlData {
  available: boolean
  reason?: string
  detail?: string
  running?: boolean
  managed?: boolean
  session?: string
  dir?: string
  name?: string
  tmux_available?: boolean
  claude_available?: boolean
}

export default function RemoteControl({ isAdmin }: { isAdmin: boolean }) {
  const [data, setData] = useState<RemoteControlData | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    try {
      const res = await systemApi.getRemoteControl()
      setData(res.data)
    } catch {
      setData(null)
    } finally {
      setLoaded(true)
    }
  }

  useEffect(() => { if (isAdmin) load() }, [isAdmin])

  const act = async (action: 'start' | 'stop' | 'restart') => {
    setBusy(true)
    setError(null)
    try {
      const res = await systemApi.remoteControlAction(action)
      setData({ available: true, ...res.data })
    } catch (e: any) {
      setError(e.response?.data?.detail ?? e.message)
    } finally {
      setBusy(false)
    }
  }

  // Hide entirely when not admin, still loading, or the agent isn't configured.
  if (!isAdmin || !loaded || !data || !data.available) return null

  const running = !!data.running
  const missingDeps = data.tmux_available === false || data.claude_available === false

  return (
    <Card raised padded={false} className="p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center">
        <Smartphone className="h-5 w-5 mr-2 text-blue-600" />
        Claude Remote Control
      </h3>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
        Run a <code className="px-1 bg-gray-100 dark:bg-gray-700 rounded">claude remote-control</code> server
        on this host so you can spawn new Claude Code sessions from the Claude mobile app (Code tab). You'll
        get a push notification when a session is live.
      </p>

      {missingDeps && (
        <div className="mb-3 text-sm text-yellow-700 dark:text-yellow-400">
          {data.claude_available === false && 'claude CLI not found on host. '}
          {data.tmux_available === false && 'tmux not found on host. '}
          Install them on the machine running the service-manager agent.
        </div>
      )}

      <div className="p-3 bg-gray-50 dark:bg-gray-700 rounded-md flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center space-x-3 min-w-0">
          {running ? (
            <CheckCircle className="h-5 w-5 text-green-500 flex-shrink-0" />
          ) : (
            <Circle className="h-5 w-5 text-gray-400 flex-shrink-0" />
          )}
          <div className="min-w-0">
            <div className="font-medium text-gray-900 dark:text-gray-100">
              {running ? 'Running' : 'Stopped'}
              {data.name && <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">{data.name}</span>}
              {data.managed && (
                <MetadataChip className="ml-2">auto-start on boot</MetadataChip>
              )}
            </div>
            {data.dir && (
              <div className="text-sm text-gray-600 dark:text-gray-400 truncate">
                sessions run in <code className="text-xs">{data.dir}</code>
                {data.session && <> · tmux <code className="text-xs">{data.session}</code></>}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {running ? (
            <>
              <Button
                variant="primary"
                onClick={() => act('restart')}
                disabled={busy}
                icon={<RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />}
              >
                Restart
              </Button>
              <Button
                variant="danger"
                onClick={() => act('stop')}
                disabled={busy}
                icon={<Square className="h-3.5 w-3.5" />}
              >
                Stop
              </Button>
            </>
          ) : (
            <button
              onClick={() => act('start')}
              disabled={busy || missingDeps}
              className="flex items-center gap-1 px-3 py-1.5 text-sm bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              <span>Start</span>
            </button>
          )}
        </div>
      </div>

      {error && <div className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</div>}

      <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
        Backed by a tmux session (<code className="text-xs">tmux attach -t {data.session ?? 'chronicle-rc'}</code> at
        the desktop). Make it survive reboots from the wizard's "auto-start on boot" step, or{' '}
        <code className="text-xs">./services remote-control install</code>.
      </p>
    </Card>
  )
}
