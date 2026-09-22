import type { MemorySpace } from '../services/api'

export const spaceStateLabel: Record<MemorySpace['state'], string> = {
  active: 'Active',
  merging: 'Preparing publication',
  archived: 'Archived',
}

export const spaceSyncLabel: Record<MemorySpace['sync_state'], string> = {
  unpaired: 'Sync not connected',
  syncing: 'Syncing',
  healthy: 'Synced',
  frozen: 'Sync paused',
  error: 'Sync error',
}
