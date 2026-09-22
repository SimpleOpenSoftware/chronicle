import { useState, useRef, useEffect } from 'react'
import { Send, Bot, User, Wrench, AlertCircle, ShieldCheck, ShieldX } from 'lucide-react'
import { systemApi } from '../../services/api'
import { Alert, Button } from '../ui'

interface Message {
  role: 'user' | 'assistant'
  content: string
}

interface ToolIndicator {
  name: string
  status: 'running' | 'done'
}

interface ConfirmationRequest {
  toolCallId: string
  toolName: string
  toolArgs: Record<string, any>
  preview: string
  assistantMessage: any
}

const EXAMPLE_PROMPTS = [
  'What plugins are available?',
  'Create a new plugin for Slack notifications',
  'What events can plugins listen to?',
  'Show recent plugin activity',
  'Test Home Assistant connection',
]

const TOOL_LABELS: Record<string, string> = {
  get_plugin_status: 'Checking plugin status...',
  apply_plugin_config: 'Applying configuration...',
  test_plugin_connection: 'Testing connection...',
  create_plugin: 'Creating plugin...',
  write_plugin_code: 'Writing plugin code...',
  delete_plugin: 'Removing plugin...',
  get_available_events: 'Listing available events...',
  get_recent_events: 'Fetching event log...',
}

