import { useState, useEffect } from 'react'
import { RefreshCw, AlertCircle } from 'lucide-react'
import { systemApi } from '../services/api'
import PluginListSidebar from './plugins/PluginListSidebar'
import PluginConfigPanel from './plugins/PluginConfigPanel'
import { Alert, Card } from './ui'
import { pluginSaveFeedback } from './plugins/saveFeedback'

interface PluginMetadata {
  plugin_id: string
  name: string
  description: string
  enabled: boolean
  status: 'active' | 'disabled' | 'error'
  supports_testing: boolean
  orchestration: {
    enabled: boolean
    events: string[]
    condition: {
      type: 'always' | 'wake_word' | 'keyword_anywhere' | 'acoustic_wake_word'
      wake_words?: string[]
      keywords?: string[]
      threshold?: number
    }
  }
  config_schema: {
    settings: Record<string, any>
    env_vars: Record<string, any>
  }
}

interface PluginConfig {
  orchestration: {
    enabled: boolean
    events: string[]
    condition: {
      type: 'always' | 'wake_word' | 'keyword_anywhere' | 'acoustic_wake_word'
      wake_words?: string[]
      keywords?: string[]
      threshold?: number
    }
  }
  settings: Record<string, any>
  env_vars: Record<string, any>
}

interface PluginSettingsFormProps {
  className?: string
}

