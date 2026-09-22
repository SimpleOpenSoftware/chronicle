import { ButtonHTMLAttributes, ReactNode, forwardRef } from 'react'
import clsx from 'clsx'

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Accessible label — applied to both aria-label and title. */
  label: string
  /** Red hover treatment for destructive actions. */
  danger?: boolean
  children: ReactNode
}

/** Borderless icon-only control (toolbars, row actions). */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, danger, children, className, type = 'button', ...rest },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={label}
      className={clsx(
        'inline-flex items-center justify-center min-h-11 min-w-11 shrink-0 rounded-md p-2 transition-colors',
        'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50',
        'disabled:cursor-not-allowed disabled:opacity-40',
        danger
          ? 'hover:text-red-600 dark:hover:text-red-400'
          : 'hover:text-gray-700 dark:hover:text-gray-200',
        className
      )}
      {...rest}
    >
      {children}
    </button>
  )
})
