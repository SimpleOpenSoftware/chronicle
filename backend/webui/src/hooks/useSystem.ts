import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { systemApi } from '../services/api'

interface HealthService {
  healthy: boolean
  status?: string
}

export interface SystemHealthSummary {
  services: Record<string, HealthService>
  overall_healthy: boolean
  status: 'healthy' | 'degraded' | 'critical'
}

function parseSystemHealth(data: unknown): SystemHealthSummary {
  const record = data as Partial<SystemHealthSummary> | null
  if (!record || typeof record !== 'object'
    || !['healthy', 'degraded', 'critical'].includes(String(record.status))
    || typeof record.overall_healthy !== 'boolean'
    || !record.services || typeof record.services !== 'object' || Array.isArray(record.services)
    || Object.values(record.services).some(service => !service || typeof service !== 'object' || typeof service.healthy !== 'boolean')) {
    throw new Error('Invalid system health response')
  }
  return record as SystemHealthSummary
}

/** Lightweight fleet heartbeat for persistent navigation chrome. */
export function useSystemHealthSummary(isAdmin: boolean) {
  return useQuery<SystemHealthSummary>({
    queryKey: ['system', 'healthSummary'],
    queryFn: async () => {
      const response = await systemApi.getHealth()
      return parseSystemHealth(response.data)
    },
    enabled: isAdmin,
    staleTime: 15_000,
    refetchInterval: 30_000,
    refetchIntervalInBackground: true,
    retry: 1,
  })
}

export function useSystemData(isAdmin: boolean) {
  return useQuery({
    queryKey: ['system', 'data'],
    queryFn: async () => {
      const [health, readiness, metrics, diagnostics, clients] = await Promise.allSettled([
        systemApi.getHealth().then(response => ({ data: parseSystemHealth(response.data) })),
        systemApi.getReadiness(),
        systemApi.getMetrics().catch(() => ({ data: null })),
        systemApi.getConfigDiagnostics().catch(() => ({ data: null })),
        systemApi.getActiveClients(),
      ])

      return {
        healthData: health.status === 'fulfilled' ? health.value.data : null,
        readinessData: readiness.status === 'fulfilled' ? readiness.value.data : null,
        metricsData: metrics.status === 'fulfilled' ? metrics.value.data : null,
        configDiagnostics: diagnostics.status === 'fulfilled' ? diagnostics.value.data : null,
        activeClientCount: clients.status === 'fulfilled' ? clients.value.data.total_count : null,
      }
    },
    enabled: isAdmin,
    staleTime: 60_000,
  })
}

