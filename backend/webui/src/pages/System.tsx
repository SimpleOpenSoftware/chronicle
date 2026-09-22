import { useState, useEffect, useRef, useCallback } from 'react'
import { Activity, RefreshCw, CheckCircle, XCircle, AlertCircle, Users, Database, MoreVertical, RotateCcw, Power, Smartphone, Copy, Check } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import { useSystemData, useRestartWorkers, useRestartBackend, useBackendVersion } from '../hooks/useSystem'
import { systemApi } from '../services/api'
import ExternalServices from '../components/ExternalServices'
import RemoteControl from '../components/RemoteControl'
import { Alert, Button, IconButton, Modal } from '../components/ui'

function getBackendHttpUrl(): string {
  const { protocol, hostname, port } = window.location

  const isStandardPort =
    (protocol === 'https:' && (port === '' || port === '443')) ||
    (protocol === 'http:' && (port === '' || port === '80'))

  const basePath = import.meta.env.BASE_URL
  if (isStandardPort && basePath && basePath !== '/') {
    return `${protocol}//${hostname}`
  }

  if (import.meta.env.VITE_BACKEND_URL) {
    const url = import.meta.env.VITE_BACKEND_URL as string
    if (url.startsWith('/') || url === '') {
      return `${protocol}//${hostname}${port ? `:${port}` : ''}`
    }
    return url
  }

  if (isStandardPort) {
    return `${protocol}//${hostname}`
  }

  if (port === '5173') {
    return `${protocol}//${hostname}:8000`
  }

  return `${protocol}//${hostname}${port ? `:${port}` : ''}`
}

interface ServiceStatus {
  healthy: boolean
  message?: string
  status?: string
  provider?: string
  model?: string
}