export default function PluginSettingsForm({ className }: PluginSettingsFormProps) {
  const [plugins, setPlugins] = useState<PluginMetadata[]>([])
  const [selectedPluginId, setSelectedPluginId] = useState<string | null>(null)
  const [currentConfig, setCurrentConfig] = useState<PluginConfig | null>(null)
  const [originalConfig, setOriginalConfig] = useState<PluginConfig | null>(null)
  const [loading, setLoading] = useState(false)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [message, setMessage] = useState('')
  const [messageTone, setMessageTone] = useState<'success' | 'warning'>('success')
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState<any>(null)
  const [connectivity, setConnectivity] = useState<Record<string, any>>({})

  const selectedPlugin = plugins.find((p) => p.plugin_id === selectedPluginId)

  useEffect(() => {
    loadPlugins()
  }, [])

  useEffect(() => {
    if (selectedPluginId) {
      loadPluginConfig(selectedPluginId)
    }
  }, [selectedPluginId])

  const loadPlugins = async () => {
    setLoading(true)
    setError('')
    setMessage('')
    setConnectivity({})

    try {
      const response = await systemApi.getPluginsMetadata()
      const pluginsData = response.data.plugins || []
      setPlugins(pluginsData)

      // Auto-select first plugin if none selected
      if (!selectedPluginId && pluginsData.length > 0) {
        setSelectedPluginId(pluginsData[0].plugin_id)
      }


      // Fetch live connectivity in background (non-blocking)
      systemApi.getPluginsConnectivity()
        .then((res) => setConnectivity(res.data.plugins || {}))
        .catch(() => {}) // Silently ignore — dots stay gray
    } catch (err: any) {
      const status = err.response?.status
      if (status === 401) {
        setError('Unauthorized: admin privileges required')
      } else if (status === 404 || status === 405) {
        setError('Backend does not expose plugin configuration endpoints')
      } else {
        setError(err.response?.data?.detail || 'Failed to load plugins')
      }
    } finally {
      setLoading(false)
    }
  }

  const loadPluginConfig = (pluginId: string) => {
    const plugin = plugins.find((p) => p.plugin_id === pluginId)
    if (!plugin) return

    // Extract current configuration from plugin metadata
    const orch = plugin.orchestration || { enabled: false, events: [], condition: { type: 'always' } }
    const config: PluginConfig = {
      orchestration: {
        enabled: orch.enabled || false,
        events: orch.events || [],
        condition: orch.condition || { type: 'always' }
      },
      settings: {},
      env_vars: {}
    }

    // Load settings with defaults
    Object.keys(plugin.config_schema.settings || {}).forEach((key) => {
      const schema = plugin.config_schema.settings[key]
      config.settings[key] = schema.default ?? ''
    })

    // Load env vars (will be masked values from backend)
    Object.keys(plugin.config_schema.env_vars || {}).forEach((key) => {
      const schema = plugin.config_schema.env_vars[key]
      config.env_vars[key] = schema.value ?? ''
    })

    setCurrentConfig(config)
    setOriginalConfig(JSON.parse(JSON.stringify(config)))
    setErrors({})
    setTestResult(null)
  }

  const handlePluginSelect = (pluginId: string) => {
    setSelectedPluginId(pluginId)
  }

  const handleConfigChange = (config: PluginConfig) => {
    setCurrentConfig(config)
    setErrors({})
  }

  const handleTestConnection = async () => {
    if (!selectedPluginId || !currentConfig) return

    setTesting(true)
    setTestResult(null)
    setError('')

    try {
      const response = await systemApi.testPluginConnection(selectedPluginId, {
        orchestration: currentConfig.orchestration,
        settings: currentConfig.settings,
        env_vars: currentConfig.env_vars
      })

      setTestResult(response.data)

      if (response.data.success) {
        setMessageTone('success')
        setMessage('Connection test successful')
        setTimeout(() => setMessage(''), 3000)
      }
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || 'Connection test failed'
      setTestResult({
        success: false,
        message: errorMessage
      })
      setError(errorMessage)
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async () => {
    if (!selectedPluginId || !currentConfig) return

    setSaving(true)
    setError('')
    setMessage('')
    setErrors({})

    try {
      // Filter out masked env vars (don't send unchanged secrets)
      const envVarsToSend: Record<string, any> = {}
      Object.keys(currentConfig.env_vars).forEach((key) => {
        const value = currentConfig.env_vars[key]
        // Only send if value is not masked
        if (typeof value !== 'string' || !value.includes('••••')) {
          envVarsToSend[key] = value
        }
      })

      const response = await systemApi.updatePluginConfigStructured(selectedPluginId, {
        orchestration: currentConfig.orchestration,
        settings: currentConfig.settings,
        env_vars: Object.keys(envVarsToSend).length > 0 ? envVarsToSend : undefined
      })

      setOriginalConfig(JSON.parse(JSON.stringify(currentConfig)))
      await loadPlugins()
      const feedback = pluginSaveFeedback(response.data)
      setMessageTone(feedback.tone)
      setMessage(feedback.message)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    if (originalConfig) {
      setCurrentConfig(JSON.parse(JSON.stringify(originalConfig)))
      setErrors({})
      setTestResult(null)
      setMessageTone('success')
      setMessage('Configuration reset to original values')
      setTimeout(() => setMessage(''), 3000)
    }
  }

  const handleRefresh = async () => {
    await loadPlugins()
  }

  return (
    <div className={className}>
      <Card raised padded={false} className="overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-gray-200 dark:border-gray-700">
          <div>
            <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Plugin Configuration
            </h2>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              Saving reloads backend plugins and requests a worker restart.
            </p>
          </div>
          <button
            onClick={handleRefresh}
            disabled={loading}
            className="flex items-center space-x-2 px-4 py-2 bg-gray-600 text-white rounded-md hover:bg-gray-700 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>

        {/* Status Messages */}
        {message && (
          <Alert tone={messageTone} className="mx-6 mt-4">{message}</Alert>
        )}

        {error && (
          <Alert tone="danger" icon={<AlertCircle className="h-5 w-5" />} className="mx-6 mt-4">
            {error}
          </Alert>
        )}

        {/* Main Content */}
        <div className="flex h-[600px]">
          {/* Sidebar */}
          <div className="w-1/3 border-r border-gray-200 dark:border-gray-700 overflow-y-auto">
            <PluginListSidebar
              plugins={plugins}
              selectedPluginId={selectedPluginId}
              onSelectPlugin={handlePluginSelect}
              loading={loading}
              connectivity={connectivity}
            />
          </div>

          {/* Config Panel */}
          <div className="flex-1 overflow-y-auto">
            {selectedPlugin && currentConfig ? (
              <PluginConfigPanel
                plugin={selectedPlugin}
                config={currentConfig}
                onChange={handleConfigChange}
                onTestConnection={selectedPlugin.supports_testing ? handleTestConnection : undefined}
                onSave={handleSave}
                onReset={handleReset}
                errors={errors}
                testResult={testResult}
                testing={testing}
                saving={saving}
                disabled={loading}
              />
            ) : (
              <div className="flex items-center justify-center h-full text-gray-500 dark:text-gray-400">
                <p>Select a plugin to configure</p>
              </div>
            )}
          </div>
        </div>
      </Card>
    </div>
  )
}