export default function PluginAssistant() {
  const [messages, setMessages] = useState<Message[]>([])
  const [toolIndicators, setToolIndicators] = useState<ToolIndicator[]>([])
  const [inputMessage, setInputMessage] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [streamingMessage, setStreamingMessage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<ConfirmationRequest | null>(null)
  // Internal message history including tool messages for resumption
  const internalHistoryRef = useRef<Array<{ role: string; content: string }>>([])
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMessage, toolIndicators, confirmation])

  const sendMessage = async (text?: string) => {
    const messageText = (text || inputMessage).trim()
    if (!messageText || isSending) return

    setInputMessage('')
    setError(null)
    setIsSending(true)
    setToolIndicators([])

    const userMessage: Message = { role: 'user', content: messageText }
    const updatedMessages = [...messages, userMessage]
    setMessages(updatedMessages)

    // Build API messages from display messages
    const apiMessages = updatedMessages.map(m => ({ role: m.role, content: m.content }))
    internalHistoryRef.current = apiMessages

    await streamResponse(apiMessages)
  }

  const streamResponse = async (apiMessages: Array<{ role: string; content: string }>) => {
    try {
      const response = await systemApi.pluginAssistantChat(apiMessages)

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }

      const reader = response.body?.getReader()
      if (!reader) throw new Error('No response body')

      const decoder = new TextDecoder()
      let buffer = ''
      let accumulatedContent = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const data = line.slice(6)
          if (data === '[DONE]') break

          try {
            const event = JSON.parse(data)

            switch (event.type) {
              case 'tool_call':
                setToolIndicators(prev => [...prev, { name: event.name, status: 'running' }])
                break
              case 'tool_result':
                setToolIndicators(prev =>
                  prev.map(t => t.name === event.name ? { ...t, status: 'done' } : t)
                )
                break
              case 'token':
                accumulatedContent += event.data
                setStreamingMessage(accumulatedContent)
                break
              case 'confirmation_required':
                setConfirmation({
                  toolCallId: event.tool_call_id,
                  toolName: event.tool_name,
                  toolArgs: event.tool_args,
                  preview: event.preview,
                  assistantMessage: event.assistant_message,
                })
                setToolIndicators([])
                break
              case 'complete':
                if (accumulatedContent) {
                  setMessages(prev => [...prev, { role: 'assistant', content: accumulatedContent }])
                }
                setStreamingMessage('')
                setToolIndicators([])
                break
              case 'error':
                throw new Error(event.data?.error || 'Unknown error')
            }
          } catch (parseError) {
            if (parseError instanceof SyntaxError) continue
            throw parseError
          }
        }
      }

      // Finalize if stream ended without a complete event
      if (accumulatedContent && !messages.find(m => m.content === accumulatedContent)) {
        setMessages(prev => {
          const last = prev[prev.length - 1]
          if (last?.role === 'assistant' && last.content === accumulatedContent) return prev
          return [...prev, { role: 'assistant', content: accumulatedContent }]
        })
      }
      setStreamingMessage('')
      setToolIndicators([])
    } catch (err: any) {
      setError(err.message || 'Failed to get response')
      setStreamingMessage('')
      setToolIndicators([])
    } finally {
      setIsSending(false)
    }
  }

  const handleConfirmation = async (approved: boolean) => {
    if (!confirmation) return

    setIsSending(true)
    setError(null)

    // Build message history including the assistant message with tool_calls
    // and a synthetic tool result for the confirmation
    const currentApiMessages = internalHistoryRef.current
    const toolResultContent = approved
      ? JSON.stringify({ confirmed: true })
      : JSON.stringify({ rejected: true, reason: 'User declined' })

    const resumeMessages = [
      ...currentApiMessages,
      // The assistant message that contained the tool call
      confirmation.assistantMessage,
      // The user's confirmation/rejection as a tool result
      {
        role: 'tool',
        tool_call_id: confirmation.toolCallId,
        content: toolResultContent,
      },
    ]

    internalHistoryRef.current = resumeMessages
    setConfirmation(null)

    await streamResponse(resumeMessages)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  const renderPreview = (preview: string) => {
    // Simple markdown-ish rendering for previews
    return preview.split('\n').map((line, i) => {
      // Bold text
      const boldRendered = line.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      // Code blocks
      if (line.startsWith('```')) {
        return null // handled by block below
      }
      // Inline code
      const codeRendered = boldRendered.replace(/`([^`]+)`/g, '<code class="px-1 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs">$1</code>')
      return (
        <div key={i} dangerouslySetInnerHTML={{ __html: codeRendered }} className="text-sm" />
      )
    })
  }

  return (
    <div className="flex flex-col h-[calc(100vh-12rem)] bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
      {/* Messages area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && !streamingMessage && !confirmation && (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <Bot className="h-12 w-12 text-gray-400 mb-4" />
            <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-300 mb-2">
              Plugin Configuration Assistant
            </h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-6 max-w-md">
              Inspect plugin activity or request configuration changes.
            </p>
            <div className="flex flex-wrap gap-2 justify-center max-w-lg">
              {EXAMPLE_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  onClick={() => sendMessage(prompt)}
                  className="px-3 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-full text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message, idx) => (
          <div
            key={idx}
            className={`flex items-start space-x-3 ${
              message.role === 'user' ? 'flex-row-reverse space-x-reverse' : ''
            }`}
          >
            <div
              className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
                message.role === 'user'
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-300 dark:bg-gray-600 text-gray-700 dark:text-gray-300'
              }`}
            >
              {message.role === 'user' ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
            </div>
            <div
              className={`max-w-2xl p-3 rounded-lg ${
                message.role === 'user'
                  ? 'bg-blue-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700'
              }`}
            >
              <div className="whitespace-pre-wrap text-sm">{message.content}</div>
            </div>
          </div>
        ))}

        {/* Tool call indicators */}
        {toolIndicators.map((tool, idx) => (
          <div key={idx} className="flex items-center space-x-2 px-3 py-1.5">
            <Wrench className={`h-3.5 w-3.5 ${tool.status === 'running' ? 'animate-spin text-blue-500' : 'text-green-500'}`} />
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {TOOL_LABELS[tool.name] || `Running ${tool.name}...`}
              {tool.status === 'done' && ' Done.'}
            </span>
          </div>
        ))}

        {/* Confirmation dialog */}
        {confirmation && (
          <div className="mx-2 p-4 bg-yellow-50 dark:bg-yellow-900/20 border-2 border-yellow-300 dark:border-yellow-700 rounded-lg">
            <div className="flex items-center gap-2 mb-3">
              <ShieldCheck className="h-5 w-5 text-yellow-600 dark:text-yellow-400" />
              <h4 className="font-semibold text-yellow-800 dark:text-yellow-200 text-sm">
                Confirm: {TOOL_LABELS[confirmation.toolName]?.replace('...', '') || confirmation.toolName}
              </h4>
            </div>
            <div className="mb-4 text-gray-700 dark:text-gray-300 space-y-1 overflow-x-auto max-h-80 overflow-y-auto">
              {confirmation.preview.includes('```') ? (
                // Render with code blocks
                <pre className="text-xs whitespace-pre-wrap bg-gray-100 dark:bg-gray-800 p-3 rounded">
                  {confirmation.preview}
                </pre>
              ) : (
                renderPreview(confirmation.preview)
              )}
            </div>
            <div className="flex gap-3">
              <button
                onClick={() => handleConfirmation(true)}
                disabled={isSending}
                className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors text-sm font-medium"
              >
                <ShieldCheck className="h-4 w-4" />
                Approve
              </button>
              <Button
                variant="secondary"
                size="md"
                onClick={() => handleConfirmation(false)}
                disabled={isSending}
                icon={<ShieldX className="h-4 w-4" />}
              >
                Reject
              </Button>
            </div>
          </div>
        )}

        {/* Streaming message */}
        {streamingMessage && (
          <div className="flex items-start space-x-3">
            <div className="w-8 h-8 rounded-full bg-gray-300 dark:bg-gray-600 text-gray-700 dark:text-gray-300 flex items-center justify-center flex-shrink-0">
              <Bot className="h-4 w-4" />
            </div>
            <div className="max-w-2xl p-3 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700">
              <div className="whitespace-pre-wrap text-sm">{streamingMessage}</div>
              <div className="text-xs mt-2 text-gray-500 dark:text-gray-400">
                <span className="animate-pulse">●</span> Typing...
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Error */}
      {error && (
        <Alert tone="danger" className="mx-4 mb-2" icon={<AlertCircle className="h-4 w-4 flex-shrink-0" />}>
          <span className="flex w-full items-center justify-between gap-2">
            <span>{error}</span>
            <button onClick={() => setError(null)} className="flex-shrink-0 text-red-500 hover:text-red-700">
              ✕
            </button>
          </span>
        </Alert>
      )}

      {/* Input area */}
      <div className="p-4 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 rounded-b-lg">
        <div className="flex items-end space-x-3">
          <div className="flex-1">
            <textarea
              ref={inputRef}
              value={inputMessage}
              onChange={(e) => setInputMessage(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about plugin configuration..."
              className="w-full p-3 border border-gray-300 dark:border-gray-600 rounded-lg resize-none bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
              rows={1}
              style={{ minHeight: '44px', maxHeight: '120px' }}
              disabled={isSending || !!confirmation}
            />
          </div>
          <Button
            variant="primary"
            size="md"
            onClick={() => sendMessage()}
            disabled={!inputMessage.trim() || isSending || !!confirmation}
            title="Send Message (Enter)"
            icon={<Send className="h-5 w-5" />}
          />
        </div>
      </div>
    </div>
  )
}
