import { afterEach, describe, expect, it } from 'vitest'
import { browserTimezone, formatDateTime, formatTime, getDisplayTimezone, setDisplayTimezone } from './datetime'

const NOON_UTC = '2026-11-07T12:00:00Z'

describe('display timezone', () => {
  afterEach(() => setDisplayTimezone(null))

  it('shows times in the saved zone, not a fixed one', () => {
    setDisplayTimezone('Europe/London')
    expect(formatTime(NOON_UTC, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })).toBe('12:00')
    setDisplayTimezone('Asia/Kolkata')
    expect(formatTime(NOON_UTC, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })).toBe('17:30')
  })

  it('treats offset-less Mongo strings as UTC', () => {
    setDisplayTimezone('UTC')
    expect(formatTime('2026-11-07T12:00:00', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })).toBe('12:00')
  })

  it('falls back to the browser zone when unset or unknown', () => {
    setDisplayTimezone('Not/AZone')
    expect(getDisplayTimezone()).toBe(browserTimezone())
  })

  it('lets a caller pin its own zone', () => {
    setDisplayTimezone('UTC')
    expect(formatTime(NOON_UTC, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timezone: 'Asia/Tokyo' })).toBe('21:00')
  })

  it('includes the zone name by default and returns empty for bad input', () => {
    setDisplayTimezone('UTC')
    expect(formatDateTime(NOON_UTC)).toMatch(/UTC/)
    expect(formatDateTime('not a date')).toBe('')
  })
})
