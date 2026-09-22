import { lazy, Suspense } from 'react'
import { BrowserRouter as Router, Routes, Route, Navigate, useParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import { RecordingProvider } from './contexts/RecordingContext'
import Layout from './components/layout/Layout'
import LoginPage from './pages/LoginPage'
import ProtectedRoute from './components/auth/ProtectedRoute'
import { ErrorBoundary, PageErrorBoundary } from './components/ErrorBoundary'

/** Preserves the id when an old `/conversations/:id` link is followed. */
function ConversationRedirect() {
  const { id } = useParams()
  return <Navigate to={`/recordings/${id}`} replace />
}

// Lazy-loaded page components (code-split into separate chunks)
const Chat = lazy(() => import('./pages/Chat'))
const RecordingsRouter = lazy(() => import('./pages/RecordingsRouter'))
const RecordingDetail = lazy(() => import('./pages/RecordingDetail'))
const EpisodeDetail = lazy(() => import('./pages/EpisodeDetail'))
const EpisodeByKey = lazy(() => import('./pages/EpisodeByKey'))
const Users = lazy(() => import('./pages/Users'))
const System = lazy(() => import('./pages/System'))
const Settings = lazy(() => import('./pages/Settings'))
const Upload = lazy(() => import('./pages/Upload'))
const Queue = lazy(() => import('./pages/Queue'))
const LiveRecord = lazy(() => import('./pages/LiveRecord'))
const Plugins = lazy(() => import('./pages/Plugins'))
const Finetuning = lazy(() => import('./pages/Finetuning'))
const Network = lazy(() => import('./pages/Network'))
const DataAudit = lazy(() => import('./pages/DataAudit'))
const WakeWordLab = lazy(() => import('./pages/WakeWordLab'))
const MemoryLedger = lazy(() => import('./pages/MemoryLedger'))
const SystemEvents = lazy(() => import('./pages/SystemEvents'))
const Timeline = lazy(() => import('./pages/Timeline'))
const MemorySpaces = lazy(() => import('./pages/MemorySpaces'))
const MemorySpaceWorkspace = lazy(() => import('./pages/MemorySpaceWorkspace'))


function PageSkeleton() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
    </div>
  )
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

function App() {
  // Get base path from Vite config (e.g., "/prod/" for path-based routing)
  const basename = import.meta.env.BASE_URL

  return (
    <ErrorBoundary>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <RecordingProvider>
            <Router basename={basename} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/" element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }>
                <Route index element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <RecordingsRouter />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="live-record" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <LiveRecord />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="chat" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Chat />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="data-audit/recordings/:id" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <RecordingDetail dataset />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="recordings/:id" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <RecordingDetail />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="recordings" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <RecordingsRouter />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                {/* A recording is the artifact; "conversation" is now an episode kind.
                    Old links (bookmarks, vault notes) keep working. */}
                <Route path="conversations/:id" element={<ConversationRedirect />} />
                <Route path="conversations" element={<Navigate to="/recordings" replace />} />
                <Route path="timeline" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}><Timeline /></Suspense>
                  </PageErrorBoundary>
                } />
                {/* Durable identity: survives reanalysis, split, and merge. Must be
                    declared before the `:episodeId` route so "key" is not read as one. */}
                <Route path="timeline/key/:episodeKey" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}><EpisodeByKey /></Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="timeline/:episodeId" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}><EpisodeDetail /></Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="memory-ledger" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <MemoryLedger />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="spaces" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}><MemorySpaces /></Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="spaces/:spaceId/:tab?" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}><MemorySpaceWorkspace /></Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="users" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Users />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="system" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <System />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="system-errors" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <SystemEvents />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="settings" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Settings />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="upload" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Upload />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="queue" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Queue />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="plugins" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Plugins />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="finetuning" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Finetuning />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="network" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Network />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="data-audit" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <DataAudit />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="wakeword-lab" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <WakeWordLab />
                    </Suspense>
                  </PageErrorBoundary>
                } />

              </Route>
            </Routes>
            </Router>
          </RecordingProvider>
        </AuthProvider>
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}

export default App
