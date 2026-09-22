import { useState, useEffect } from 'react'
import { Link, useLocation, Outlet } from 'react-router-dom'
import { Music, MessageSquare, MessageCircle, Users, Upload, Settings, LogOut, Sun, Moon, Shield, Radio, Layers, Puzzle, Zap, Activity, Network, Sparkles, ScrollText, AlertTriangle, Menu, X, CalendarDays, PanelLeftClose, PanelLeftOpen, BookOpen } from 'lucide-react'
import { useAuth } from '../../contexts/AuthContext'
import { useTheme } from '../../contexts/ThemeContext'
import { useSSE, SSEStatus } from '../../hooks/useSSE'
import { useSystemEventsSummary } from '../../hooks/useSystemEvents'
import { useSystemHealthSummary } from '../../hooks/useSystem'
import GlobalRecordingIndicator from './GlobalRecordingIndicator'
import UserLoopModal from '../UserLoopModal'
import { IconButton } from '../ui'

export default function Layout() {
  const location = useLocation()
  const { user, logout, isAdmin } = useAuth()
  const { isDark, toggleTheme } = useTheme()

  // Mobile navigation drawer (below the lg breakpoint the sidebar is hidden)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [desktopSidebarOpen, setDesktopSidebarOpen] = useState(() => {
    return localStorage.getItem('chronicle_desktop_sidebar_open') !== 'false'
  })

  useEffect(() => {
    localStorage.setItem('chronicle_desktop_sidebar_open', String(desktopSidebarOpen))
  }, [desktopSidebarOpen])

  // Close the drawer whenever the route changes (e.g. after tapping a link)
  useEffect(() => {
    setMobileNavOpen(false)
  }, [location.pathname])

  // Single SSE connection for real-time updates across all pages
  const sseStatus = useSSE()

  // Live unacknowledged-event count for the nav badge (admin only; refreshed by
  // the SSE 'system.error' invalidation in useSSE).
  const { data: sysSummary } = useSystemEventsSummary(24, isAdmin)
  const unackedEvents = sysSummary?.unacked ?? 0

  // Keep service outages visible from every page. The health endpoint includes
  // optional configured services (wake-word, speaker recognition, etc.), not
  // just the critical dependencies that decide whether the backend can serve.
  const { data: healthSummary, isError: healthRequestFailed } = useSystemHealthSummary(isAdmin)
  const unhealthyServices = Object.entries(healthSummary?.services ?? {})
    .filter(([, service]) => !service.healthy)
    .map(([name]) => name.replace(/_/g, ' '))
  const systemIssueCount = healthRequestFailed
    ? 1
    : unhealthyServices.length
  const systemIssueTitle = healthRequestFailed
    ? 'Health check unavailable'
    : unhealthyServices.join(', ')

  const navigationGroups = [
    { label: 'Workspace', items: [
      { path: '/live-record', label: 'Live Record', icon: Radio },
      { path: '/chat', label: 'Chat', icon: MessageCircle },
      { path: '/recordings', label: 'Recordings', icon: MessageSquare },
      { path: '/timeline', label: 'Timeline', icon: CalendarDays },
      ...(isAdmin ? [{ path: '/upload', label: 'Upload Audio', icon: Upload }] : []),
    ] },
    { label: 'Memory', items: [
      { path: '/memory-ledger', label: 'Memory Ledger', icon: ScrollText },
      { path: '/spaces', label: 'Memory Spaces', icon: BookOpen },
    ] },
    { label: 'Administration', items: [
      { path: '/users', label: 'User Management', icon: Users },
      ...(isAdmin ? [
        { path: '/data-audit', label: 'Data Audit', icon: Sparkles },
        { path: '/queue', label: 'Queue & Events', icon: Layers },
        { path: '/plugins', label: 'Plugins', icon: Puzzle },
        { path: '/finetuning', label: 'Training', icon: Zap },
        { path: '/network', label: 'Network', icon: Network },
      ] : []),
    ] },
    ...(isAdmin ? [{ label: 'System', items: [
      { path: '/system', label: 'System Status', icon: Activity },
      { path: '/system-errors', label: 'System Events', icon: AlertTriangle },
      { path: '/settings', label: 'Settings', icon: Settings },
    ] }] : []),
  ]
  const isCurrent = (path: string) => location.pathname === path || location.pathname.startsWith(path + '/') || (path === '/data-audit' && location.pathname === '/wakeword-lab')

  // Shared nav <li> items rendered in both the desktop sidebar and the mobile drawer
  const navLinks = navigationGroups.map(group => (
    <li key={group.label} className="pt-3 first:pt-0">
      <p className="px-3 pb-2 text-xs font-semibold tracking-wide text-[var(--tape-activity)]">{group.label}</p>
      <ul className="space-y-1">{group.items.map(({ path, label, icon: Icon }) => (
    <li key={path}>
      <Link
        to={path}
        aria-current={isCurrent(path) ? 'page' : undefined}
        className={`flex min-h-11 items-center space-x-3 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
          isCurrent(path)
            ? 'bg-[var(--tape-selected)] text-[var(--tape-focus)] font-semibold'
            : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
        }`}
      >
        <Icon className="h-5 w-5" />
        <span>{label}</span>
        {path === '/system' && systemIssueCount > 0 && (
          <span
            className="group relative ml-auto inline-flex min-w-[1.25rem] items-center justify-center gap-1 rounded-full bg-red-600 px-1.5 py-0.5 text-xs font-semibold text-white"
            aria-label={`${systemIssueCount} system ${systemIssueCount === 1 ? 'issue' : 'issues'}: ${systemIssueTitle}`}
          >
            <AlertTriangle className="h-3 w-3" aria-hidden="true" />
            {systemIssueCount > 99 ? '99+' : systemIssueCount}
            <span
              role="tooltip"
              className="pointer-events-none absolute right-0 top-full z-20 mt-1.5 hidden max-w-52 whitespace-normal rounded bg-gray-950 px-2 py-1 text-left text-[11px] font-normal leading-tight text-white shadow-sm group-hover:block"
            >
              {systemIssueTitle}
            </span>
          </span>
        )}
        {path === '/system-errors' && unackedEvents > 0 && (
          <span className="ml-auto inline-flex min-w-[1.25rem] items-center justify-center rounded-full bg-[var(--tape-chip)] px-1.5 py-0.5 text-xs font-semibold text-[var(--tape-ink)]" title="Unacknowledged events; see System Status for current outages">
            {unackedEvents > 99 ? '99+' : unackedEvents}
          </span>
        )}
      </Link>
    </li>
      ))}</ul>
    </li>
  ))

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <header className="bg-white dark:bg-gray-800 shadow-sm border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center h-16 gap-2">
            <div className="flex items-center space-x-2 sm:space-x-4 min-w-0">
              {/* Mobile drawer control */}
              <IconButton
                label="Open navigation menu"
                onClick={() => setMobileNavOpen(true)}
                className="lg:hidden -ml-2"
              >
                <Menu className="h-6 w-6" />
              </IconButton>
              {/* Wide-screen sidebar control */}
              <IconButton
                label={desktopSidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
                aria-expanded={desktopSidebarOpen}
                onClick={() => setDesktopSidebarOpen((open) => !open)}
                className="hidden lg:inline-flex -ml-2"
              >
                {desktopSidebarOpen
                  ? <PanelLeftClose className="h-5 w-5" />
                  : <PanelLeftOpen className="h-5 w-5" />}
              </IconButton>
              <Music className="h-8 w-8 text-blue-600 flex-shrink-0" />
              <h1 className="text-base sm:text-xl font-semibold text-gray-900 dark:text-gray-100 whitespace-nowrap">
                {/* The full title does not fit beside the status/action icons on a
                    phone, and truncating it reads as "Chronicle Dashb…". */}
                <span className="sm:hidden">Chronicle</span>
                <span className="hidden sm:inline">Chronicle Dashboard</span>
              </h1>
            </div>
            <div className="flex items-center space-x-2 sm:space-x-4 flex-shrink-0">
              {/* SSE connection status */}
              <SSEIndicator status={sseStatus} />

              {/* Global Recording Indicator */}
              <GlobalRecordingIndicator />

              <IconButton label="Toggle theme" onClick={toggleTheme}>
                {isDark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
              </IconButton>

              {/* User info — hidden on small screens to avoid overflow */}
              <div className="hidden md:flex items-center space-x-2 text-sm text-gray-700 dark:text-gray-300">
                <div className="flex items-center space-x-1">
                  {isAdmin && <Shield className="h-4 w-4 text-blue-600" />}
                  <span className="truncate max-w-[160px]">{user?.display_name || user?.email}</span>
                </div>
              </div>

              <button
                onClick={logout}
                className="flex items-center space-x-2 px-2 sm:px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-gray-600 dark:text-gray-300"
                aria-label="Logout"
              >
                <LogOut className="h-5 w-5 sm:h-4 sm:w-4" />
                <span className="hidden sm:inline text-sm">Logout</span>
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Mobile navigation drawer */}
      {mobileNavOpen && (
        <div className="lg:hidden fixed inset-0 z-50">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setMobileNavOpen(false)}
            aria-hidden="true"
          />
          {/* Drawer panel */}
          <nav className="absolute left-0 top-0 h-full w-72 max-w-[85%] bg-white dark:bg-gray-800 shadow-xl flex flex-col">
            <div className="flex items-center justify-between h-16 px-4 border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center space-x-2">
                <Music className="h-6 w-6 text-blue-600" />
                <span className="font-semibold text-gray-900 dark:text-gray-100">Chronicle</span>
              </div>
              <IconButton
                label="Close navigation menu"
                onClick={() => setMobileNavOpen(false)}
              >
                <X className="h-6 w-6" />
              </IconButton>
            </div>
            <ul className="flex-1 overflow-y-auto p-4 space-y-1">
              {navLinks}
            </ul>
          </nav>
        </div>
      )}

      <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-4 lg:py-8">
        <div className="flex flex-col lg:flex-row gap-5">
          {/* Sidebar Navigation — desktop only; mobile uses the drawer above */}
          <nav
            aria-label="Primary navigation"
            className={`${desktopSidebarOpen ? 'hidden lg:block' : 'hidden'} lg:w-60 flex-shrink-0`}
          >
            <div className="bg-[var(--chronicle-navigation)] rounded-lg border border-[var(--chronicle-border-subtle)] p-3">
              <ul className="space-y-2">
                {navLinks}
              </ul>
            </div>
          </nav>

          {/* Main Content */}
          <main className="flex-1 min-w-0">
            <div className="bg-[var(--tape-paper)] rounded-lg border border-[var(--chronicle-border-subtle)] p-4 sm:p-6">
              <Outlet />
            </div>
          </main>
        </div>
      </div>

      {/* Footer */}
      <footer className="bg-white dark:bg-gray-800 border-t border-gray-200 dark:border-gray-700 mt-auto">
        <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="text-center text-sm text-gray-500 dark:text-gray-400">
            Chronicle — understand everything, everywhere, all at once.
          </div>
        </div>
      </footer>

      {/* User Loop: AI suggestion review modal (auto-opens when suggestions exist) */}
      <UserLoopModal />
    </div>
  )
}

const sseStatusConfig: Record<SSEStatus, { color: string; label: string }> = {
  connected:    { color: 'bg-green-500', label: 'Connected' },
  connecting:   { color: 'bg-gray-400',  label: 'Connecting' },
  reconnecting: { color: 'bg-gray-400',  label: 'Reconnecting' },
  error:        { color: 'bg-red-500',   label: 'Disconnected' },
}

function SSEIndicator({ status }: { status: SSEStatus }) {
  const { color, label } = sseStatusConfig[status]
  return (
    <div className="flex items-center space-x-1.5" title={`Live updates: ${label}`}>
      <span className={`h-2 w-2 rounded-full ${color}`} />
      <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
    </div>
  )
}
