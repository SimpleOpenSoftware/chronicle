/** Mongo timestamps are UTC, including responses without an explicit offset. */
export function sourceDate(value: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`)
}
