import ServiceDeployments from '../components/ServiceDeployments'
import PrivacyScreeningStatus from '../components/PrivacyScreeningStatus'
import { useState } from 'react'
import { Network as NetworkIcon, RefreshCw, CheckCircle, XCircle, Wifi, WifiOff, Radio, Search, Server, Smartphone, Pencil, Trash2, Check, X, Monitor, Link2, Copy } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import { systemApi, clientsApi, deviceInputApi } from '../services/api'
import { timeAgo } from '../utils/timeAgo'
import { Alert, Button, IconButton } from '../components/ui'

interface DiscoveredService {
  name: string
  url: string | null
  reachable: boolean
  error?: string
  labels?: Record<string, string>
  host?: string
}

interface AdvertisedService {
  name: string
  port: number
  label?: string
}

interface ConnectedDevice {
  client_id: string
  device_name: string
  name?: string  // user-editable friendly label
  user_email?: string
  connected: boolean
  has_active_conversation: boolean
  last_seen?: number  // seconds since last inbound message
}

interface NetworkData {
  tailscale_available: boolean
  advertising: AdvertisedService[]
  discovered_services: DiscoveredService[]
  connected_devices?: ConnectedDevice[]
  error?: string
}

// Human-readable "last seen" from seconds-since-last-message.
function formatAgo(secs?: number): string {
  if (secs == null) return 'unknown'
  if (secs < 60) return `${Math.round(secs)}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}

function sourceStatusLabel(status: string) {
  return status.charAt(0).toUpperCase() + status.slice(1)
}

const SERVICE_DISPLAY: Record<string, { label: string; description: string }> = {
  'chronicle-backend': { label: 'Chronicle Backend', description: 'Core API and audio processing' },
  'chronicle-speaker': { label: 'Speaker Recognition', description: 'Voice identification service' },
  'chronicle-asr': { label: 'ASR Service', description: 'Offline speech-to-text' },
  'chronicle-llm': { label: 'Local LLM', description: 'Local LLM via llama.cpp' },
  'chronicle-tts': { label: 'Text-to-Speech', description: 'TTS synthesis service' },
  'chronicle-relay': { label: 'HAVPE Relay', description: 'ESP32 audio bridge' },
}

function getServiceDisplay(name: string) {
  if (SERVICE_DISPLAY[name]) return SERVICE_DISPLAY[name]
  // Derive a label from unknown chronicle-* names
  const suffix = name.replace('chronicle-', '')
  return { label: suffix.charAt(0).toUpperCase() + suffix.slice(1), description: '' }
}

// The advertising node's OWN view of a service's health, carried in the minidisc
// labels (health/running) that the node agent refreshes live. Distinct from the
// reachability icon, which is this backend's own probe of the service URL.
function healthBadge(health?: string): { text: string; cls: string } | null {
  switch (health) {
    case 'healthy':
      return { text: 'healthy', cls: 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300' }
    case 'partial':
      return { text: 'partial', cls: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300' }
    case 'unhealthy':
      return { text: 'unhealthy', cls: 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300' }
    case 'stopped':
      return { text: 'stopped', cls: 'bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300' }
    default:
      return null
  }
}

// Group discovered services by host
function groupByHost(services: DiscoveredService[]): Record<string, DiscoveredService[]> {
  const groups: Record<string, DiscoveredService[]> = {}
  for (const svc of services) {
    const host = svc.host || svc.url?.replace(/^https?:\/\//, '').replace(/:\d+$/, '') || 'unknown'
    if (!groups[host]) groups[host] = []
    groups[host].push(svc)
  }
  return groups
}

export default function Network() {
  const { isAdmin } = useAuth()
  const queryClient = useQueryClient()
  const [isScanning, setIsScanning] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftName, setDraftName] = useState('')

  const { data, isLoading, isError: networkError, refetch, dataUpdatedAt } = useQuery<NetworkData>({
    queryKey: ['system', 'network'],
    queryFn: async () => {
      const response = await systemApi.getNetworkDiscovery()
      return response.data
    },
    enabled: isAdmin,
    staleTime: 60_000,
  })
  const sources = useQuery({
    queryKey: ['device-input-sources'],
    queryFn: async () => (await deviceInputApi.getSources()).data.sources,
    enabled: isAdmin,
    refetchInterval: 30_000,
  })
  const pairing = useMutation({
    mutationFn: async () => (await deviceInputApi.createPairingCode()).data,
  })

  const startEdit = (d: ConnectedDevice) => {
    setEditingId(d.client_id)
    setDraftName(d.name || d.device_name || d.client_id)
  }
  const cancelEdit = () => {
    setEditingId(null)
    setDraftName('')
  }
  const saveEdit = async (clientId: string) => {
    const name = draftName.trim()
    if (name) await clientsApi.rename(clientId, name)
    cancelEdit()
    await queryClient.invalidateQueries({ queryKey: ['system', 'network'] })
  }
  const forgetDevice = async (clientId: string) => {
    if (!window.confirm('Forget this device? It will reappear if it reconnects.')) return
    await clientsApi.forget(clientId)
    await queryClient.invalidateQueries({ queryKey: ['system', 'network'] })
  }

  const handleScan = async () => {
    setIsScanning(true)
    try {
      await refetch()
    } finally {
      setIsScanning(false)
    }
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <NetworkIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view network status.
        </p>
      </div>
    )
  }

  const loading = isLoading || isScanning
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null
  const advertisedNames = new Set(data?.advertising?.map(s => s.name) || [])
  const nodeGroups = data?.discovered_services ? groupByHost(data.discovered_services) : {}

  return (
    <div>
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center space-x-2">
          <NetworkIcon className="h-6 w-6 text-blue-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Network
          </h1>
        </div>
        <div className="flex items-center space-x-4">
          {lastUpdated && (
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Last scan: {lastUpdated.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST
            </span>
          )}
          <Button
            variant="primary"
            size="md"
            onClick={handleScan}
            disabled={loading}
            icon={loading ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          >
            {loading ? 'Scanning...' : 'Scan Network'}
          </Button>
        </div>
      </div>

      <ServiceDeployments />

      {/* Capture inputs */}
      <section className="mb-6 rounded-lg border border-gray-200 bg-white p-4 sm:p-6 dark:border-gray-700 dark:bg-gray-800">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center text-lg font-semibold text-gray-900 dark:text-gray-100">
              <Monitor className="mr-2 h-5 w-5 text-blue-600" />
              Capture sources
            </h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Screens and libraries that contribute evidence to Chronicle.
            </p>
          </div>
          <Button
            variant="secondary"
            size="md"
            onClick={() => pairing.mutate()}
            disabled={pairing.isPending}
            icon={pairing.isPending ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
          >
            Pair ScreenPipe
          </Button>
        </div>

        {pairing.data && (
          <div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-gray-700 dark:border-blue-800 dark:bg-blue-950/30 dark:text-gray-200">
            <span>
              Pairing code <code className="mx-1 font-mono font-bold">{pairing.data.code}</code>
              expires {new Date(pairing.data.expires_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST.
            </span>
            <IconButton label="Copy pairing code" onClick={() => navigator.clipboard.writeText(pairing.data!.code)}>
              <Copy className="h-4 w-4" />
            </IconButton>
          </div>
        )}

        {pairing.isError && (
          <p className="mb-4 text-sm text-red-600 dark:text-red-400">Could not create a pairing code. Try again.</p>
        )}

        <div className="divide-y divide-gray-100 rounded-md border border-gray-200 dark:divide-gray-700 dark:border-gray-700">
          {(sources.data || []).map(source => (
            <div key={source.source_id} className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 px-3 py-3 sm:flex sm:items-start sm:px-4">
              <Monitor className="h-5 w-5 shrink-0 text-gray-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium text-gray-900 dark:text-gray-100">{source.name}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400">{source.provider} · {source.platform}</div>
                {source.provider === 'screenpipe' && <div className={`text-xs ${source.privacy_enabled_from ? 'text-gray-500 dark:text-gray-400' : 'text-amber-700 dark:text-amber-400'}`}>
                  {source.privacy_waiting_from
                    ? 'Waiting for local screening · processing held'
                    : source.privacy_enabled_from
                      ? 'Privacy screening enabled · unresolved captures held'
                      : 'Privacy screening not active · source unprotected'}
                </div>}
                {source.provider === 'screenpipe' && <PrivacyScreeningStatus value={source.health.privacy_screening} />}
              </div>
              <div className={`col-start-2 shrink-0 text-xs sm:text-right ${source.status === 'online' ? 'text-green-600 dark:text-green-400' : source.status === 'error' ? 'text-red-600 dark:text-red-400' : 'text-gray-500 dark:text-gray-400'}`}>
                <div className="font-medium">{sourceStatusLabel(source.status)}</div>
                {source.last_seen_at && <div>Last seen {timeAgo(source.last_seen_at)}</div>}
              </div>
            </div>
          ))}
          {sources.isError && <div className="px-4 py-5 text-sm text-red-600 dark:text-red-400">Capture sources could not be loaded.</div>}
          {sources.isLoading && (
            <div className="px-4 py-5 text-sm text-gray-500 dark:text-gray-400">Loading capture sources…</div>
          )}
          {!sources.isLoading && !sources.isError && !sources.data?.length && (
            <div className="px-4 py-5 text-sm text-gray-500 dark:text-gray-400">No capture sources paired.</div>
          )}
        </div>
      </section>

      {networkError && <Alert tone="warning" className="mb-6">Network discovery is unavailable. Scan again to retry.</Alert>}

      {(!networkError || data) && <>
      {/* Tailscale Status */}
      <div className={`rounded-lg p-4 border mb-6 ${
        loading && !data
          ? 'bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800'
          : data?.tailscale_available
            ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
            : 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-200 dark:border-yellow-800'
      }`}>
        <div className="flex items-center space-x-3">
          {loading && !data ? (
            <>
              <RefreshCw className="h-5 w-5 text-blue-600 dark:text-blue-400 animate-spin" />
              <div>
                <span className="font-medium text-blue-800 dark:text-blue-200">Scanning Network…</span>
                <p className="text-sm text-blue-600 dark:text-blue-400">
                  Checking for Tailscale and discovering services on your Tailnet.
                </p>
              </div>
            </>
          ) : data?.tailscale_available ? (
            <>
              <Wifi className="h-5 w-5 text-green-600 dark:text-green-400" />
              <div>
                <span className="font-medium text-green-800 dark:text-green-200">Tailscale Connected</span>
                <p className="text-sm text-green-600 dark:text-green-400">
                  Service discovery is available.
                </p>
              </div>
            </>
          ) : (
            <>
              <WifiOff className="h-5 w-5 text-yellow-600 dark:text-yellow-400" />
              <div>
                <span className="font-medium text-yellow-800 dark:text-yellow-200">{networkError ? 'Tailscale status unavailable' : 'Tailscale Not Detected'}</span>
                <p className="text-sm text-yellow-600 dark:text-yellow-400">
                  {networkError ? 'The discovery request failed.' : data?.error
                    ? data.error
                    : 'Install Tailscale and mount the socket to enable automatic service discovery across machines.'}
                </p>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Advertising (This Node) */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Radio className="h-5 w-5 mr-2 text-blue-600" />
          This Node is Advertising
        </h3>
        {data?.advertising && data.advertising.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {data.advertising.map((svc) => {
              const display = getServiceDisplay(svc.name)
              const displayLabel = svc.label || display.label
              return (
                <div key={svc.name} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div>
                    <div className="font-medium text-gray-900 dark:text-gray-100">{displayLabel}</div>
                    <div className="text-sm text-gray-500 dark:text-gray-400">Port {svc.port}</div>
                  </div>
                  <span className="text-xs px-2 py-1 bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200 rounded">
                    broadcasting
                  </span>
                </div>
              )
            })}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-center py-4">
            {data?.tailscale_available
              ? 'No services being advertised'
              : 'Tailscale required for service advertisement'}
          </p>
        )}
      </div>

      {/* Discovered Services — grouped by node */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Search className="h-5 w-5 mr-2 text-blue-600" />
          Discovered on Tailnet
        </h3>
        {Object.keys(nodeGroups).length > 0 ? (
          <div className="space-y-4">
            {Object.entries(nodeGroups).map(([host, allServices]) => {
              // The node agent advertises a `chronicle-node` self-entry carrying this
              // node's identity (arch/gpu) — pull it out for the header rather than
              // showing it as a service row.
              const nodeEntry = allServices.find(s => s.labels?.type === 'node')
              const services = allServices.filter(s => s.labels?.type !== 'node')
              const hasEdge = allServices.some(s => s.labels?.type === 'edge')
              const arch = nodeEntry?.labels?.arch
              const hasGpu = nodeEntry?.labels?.gpu === '1'
              return (
                <div key={host} className="border border-gray-200 dark:border-gray-600 rounded-lg overflow-hidden">
                  {/* Node header */}
                  <div className="flex items-center justify-between px-4 py-2.5 bg-gray-100 dark:bg-gray-700">
                    <div className="flex items-center space-x-2 flex-wrap">
                      <Server className="h-4 w-4 text-gray-500 dark:text-gray-400" />
                      <span className="font-medium text-gray-900 dark:text-gray-100 font-mono text-sm">{host}</span>
                      {nodeEntry && (
                        <span className="text-xs px-1.5 py-0.5 bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300 rounded">
                          managed node
                        </span>
                      )}
                      {hasEdge && (
                        <span className="text-xs px-1.5 py-0.5 bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300 rounded">
                          discovery only
                        </span>
                      )}
                      {arch && (
                        <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">{arch}</span>
                      )}
                      {hasGpu && (
                        <span className="text-xs px-1.5 py-0.5 bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300 rounded">
                          GPU
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                      {services.length} service{services.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  {/* Services on this node */}
                  <div className="divide-y divide-gray-100 dark:divide-gray-700">
                    {services.map((svc) => {
                      const display = getServiceDisplay(svc.name)
                      const isLocal = advertisedNames.has(svc.name)
                      const hb = healthBadge(svc.labels?.health)
                      return (
                        <div key={svc.name} className={`p-3 ${!svc.url ? 'opacity-60' : ''}`}>
                          <div className="flex items-center justify-between">
                            <div className="flex-1">
                              <div className="flex items-center space-x-2">
                                <span className="font-medium text-gray-900 dark:text-gray-100">
                                  {display.label}
                                </span>
                                {hb && (
                                  <span className={`text-xs px-1.5 py-0.5 rounded ${hb.cls}`}>
                                    Node: {hb.text}
                                  </span>
                                )}
                                {isLocal && svc.url && (
                                  <span className="text-xs px-1.5 py-0.5 bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 rounded">
                                    local
                                  </span>
                                )}
                              </div>
                              {display.description && (
                                <div className="text-sm text-gray-500 dark:text-gray-400">
                                  {display.description}
                                </div>
                              )}
                              {svc.url && (
                                <code className="text-xs text-gray-600 dark:text-gray-400 font-mono mt-1 block">
                                  {svc.url}
                                </code>
                              )}
                            </div>
                            <div className="ml-3 flex-shrink-0">
                              {svc.url ? (
                                <span className="inline-flex items-center gap-1 text-xs text-gray-600 dark:text-gray-300" title={svc.error || undefined}>
                                  {svc.reachable ? <CheckCircle className="h-4 w-4 text-green-500" /> : <XCircle className="h-4 w-4 text-yellow-500" />}
                                  Backend probe: {svc.reachable ? 'reachable' : 'unreachable'}
                                </span>
                              ) : (
                                <span className="text-xs text-gray-400 dark:text-gray-500">not found</span>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-center py-4">
            {loading
              ? 'Scanning Tailnet...'
              : data?.tailscale_available
                ? 'No services discovered. Make sure other machines are running Chronicle services.'
                : 'Tailscale required for service discovery'}
          </p>
        )}
      </div>

      {/* Devices */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
          <Smartphone className="h-5 w-5 mr-2 text-blue-600" />
          Devices
        </h3>
        {data?.connected_devices && data.connected_devices.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {data.connected_devices.map((device) => (
              <div key={device.client_id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                <div className="min-w-0">
                  {editingId === device.client_id ? (
                    <div className="flex items-center gap-1">
                      <input
                        autoFocus
                        value={draftName}
                        onChange={(e) => setDraftName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') saveEdit(device.client_id)
                          if (e.key === 'Escape') cancelEdit()
                        }}
                        className="px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 w-36"
                      />
                      <button onClick={() => saveEdit(device.client_id)} title="Save" className="p-1 text-green-600 hover:text-green-700">
                        <Check className="h-4 w-4" />
                      </button>
                      <button onClick={cancelEdit} title="Cancel" className="p-1 text-gray-500 hover:text-gray-700">
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5 group">
                      <span className="font-medium text-gray-900 dark:text-gray-100 truncate">{device.name || device.device_name}</span>
                      <button onClick={() => startEdit(device)} title="Rename" className="p-0.5 text-gray-400 hover:text-blue-600 opacity-0 group-hover:opacity-100">
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  )}
                  {device.user_email && (
                    <div className="text-sm text-gray-500 dark:text-gray-400 truncate">{device.user_email}</div>
                  )}
                  <code className="text-xs text-gray-400 dark:text-gray-500 font-mono">{device.client_id}</code>
                </div>
                <div className="ml-3 flex-shrink-0 flex flex-col items-end space-y-1">
                  {device.connected ? (
                    <span className="text-xs px-2 py-1 bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 rounded">
                      online
                    </span>
                  ) : (
                    <span className="text-xs px-2 py-1 bg-gray-100 text-gray-600 dark:bg-gray-600 dark:text-gray-300 rounded">
                      offline
                    </span>
                  )}
                  <span className="text-xs text-gray-400 dark:text-gray-500">
                    last seen {formatAgo(device.last_seen)}
                  </span>
                  {device.has_active_conversation && (
                    <span className="text-xs px-2 py-1 bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200 rounded">
                      streaming
                    </span>
                  )}
                  <button onClick={() => forgetDevice(device.client_id)} title="Forget device" className="p-0.5 text-gray-400 hover:text-red-600">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-center py-4">
            No devices registered yet
          </p>
        )}
      </div>

      </>}
    </div>
  )
}
