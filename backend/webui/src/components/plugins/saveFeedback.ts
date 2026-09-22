export interface PluginSaveResult {
  reload: { success: boolean; failed?: unknown[] } | null
}

// Saving the config and applying it are separate outcomes in the save API.
export function pluginSaveFeedback(result: PluginSaveResult): { message: string; tone: 'success' | 'warning' } {
  if (!result.reload?.success) {
    return { message: 'Configuration saved. Plugin reload was not confirmed; check System Status before restarting.', tone: 'warning' }
  }
  if (result.reload.failed?.length) {
    return { message: `Configuration saved, but ${result.reload.failed.length} plugin(s) failed to initialize. Check System Events.`, tone: 'warning' }
  }
  return { message: 'Configuration saved and backend plugins reloaded.', tone: 'success' }
}
