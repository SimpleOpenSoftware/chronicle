import { useState, useEffect } from 'react'
import { Puzzle, RefreshCw, CheckCircle, Save, RotateCcw, AlertCircle } from 'lucide-react'
import { systemApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { Alert, Button, Card, Textarea } from './ui'
import { pluginSaveFeedback } from './plugins/saveFeedback'

interface PluginSettingsProps {
  className?: string
}

export default function PluginSettings({ className }: PluginSettingsProps) {
  const [configYaml, setConfigYaml] = useState('')
  const [loading, setLoading] = useState(false)
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [messageTone, setMessageTone] = useState<'success' | 'warning'>('success')
  const [error, setError] = useState('')
  const { isAdmin } = useAuth()

  useEffect(() => {
    loadPluginsConfig()
  }, [])

  const loadPluginsConfig = async () => {
    setLoading(true)
    setError('')
    setMessage('')

    try {
      const response = await systemApi.getPluginsConfigRaw()
      setConfigYaml(response.data.config_yaml || response.data)
    } catch (err: any) {
      const status = err.response?.status
      if (status === 401) {
        setError('Unauthorized: admin privileges required')
      } else {
        setError(err.response?.data?.error || 'Failed to load configuration')
      }
    } finally {
      setLoading(false)
    }
  }

  const validateConfig = async () => {
    if (!configYaml.trim()) {
      setError('Configuration cannot be empty')
      return
    }

    setValidating(true)
    setError('')
    setMessage('')

    try {
      const response = await systemApi.validatePluginsConfig(configYaml)
      if (response.data.valid) {
        setMessageTone('success')
        setMessage('Configuration is valid')
      } else {
        setError(response.data.error || 'Validation failed')
      }
      setTimeout(() => setMessage(''), 3000)
    } catch (err: any) {
      setError(err.response?.data?.error || 'Validation failed')
    } finally {
      setValidating(false)
    }
  }

  const saveConfig = async () => {
    if (!configYaml.trim()) {
      setError('Configuration cannot be empty')
      return
    }

    setSaving(true)
    setError('')
    setMessage('')

    try {
      const response = await systemApi.updatePluginsConfigRaw(configYaml)
      const feedback = pluginSaveFeedback(response.data)
      setMessageTone(feedback.tone)
      setMessage(feedback.message)
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }

  const resetConfig = () => {
    loadPluginsConfig()
    setMessage('Configuration reset to file version')
    setTimeout(() => setMessage(''), 3000)
  }

  if (!isAdmin) {
    return null
  }

  return (
    <div className={className}>
      <Card raised padded={false} className="p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center space-x-2">
            <Puzzle className="h-5 w-5 text-blue-600" />
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Plugin Configuration
            </h3>
          </div>
          <div className="flex items-center space-x-2">
            <Button
              variant="ghost"
              onClick={resetConfig}
              disabled={loading || saving}
              icon={<RotateCcw className="h-4 w-4" />}
            >
              Reset
            </Button>
            <Button
              variant="ghost"
              onClick={loadPluginsConfig}
              disabled={loading || saving}
              icon={<RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />}
            >
              Reload
            </Button>
          </div>
        </div>

        {/* Messages */}
        {message && (
          <Alert tone={messageTone} icon={<CheckCircle className="h-5 w-5" />} className="mb-4">
            {message}
          </Alert>
        )}

        {error && (
          <Alert tone="danger" icon={<AlertCircle className="h-5 w-5" />} className="mb-4">
            {error}
          </Alert>
        )}

        {/* Editor */}
        <div className="mb-4">
          <Textarea
            value={configYaml}
            onChange={(e) => setConfigYaml(e.target.value)}
            disabled={loading || saving}
            className="h-96 p-4 font-mono bg-gray-50 dark:bg-gray-900"
            placeholder="Loading configuration..."
            spellCheck={false}
          />
        </div>

        {/* Actions */}
        <div className="flex space-x-3">
          <Button
            variant="secondary"
            size="md"
            onClick={validateConfig}
            disabled={loading || validating || saving}
            icon={<CheckCircle className="h-4 w-4" />}
          >
            {validating ? 'Validating...' : 'Validate'}
          </Button>

          <Button
            variant="primary"
            size="md"
            onClick={saveConfig}
            disabled={loading || saving || validating}
            icon={<Save className="h-4 w-4" />}
          >
            {saving ? 'Saving...' : 'Save Changes'}
          </Button>
        </div>

        <p className="mt-4 text-xs text-gray-600 dark:text-gray-400">
          Saving reloads backend plugins and requests a worker restart.
        </p>
      </Card>
    </div>
  )
}