export function useDiarizationSettings() {
  return useQuery({
    queryKey: ['system', 'diarizationSettings'],
    queryFn: async () => {
      const response = await systemApi.getDiarizationSettings()
      if (response.data.status === 'success') {
        return response.data.settings
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export function useMemoryProvider() {
  return useQuery({
    queryKey: ['system', 'memoryProvider'],
    queryFn: async () => {
      const response = await systemApi.getMemoryProvider()
      if (response.data.status === 'success') {
        return {
          currentProvider: response.data.current_provider,
          availableProviders: response.data.available_providers,
        }
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export function useMiscSettings() {
  return useQuery({
    queryKey: ['system', 'miscSettings'],
    queryFn: async () => {
      const response = await systemApi.getMiscSettings()
      if (response.data.status === 'success') {
        return response.data.settings
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

export function useTimelineGroupingSettings() {
  return useQuery({
    queryKey: ['system', 'timelineGroupingSettings'],
    queryFn: async () => {
      const response = await systemApi.getTimelineGroupingSettings()
      return response.data.status === 'success' ? response.data.settings : null
    },
    staleTime: 5 * 60_000,
  })
}

export function useLLMOperations() {
  return useQuery({
    queryKey: ['system', 'llmOperations'],
    queryFn: async () => {
      const response = await systemApi.getLLMOperations()
      if (response.data.status === 'success') {
        return response.data
      }
      return null
    },
    staleTime: 5 * 60_000,
  })
}

// ── Model registry (Chronicle model configuration) ──────────────────────────
export type ModelType = 'llm' | 'embedding' | 'stt' | 'stt_stream' | 'tts'

export interface ModelView {
  name: string
  model_type: ModelType
  model_provider: string
  model_name: string
  model_url: string
  deployment?: string | null
  deployment_endpoint?: string | null
  api_family: string
  api_key: string // masked ('••••••••') for inline secrets; ${oc.env:...} shown verbatim
  api_key_is_set: boolean
  api_key_is_ref: boolean
  description: string | null
  model_params: Record<string, any>
  capabilities: string[]
  embedding_dimensions: number | null
  model_output: string | null
  thinking: boolean
  source: 'config' | 'default'
  is_default: boolean
}

export interface ModelsData {
  defaults: Record<string, string | null>
  models: Record<ModelType, ModelView[]>
  status: string
}

export function useModels(isAdmin: boolean) {
  return useQuery<ModelsData | null>({
    queryKey: ['system', 'models'],
    queryFn: async () => {
      const response = await systemApi.getModels()
      return response.data?.status === 'success' ? response.data : null
    },
    enabled: isAdmin,
    staleTime: 5 * 60_000,
  })
}

export interface ExternalServiceProvider {
  env_key: string
  current: string
  streaming_current?: string
  // `local` = the provider runs a local container (switching to/from it is heavy:
  // start/stop, possibly a model download). Absent/false = cloud (config-only switch).
  available: { key: string; label: string; local?: boolean }[]
  // Streaming-lane (stt_stream) provider options; present only for asr-services.
  streaming_available?: { key: string; label: string; local?: boolean }[]
}

export interface ExternalService {
  name: string
  description: string
  ports: string[]
  // Canonical browser-facing UI on the owning node. The node agent resolves
  // Tailscale host + Caddy/HTTP fallback; clients must not guess from raw ports.
  ui_url?: string | null
  enabled: boolean
  health: 'healthy' | 'partial' | 'unhealthy' | 'stopped' | 'starting'
  health_detail: string
  provider: ExternalServiceProvider | null
  // The node (host) this service runs on, and whether it's a remote cluster node.
  node?: string | null
  remote?: boolean
}

export interface ServiceOperation {
  id: string
  service: string
  action: string
  status: 'running' | 'done' | 'failed'
  ok: boolean | null
  log: string
  phase?: string
  // Node the operation runs on — used to poll the owning node's agent.
  node?: string | null
}

export interface ExternalServicesData {
  available: boolean
  reason?: string
  detail?: string
  services?: ExternalService[]
  operation?: ServiceOperation | null
}

export function useExternalServices(isAdmin: boolean, pollWhileBusy: boolean) {
  return useQuery<ExternalServicesData>({
    queryKey: ['system', 'externalServices'],
    queryFn: async () => {
      const response = await systemApi.getExternalServices()
      return response.data
    },
    enabled: isAdmin,
    staleTime: 30_000,
    // Poll while an operation runs or a service is still booting (model loading),
    // so health flips to healthy without a manual refresh.
    refetchInterval: query => {
      if (pollWhileBusy) return 3_000
      const services = query.state.data?.services
      return services?.some(s => s.health === 'starting') ? 5_000 : false
    },
  })
}

// ── Node code-version + update flow ─────────────────────────────────────────
export interface UpdateCheckResult {
  available: boolean
  reason?: string
  detail?: string
  node?: string | null
  current?: { describe: string; commit: string; branch: string; dirty: boolean }
  target?: { ref: string; kind: 'branch' | 'tag' | 'ref'; commit: string } | null
  update_available?: boolean
  error?: string
}

export interface VersionInfo {
  version: string
  package_version: string
  timestamp: string
}

export function useBackendVersion() {
  return useQuery<VersionInfo | null>({
    queryKey: ['system', 'version'],
    queryFn: async () => {
      const response = await systemApi.getVersion()
      return response.data ?? null
    },
    staleTime: 5 * 60_000,
    retry: 1,
  })
}

export function useRestartWorkers() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => systemApi.restartWorkers(),
    onSuccess: () => {
      // Workers take a few seconds to restart; refresh system data after delay
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ['system'] }), 5000)
    },
  })
}

export function useRestartBackend() {
  return useMutation({
    mutationFn: () => systemApi.restartBackend(),
    // No auto-invalidation — the backend is going down
  })
}