export default function System() {
  const { isAdmin } = useAuth()
  const { isDark } = useTheme()
  const [copied, setCopied] = useState(false)
  const backendUrl = getBackendHttpUrl()

  // QR payload is a JSON bundle the mobile app parses (it also still accepts a
  // bare URL for backwards compatibility). serviceManagerUrl is the backend host
  // on :8775 so the app can auto-discover the agent. The SM token is NOT included
  // — it's a server-side secret and is not exposed to the browser.
  const qrPayload = (() => {
    try {
      const u = new URL(backendUrl)
      // The service-manager agent serves plain HTTP on the tailnet (no TLS).
      const serviceManagerUrl = `http://${u.hostname}:8775`
      return JSON.stringify({ backendUrl, serviceManagerUrl })
    } catch {
      return backendUrl
    }
  })()

  const handleCopyUrl = async () => {
    try {
      await navigator.clipboard.writeText(backendUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      const textArea = document.createElement('textarea')
      textArea.value = backendUrl
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  // TanStack Query hooks for data fetching
  const { data: systemData, isLoading: loading, error: systemError, refetch: refetchSystem, dataUpdatedAt } = useSystemData(isAdmin)

  // Restart mutations
  const restartWorkersMutation = useRestartWorkers()
  const restartBackendMutation = useRestartBackend()

  // Backend build version (muted chip in the header)
  const { data: backendVersion } = useBackendVersion()

  // UI state
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmModal, setConfirmModal] = useState<'workers' | 'backend' | 'both' | null>(null)
  const [restartingBackend, setRestartingBackend] = useState(false)
  const [workerBanner, setWorkerBanner] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Derive state from query results
  const healthData = systemData?.healthData ?? null
  const readinessData = systemData?.readinessData ?? null
  const metricsData = systemData?.metricsData ?? null
  const configDiagnostics = systemData?.configDiagnostics ?? null
  const activeClientCount = systemData?.activeClientCount ?? null
  const unhealthyServices = Object.entries((healthData?.services ?? {}) as Record<string, ServiceStatus>).filter(([, status]) => !status.healthy)
  const error = systemError?.message ?? null
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null

  const loadSystemData = () => refetchSystem()

  // Close menu on click outside
  useEffect(() => {
    if (!menuOpen) return
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [menuOpen])

  // Close modal on ESC
  useEffect(() => {
    if (!confirmModal) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setConfirmModal(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [confirmModal])

  // Poll health during backend restart
  const pollHealth = useCallback(async () => {
    setRestartingBackend(true)
    // Wait for the backend to actually go down
    await new Promise(r => setTimeout(r, 3000))

    let attempts = 0
    const maxAttempts = 60
    const poll = async () => {
      while (attempts < maxAttempts) {
        attempts++
        try {
          await systemApi.getHealth()
          // Backend is back
          setRestartingBackend(false)
          refetchSystem()
          return
        } catch {
          // Still down, wait and retry
          await new Promise(r => setTimeout(r, 2000))
        }
      }
      // Timed out
      setRestartingBackend(false)
    }
    await poll()
  }, [refetchSystem])

  const handleRestartWorkers = () => {
    setConfirmModal(null)
    restartWorkersMutation.mutate(undefined, {
      onSuccess: () => {
        setWorkerBanner(true)
        setTimeout(() => setWorkerBanner(false), 8000)
      },
    })
  }

  const handleRestartBackend = () => {
    setConfirmModal(null)
    restartBackendMutation.mutate(undefined, {
      onSuccess: () => {
        pollHealth()
      },
    })
  }

  const handleRestartBoth = () => {
    setConfirmModal(null)
    restartWorkersMutation.mutate(undefined, {
      onSuccess: () => {
        restartBackendMutation.mutate(undefined, {
          onSuccess: () => {
            pollHealth()
          },
        })
      },
    })
  }

  const getStatusIcon = (healthy: boolean) => {
    return healthy
      ? <CheckCircle className="h-5 w-5 text-green-500" />
      : <XCircle className="h-5 w-5 text-red-500" />
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'healthy': return 'text-green-600'
      case 'degraded': return 'text-yellow-600'
      default: return 'text-red-600'
    }
  }

  const getServiceDisplayName = (service: string) => {
    const displayNames: Record<string, string> = {
      'mongodb': 'MongoDB',
      'redis': 'Redis & RQ workers',
      'llm': 'LLM',
      'fast_llm': 'LLM (FAST)',
      'mem0': 'MEM0',
      'memory_service': 'Memory',
      'speech_to_text': 'Batch transcription',
      'speech_to_text_streaming': 'Streaming transcription',
      'speaker_recognition': 'Speaker recognition'
    }
    return displayNames[service] || service.replace(/_/g, ' ')
  }


  if (!isAdmin) {
    return (
      <div className="text-center">
        <Activity className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to view system status.
        </p>
      </div>
    )
  }

  // Count diagnostics for the collapsible summary
  const issueCount = configDiagnostics?.issues?.length ?? 0
  const warningCount = configDiagnostics?.warnings?.length ?? 0
  const infoCount = configDiagnostics?.info?.length ?? 0
  const totalDiagnostics = issueCount + warningCount + infoCount

  return (
    <div>
      {/* Backend Restarting Overlay */}
      {restartingBackend && (
        <div className="fixed inset-0 z-50 bg-gray-900/80 flex items-center justify-center">
          <div className="text-center">
            <RefreshCw className="h-12 w-12 text-blue-400 animate-spin mx-auto mb-4" />
            <h2 className="text-xl font-semibold text-white mb-2">
              Backend Restarting
            </h2>
            <p className="text-gray-300 text-sm">
              Waiting for the service to come back online...
            </p>
          </div>
        </div>
      )}

      {/* Worker Restart Success Banner */}
      {workerBanner && (
        <div className="mb-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md p-3 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <CheckCircle className="h-5 w-5 text-green-500" />
            <span className="text-sm text-green-700 dark:text-green-300">
              Worker restart signal sent. Workers will restart after finishing current jobs.
            </span>
          </div>
          <button onClick={() => setWorkerBanner(false)} className="text-green-500 hover:text-green-700">
            <XCircle className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center mb-6">
        <div className="flex items-center space-x-2">
          <Activity className="h-6 w-6 text-blue-600 flex-shrink-0" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            System Status
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-4">
          {backendVersion?.version && (
            <span
              className="text-xs font-mono px-2 py-1 rounded bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400"
              title={backendVersion.timestamp ? `Built ${backendVersion.timestamp}` : undefined}
            >
              Backend {backendVersion.version}
            </span>
          )}
          {lastUpdated && (
            <span className="text-sm text-gray-600 dark:text-gray-400">
              Last updated: {lastUpdated.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST
            </span>
          )}
          <Button
            variant="primary"
            size="md"
            icon={<RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />}
            onClick={loadSystemData}
            disabled={loading}
          >
            Refresh
          </Button>

          {/* Three-dot menu */}
          <div className="relative" ref={menuRef}>
            <IconButton label="System actions" onClick={() => setMenuOpen(prev => !prev)}>
              <MoreVertical className="h-5 w-5" />
            </IconButton>
            {menuOpen && (
              <div className="absolute right-0 mt-1 w-52 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-20 py-1">
                <button
                  onClick={() => { setMenuOpen(false); setConfirmModal('workers') }}
                  className="w-full flex items-center px-4 py-2 text-sm text-blue-600 dark:text-blue-400 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  <RotateCcw className="h-4 w-4 mr-2" />
                  Restart Workers
                </button>
                <button
                  onClick={() => { setMenuOpen(false); setConfirmModal('backend') }}
                  className="w-full flex items-center px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  <Power className="h-4 w-4 mr-2" />
                  Restart Backend
                </button>
                <div className="border-t border-gray-200 dark:border-gray-700 my-1" />
                <button
                  onClick={() => { setMenuOpen(false); setConfirmModal('both') }}
                  className="w-full flex items-center px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-gray-50 dark:hover:bg-gray-700"
                >
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Restart Both
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Confirmation Modals */}
      {confirmModal && (
        <Modal
          open
          onClose={() => setConfirmModal(null)}
          title={
            confirmModal === 'workers' ? 'Restart Workers'
              : confirmModal === 'backend' ? 'Restart Backend'
              : 'Restart Both'
          }
          icon={
            confirmModal === 'workers' ? (
              <div className="p-2 rounded-full bg-blue-100 dark:bg-blue-900/30">
                <RotateCcw className="h-5 w-5 text-blue-600 dark:text-blue-400" />
              </div>
            ) : confirmModal === 'backend' ? (
              <div className="p-2 rounded-full bg-red-100 dark:bg-red-900/30">
                <Power className="h-5 w-5 text-red-600 dark:text-red-400" />
              </div>
            ) : (
              <div className="p-2 rounded-full bg-red-100 dark:bg-red-900/30">
                <RefreshCw className="h-5 w-5 text-red-600 dark:text-red-400" />
              </div>
            )
          }
          footer={
            <>
              <Button variant="ghost" size="md" onClick={() => setConfirmModal(null)}>
                Cancel
              </Button>
              <Button
                variant={confirmModal === 'workers' ? 'primary' : 'danger'}
                size="md"
                onClick={
                  confirmModal === 'workers' ? handleRestartWorkers
                    : confirmModal === 'backend' ? handleRestartBackend
                    : handleRestartBoth
                }
              >
                {confirmModal === 'workers' ? 'Restart Workers'
                  : confirmModal === 'backend' ? 'Restart Backend'
                  : 'Restart Both'}
              </Button>
            </>
          }
        >
          {confirmModal === 'workers' ? (
            <>
              <p className="mb-2">
                Workers will finish their current jobs before restarting. This is safe to run at any time.
              </p>
              <p className="text-gray-500 dark:text-gray-500">
                Use this after changing plugin configuration or config.yml settings.
              </p>
            </>
          ) : (
            <>
              <p className="mb-2">
                {confirmModal === 'backend'
                  ? 'This will restart the entire backend process. The service will be briefly unavailable.'
                  : 'This will restart workers and then the backend. The service will be briefly unavailable.'}
              </p>
              <Alert tone="danger">
                Active WebSocket connections and streaming sessions will be dropped.
              </Alert>
            </>
          )}
        </Modal>
      )}

      {/* Error Message */}
      {error && (
        <Alert tone="danger" className="mb-6">
          {error}
        </Alert>
      )}

      {/* Overall Health Status */}
      {healthData && (
        <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-6 border border-gray-200 dark:border-gray-600 mb-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <Activity className="h-6 w-6 text-blue-600" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                System Health
              </h2>
            </div>
            <div className="flex items-center space-x-2">
              {healthData.status === 'healthy' && <CheckCircle className="h-6 w-6 text-green-500" />}
              {healthData.status === 'degraded' && <AlertCircle className="h-6 w-6 text-yellow-500" />}
              {healthData.status === 'critical' && <XCircle className="h-6 w-6 text-red-500" />}
              <span className={`font-semibold ${getStatusColor(healthData.status)}`}>
                {healthData.status.toUpperCase()}
              </span>
            </div>
          </div>
          {unhealthyServices.length > 0 && (
            <ul className="mt-3 space-y-1 text-sm text-gray-700 dark:text-gray-300">
              {unhealthyServices.map(([service]) => (
                <li key={service}><span className="font-medium">{getServiceDisplayName(service)}</span> is unavailable.</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {!loading && !healthData && <Alert tone="warning" className="mb-6">System health is unavailable. Refresh to retry.</Alert>}

      {/* Configuration Diagnostics (collapsible) */}
      {configDiagnostics && totalDiagnostics > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6 mb-6">
          <details>
            <summary className="cursor-pointer flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
                <AlertCircle className="h-5 w-5 mr-2 text-blue-600" />
                Configuration Diagnostics
              </h3>
              <div className="flex items-center space-x-3">
                <span className="text-sm text-gray-600 dark:text-gray-400">
                  {issueCount > 0 && `${issueCount} issue${issueCount !== 1 ? 's' : ''}`}
                  {issueCount > 0 && warningCount > 0 && ', '}
                  {warningCount > 0 && `${warningCount} warning${warningCount !== 1 ? 's' : ''}`}
                  {(issueCount > 0 || warningCount > 0) && infoCount > 0 && ', '}
                  {infoCount > 0 && `${infoCount} info`}
                </span>
                {configDiagnostics.overall_status === 'healthy' && <CheckCircle className="h-5 w-5 text-green-500" />}
                {configDiagnostics.overall_status === 'partial' && <AlertCircle className="h-5 w-5 text-yellow-500" />}
                {configDiagnostics.overall_status === 'unhealthy' && <XCircle className="h-5 w-5 text-red-500" />}
              </div>
            </summary>

            <div className="space-y-3 mt-4">
              {/* Errors */}
              {configDiagnostics.issues.map((issue: any, idx: number) => (
                <div key={`error-${idx}`} className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <XCircle className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <span className="text-xs font-semibold text-red-700 dark:text-red-300 uppercase">
                          {issue.component}
                        </span>
                        <span className="text-xs px-2 py-0.5 bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200 rounded">
                          ERROR
                        </span>
                      </div>
                      <p className="text-sm text-red-700 dark:text-red-300 mb-1">
                        {issue.message}
                      </p>
                      {issue.resolution && (
                        <p className="text-xs text-red-600 dark:text-red-400">
                          {issue.resolution}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {/* Warnings */}
              {configDiagnostics.warnings.map((warning: any, idx: number) => (
                <div key={`warning-${idx}`} className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <AlertCircle className="h-5 w-5 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <span className="text-xs font-semibold text-yellow-700 dark:text-yellow-300 uppercase">
                          {warning.component}
                        </span>
                        <span className="text-xs px-2 py-0.5 bg-yellow-200 dark:bg-yellow-800 text-yellow-800 dark:text-yellow-200 rounded">
                          WARNING
                        </span>
                      </div>
                      <p className="text-sm text-yellow-700 dark:text-yellow-300 mb-1">
                        {warning.message}
                      </p>
                      {warning.resolution && (
                        <p className="text-xs text-yellow-600 dark:text-yellow-400">
                          {warning.resolution}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {/* Info */}
              {configDiagnostics.info.map((info: any, idx: number) => (
                <div key={`info-${idx}`} className="bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-md p-3">
                  <div className="flex items-start space-x-2">
                    <CheckCircle className="h-5 w-5 text-gray-400 dark:text-gray-500 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                      <div className="flex items-center space-x-2 mb-1">
                        <span className="text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase">
                          {info.component}
                        </span>
                        <span className="text-xs px-2 py-0.5 bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300 rounded">
                          INFO
                        </span>
                      </div>
                      <p className="text-sm text-gray-600 dark:text-gray-400">
                        {info.message}
                      </p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </details>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Services Status */}
        {healthData?.services && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
              <Database className="h-5 w-5 mr-2 text-blue-600" />
              Services Status
            </h3>
            <div className="space-y-3">
              {Object.entries(healthData.services as Record<string, ServiceStatus>).map(([service, status]) => (
                <div key={service} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
                  <div className="flex items-center space-x-3">
                    {getStatusIcon(status.healthy)}
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {getServiceDisplayName(service)}
                    </span>
                  </div>
                  <div className="text-right">
                    <span className="block text-sm text-gray-600 dark:text-gray-400">
                      {status.healthy ? 'Available' : 'Unavailable'}
                    </span>
                    {(status.message || status.status || status.provider || status.model) && (
                      <details className="mt-1 text-xs text-gray-600 dark:text-gray-400">
                        <summary className="cursor-pointer">Technical details</summary>
                        <div className="mt-2 max-w-md space-y-1 break-words text-left">
                          {status.message && <div>{status.message}</div>}
                          {status.status && <div>Status: {status.status}</div>}
                          {status.provider && <div>Provider: {status.provider}</div>}
                          {status.model && <div>Model: {status.model}</div>}
                        </div>
                      </details>
                    )}
                    {service === 'redis' && (status as any).worker_count !== undefined && (
                      <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                        Workers: {(status as any).worker_count} total
                        ({(status as any).active_workers || 0} active, {(status as any).idle_workers || 0} idle)
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Active Clients — full device management lives on the Network page (Devices),
            which is the superset (online + offline, rename/forget, last-seen). Keep a
            live count here with a link, rather than duplicating the table. */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
            <Users className="h-5 w-5 mr-2 text-blue-600" />
            Active Clients{activeClientCount !== null ? ` (${activeClientCount})` : ''}
          </h3>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {activeClientCount === null
              ? (loading ? 'Checking connected clients…' : 'Connected-client count unavailable.')
              : `${activeClientCount} client${activeClientCount !== 1 ? 's' : ''} currently connected.`}{' '}
            <Link to="/network" className="text-blue-600 dark:text-blue-400 hover:underline">
              Manage all devices on the Network page →
            </Link>
          </p>
        </div>

        {/* Debug Metrics */}
        {metricsData?.debug_tracker && (
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
              Debug Metrics
            </h3>
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Total Files</div>
                <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {metricsData.debug_tracker.total_files}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Processed</div>
                <div className="text-2xl font-bold text-green-600">
                  {metricsData.debug_tracker.processed_files}
                </div>
              </div>
              <div className="bg-gray-50 dark:bg-gray-700 rounded-md p-3">
                <div className="text-sm text-gray-600 dark:text-gray-400">Failed</div>
                <div className="text-2xl font-bold text-red-600">
                  {metricsData.debug_tracker.failed_files}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* External Services (host service-manager agent) — lifecycle only.
          Provider config + ASR context now live on the Settings page. */}
      <div className="mb-6">
        <ExternalServices isAdmin={isAdmin} mode="lifecycle" />
      </div>

      {/* Claude remote-control session (spawn Claude Code sessions from the phone) */}
      <div className="mb-6">
        <RemoteControl isAdmin={isAdmin} />
      </div>

      {/* Connect App */}
      <div className="mt-6 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
        <div className="flex items-center space-x-3 mb-3">
          <Smartphone className="h-5 w-5 text-blue-600" />
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Connect App</h3>
        </div>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          Scan this QR code with the Chronicle mobile app to connect it to your backend.
        </p>
        <div className="flex flex-col items-center space-y-4">
          <div className="p-4 bg-white rounded-xl shadow-sm border border-gray-200 dark:border-gray-600">
            <QRCodeSVG
              value={qrPayload}
              size={200}
              level="M"
              fgColor={isDark ? '#1f2937' : '#111827'}
              bgColor="#ffffff"
            />
          </div>
          <div className="flex items-center space-x-2">
            <code className="px-3 py-1.5 bg-gray-100 dark:bg-gray-700 rounded text-sm text-gray-800 dark:text-gray-200 font-mono">
              {backendUrl}
            </code>
            <IconButton label="Copy URL" onClick={handleCopyUrl}>
              {copied ? (
                <Check className="h-4 w-4 text-green-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </IconButton>
          </div>
        </div>
      </div>

      {/* Raw Data (Debug) */}
      {readinessData && (
        <div className="mt-6 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <details>
            <summary className="cursor-pointer text-lg font-semibold text-gray-900 dark:text-gray-100 hover:text-blue-600">
              View Raw Readiness Data
            </summary>
            <pre className="mt-4 p-4 bg-gray-100 dark:bg-gray-700 rounded-md text-sm overflow-x-auto">
              {JSON.stringify(readinessData, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  )
}
