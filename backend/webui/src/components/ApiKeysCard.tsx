import { Key } from 'lucide-react'
import ApiKeysPanel from './ApiKeysPanel'

/**
 * Settings-page card for the logged-in user's own API keys.
 *
 * Long-lived credentials for clients that store one secret and never see a
 * login form again (Handy dictation, relays, sync daemons). Admins manage other
 * users' keys from the Users page instead.
 */
export default function ApiKeysCard() {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Key className="h-5 w-5 mr-2 text-blue-600" />
        API Keys
      </h3>

      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Long-lived credentials for connected apps and devices. Revoking a key removes its access.
      </p>

      <ApiKeysPanel />
    </div>
  )
}
