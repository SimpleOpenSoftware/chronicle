import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import { timelineApi } from '../services/api'
import { timezonesEquivalent } from '../components/timeline/timelineNavigation'
import { setDisplayTimezone } from '../utils/datetime'

/**
 * One timezone authority for day projections and chronological memory review.
 * Reading a page is side-effect free; only the explicit switch persists a zone.
 */
export function useTimelineTimezone() {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const [override, setOverride] = useState<string | null>(null)
  const timezone = override || user?.timezone || browserTimezone
  const saveBrowserTimezone = useMutation({
    mutationFn: () => timelineApi.setTimezone(browserTimezone),
    onSuccess: async () => {
      setOverride(browserTimezone)
      setDisplayTimezone(browserTimezone)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['semantic-timeline'] }),
        queryClient.invalidateQueries({ queryKey: ['timeline-review-queue'] }),
      ])
    },
  })
  return {
    timezone,
    browserTimezone,
    storedTimezone: user?.timezone || null,
    shouldOfferBrowserTimezone: !user?.timezone || !timezonesEquivalent(timezone, browserTimezone),
    saveBrowserTimezone: () => saveBrowserTimezone.mutate(),
    savingBrowserTimezone: saveBrowserTimezone.isPending,
    timezoneError: saveBrowserTimezone.error,
  }
}
