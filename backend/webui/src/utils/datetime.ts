import { useSyncExternalStore } from 'react'
import { sourceDate } from './sourceTime'

/**
 * The one display-timezone authority for the WebUI.
 *
 * Stored timestamps are UTC; this module decides which zone they are shown in:
 * the signed-in user's saved `timezone`, falling back to the browser's.
 * AuthProvider keeps it in sync with the current user.
 */

export type DateInput = string | number | Date

export function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

function isValidTimezone(zone: string): boolean {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: zone })
    return true
  } catch {
    return false
  }
}

let displayTimezone = browserTimezone()
const listeners = new Set<() => void>()

export function getDisplayTimezone(): string {
  return displayTimezone
}

/** Use `zone` for display; an empty or unknown zone falls back to the browser's. */
export function setDisplayTimezone(zone: string | null | undefined): void {
  const next = zone && isValidTimezone(zone) ? zone : browserTimezone()
  if (next === displayTimezone) return
  displayTimezone = next
  listeners.forEach(listener => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Re-render when the display timezone changes. */
export function useDisplayTimezone(): string {
  return useSyncExternalStore(subscribe, getDisplayTimezone)
}

/** Strings are UTC even without an offset; numbers are epoch milliseconds. */
export function toDate(value: DateInput): Date {
  if (value instanceof Date) return value
  return typeof value === 'string' ? sourceDate(value) : new Date(value)
}

export interface FormatOptions extends Intl.DateTimeFormatOptions {
  /** Show in this zone instead, e.g. a timeline day built in its own zone. */
  timezone?: string
}

const DATE_TIME: Intl.DateTimeFormatOptions = {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
}
const TIME: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }
const DATE: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'short', year: 'numeric' }
/** For logs, jobs and audits, where seconds matter. */
export const PRECISE_DATE_TIME: FormatOptions = { ...DATE_TIME, second: '2-digit' }
export const PRECISE_TIME: FormatOptions = { ...TIME, second: '2-digit' }

/** Format options, when given, replace the defaults rather than merge with them. */
function format(value: DateInput, defaults: Intl.DateTimeFormatOptions, options?: FormatOptions): string {
  const date = toDate(value)
  if (Number.isNaN(date.getTime())) return ''
  const { timezone, ...fields } = options ?? {}
  return date.toLocaleString(undefined, {
    ...(Object.keys(fields).length ? fields : defaults),
    timeZone: timezone || displayTimezone,
  })
}

/** Date and time with the zone name, e.g. "7 Nov 2026, 14:05 GMT". */
export const formatDateTime = (value: DateInput, options?: FormatOptions) => format(value, DATE_TIME, options)
/** Time with the zone name, e.g. "14:05 GMT". */
export const formatTime = (value: DateInput, options?: FormatOptions) => format(value, TIME, options)
/** Date only, e.g. "7 Nov 2026". */
export const formatDate = (value: DateInput, options?: FormatOptions) => format(value, DATE, options)
