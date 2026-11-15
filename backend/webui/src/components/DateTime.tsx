import { formatDate, formatDateTime, formatTime, toDate, useDisplayTimezone, type DateInput, type FormatOptions } from '../utils/datetime'

const FORMATTERS = { datetime: formatDateTime, date: formatDate, time: formatTime }

interface DateTimeProps {
  value: DateInput
  /** Defaults to "datetime". */
  format?: keyof typeof FORMATTERS
  options?: FormatOptions
  className?: string
}

/** A timestamp shown in the user's display timezone; hover shows the full date and time. */
export default function DateTime({ value, format = 'datetime', options, className }: DateTimeProps) {
  useDisplayTimezone()
  const date = toDate(value)
  if (Number.isNaN(date.getTime())) return null
  return (
    <time
      dateTime={date.toISOString()}
      title={formatDateTime(date, { dateStyle: 'full', timeStyle: 'long', timezone: options?.timezone })}
      className={className}
    >
      {FORMATTERS[format](date, options)}
    </time>
  )
}
