import { sourceDate } from '../utils/sourceTime';
import ReconciliationProgress from '../components/timeline/ReconciliationProgress';
import React, { useState, useEffect, useMemo, useRef } from 'react';
import {
  Clock,
  Play,
  Pause,
  CheckCircle,
  XCircle,
  MinusCircle,
  RotateCcw,
  StopCircle,
  Eye,
  Filter,
  X,
  RefreshCw,
  Layers,
  Trash2,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileAudio,
  FileText,
  Brain,
  Repeat,
  Zap
} from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { useQueueDashboard } from '../hooks/useQueue';
import { queueApi, conversationsApi } from '../services/api';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  IconButton,
  Label,
  MetadataChip,
  Modal,
  Select,
  StatCard,
  StateBadge,
} from '../components/ui';
import type { StateTone } from '../components/ui';

function queueDate(value: string | number): Date {
  return typeof value === 'string' ? sourceDate(value) : new Date(value);
}

interface QueueStats {
  total_jobs: number;
  queued_jobs: number;
  started_jobs: number;  // RQ standard, not "processing_jobs"
  finished_jobs: number;  // RQ standard, not "completed_jobs"
  failed_jobs: number;
  canceled_jobs: number;  // RQ standard (US spelling), not "cancelled_jobs"
  deferred_jobs: number;
  timestamp: string;
}

// Priority is deliberately absent: the backend stubs every job's priority to the
// constant "normal" ("RQ doesn't track priority in metadata"), so filtering on it
// can only ever match everything or nothing.
interface Filters {
  status: string;
  job_type: string;
}

interface StreamingSession {
  privacy_held?: boolean;
  privacy_reason?: string;
  session_id: string;
  user_id: string;
  client_id: string;
  provider: string;
  mode: string;
  status: string;
  chunks_published: number;
  started_at: number;
  last_chunk_at: number;
  age_seconds: number;
  idle_seconds: number;
  conversation_count?: number;
  // Speech detection events
  last_event?: string;
  speech_detected_at?: string;
  speaker_check_status?: string;
  identified_speakers?: string;
}

interface StreamConsumer {
  name: string;
  pending: number;
  idle_ms: number;
}

interface StreamConsumerGroup {
  name: string;
  consumers: StreamConsumer[];
  pending: number;
}

interface StreamHealth {
  session_id: string;
  client_id: string;
  stream_length?: number;
  consumer_groups?: StreamConsumerGroup[];
  total_pending?: number;
  error?: string;
  exists?: boolean;
}

interface CompletedSession {
  session_id: string;
  client_id: string;
  conversation_id: string | null;
  has_conversation: boolean;
  action: string;
  reason: string;
  completed_at: number;
  audio_file: string;
}

interface StreamingStatus {
  active_sessions: StreamingSession[];  // Kept for backward compatibility
  completed_sessions: CompletedSession[];
  stream_health: {
    [streamKey: string]: StreamHealth & {
      stream_age_seconds?: number;
    };
  };
  rq_queues: {
    [queue: string]: {
      count: number;
      failed_count: number;
    };
  };
  timestamp: number;
}

interface EventRecord {
  privacy_held?: boolean;
  description?: string;
  timestamp: number;
  event: string;
  user_id: string;
  plugins_subscribed: string[];
  plugins_executed: Array<{ plugin_id: string; success: boolean; message: string; data?: Record<string, any> | null }>;
  metadata: Record<string, any>;
}

// Known event type colors — unknown types fall back to neutral via getEventColor().
// Event kind is descriptive, so these stay muted tints (same shape as the category
// chips on the System Errors page) rather than solid accents.
const EVENT_TYPE_COLORS: Record<string, string> = {
  'conversation.complete': 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  'transcript.batch': 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  'memory.processed': 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
  'button.single_press': 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300',
  'button.double_press': 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300',
  'plugin_action': 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300',
  'wake_word.detected': 'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-300',
};
const DEFAULT_EVENT_COLOR = 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300';
const getEventColor = (eventType: string) => EVENT_TYPE_COLORS[eventType] || DEFAULT_EVENT_COLOR;

/** Panel heading inside a Card — the section title above a table or grid. */
const SectionTitle = ({ children }: { children: React.ReactNode }) => (
  <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100">{children}</h3>
);

/** Sub-heading for a group inside a panel. */
const GroupTitle = ({ children, className = '' }: { children: React.ReactNode; className?: string }) => (
  <h4 className={`text-sm font-medium text-gray-700 dark:text-gray-300 ${className}`}>{children}</h4>
);

/** Placeholder shown where a list has nothing to render. */
const EmptyState = ({ children }: { children: React.ReactNode }) => (
  <div className="rounded-lg border border-gray-200 bg-gray-50 py-8 text-center text-sm text-gray-500 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-400">
    {children}
  </div>
);

/** RQ registry snapshots can contain the same job in more than one group. */
function uniqueJobs(jobs: any[]): any[] {
  const byId = new Map<string, any>();
  for (const job of jobs) {
    if (!job?.job_id || byId.has(job.job_id)) continue;
    byId.set(job.job_id, job);
  }
  return [...byId.values()];
}

const Queue: React.FC = () => {
  const queryClient = useQueryClient();

  // UI-only state
  const [selectedJob, setSelectedJob] = useState<any | null>(null);
  const [loadingJobDetails, setLoadingJobDetails] = useState(false);
  const detailRequest = useRef(0);
  const [filters, setFilters] = useState<Filters>({
    status: '',
    job_type: ''
  });
  const [pagination, setPagination] = useState({
    offset: 0,
    limit: 20,
    total: 0,
    has_more: false
  });
  const [showFlushModal, setShowFlushModal] = useState(false);
  const [flushSettings, setFlushSettings] = useState({
    older_than_hours: 24,
    statuses: ['finished', 'failed'],  // RQ standard status names
    flush_all: false,
    include_failed: false,  // For flush_all mode
    include_finished: false  // For flush_all mode (RQ standard status name)
  });
  const [flushing, setFlushing] = useState(false);
  // Preview ("dry run") of the jobs a flush would remove, or null when not previewed yet
  const [flushPreview, setFlushPreview] = useState<{
    total_matched: number;
    jobs: any[];
    redis_keys_matched?: number;
    skipped_session_level?: number;
  } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [expandedConversations, setExpandedConversations] = useState<Set<string>>(new Set());
  const [expandedJobs, setExpandedJobs] = useState<Set<string>>(new Set());
  const [selectedEvent, setSelectedEvent] = useState<EventRecord | null>(null);

  // System events
  const [eventFilters, setEventFilters] = useState<Record<string, 'include' | 'exclude'>>({});

  const cycleEventFilter = (eventType: string) => {
    setEventFilters(prev => {
      const current = prev[eventType];
      const next = { ...prev };
      if (!current) {
        next[eventType] = 'include';
      } else if (current === 'include') {
        next[eventType] = 'exclude';
      } else {
        delete next[eventType];
      }
      return next;
    });
  };
  const [eventsExpanded, setEventsExpanded] = useState<boolean>(() => {
    const saved = localStorage.getItem('queue_events_expanded');
    return saved !== null ? saved === 'true' : true;
  });

  // Completed conversations pagination
  const [completedConvPage, setCompletedConvPage] = useState(1);
  const [completedConvItemsPerPage] = useState(10);
  const [completedConvTimeRange, setCompletedConvTimeRange] = useState(24); // hours

  // 1-second tick for live time elapsed display (no API calls)
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // React Query: SSE events invalidate ['queue'] automatically
  // Sorted so the query key is canonical — expanding A then B and B then A hit
  // the same cache entry instead of refetching.
  const expandedConversationIds = useMemo(
    () => Array.from(expandedConversations).sort(),
    [expandedConversations]
  );
  const { data: dashboardData, isLoading: loading, isFetching: refreshing, error: dashboardError } = useQueueDashboard(expandedConversationIds);

  // Derive state from dashboard data
  const { jobs, conversationJobs, stats, streamingStatus, events } = useMemo<{
    jobs: any[];
    conversationJobs: {[conversationId: string]: any[]};
    stats: QueueStats | null;
    streamingStatus: StreamingStatus | null;
    events: EventRecord[];
  }>(() => {
    if (!dashboardData || dashboardError) {
      return { jobs: [], conversationJobs: {}, stats: null, streamingStatus: null, events: [] };
    }

    // Extract jobs from response (using RQ standard status names)
    const queuedJobs = dashboardData.jobs?.queued || [];
    const startedJobs = dashboardData.jobs?.started || [];
    const finishedJobs = dashboardData.jobs?.finished || [];
    const failedJobs = dashboardData.jobs?.failed || [];
    const deferredJobs = dashboardData.jobs?.deferred || [];  // chained jobs waiting on a dependency
    const scheduledJobs = dashboardData.jobs?.scheduled || [];
    const allFetchedJobs = uniqueJobs([...queuedJobs, ...startedJobs, ...finishedJobs, ...failedJobs, ...deferredJobs, ...scheduledJobs]);

    // Group jobs by conversation_id
    const jobsByConversation: {[conversationId: string]: any[]} = {};
    allFetchedJobs.forEach(job => {
      if (!job || !job.job_id) return;
      const conversationId = job.meta?.conversation_id;
      if (conversationId) {
        if (!jobsByConversation[conversationId]) {
          jobsByConversation[conversationId] = [];
        }
        jobsByConversation[conversationId].push(job);
      }
    });

    // Merge conversation jobs from dashboard response
    const dashboardConvJobs = dashboardData.conversation_jobs || dashboardData.session_jobs;
    if (dashboardConvJobs) {
      Object.entries(dashboardConvJobs).forEach(([conversationId, cJobs]: [string, any]) => {
        const existingJobs = jobsByConversation[conversationId] || [];
        jobsByConversation[conversationId] = uniqueJobs([...existingJobs, ...cJobs]);
      });
    }

    return {
      jobs: allFetchedJobs,
      conversationJobs: jobsByConversation,
      stats: dashboardData.stats || null,
      streamingStatus: dashboardData.streaming_status || null,
      events: dashboardData.events || [],
    };
  }, [dashboardData, dashboardError]);

  // A failed privacy refresh must not leave previously fetched payloads on screen.
  useEffect(() => {
    if (dashboardError) {
      detailRequest.current += 1;
      setSelectedJob(null);
      setSelectedEvent(null);
      setFlushPreview(null);
    }
  }, [dashboardError]);
  const visibleEvent = !dashboardError && selectedEvent
    ? events.find(event => event.timestamp === selectedEvent.timestamp && event.event === selectedEvent.event)
    : undefined;
  const currentJobSummary = jobs.find(job => job.job_id === selectedJob?.job_id);
  const visibleJob = dashboardError ? null
    : currentJobSummary?.privacy_held ? currentJobSummary : selectedJob;

  // Job Type filter options come from the jobs actually loaded rather than a
  // hardcoded list, which had drifted to four names that are not job types at all.
  const jobTypeOptions = useMemo(
    () => Array.from(new Set(jobs.map(j => j?.job_type).filter(Boolean))).sort(),
    [jobs]
  );

  // Auto-expand active conversations when data changes
  const prevAutoExpandedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!conversationJobs || Object.keys(conversationJobs).length === 0) return;

    const newExpanded = new Set(expandedConversations);
    const newExpandedJobs = new Set(expandedJobs);
    let changed = false;
    let jobsChanged = false;

    Object.entries(conversationJobs).forEach(([, cJobs]) => {
      const openConvJob = (cJobs as any[]).find((j: any) => j.job_type === 'open_conversation_job');
      if (openConvJob && openConvJob.status === 'started') {
        const convId = openConvJob.meta?.conversation_id;
        if (convId && !newExpanded.has(convId) && !prevAutoExpandedRef.current.has(convId)) {
          newExpanded.add(convId);
          prevAutoExpandedRef.current.add(convId);
          changed = true;

          (cJobs as any[]).forEach((job: any) => {
            if (!newExpandedJobs.has(job.job_id)) {
              newExpandedJobs.add(job.job_id);
              jobsChanged = true;
            }
          });
        }
      }
    });

    if (changed) setExpandedConversations(newExpanded);
    if (jobsChanged) setExpandedJobs(newExpandedJobs);
  }, [conversationJobs]);

  const invalidateQueue = () => queryClient.invalidateQueries({ queryKey: ['queue'] });


  const viewJobDetails = async (jobId: string) => {
    const request = ++detailRequest.current;
    setSelectedJob(null);
    setLoadingJobDetails(true);
    try {
      const response = await queueApi.getJob(jobId);
      if (request === detailRequest.current) setSelectedJob(response.data);
    } catch (error) {
      if (request !== detailRequest.current) return;
      setSelectedJob(null);
      alert('Failed to fetch job details');
    } finally {
      setLoadingJobDetails(false);
    }
  };

  // ESC key handler for modals
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedEvent) {
          setSelectedEvent(null);
        } else if (selectedJob) {
          setSelectedJob(null);
        } else if (showFlushModal) {
          setShowFlushModal(false);
        }
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [selectedJob, showFlushModal, selectedEvent]);

  // One control replaces three. "Cleanup Old Sessions" and "Remove All Streams"
  // called the same endpoint with a different age parameter that only affected
  // session metadata — so the second did not do what its name or its confirmation
  // dialog claimed. "Cleanup Stuck Workers" was named for the one thing it refuses
  // to do: only the consumer that committed a side effect may acknowledge its own
  // messages, so a genuinely stuck consumer is reported, never force-cleared.
  //
  // Reclaiming is now continuous (the audio_stream_reclaim cron). This button runs
  // that same sweep on demand while diagnosing.
  const reclaimStreams = async () => {
    try {
      const { data } = await queueApi.reclaimStreams();
      const blocked = Object.entries(data.blocked ?? {});
      alert(
        `Reclaimed ${data.reclaimed} of ${data.examined} stream(s); ` +
        `dropped ${data.dropped_consumers} finished consumer(s).` +
        (blocked.length
          ? `\n\nBlocked by an undrained consumer — see System Errors:\n` +
            blocked.map(([name, reason]) => `${name.replace('audio:stream:', '')}: ${reason}`).join('\n')
          : '')
      );
      invalidateQueue();
    } catch (error: any) {
      console.error('Error reclaiming streams:', error);
      alert(`Failed to reclaim streams: ${error.response?.data?.error || error.message}`);
    }
  };

  const applyFilters = () => {
    setPagination(prev => ({ ...prev, offset: 0 }));
    invalidateQueue();
  };

  const clearFilters = () => {
    setFilters({ status: '', job_type: '' });
    setPagination(prev => ({ ...prev, offset: 0 }));
  };


  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'queued': return <Clock className="w-4 h-4" />;
      case 'started': return <Play className="w-4 h-4 animate-pulse" />;  // RQ standard
      case 'finished': return <CheckCircle className="w-4 h-4" />;  // RQ standard
      case 'failed': return <XCircle className="w-4 h-4" />;
      case 'canceled': return <StopCircle className="w-4 h-4" />;  // RQ standard (US spelling)
      case 'deferred': return <Pause className="w-4 h-4" />;
      case 'scheduled': return <Pause className="w-4 h-4" />;  // RQ standard, not "waiting"
      default: return <Clock className="w-4 h-4" />;
    }
  };

  // Job status is a genuine state signal, so it renders as a StateBadge tone
  // rather than hand-picked classes.
  const getStatusTone = (status: string): StateTone => {
    switch (status) {
      case 'queued': return 'warning';
      case 'started': return 'info';  // RQ standard
      case 'finished': return 'success';  // RQ standard
      case 'failed': return 'danger';
      case 'canceled': return 'neutral';  // RQ standard (US spelling)
      case 'deferred': return 'info';
      case 'scheduled': return 'info';  // RQ standard, not "waiting"
      default: return 'neutral';
    }
  };

  const getJobTypeIcon = (type: string) => {
    const iconClass = "w-3.5 h-3.5";
    switch (type) {
      case 'audio_transcription':
      case 'process_audio_chunk':
        return <FileAudio className={iconClass} />;
      case 'transcript_processing':
      case 'reprocess_transcript':
        return <FileText className={iconClass} />;
      case 'memory_extraction':
      case 'reprocess_memory':
        return <Brain className={iconClass} />;
      case 'process_audio_files':
      case 'process_single_audio_file':
        return <Zap className={iconClass} />;
      default:
        return <Repeat className={iconClass} />;
    }
  };

  const getJobTypeColor = (type: string, status: string) => {
    // Safety check for undefined/null values
    if (!type || !status) {
      return { bgColor: 'bg-gray-400', borderColor: 'border-gray-500' };
    }

    // Base colors by job type
    let bgColor = 'bg-gray-400';
    let borderColor = 'border-gray-500';

    // Transcription jobs - blue shades
    if (type.includes('transcribe') || type === 'transcribe_full_audio_job') {
      bgColor = 'bg-blue-500';
      borderColor = 'border-blue-600';
    }
    // Speaker recognition - purple shades
    else if (type.includes('speaker') || type.includes('recognise') || type === 'recognise_speakers_job') {
      bgColor = 'bg-purple-500';
      borderColor = 'border-purple-600';
    }
    // Memory jobs - pink shades
    else if (type.includes('memory') || type === 'process_memory_job') {
      bgColor = 'bg-pink-500';
      borderColor = 'border-pink-600';
    }
    // Conversation/open jobs - cyan shades (check this AFTER memory to avoid confusion)
    else if (type.includes('conversation') || type.includes('open_conversation') || type === 'open_conversation_job') {
      bgColor = 'bg-cyan-500';
      borderColor = 'border-cyan-600';
    }
    // Speech detection jobs - green shades
    else if (type.includes('speech') || type.includes('detect')) {
      bgColor = 'bg-green-500';
      borderColor = 'border-green-600';
    }
    // Audio processing - orange shades
    else if (type.includes('audio') || type.includes('persist')) {
      bgColor = 'bg-orange-500';
      borderColor = 'border-orange-600';
    }
    // Default - gray
    else {
      bgColor = 'bg-gray-400';
      borderColor = 'border-gray-500';
    }

    // Failed jobs - always red
    if (status === 'failed') {
      bgColor = 'bg-red-500';
      borderColor = 'border-red-600';
    }
    // Processing jobs - add pulse animation
    else if (status === 'started') {
      bgColor = bgColor + ' animate-pulse';
    }

    return { bgColor, borderColor };
  };


  // A preview reflects a specific set of flush settings; drop it whenever they change
  useEffect(() => { setFlushPreview(null); }, [flushSettings]);

  const buildFlushBody = (dryRun: boolean) =>
    flushSettings.flush_all
      ? {
          confirm: true,
          include_failed: flushSettings.include_failed,
          include_finished: flushSettings.include_finished,
          dry_run: dryRun,
        }
      : {
          older_than_hours: flushSettings.older_than_hours,
          statuses: flushSettings.statuses,
          dry_run: dryRun,
        };

  const previewFlush = async () => {
    setPreviewing(true);
    try {
      const response = await queueApi.flushJobs(flushSettings.flush_all, buildFlushBody(true));
      setFlushPreview(response.data);
    } catch (error: any) {
      console.error('Error previewing flush:', error);
      if (error.response?.status === 403) {
        alert('Admin access required to preview flush');
      } else {
        alert(`Failed to preview flush: ${error.response?.data?.detail || error.message}`);
      }
    } finally {
      setPreviewing(false);
    }
  };

  const flushJobs = async () => {
    setFlushing(true);
    try {
      const response = await queueApi.flushJobs(flushSettings.flush_all, buildFlushBody(false));
      alert(`Successfully flushed ${response.data.total_removed} jobs!`);
      setShowFlushModal(false);
      setFlushPreview(null);
      invalidateQueue();
    } catch (error: any) {
      console.error('Error flushing jobs:', error);
      if (error.response?.status === 403) {
        alert('Admin access required to flush jobs');
      } else if (error.response?.status === 401) {
        localStorage.removeItem('token');
        window.location.href = '/login';
      } else {
        alert(`Failed to flush jobs: ${error.response?.data?.detail || error.message}`);
      }
    } finally {
      setFlushing(false);
    }
  };

  const formatDate = (dateString: string) => {
    return queueDate(dateString).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' });
  };

  // Short display names for the Jobs table. Keyed on the RQ job function names in
  // backend.workers; the full job_type stays available as a tooltip.
  const getJobTypeShort = (jobType: string) => {
    const typeMap: {[key: string]: string} = {
      // Session / conversation lifecycle
      'stream_speech_detection_job': 'Speech Detect',
      'open_conversation_job': 'Open Conv',
      'audio_streaming_persistence_job': 'Audio Persist',
      // Post-conversation chain
      'transcribe_full_audio_job': 'Transcribe',
      'transcription_fallback_check_job': 'Fallback Check',
      'recognise_speakers_job': 'Speakers',
      'check_enrolled_speakers_job': 'Check Speakers',
      'process_memory_job': 'Memory',
      'generate_title_job': 'Title',
      'generate_short_summary_job': 'Short Summary',
      'generate_detailed_summary_job': 'Detailed Summary',
      'dispatch_conversation_complete_event_job': 'Dispatch Event',
    };
    return typeMap[jobType] || jobType;
  };

  const retryJob = async (jobId: string) => {
    try {
      await queueApi.retryJob(jobId);
      invalidateQueue();
    } catch (error) {
      console.error('Failed to retry job:', error);
    }
  };

  const cancelJob = async (jobId: string) => {
    try {
      await queueApi.cancelJob(jobId);
      invalidateQueue();
    } catch (error) {
      console.error('Failed to cancel job:', error);
    }
  };

  const prevPage = () => {
    setPagination(prev => ({
      ...prev,
      offset: Math.max(0, prev.offset - prev.limit)
    }));
  };

  const nextPage = () => {
    setPagination(prev => ({
      ...prev,
      offset: prev.offset + prev.limit
    }));
  };

  const formatDuration = (job: any) => {
    if (!job.started_at) return '-';

    const start = queueDate(job.started_at).getTime();
    // For failed/finished jobs, use completed_at or ended_at. For running jobs, use current time.
    const end = job.completed_at || job.ended_at
      ? queueDate((job.completed_at || job.ended_at)!).getTime()
      : (job.status === 'started' ? Date.now() : start); // Don't show increasing time for failed jobs
    // RQ's started_at/ended_at can be sub-millisecond out of order for near-instant
    // jobs, yielding a tiny negative; clamp so we never render e.g. "-27ms".
    const durationMs = Math.max(0, end - start);

    if (durationMs < 1000) return `${durationMs}ms`;
    if (durationMs < 60000) return `${(durationMs / 1000).toFixed(1)}s`;
    if (durationMs < 3600000) return `${Math.floor(durationMs / 60000)}m ${Math.floor((durationMs % 60000) / 1000)}s`;
    return `${Math.floor(durationMs / 3600000)}h ${Math.floor((durationMs % 3600000) / 60000)}m`;
  };

  const toggleConversationExpansion = (conversationId: string) => {
    const newExpanded = new Set(expandedConversations);

    if (newExpanded.has(conversationId)) {
      // Collapse
      newExpanded.delete(conversationId);
      setExpandedConversations(newExpanded);
    } else {
      // Expand — React Query will refetch with the new expanded list
      newExpanded.add(conversationId);
      setExpandedConversations(newExpanded);
    }
  };

  const toggleJobExpansion = (jobId: string) => {
    const newExpanded = new Set(expandedJobs);
    if (newExpanded.has(jobId)) {
      newExpanded.delete(jobId);
    } else {
      newExpanded.add(jobId);
    }
    setExpandedJobs(newExpanded);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center">
        <div className="flex items-center space-x-3">
          <Layers className="w-6 h-6 text-blue-600 flex-shrink-0" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Queue & Events</h1>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Jobs and audio processing
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            size="md"
            icon={<RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />}
            onClick={invalidateQueue}
            disabled={refreshing}
          >
            Refresh
          </Button>
          <details className="relative">
            <summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-300">Maintenance</summary>
            <div className="absolute right-0 z-20 mt-2 flex flex-col gap-2 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800">
              <Button variant="secondary" onClick={reclaimStreams}>Reclaim finished streams</Button>
              <Button variant="danger" onClick={() => setShowFlushModal(true)}>Flush jobs…</Button>
            </div>
          </details>
        </div>
      </div>
      {dashboardError && <Alert role="alert" tone="danger">Queue details are hidden until their privacy status can be checked. Refresh to try again.</Alert>}

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
          <StatCard label="Total" value={stats.total_jobs} />
          <StatCard label="Queued" value={stats.queued_jobs} tone="amber" />
          <StatCard
            label="Started"
            tone="blue"
            value={<span className={stats.started_jobs > 0 ? 'animate-pulse' : ''}>{stats.started_jobs}</span>}
          />
          <StatCard label="Finished" value={stats.finished_jobs} tone="green" />
          <StatCard label="Failed" value={stats.failed_jobs} tone="red" />
          <StatCard label="Canceled" value={stats.canceled_jobs} />
          <StatCard label="Deferred" value={stats.deferred_jobs} tone="blue" />
        </div>
      )}

      {/* Streaming Status */}
      {streamingStatus && (
        <Card raised padded={false} className="overflow-hidden">
          <div className="px-4 sm:px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center">
            <SectionTitle>Audio processing</SectionTitle>
          </div>

          <div className="p-6 space-y-6">
            {/* Stream Workers Section - Shows audio streams + listen jobs */}
            <div>
              <GroupTitle className="mb-3">Retained audio streams</GroupTitle>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {streamingStatus?.stream_health && Object.entries(streamingStatus.stream_health).map(([streamKey, health]) => {
                  const clientId = health.client_id;
                  const session = streamingStatus.active_sessions?.find(s => s.session_id === health.session_id);

                  // Find all listen jobs for this client with deduplication
                  const allJobsRaw = Object.values(conversationJobs).flat().filter(job => job != null);

                  // Deduplicate by job_id
                  const jobMap = new Map();
                  allJobsRaw.forEach((job: any) => {
                    if (job && job.job_id) {
                      jobMap.set(job.job_id, job);
                    }
                  });
                  const allJobs = Array.from(jobMap.values());

                  // Get all listen jobs for this client (only active/queued/processing, not completed)
                  const allListenJobs = allJobs.filter((job: any) =>
                    job && job.job_type === 'stream_speech_detection_job' &&
                    job.meta?.client_id === clientId &&
                    job.status !== 'finished' &&
                    job.status !== 'failed'
                  );

                  // Show only the LATEST active speech detection job (most recent created_at)
                  // Completed ones have already exited and shouldn't be shown here
                  const listenJobs = allListenJobs.length > 0
                    ? [allListenJobs.sort((a, b) =>
                        queueDate(b.created_at).getTime() - queueDate(a.created_at).getTime()
                      )[0]]
                    : [];

                  return (
                    <div key={streamKey} className="p-4 bg-gray-50 rounded-lg border border-gray-200 dark:bg-gray-900/40 dark:border-gray-700">
                      <div className="flex items-start justify-between gap-2 mb-2">
                        <span className="min-w-0 flex-1 break-words text-sm font-medium text-gray-900 dark:text-gray-100" title={streamKey}>{clientId || 'Unlinked stream'}</span>
                        <StateBadge className="shrink-0" tone={health.error ? "danger" : "neutral"}>{health.error ? "Unavailable" : health.exists === false ? "Not present" : "Retained"}</StateBadge>
                      </div>

                      {session?.privacy_held && (
                        <p role="status" className="mb-2 text-xs text-amber-700 dark:text-amber-300">{session.privacy_reason || 'Private or unscreened session details held'}</p>
                      )}
                      <div className="space-y-2">
                        <div className="flex justify-between text-xs">
                          <span className="text-gray-600 dark:text-gray-400">Stream Length:</span>
                          <span className="font-medium text-gray-900 dark:text-gray-100">{health.stream_length ?? "Unknown"}</span>
                        </div>
                        <div className="flex justify-between text-xs">
                          <span className="text-gray-600 dark:text-gray-400">Age:</span>
                          <span className="font-medium text-gray-900 dark:text-gray-100">{health.stream_age_seconds == null ? "Unknown" : `${health.stream_age_seconds.toFixed(0)}s`}</span>
                        </div>
                        <div className="flex justify-between text-xs">
                          <span className="text-gray-600 dark:text-gray-400">Pending:</span>
                          <span className={`font-medium ${health.total_pending == null ? 'text-gray-500 dark:text-gray-400' : health.total_pending > 0 ? 'text-yellow-600 dark:text-yellow-400' : 'text-green-600 dark:text-green-400'}`}>
                            {health.total_pending ?? "Unknown"}
                          </span>
                        </div>
                        {health.consumer_groups && health.consumer_groups.map((group) => (
                          <div key={group.name} className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                            <div className="text-xs text-gray-600 dark:text-gray-400 mb-1">{group.name}:</div>
                            {(group.consumers || []).map((consumer) => (
                              <div key={consumer.name} className="flex justify-between text-xs pl-2">
                                <span className="text-gray-700 dark:text-gray-300 truncate">{consumer.name}</span>
                                <span className={consumer.pending > 0 ? 'text-yellow-600 dark:text-yellow-400' : 'text-green-600 dark:text-green-400'}>
                                  {consumer.pending} pending
                                </span>
                              </div>
                            ))}
                          </div>
                        ))}

                        {/* Current Speech Detection Job */}
                        {listenJobs.length > 0 && (
                          <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                            <div className="text-xs text-gray-600 dark:text-gray-400 mb-1">Current Speech Detection:</div>
                            {listenJobs.map((job) => {
                              const runtime = job.started_at
                                ? Math.floor((Date.now() - queueDate(job.started_at).getTime()) / 1000)
                                : 0;
                              const minutes = Math.floor(runtime / 60);
                              const seconds = runtime % 60;

                              return (
                                <div key={job.job_id} className="bg-white dark:bg-gray-800 rounded p-2 space-y-1">
                                  <div className="flex items-center justify-between">
                                    <div className="flex items-center space-x-1 text-gray-500 dark:text-gray-400">
                                      {getStatusIcon(job.status)}
                                      <span className="text-gray-700 dark:text-gray-300 font-medium text-xs">{job.job_type}</span>
                                      <StateBadge tone={getStatusTone(job.status)}>{job.status}</StateBadge>
                                    </div>
                                    <IconButton
                                      label="View job details"
                                      className="flex-shrink-0"
                                      onClick={() => viewJobDetails(job.job_id)}
                                    >
                                      <Eye className="w-3 h-3" />
                                    </IconButton>
                                  </div>

                                  {/* Job metadata */}
                                  <div className="text-xs text-gray-600 dark:text-gray-400 space-y-0.5 pl-4">
                                    <div className="flex justify-between">
                                      <span>Job ID:</span>
                                      <span className="font-mono text-gray-800 dark:text-gray-200">{job.job_id.substring(0, 12)}...</span>
                                    </div>
                                    {job.started_at && (
                                      <div className="flex justify-between">
                                        <span>Runtime:</span>
                                        <span className="font-medium text-gray-800 dark:text-gray-200">{minutes}m {seconds}s</span>
                                      </div>
                                    )}
                                    {job.created_at && (
                                      <div className="flex justify-between">
                                        <span>Created:</span>
                                        <span className="text-gray-800 dark:text-gray-200">{queueDate(job.created_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}</span>
                                      </div>
                                    )}
                                    {job.meta?.speech_detected_at && (
                                      <div className="flex justify-between">
                                        <span>Speech Detected:</span>
                                        <span className="text-green-700 dark:text-green-400 font-medium">{queueDate(job.meta.speech_detected_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}</span>
                                      </div>
                                    )}
                                    {job.meta?.status && (
                                      <div className="flex justify-between">
                                        <span>Status:</span>
                                        <span className="text-blue-700 dark:text-blue-400 font-medium">{job.meta.status.replace(/_/g, ' ')}</span>
                                      </div>
                                    )}
                                  </div>

                                  {/* Session Events */}
                                  {(() => {
                                    const session = streamingStatus?.active_sessions?.find((s: StreamingSession) => s.session_id === job.meta?.session_id);
                                    if (!session) return null;

                                    return (
                                      <div className="text-xs space-y-1 pl-4 mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                                        <div className="font-semibold text-gray-700 dark:text-gray-300 mb-1">Speech Detection Events:</div>
                                        {!session.privacy_held && session.last_event && (
                                          <div className="flex justify-between">
                                            <span className="text-gray-600 dark:text-gray-400">Last Event:</span>
                                            <span className="text-gray-800 dark:text-gray-200 font-mono text-xs">{session.last_event.split(':')[0]}</span>
                                          </div>
                                        )}
                                        {session.speaker_check_status && (
                                          <div className="flex justify-between">
                                            <span className="text-gray-600 dark:text-gray-400">Speaker Check:</span>
                                            <span className={`font-medium ${
                                              session.speaker_check_status === 'enrolled' ? 'text-green-700 dark:text-green-400' :
                                              session.speaker_check_status === 'checking' ? 'text-blue-700 dark:text-blue-400' :
                                              session.speaker_check_status === 'failed' ? 'text-red-700 dark:text-red-400' :
                                              session.speaker_check_status === 'timeout' ? 'text-yellow-700 dark:text-yellow-400' :
                                              'text-gray-700 dark:text-gray-300'
                                            }`}>{session.speaker_check_status}</span>
                                          </div>
                                        )}
                                        {!session.privacy_held && session.identified_speakers && (
                                          <div className="flex justify-between">
                                            <span className="text-gray-600 dark:text-gray-400">Speakers:</span>
                                            <span className="text-green-700 dark:text-green-400 font-medium">{session.identified_speakers}</span>
                                          </div>
                                        )}
                                      </div>
                                    );
                                  })()}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Active and Completed Conversations Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Active Conversations - Grouped by conversation_id */}
              <div>
                <GroupTitle className="mb-3">Active Conversations</GroupTitle>
                {(() => {
                  // Group all jobs by conversation_id with deduplication
                  const allJobsRaw = Object.values(conversationJobs).flat().filter(job => job != null);

                  // Deduplicate by job_id
                  const jobMap = new Map();
                  allJobsRaw.forEach((job: any) => {
                    if (job && job.job_id) {
                      jobMap.set(job.job_id, job);
                    }
                  });
                  const allJobs = Array.from(jobMap.values());

                  // Group ALL jobs by conversation_id (regardless of status)
                  const allConversationJobs = new Map<string, any[]>();

                  // Group jobs by conversation_id only
                  // EXCLUDE session-level jobs (like audio persistence)
                  allJobs.forEach(job => {
                    if (!job) return;

                    // Skip session-level jobs (they run for entire session, not per conversation)
                    // Also skip audio persistence jobs by job_type
                    if (job.meta?.session_level === true || job.job_type === 'audio_streaming_persistence_job') {
                      return;
                    }

                    const conversationId = job.meta?.conversation_id;
                    if (conversationId) {
                      if (!allConversationJobs.has(conversationId)) {
                        allConversationJobs.set(conversationId, []);
                      }
                      allConversationJobs.get(conversationId)!.push(job);
                    }
                  });

                  // Filter to only show conversations where at least one job is NOT completed
                  const conversationMap = new Map<string, any[]>();
                  allConversationJobs.forEach((jobs, conversationId) => {
                    const hasActiveJob = jobs.some(j => j.status !== 'finished' && j.status !== 'failed');
                    if (hasActiveJob) {
                      conversationMap.set(conversationId, jobs);
                    }
                  });

                  if (conversationMap.size === 0) {
                    return <EmptyState>No active conversations</EmptyState>;
                  }

                  return (
                    <div className="space-y-2">
                      {Array.from(conversationMap.entries()).map(([conversationId, jobs]) => {
                        const isExpanded = expandedConversations.has(conversationId);

                        // Find the open_conversation_job for metadata, or fallback to any job with metadata
                        const openConvJob = jobs.find(j => j.job_type === 'open_conversation_job');
                        const fallbackJob = jobs.find(j => j.meta && Object.keys(j.meta).length > 0);
                        const meta = openConvJob?.meta || fallbackJob?.meta || {};

                        // Extract conversation info
                        const clientId = meta.client_id || 'Unknown';
                        const transcript = meta.transcript || '';
                        const speakers = meta.speakers || [];
                        const wordCount = meta.word_count || 0;
                        const lastUpdate = meta.last_update || '';
                        const createdAt = openConvJob?.created_at || null;

                        // Check if any jobs have failed
                        const hasFailedJob = jobs.some(j => j.status === 'failed');
                        const failedJobCount = jobs.filter(j => j.status === 'failed').length;

                        return (
                          <div key={conversationId} className={`rounded-lg border overflow-hidden ${hasFailedJob ? 'bg-red-50 border-red-300 dark:bg-red-900/20 dark:border-red-800' : 'bg-cyan-50 border-cyan-200 dark:bg-cyan-900/20 dark:border-cyan-800'}`}>
                            <div
                              className={`flex items-center justify-between p-3 cursor-pointer transition-colors ${hasFailedJob ? 'hover:bg-red-100 dark:hover:bg-red-900/30' : 'hover:bg-cyan-100 dark:hover:bg-cyan-900/30'}`}
                              onClick={() => toggleConversationExpansion(conversationId)}
                            >
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center space-x-2">
                                  {isExpanded ? (
                                    <ChevronDown className={`w-4 h-4 ${hasFailedJob ? 'text-red-600 dark:text-red-400' : 'text-cyan-600 dark:text-cyan-400'}`} />
                                  ) : (
                                    <ChevronRight className={`w-4 h-4 ${hasFailedJob ? 'text-red-600 dark:text-red-400' : 'text-cyan-600 dark:text-cyan-400'}`} />
                                  )}
                                  {hasFailedJob ? (
                                    <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400" />
                                  ) : (
                                    <Brain className="w-4 h-4 text-cyan-600 dark:text-cyan-400 animate-pulse" />
                                  )}
                                  <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{clientId}</span>
                                  {hasFailedJob ? (
                                    <StateBadge tone="danger">
                                      {failedJobCount} Error{failedJobCount > 1 ? 's' : ''}
                                    </StateBadge>
                                  ) : (
                                    <StateBadge tone="info">Active</StateBadge>
                                  )}
                                  {speakers.length > 0 && (
                                    <MetadataChip>
                                      {speakers.length} speaker{speakers.length > 1 ? 's' : ''}
                                    </MetadataChip>
                                  )}
                                </div>
                                <div className="mt-1 text-xs text-gray-600 dark:text-gray-400 truncate">
                                  Conversation: {conversationId.substring(0, 8)}... •
                                  {createdAt && `Started: ${queueDate(createdAt).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })} • `}
                                  Words: {wordCount}
                                  {lastUpdate && ` • Updated: ${queueDate(lastUpdate).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}`}
                                </div>
                                {transcript && (
                                  <div className="mt-1 text-xs text-gray-700 dark:text-gray-300 italic truncate">
                                    "{transcript.substring(0, 100)}{transcript.length > 100 ? '...' : ''}"
                                  </div>
                                )}
                              </div>
                              {/* Close Conversation Button - only for actively running conversations */}
                              {openConvJob && openConvJob.status === 'started' && (
                                <Button
                                  variant="danger"
                                  className="flex-shrink-0 ml-3"
                                  icon={<StopCircle className="w-4 h-4" />}
                                  title="Close the current active conversation"
                                  onClick={async (e) => {
                                    e.stopPropagation();
                                    if (!confirm(`Close the active conversation for ${clientId}? This will end the current conversation and trigger post-processing.`)) return;
                                    try {
                                      await conversationsApi.closeActiveConversation(clientId);
                                      invalidateQueue();
                                    } catch (error: any) {
                                      console.error('Failed to close conversation:', error);
                                      alert(`Failed to close conversation: ${error.response?.data?.error || error.message}`);
                                    }
                                  }}
                                >
                                  Close
                                </Button>
                              )}
                            </div>

                          {/* Expanded Jobs Section */}
                          {isExpanded && (
                            <div className="border-t border-cyan-200 dark:border-cyan-800 bg-white dark:bg-gray-800 p-3">
                              {/* Pipeline Timeline */}
                              <div className="mb-4">
                                <h5 className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-3">Pipeline Timeline:</h5>
                                {(() => {
                                  // Helper function to get display name from job type
                                  const getJobDisplayName = (jobType: string) => {
                                    const nameMap: { [key: string]: string } = {
                                      'stream_speech_detection_job': 'Speech',
                                      'open_conversation_job': 'Open',
                                      'transcribe_full_audio_job': 'Transcript',
                                      'recognise_speakers_job': 'Speakers',
                                      'process_memory_job': 'Memory'
                                    };
                                    return nameMap[jobType] || jobType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                                  };

                                  // Helper function to get icon for job type
                                  const getJobIcon = (jobType: string) => {
                                    if (jobType.includes('speech') || jobType.includes('detect')) return Brain;
                                    if (jobType.includes('conversation') || jobType.includes('open')) return Brain;
                                    if (jobType.includes('transcribe')) return FileText;
                                    if (jobType.includes('speaker') || jobType.includes('recognise')) return Brain;
                                    if (jobType.includes('memory')) return Brain;
                                    return Brain; // Default icon
                                  };

                                  // Build dynamic pipeline from actual jobs with timing data
                                  // Sort by start time to show chronological order
                                  const jobsWithTiming = jobs
                                    .filter(j => j && j.started_at)
                                    .map(job => {
                                      const startTime = queueDate(job.started_at!).getTime();
                                      const endTime = job.completed_at || job.ended_at
                                        ? queueDate((job.completed_at || job.ended_at)!).getTime()
                                        : (job.status === 'started' ? Date.now() : startTime);

                                      return {
                                        job,
                                        startTime,
                                        endTime,
                                        duration: Math.max(0, endTime - startTime) / 1000,
                                        name: getJobDisplayName(job.job_type),
                                        icon: getJobIcon(job.job_type)
                                      };
                                    })
                                    .sort((a, b) => a.startTime - b.startTime);

                                  const jobTimes = jobsWithTiming;

                                  // Find earliest start and latest end
                                  const validTimes = jobTimes.filter(t => t !== null);
                                  if (validTimes.length === 0) {
                                    return (
                                      <div className="text-xs text-gray-500 dark:text-gray-400 italic">No job timing data available</div>
                                    );
                                  }

                                  const earliestStart = Math.min(...validTimes.map(t => t!.startTime));
                                  const latestEnd = Math.max(...validTimes.map(t => t!.endTime));
                                  const totalDuration = (latestEnd - earliestStart) / 1000; // in seconds

                                  // Format duration for display
                                  const formatDuration = (seconds: number) => {
                                    if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
                                    if (seconds < 60) return `${seconds.toFixed(1)}s`;
                                    const mins = Math.floor(seconds / 60);
                                    const secs = Math.floor(seconds % 60);
                                    return `${mins}m ${secs}s`;
                                  };

                                  // Generate time axis markers (0%, 25%, 50%, 75%, 100%)
                                  const timeMarkers = [0, 0.25, 0.5, 0.75, 1].map(pct => ({
                                    percent: pct * 100,
                                    time: formatDuration(totalDuration * pct)
                                  }));

                                  return (
                                    <div className="space-y-2">
                                      {/* Time axis */}
                                      <div className="relative h-4 border-b border-gray-300 dark:border-gray-600">
                                        {timeMarkers.map((marker, idx) => (
                                          <div
                                            key={idx}
                                            className="absolute"
                                            style={{ left: `${marker.percent}%`, transform: 'translateX(-50%)' }}
                                          >
                                            <div className="w-px h-2 bg-gray-400"></div>
                                            <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 whitespace-nowrap">
                                              {marker.time}
                                            </div>
                                          </div>
                                        ))}
                                      </div>

                                      {/* Job timeline bars */}
                                      <div className="space-y-2 mt-6">
                                        {jobTimes.map((jobTime) => {
                                          const { job, startTime, endTime, duration, name, icon: Icon } = jobTime;

                                          // Calculate position and width as percentage of total timeline
                                          const startPercent = ((startTime - earliestStart) / (latestEnd - earliestStart)) * 100;
                                          const widthPercent = ((endTime - startTime) / (latestEnd - earliestStart)) * 100;

                                          // Use job type colors
                                          const jobColors = getJobTypeColor(job.job_type, job.status);
                                          const barColor = jobColors.bgColor;
                                          const borderColor = jobColors.borderColor;

                                          return (
                                            <div key={job.job_id} className="flex items-center space-x-2 h-8">
                                              {/* Stage Icon */}
                                              <div className={`w-8 h-8 rounded-full border-2 ${borderColor} ${barColor} flex items-center justify-center flex-shrink-0`}>
                                                <Icon className="w-4 h-4 text-white" />
                                              </div>

                                              {/* Stage Name */}
                                              <span className="text-xs text-gray-700 dark:text-gray-300 w-20 flex-shrink-0">{name}</span>

                                              {/* Timeline Container */}
                                              <div className="flex-1 relative h-6 bg-gray-100 dark:bg-gray-700 rounded">
                                                {/* Job Bar */}
                                                <div
                                                  className={`absolute h-6 rounded ${barColor} ${job.status === 'started' ? 'animate-pulse' : ''} flex items-center justify-center`}
                                                  style={{
                                                    left: `${startPercent}%`,
                                                    width: `${widthPercent}%`
                                                  }}
                                                  title={`Started: ${queueDate(startTime).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}\nDuration: ${formatDuration(duration)}`}
                                                >
                                                  <span className="text-xs text-white font-medium px-2 truncate">
                                                    {formatDuration(duration)}
                                                  </span>
                                                </div>
                                              </div>
                                            </div>
                                          );
                                        })}
                                      </div>

                                      {/* Total Duration */}
                                      <div className="text-xs text-gray-600 dark:text-gray-400 text-right mt-2">
                                        Total: {formatDuration(totalDuration)}
                                      </div>
                                    </div>
                                  );
                                })()}
                              </div>

                              <h5 className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2">Conversation Jobs:</h5>
                              {jobs.filter(j => j != null && j.job_id).length > 0 ? (
                                <div className="space-y-1">
                                  {jobs
                                    .filter(j => j != null && j.job_id)
                                    .sort((a, b) => queueDate(a.created_at).getTime() - queueDate(b.created_at).getTime())
                                    .map((job, index) => (
                                    <div key={job.job_id} className={`p-2 bg-gray-50 dark:bg-gray-900/40 rounded border ${getJobTypeColor(job.job_type, job.status).borderColor}`} style={{ borderLeftWidth: '12px' }}>
                                      <div
                                        className="flex items-center justify-between cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors rounded px-1 py-0.5"
                                        onClick={() => toggleJobExpansion(job.job_id)}
                                      >
                                        <div className="flex-1 min-w-0">
                                          <div className="flex items-center space-x-2">
                                            <span className="text-xs font-mono text-gray-500 dark:text-gray-400 flex-shrink-0">#{index + 1}</span>
                                            <span className="flex-shrink-0">{getJobTypeIcon(job.job_type)}</span>
                                            <span className="flex-shrink-0">{getStatusIcon(job.status)}</span>
                                            <span className="text-xs font-medium text-gray-900 dark:text-gray-100 truncate">{job.job_type}</span>
                                            <StateBadge tone={getStatusTone(job.status)}>{job.status}</StateBadge>
                                            <span className="text-xs text-gray-500 dark:text-gray-400">{job.queue}</span>
                                            {/* Show memory count badge on collapsed card */}
                                            {!expandedJobs.has(job.job_id) && job.job_type === 'process_memory_job' && job.result?.memories_created !== undefined && (
                                              <MetadataChip>{job.result.memories_created} memories</MetadataChip>
                                            )}
                                          </div>
                                        </div>
                                      </div>

                                      {job.meta?.progress && <ReconciliationProgress progress={job.meta.progress} status={job.status} compact />}
                                      {/* Collapsible metadata section */}
                                      {expandedJobs.has(job.job_id) && (
                                        <div className="mt-1 text-xs text-gray-600 dark:text-gray-400 space-y-0.5">
                                          <div>
                                            {job.started_at && (
                                              <span>Started: {queueDate(job.started_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}</span>
                                            )}
                                            {job.started_at && (
                                              <span> • Duration: {formatDuration(job)}</span>
                                            )}
                                          </div>

                                          {/* Show job-specific metadata */}
                                          {job.meta && (
                                            <div className="space-y-0.5 pl-2 border-l-2 border-gray-300 dark:border-gray-600">
                                              {/* open_conversation_job metadata */}
                                              {job.job_type === 'open_conversation_job' && (
                                                <>
                                                  {job.meta.word_count !== undefined && (
                                                    <div>Words: <span className="font-medium">{job.meta.word_count}</span></div>
                                                  )}
                                                  {job.meta.speakers && job.meta.speakers.length > 0 && (
                                                    <div>Speakers: <span className="font-medium">{job.meta.speakers.join(', ')}</span></div>
                                                  )}
                                                  {job.meta.inactivity_seconds !== undefined && (
                                                    <div>Idle: <span className="font-medium">{Math.floor(job.meta.inactivity_seconds)}s</span></div>
                                                  )}
                                                  {job.meta.transcript && (
                                                    <div className="italic text-gray-500 dark:text-gray-400 truncate max-w-md">
                                                      "{job.meta.transcript.substring(0, 80)}..."
                                                    </div>
                                                  )}
                                                </>
                                              )}

                                              {/* transcribe_full_audio_job batch progress */}
                                              {job.job_type === 'transcribe_full_audio_job' && job.status === 'started' && job.meta?.batch_progress && (
                                                <div className="mt-1">
                                                  <div className="flex items-center justify-between text-xs mb-1">
                                                    <span className="text-blue-700 dark:text-blue-400">{job.meta.batch_progress.message}</span>
                                                    <span className="text-blue-600 dark:text-blue-400 font-medium">{job.meta.batch_progress.percent}%</span>
                                                  </div>
                                                  <div className="w-full bg-blue-200 dark:bg-blue-900/40 rounded-full h-1.5">
                                                    <div className="bg-blue-600 h-1.5 rounded-full transition-all duration-300" style={{ width: `${job.meta.batch_progress.percent}%` }} />
                                                  </div>
                                                </div>
                                              )}

                                              {/* transcribe_full_audio_job metadata */}
                                              {job.job_type === 'transcribe_full_audio_job' && job.result && (
                                                <>
                                                  {job.result.transcript && (
                                                    <div>Transcript: <span className="font-medium">{job.result.transcript.length} chars</span></div>
                                                  )}
                                                  {job.result.processing_time_seconds && (
                                                    <div>Processing: <span className="font-medium">{job.result.processing_time_seconds.toFixed(1)}s</span></div>
                                                  )}
                                                </>
                                              )}

                                              {/* recognise_speakers_job metadata */}
                                              {job.job_type === 'recognise_speakers_job' && job.result && (
                                                <>
                                                  {job.result.identified_speakers && job.result.identified_speakers.length > 0 && (
                                                    <div>Identified: <span className="font-medium">{job.result.identified_speakers.join(', ')}</span></div>
                                                  )}
                                                  {job.result.segment_count && (
                                                    <div>Segments: <span className="font-medium">{job.result.segment_count}</span></div>
                                                  )}
                                                </>
                                              )}

                                              {/* process_memory_job metadata */}
                                              {job.job_type === 'process_memory_job' && job.meta && (
                                                <>
                                                  {job.meta.memories_created !== undefined && (
                                                    <div>Memories: <span className="font-medium">{job.meta.memories_created} created</span></div>
                                                  )}
                                                  {job.meta.processing_time && (
                                                    <div>Processing: <span className="font-medium">{job.meta.processing_time.toFixed(1)}s</span></div>
                                                  )}
                                                  {job.meta.memory_details && job.meta.memory_details.length > 0 && (
                                                    <div className="mt-2">
                                                      <div className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Memories Created:</div>
                                                      {job.meta.memory_details.map((memory: any, idx: number) => (
                                                        <div key={idx} className="text-xs bg-pink-50 dark:bg-pink-900/20 text-gray-700 dark:text-gray-300 p-2 rounded mb-1">
                                                          "{memory.text}"
                                                        </div>
                                                      ))}
                                                    </div>
                                                  )}
                                                </>
                                              )}

                                              {/* Show conversation_id if present */}
                                              {job.meta.conversation_id && (
                                                <div className="font-mono text-gray-500 dark:text-gray-400">
                                                  Conv: {job.meta.conversation_id.substring(0, 8)}...
                                                </div>
                                              )}
                                            </div>
                                          )}
                                        </div>
                                      )}
                                      <IconButton
                                        label="View job details"
                                        className="ml-2 flex-shrink-0"
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          viewJobDetails(job.job_id);
                                        }}
                                      >
                                        <Eye className="w-3 h-3" />
                                      </IconButton>
                                    </div>
                                  ))}
                                </div>
                              ) : (
                                <div className="text-xs text-gray-500 dark:text-gray-400 italic">No jobs found for this conversation</div>
                              )}
                            </div>
                          )}
                        </div>
                        );
                      })}
                    </div>
                  );
                })()}
              </div>

              {/* Completed Conversations - Grouped by conversation_id */}
              <div>
                <div className="flex items-center justify-between mb-3 gap-2">
                  <GroupTitle>Completed Conversations</GroupTitle>
                  <div className="flex items-center space-x-2">
                    <Label htmlFor="completed-conv-range" className="whitespace-nowrap text-xs">Time range:</Label>
                    <Select
                      id="completed-conv-range"
                      className="w-auto py-1 text-xs"
                      value={completedConvTimeRange}
                      onChange={(e) => {
                        setCompletedConvTimeRange(Number(e.target.value));
                        setCompletedConvPage(1); // Reset to first page
                      }}
                    >
                      <option value={1}>Last 1 hour</option>
                      <option value={6}>Last 6 hours</option>
                      <option value={24}>Last 24 hours</option>
                      <option value={168}>Last 7 days</option>
                    </Select>
                  </div>
                </div>
                {(() => {
                  // Group all jobs by conversation_id for completed conversations with deduplication
                  const allJobsRaw = Object.values(conversationJobs).flat().filter(job => job != null);

                  // Deduplicate by job_id
                  const jobMap = new Map();
                  allJobsRaw.forEach((job: any) => {
                    if (job && job.job_id) {
                      jobMap.set(job.job_id, job);
                    }
                  });
                  const allJobs = Array.from(jobMap.values());

                  // Group ALL jobs by conversation_id (regardless of status)
                  const allConversationJobs = new Map<string, any[]>();

                  // Group jobs by conversation_id only
                  // EXCLUDE session-level jobs (like audio persistence)
                  allJobs.forEach(job => {
                    if (!job) return;

                    // Skip session-level jobs (they run for entire session, not per conversation)
                    // Also skip audio persistence jobs by job_type
                    if (job.meta?.session_level === true || job.job_type === 'audio_streaming_persistence_job') {
                      return;
                    }

                    const conversationId = job.meta?.conversation_id;
                    if (conversationId) {
                      if (!allConversationJobs.has(conversationId)) {
                        allConversationJobs.set(conversationId, []);
                      }
                      allConversationJobs.get(conversationId)!.push(job);
                    }
                  });

                  // Filter to only show conversations where ALL jobs are completed or failed
                  const conversationMap = new Map<string, any[]>();
                  allConversationJobs.forEach((jobs, conversationId) => {
                    const allJobsComplete = jobs.every(j => j.status === 'finished' || j.status === 'failed');
                    if (allJobsComplete) {
                      conversationMap.set(conversationId, jobs);
                    }
                  });

                  if (conversationMap.size === 0) {
                    return <EmptyState>No completed conversations</EmptyState>;
                  }

                  // Convert to array and filter by time range
                  const now = Date.now();
                  const timeRangeMs = completedConvTimeRange * 60 * 60 * 1000; // hours to milliseconds

                  let conversationsArray = Array.from(conversationMap.entries())
                    .map(([conversationId, jobs]) => {
                      // Find the open_conversation_job for created_at
                      const openConvJob = jobs.find(j => j.job_type === 'open_conversation_job');
                      const createdAt = openConvJob?.created_at ? queueDate(openConvJob.created_at).getTime() : 0;
                      return { conversationId, jobs, createdAt };
                    })
                    .filter(({ createdAt }) => {
                      // Filter by time range
                      return createdAt > 0 && (now - createdAt) <= timeRangeMs;
                    })
                    .sort((a, b) => b.createdAt - a.createdAt); // Most recent first

                  // Apply pagination
                  const totalConversations = conversationsArray.length;
                  const totalPages = Math.ceil(totalConversations / completedConvItemsPerPage);
                  const startIndex = (completedConvPage - 1) * completedConvItemsPerPage;
                  const endIndex = startIndex + completedConvItemsPerPage;
                  const paginatedConversations = conversationsArray.slice(startIndex, endIndex);

                  if (conversationsArray.length === 0) {
                    return <EmptyState>No completed conversations in the selected time range</EmptyState>;
                  }

                  return (
                    <>
                      <div className="space-y-2">
                        {paginatedConversations.map(({ conversationId, jobs }) => {
                        const isExpanded = expandedConversations.has(conversationId);

                        // Find the open_conversation_job for metadata, or fallback to any job with metadata
                        const openConvJob = jobs.find(j => j.job_type === 'open_conversation_job');
                        const fallbackJob = jobs.find(j => j.meta && Object.keys(j.meta).length > 0);
                        const meta = openConvJob?.meta || fallbackJob?.meta || {};

                        // Find transcription job for title/summary
                        const transcriptionJob = jobs.find(j => j.job_type === 'transcribe_full_audio_job');
                        const transcriptionMeta = transcriptionJob?.meta || {};

                        // Extract conversation info from metadata
                        const clientId = meta.client_id || 'Unknown';
                        const transcript = meta.transcript || '';
                        const speakers = meta.speakers || [];
                        const wordCount = meta.word_count || 0;
                        const createdAt = openConvJob?.created_at || null;
                        const title = transcriptionMeta.title || null;
                        const summary = transcriptionMeta.summary || null;

                        // Check job statuses
                        const allComplete = jobs.every(j => j.status === 'finished');
                        const hasFailedJob = jobs.some(j => j.status === 'failed');
                        const failedJobCount = jobs.filter(j => j.status === 'failed').length;

                        // Determine status styling
                        let bgColor = 'bg-yellow-50 border-yellow-200 dark:bg-yellow-900/20 dark:border-yellow-800';
                        let hoverColor = 'hover:bg-yellow-100 dark:hover:bg-yellow-900/30';
                        let iconColor = 'text-yellow-600 dark:text-yellow-400';
                        let statusTone: StateTone = 'warning';
                        let statusText = 'Processing';
                        let StatusIcon = Clock;

                        if (hasFailedJob) {
                          bgColor = 'bg-red-50 border-red-300 dark:bg-red-900/20 dark:border-red-800';
                          hoverColor = 'hover:bg-red-100 dark:hover:bg-red-900/30';
                          iconColor = 'text-red-600 dark:text-red-400';
                          statusTone = 'danger';
                          statusText = `${failedJobCount} Error${failedJobCount > 1 ? 's' : ''}`;
                          StatusIcon = AlertTriangle;
                        } else if (allComplete) {
                          bgColor = 'bg-green-50 border-green-200 dark:bg-green-900/20 dark:border-green-800';
                          hoverColor = 'hover:bg-green-100 dark:hover:bg-green-900/30';
                          iconColor = 'text-green-600 dark:text-green-400';
                          statusTone = 'success';
                          statusText = 'Complete';
                          StatusIcon = CheckCircle;
                        }

                        return (
                          <div key={conversationId} className={`rounded-lg border overflow-hidden ${bgColor}`}>
                            <div
                              className={`flex items-center justify-between p-3 cursor-pointer transition-colors ${hoverColor}`}
                              onClick={() => toggleConversationExpansion(conversationId)}
                            >
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center space-x-2">
                                  {isExpanded ? (
                                    <ChevronDown className={`w-4 h-4 ${iconColor}`} />
                                  ) : (
                                    <ChevronRight className={`w-4 h-4 ${iconColor}`} />
                                  )}
                                  <StatusIcon className={`w-4 h-4 ${iconColor}`} />
                                  <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{clientId}</span>
                                  <StateBadge tone={statusTone}>{statusText}</StateBadge>
                                  {speakers.length > 0 && (
                                    <MetadataChip>
                                      {speakers.length} speaker{speakers.length > 1 ? 's' : ''}
                                    </MetadataChip>
                                  )}
                                </div>
                                <div className="mt-1 text-xs text-gray-600 dark:text-gray-400">
                                  Conversation: {conversationId.substring(0, 8)}... •
                                  Words: {wordCount}
                                  {createdAt && (
                                    <> • Created: {queueDate(createdAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}</>
                                  )}
                                </div>
                                {/* Show title/summary for completed, or transcript for in-progress or when no title exists */}
                                {allComplete ? (
                                  <>
                                    {title ? (
                                      <div className="mt-1 text-sm font-medium text-gray-900 dark:text-gray-100">
                                        {title}
                                      </div>
                                    ) : transcript ? (
                                      <div className="mt-1 text-xs text-gray-700 dark:text-gray-300 italic truncate">
                                        "{transcript.substring(0, 100)}{transcript.length > 100 ? '...' : ''}"
                                      </div>
                                    ) : null}
                                    {summary && (
                                      <div className="mt-1 text-xs text-gray-700 dark:text-gray-300 italic">
                                        {summary}
                                      </div>
                                    )}
                                  </>
                                ) : (
                                  transcript && (
                                    <div className="mt-1 text-xs text-gray-700 dark:text-gray-300 italic truncate">
                                      "{transcript.substring(0, 100)}{transcript.length > 100 ? '...' : ''}"
                                    </div>
                                  )
                                )}
                              </div>
                            </div>

                            {/* Expanded Jobs Section */}
                            {isExpanded && (
                              <div className={`border-t bg-white dark:bg-gray-800 p-3 ${
                                allComplete ? 'border-green-200 dark:border-green-800' : 'border-yellow-200 dark:border-yellow-800'
                              }`}>
                                {/* Pipeline Timeline */}
                                <div className="mb-4">
                                  <h5 className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-3">Pipeline Timeline:</h5>
                                  {(() => {
                                    // Helper function to get display name from job type
                                    const getJobDisplayName = (jobType: string) => {
                                      const nameMap: { [key: string]: string } = {
                                        'stream_speech_detection_job': 'Speech',
                                        'open_conversation_job': 'Open',
                                        'transcribe_full_audio_job': 'Transcript',
                                        'recognise_speakers_job': 'Speakers',
                                        'process_memory_job': 'Memory'
                                      };
                                      return nameMap[jobType] || jobType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                                    };

                                    // Helper function to get icon for job type
                                    const getJobIcon = (jobType: string) => {
                                      if (jobType.includes('speech') || jobType.includes('detect')) return Brain;
                                      if (jobType.includes('conversation') || jobType.includes('open')) return Brain;
                                      if (jobType.includes('transcribe')) return FileText;
                                      if (jobType.includes('speaker') || jobType.includes('recognise')) return Brain;
                                      if (jobType.includes('memory')) return Brain;
                                      return Brain; // Default icon
                                    };

                                    // Build dynamic pipeline from actual jobs with timing data
                                    // Sort by start time to show chronological order
                                    const jobsWithTiming = jobs
                                      .filter(j => j && j.started_at)
                                      .map(job => {
                                        const startTime = queueDate(job.started_at!).getTime();
                                        const endTime = job.completed_at || job.ended_at
                                          ? queueDate((job.completed_at || job.ended_at)!).getTime()
                                          : (job.status === 'started' ? Date.now() : startTime);

                                        return {
                                          job,
                                          startTime,
                                          endTime,
                                          duration: Math.max(0, endTime - startTime) / 1000,
                                          name: getJobDisplayName(job.job_type),
                                          icon: getJobIcon(job.job_type)
                                        };
                                      })
                                      .sort((a, b) => a.startTime - b.startTime);

                                    const jobTimes = jobsWithTiming;

                                    // Find earliest start and latest end
                                    const validTimes = jobTimes.filter(t => t !== null);
                                    if (validTimes.length === 0) {
                                      return (
                                        <div className="text-xs text-gray-500 dark:text-gray-400 italic">No job timing data available</div>
                                      );
                                    }

                                    const earliestStart = Math.min(...validTimes.map(t => t!.startTime));
                                    const latestEnd = Math.max(...validTimes.map(t => t!.endTime));
                                    const totalDuration = (latestEnd - earliestStart) / 1000; // in seconds

                                    // Format duration for display
                                    const formatDuration = (seconds: number) => {
                                      if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
                                      if (seconds < 60) return `${seconds.toFixed(1)}s`;
                                      const mins = Math.floor(seconds / 60);
                                      const secs = Math.floor(seconds % 60);
                                      return `${mins}m ${secs}s`;
                                    };

                                    // Generate time axis markers (0%, 25%, 50%, 75%, 100%)
                                    const timeMarkers = [0, 0.25, 0.5, 0.75, 1].map(pct => ({
                                      percent: pct * 100,
                                      time: formatDuration(totalDuration * pct)
                                    }));

                                    return (
                                      <div className="space-y-2">
                                        {/* Time axis */}
                                        <div className="relative h-4 border-b border-gray-300 dark:border-gray-600">
                                          {timeMarkers.map((marker, idx) => (
                                            <div
                                              key={idx}
                                              className="absolute"
                                              style={{ left: `${marker.percent}%`, transform: 'translateX(-50%)' }}
                                            >
                                              <div className="w-px h-2 bg-gray-400"></div>
                                              <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 whitespace-nowrap">
                                                {marker.time}
                                              </div>
                                            </div>
                                          ))}
                                        </div>

                                        {/* Job timeline bars */}
                                        <div className="space-y-2 mt-6">
                                          {jobTimes.map((jobTime) => {
                                            const { job, startTime, endTime, duration, name, icon: Icon } = jobTime;

                                            // Calculate position and width as percentage of total timeline
                                            const startPercent = ((startTime - earliestStart) / (latestEnd - earliestStart)) * 100;
                                            const widthPercent = ((endTime - startTime) / (latestEnd - earliestStart)) * 100;

                                            // Use job type colors
                                            const jobColors = getJobTypeColor(job.job_type, job.status);
                                            const barColor = jobColors.bgColor;
                                            const borderColor = jobColors.borderColor;

                                            return (
                                              <div key={job.job_id} className="flex items-center space-x-2 h-8">
                                                {/* Stage Icon */}
                                                <div className={`w-8 h-8 rounded-full border-2 ${borderColor} ${barColor} flex items-center justify-center flex-shrink-0`}>
                                                  <Icon className="w-4 h-4 text-white" />
                                                </div>

                                                {/* Stage Name */}
                                                <span className="text-xs text-gray-700 dark:text-gray-300 w-20 flex-shrink-0">{name}</span>

                                                {/* Timeline Container */}
                                                <div className="flex-1 relative h-6 bg-gray-100 dark:bg-gray-700 rounded">
                                                  {/* Job Bar */}
                                                  <div
                                                    className={`absolute h-6 rounded ${barColor} ${job.status === 'started' ? 'animate-pulse' : ''} flex items-center justify-center`}
                                                    style={{
                                                      left: `${startPercent}%`,
                                                      width: `${widthPercent}%`
                                                    }}
                                                    title={`Started: ${queueDate(startTime).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}\nDuration: ${formatDuration(duration)}${job.meta?.batch_progress ? `\n${job.meta.batch_progress.message}` : ''}`}
                                                  >
                                                    <span className="text-xs text-white font-medium px-2 truncate">
                                                      {job.status === 'started' && job.meta?.batch_progress
                                                        ? `${job.meta.batch_progress.current}/${job.meta.batch_progress.total}`
                                                        : formatDuration(duration)}
                                                    </span>
                                                  </div>
                                                </div>
                                              </div>
                                            );
                                          })}
                                        </div>

                                        {/* Total Duration */}
                                        <div className="text-xs text-gray-600 dark:text-gray-400 text-right mt-2">
                                          Total: {formatDuration(totalDuration)}
                                        </div>
                                      </div>
                                    );
                                  })()}
                                </div>

                                <h5 className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2">Conversation Jobs:</h5>
                                {jobs.filter(j => j != null && j.job_id).length > 0 ? (
                                  <div className="space-y-1">
                                    {jobs
                                      .filter(j => j != null && j.job_id)
                                      .sort((a, b) => queueDate(a.created_at).getTime() - queueDate(b.created_at).getTime())
                                      .map((job, index) => (
                                      <div key={job.job_id} className={`p-2 bg-gray-50 dark:bg-gray-900/40 rounded border ${getJobTypeColor(job.job_type, job.status).borderColor}`} style={{ borderLeftWidth: '12px' }}>
                                        <div className="flex items-center justify-between">
                                          <div
                                            className="flex-1 flex items-center space-x-2 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors rounded px-1 py-0.5"
                                            onClick={() => toggleJobExpansion(job.job_id)}
                                          >
                                            <span className="text-xs font-mono text-gray-500 dark:text-gray-400 flex-shrink-0">#{index + 1}</span>
                                            <span className="flex-shrink-0">{getJobTypeIcon(job.job_type)}</span>
                                            <span className="flex-shrink-0">{getStatusIcon(job.status)}</span>
                                            <span className="text-xs font-medium text-gray-900 dark:text-gray-100 truncate">{job.job_type}</span>
                                            <StateBadge tone={getStatusTone(job.status)}>{job.status}</StateBadge>
                                            <span className="text-xs text-gray-500 dark:text-gray-400">{job.queue || job.data?.queue || 'unknown'}</span>
                                            {/* Show memory count badge on collapsed card */}
                                            {!expandedJobs.has(job.job_id) && job.job_type === 'process_memory_job' && job.result?.memories_created !== undefined && (
                                              <MetadataChip>{job.result.memories_created} memories</MetadataChip>
                                            )}
                                          </div>
                                          <IconButton
                                            label="View job details"
                                            className="ml-2 flex-shrink-0"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              viewJobDetails(job.job_id);
                                            }}
                                          >
                                            <Eye className="w-3 h-3" />
                                          </IconButton>
                                        </div>

                                        {job.meta?.progress && <ReconciliationProgress progress={job.meta.progress} status={job.status} compact />}
                                      {/* Collapsible metadata section */}
                                        {expandedJobs.has(job.job_id) && (
                                          <div className="mt-1 text-xs text-gray-600 dark:text-gray-400 space-y-0.5 pl-4">
                                            <div>
                                              {job.started_at && (
                                                <span>Started: {queueDate(job.started_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}</span>
                                              )}
                                              {job.started_at && (
                                                <span> • Duration: {formatDuration(job)}</span>
                                              )}
                                            </div>

                                            {/* Show job-specific metadata */}
                                            {job.meta && (
                                              <div className="space-y-0.5 pl-2 border-l-2 border-gray-300 dark:border-gray-600">
                                                {/* open_conversation_job metadata */}
                                                {job.job_type === 'open_conversation_job' && (
                                                  <>
                                                    {job.meta.word_count !== undefined && (
                                                      <div>Words: <span className="font-medium">{job.meta.word_count}</span></div>
                                                    )}
                                                    {job.meta.speakers && job.meta.speakers.length > 0 && (
                                                      <div>Speakers: <span className="font-medium">{job.meta.speakers.join(', ')}</span></div>
                                                    )}
                                                    {job.meta.inactivity_seconds !== undefined && (
                                                      <div>Idle: <span className="font-medium">{Math.floor(job.meta.inactivity_seconds)}s</span></div>
                                                    )}
                                                    {job.meta.transcript && (
                                                      <div className="italic text-gray-500 dark:text-gray-400 truncate max-w-md">
                                                        "{job.meta.transcript.substring(0, 80)}..."
                                                      </div>
                                                    )}
                                                  </>
                                                )}

                                                {/* transcribe_full_audio_job metadata */}
                                                {job.job_type === 'transcribe_full_audio_job' && job.result && (
                                                  <>
                                                    {job.result.transcript && (
                                                      <div>Transcript: <span className="font-medium">{job.result.transcript.length} chars</span></div>
                                                    )}
                                                    {job.result.processing_time_seconds && (
                                                      <div>Processing: <span className="font-medium">{job.result.processing_time_seconds.toFixed(1)}s</span></div>
                                                    )}
                                                  </>
                                                )}

                                                {/* recognise_speakers_job metadata */}
                                                {job.job_type === 'recognise_speakers_job' && job.result && (
                                                  <>
                                                    {job.result.identified_speakers && job.result.identified_speakers.length > 0 && (
                                                      <div>Identified: <span className="font-medium">{job.result.identified_speakers.join(', ')}</span></div>
                                                    )}
                                                    {job.result.segment_count && (
                                                      <div>Segments: <span className="font-medium">{job.result.segment_count}</span></div>
                                                    )}
                                                  </>
                                                )}

                                                {/* process_memory_job metadata */}
                                                {job.job_type === 'process_memory_job' && job.result && (
                                                  <>
                                                    {job.result.memories_created !== undefined && (
                                                      <div>Memories: <span className="font-medium">{job.result.memories_created} created</span></div>
                                                    )}
                                                    {job.result.processing_time_seconds && (
                                                      <div>Processing: <span className="font-medium">{job.result.processing_time_seconds.toFixed(1)}s</span></div>
                                                    )}
                                                  </>
                                                )}

                                                {/* Show conversation_id if present */}
                                                {job.meta.conversation_id && (
                                                  <div className="font-mono text-gray-500 dark:text-gray-400">
                                                    Conv: {job.meta.conversation_id.substring(0, 8)}...
                                                  </div>
                                                )}
                                              </div>
                                            )}
                                          </div>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                ) : (
                                  <div className="text-xs text-gray-500 dark:text-gray-400 italic">No jobs found for this conversation</div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>

                      {/* Pagination Controls */}
                      {totalPages > 1 && (
                        <div className="flex items-center justify-between mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
                          <div className="text-xs text-gray-600 dark:text-gray-400">
                            Showing {startIndex + 1}-{Math.min(endIndex, totalConversations)} of {totalConversations} conversations
                          </div>
                          <div className="flex items-center space-x-2">
                            <Button
                              onClick={() => setCompletedConvPage(Math.max(1, completedConvPage - 1))}
                              disabled={completedConvPage === 1}
                            >
                              Previous
                            </Button>
                            <span className="text-xs text-gray-600 dark:text-gray-400">
                              Page {completedConvPage} of {totalPages}
                            </span>
                            <Button
                              onClick={() => setCompletedConvPage(Math.min(totalPages, completedConvPage + 1))}
                              disabled={completedConvPage === totalPages}
                            >
                              Next
                            </Button>
                          </div>
                        </div>
                      )}
                    </>
                  );
                })()}
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* Events */}
      <Card raised padded={false} className="overflow-hidden">
        <div
          className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center cursor-pointer"
          onClick={() => {
            const next = !eventsExpanded;
            setEventsExpanded(next);
            localStorage.setItem('queue_events_expanded', String(next));
          }}
        >
          <div className="flex items-center space-x-2">
            <Zap className="w-5 h-5 text-purple-600 dark:text-purple-400" />
            <SectionTitle>Events</SectionTitle>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {(() => {
                const includes = Object.entries(eventFilters).filter(([, v]) => v === 'include').map(([k]) => k);
                const excludes = Object.entries(eventFilters).filter(([, v]) => v === 'exclude').map(([k]) => k);
                const hasFilters = includes.length > 0 || excludes.length > 0;
                if (!hasFilters) return `(${events.length})`;
                const count = includes.length > 0
                  ? events.filter(e => includes.includes(e.event) && !excludes.includes(e.event)).length
                  : events.filter(e => !excludes.includes(e.event)).length;
                return `(${count} / ${events.length})`;
              })()}
            </span>
          </div>
          <div className="flex items-center space-x-3">
            {eventsExpanded && events.length > 0 && (
              <Button
                variant="ghost"
                icon={<Trash2 className="w-3.5 h-3.5" />}
                title="Clear all events"
                className="text-red-600 hover:bg-red-50 hover:text-red-700 dark:text-red-400 dark:hover:bg-red-900/30 dark:hover:text-red-300"
                onClick={(e) => {
                  e.stopPropagation();
                  queueApi.clearEvents().then(() => {
                    setEventFilters({});
                    invalidateQueue();
                  });
                }}
              >
                Clear
              </Button>
            )}
            {eventsExpanded
              ? <ChevronDown className="w-4 h-4 text-gray-500 dark:text-gray-400" />
              : <ChevronRight className="w-4 h-4 text-gray-500 dark:text-gray-400" />}
          </div>
        </div>

        {eventsExpanded && [...new Set(events.map(e => e.event))].sort().length > 0 && (
          <div className="px-6 py-2 border-b border-gray-100 dark:border-gray-700 flex flex-wrap items-center gap-2">
            {[...new Set(events.map(e => e.event))].sort().map(eventType => {
              const state = eventFilters[eventType];
              return (
                <button
                  key={eventType}
                  onClick={() => cycleEventFilter(eventType)}
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border cursor-pointer transition-colors ${
                    state === 'include'
                      ? 'bg-blue-100 text-blue-700 border-blue-400 dark:bg-blue-900/40 dark:text-blue-300 dark:border-blue-600'
                      : state === 'exclude'
                      ? 'bg-red-100 text-red-700 border-red-400 line-through dark:bg-red-900/40 dark:text-red-300 dark:border-red-600'
                      : 'bg-gray-100 text-gray-500 border-gray-300 dark:bg-gray-700/60 dark:text-gray-400 dark:border-gray-600'
                  }`}
                >
                  {eventType}
                </button>
              );
            })}
            {Object.keys(eventFilters).length > 0 && (
              <button
                onClick={() => setEventFilters({})}
                className="inline-flex items-center px-2 py-0.5 rounded text-xs text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300 transition-colors"
              >
                Clear
              </button>
            )}
          </div>
        )}

        {eventsExpanded && (
          <div className="overflow-x-auto">
            {(() => {
              const includes = Object.entries(eventFilters).filter(([, v]) => v === 'include').map(([k]) => k);
              const excludes = Object.entries(eventFilters).filter(([, v]) => v === 'exclude').map(([k]) => k);
              let filtered = events;
              if (includes.length > 0) {
                filtered = filtered.filter(e => includes.includes(e.event));
              }
              filtered = filtered.filter(e => !excludes.includes(e.event));

              if (filtered.length === 0) {
                return (
                  <div className="px-6 py-8 text-center text-gray-500 dark:text-gray-400 text-sm">
                    No events recorded yet. Events are logged when system actions like conversation.complete, memory.processed, or button presses occur.
                  </div>
                );
              }

              return (
                <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead className="bg-gray-50 dark:bg-gray-700">
                    <tr>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">Time</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">Event</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">User</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">Plugins Triggered</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                    {filtered.map((evt, idx) => {
                      const pluginsExecuted = evt.plugins_executed || [];
                      // A plugin can intentionally no-op (e.g. wake word armed on a
                      // silent capture). Those carry data.skipped and should read as
                      // "Skipped", not a failure.
                      const ranPlugins = pluginsExecuted.filter(p => !p.data?.skipped);
                      const allSuccess = ranPlugins.length > 0 && ranPlugins.every(p => p.success);
                      const anyFailure = ranPlugins.some(p => !p.success);
                      const allSkipped = pluginsExecuted.length > 0 && ranPlugins.length === 0;

                      return (
                        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                          <td className="px-4 py-2 text-xs text-gray-600 dark:text-gray-400 whitespace-nowrap">
                            {queueDate(evt.timestamp * 1000).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}
                          </td>
                          <td className="px-4 py-2">
                            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${getEventColor(evt.event)}`}>
                              {evt.event}
                            </span>
                          </td>
                          <td className="px-4 py-2 text-xs text-gray-600 dark:text-gray-400 font-mono">
                            {(evt.user_id || '').length > 12 ? `${evt.user_id.slice(-8)}` : evt.user_id}
                          </td>
                          <td className="px-4 py-2 text-xs text-gray-700 dark:text-gray-300">
                            {pluginsExecuted.length > 0
                              ? pluginsExecuted.map(p => p.plugin_id).join(', ')
                              : <span className="text-gray-400 dark:text-gray-500">none</span>
                            }
                          </td>
                          <td className="px-4 py-2">
                            <div className="flex items-center space-x-2">
                              {pluginsExecuted.length === 0 ? (
                                <span className="text-xs text-gray-400 dark:text-gray-500">no plugins ran</span>
                              ) : allSkipped ? (
                                <span className="text-xs text-gray-500 dark:text-gray-400">Skipped</span>
                              ) : allSuccess ? (
                                <span className="flex items-center space-x-1 text-xs text-green-600 dark:text-green-400">
                                  <CheckCircle className="w-3.5 h-3.5" />
                                  <span>OK</span>
                                </span>
                              ) : anyFailure ? (
                                <span className="flex items-center space-x-1 text-xs text-red-600 dark:text-red-400">
                                  <XCircle className="w-3.5 h-3.5" />
                                  <span>Error</span>
                                </span>
                              ) : (
                                <span className="text-xs text-gray-500 dark:text-gray-400">partial</span>
                              )}
                              {pluginsExecuted.length > 0 && (
                                <IconButton label="View event details" onClick={() => setSelectedEvent(evt)}>
                                  <Eye className="w-3.5 h-3.5" />
                                </IconButton>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              );
            })()}
          </div>
        )}
      </Card>

      {/* Filters */}
      <Card raised>
        <div className="mb-4">
          <SectionTitle>Filters</SectionTitle>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <Label htmlFor="filter-status" className="mb-1">Status</Label>
            <Select
              id="filter-status"
              value={filters.status}
              onChange={(e) => setFilters({ ...filters, status: e.target.value })}
            >
              <option value="">All Statuses</option>
              <option value="queued">Queued</option>
              <option value="started">Started</option>
              <option value="finished">Finished</option>
              <option value="failed">Failed</option>
              <option value="canceled">Canceled</option>
              <option value="deferred">Deferred</option>
            </Select>
          </div>

          <div>
            <Label htmlFor="filter-job-type" className="mb-1">Job Type</Label>
            <Select
              id="filter-job-type"
              value={filters.job_type}
              onChange={(e) => setFilters({ ...filters, job_type: e.target.value })}
            >
              <option value="">All Types</option>
              {jobTypeOptions.map(jobType => (
                <option key={jobType} value={jobType}>{getJobTypeShort(jobType)}</option>
              ))}
            </Select>
          </div>

          <div className="flex items-end space-x-2">
            <Button variant="primary" size="md" icon={<Filter className="w-4 h-4" />} onClick={applyFilters}>
              Apply
            </Button>
            <Button size="md" icon={<X className="w-4 h-4" />} onClick={clearFilters}>
              Clear
            </Button>
          </div>
        </div>
      </Card>

          {/* Jobs Table */}
      <Card raised padded={false} className="overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
          <SectionTitle>Jobs</SectionTitle>
          {jobs.length > 0 && (
            <Button
              variant="ghost"
              icon={<Trash2 className="w-3.5 h-3.5" />}
              title="Clear finished and failed jobs"
              className="text-red-600 hover:bg-red-50 hover:text-red-700 dark:text-red-400 dark:hover:bg-red-900/30 dark:hover:text-red-300"
              onClick={() => {
                queueApi.clearJobs().then(() => {
                  invalidateQueue();
                });
              }}
            >
              Clear
            </Button>
          )}
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-700">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Date</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Conversation ID</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Job ID</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Duration</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase whitespace-nowrap">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {jobs
                .filter((job) => {
                  if (filters.status && job.status !== filters.status) return false;
                  if (filters.job_type && job.job_type !== filters.job_type) return false;
                  return true;
                })
                .sort((a, b) => queueDate(b.created_at).getTime() - queueDate(a.created_at).getTime()).map((job) => (
                <tr key={job.job_id} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                  <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400 whitespace-nowrap">
                    {queueDate(job.created_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                  </td>
                  <td className="px-4 py-3 max-w-xs">
                    <div className="text-xs font-mono text-gray-600 dark:text-gray-400 truncate" title={job.meta?.conversation_id || 'N/A'}>
                      {job.meta?.conversation_id ? job.meta.conversation_id.substring(0, 8) : '—'}
                    </div>
                  </td>
                  <td className="px-4 py-3 max-w-[14rem]">
                    <div className="text-xs font-mono text-gray-900 dark:text-gray-100 truncate" title={job.job_id}>
                      {job.job_id}
                    </div>
                    {job.meta?.progress && <p className="mt-1 text-xs text-gray-500">{job.meta.progress.message}</p>}
                  </td>
                  <td className="px-4 py-3 max-w-[12rem]">
                    <div className="text-sm text-gray-900 dark:text-gray-100 truncate" title={job.job_type}>
                      {getJobTypeShort(job.job_type)}
                    </div>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <StateBadge tone={getStatusTone(job.status)}>
                      {getStatusIcon(job.status)}
                      <span className="ml-1">{job.status.charAt(0).toUpperCase() + job.status.slice(1)}</span>
                    </StateBadge>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <div className="text-sm text-gray-700 dark:text-gray-300 font-mono">
                      {formatDuration(job)}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-sm font-medium whitespace-nowrap">
                    <div className="flex items-center gap-1">
                      {job.status === 'failed' && (
                        <IconButton label="Retry job" onClick={() => retryJob(job.job_id)}>
                          <RotateCcw className="w-4 h-4" />
                        </IconButton>
                      )}
                      <IconButton
                        label="View details"
                        disabled={loadingJobDetails}
                        onClick={() => viewJobDetails(job.job_id)}
                      >
                        <Eye className="w-4 h-4" />
                      </IconButton>
                      {(job.status === 'queued' || job.status === 'started') && (
                        <IconButton danger label="Cancel job" onClick={() => cancelJob(job.job_id)}>
                          <StopCircle className="w-4 h-4" />
                        </IconButton>
                      )}
                      {job.status === 'finished' && (
                        <IconButton danger label="Delete job" onClick={() => cancelJob(job.job_id)}>
                          <Trash2 className="w-4 h-4" />
                        </IconButton>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {pagination.total > pagination.limit && (
          <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
            <div className="text-sm text-gray-700 dark:text-gray-300">
              Showing {pagination.offset + 1} to {Math.min(pagination.offset + pagination.limit, pagination.total)} of {pagination.total} results
            </div>
            <div className="flex space-x-2">
              <Button size="md" onClick={prevPage} disabled={pagination.offset === 0}>
                Previous
              </Button>
              <Button size="md" onClick={nextPage} disabled={!pagination.has_more}>
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>
      {/* Old Jobs Table and Pagination - Removed in favor of session-based view above */}

      {/* Job Details Modal */}
      {visibleJob && (
        <Modal
          open
          onClose={() => setSelectedJob(null)}
          title="Job Details"
          maxWidthClassName="max-w-6xl"
          className="max-h-[90vh] overflow-y-auto"
          footer={
            <Button variant="secondary" onClick={() => setSelectedJob(null)}>
              Close
            </Button>
          }
        >
            {loadingJobDetails ? (
              <div className="flex items-center justify-center py-12">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
              </div>
            ) : (
              <div className="space-y-4">
                {visibleJob.meta?.progress && <ReconciliationProgress progress={visibleJob.meta.progress} status={visibleJob.status} />}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>Job ID</Label>
                    <p className="text-sm text-gray-900 dark:text-gray-100 font-mono">{visibleJob.job_id}</p>
                  </div>
                  <div>
                    <Label>Status</Label>
                    <StateBadge tone={getStatusTone(visibleJob.status)}>
                      {getStatusIcon(visibleJob.status)}
                      <span className="ml-1">{visibleJob.status.charAt(0).toUpperCase() + visibleJob.status.slice(1)}</span>
                    </StateBadge>
                  </div>
                  {visibleJob.description && (
                    <div className="col-span-2">
                      <Label>Description</Label>
                      <p className="text-sm text-gray-900 dark:text-gray-100">{visibleJob.description}</p>
                    </div>
                  )}
                  {visibleJob.func_name && (
                    <div className="col-span-2">
                      <Label>Function Name</Label>
                      <p className="text-sm text-gray-900 dark:text-gray-100 font-mono">{visibleJob.func_name}</p>
                    </div>
                  )}
                  <div>
                    <Label>Created</Label>
                    <p className="text-sm text-gray-900 dark:text-gray-100">{visibleJob.created_at ? formatDate(visibleJob.created_at) : '-'}</p>
                  </div>
                  <div>
                    <Label>Started</Label>
                    <p className="text-sm text-gray-900 dark:text-gray-100">{visibleJob.started_at ? formatDate(visibleJob.started_at) : '-'}</p>
                  </div>
                  <div>
                    <Label>Ended</Label>
                    <p className="text-sm text-gray-900 dark:text-gray-100">{visibleJob.ended_at ? formatDate(visibleJob.ended_at) : '-'}</p>
                  </div>
                </div>

                {visibleJob.args && visibleJob.args.length > 0 && (
                  <div>
                    <Label>Arguments</Label>
                    <pre className="text-xs text-gray-900 dark:text-gray-100 bg-gray-50 dark:bg-gray-900 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {JSON.stringify(visibleJob.args, null, 2)}
                    </pre>
                  </div>
                )}

                {visibleJob.kwargs && Object.keys(visibleJob.kwargs).length > 0 && (
                  <div>
                    <Label>Keyword Arguments</Label>
                    <pre className="text-xs text-gray-900 dark:text-gray-100 bg-gray-50 dark:bg-gray-900 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {JSON.stringify(visibleJob.kwargs, null, 2)}
                    </pre>
                  </div>
                )}

                {visibleJob.error_message && (
                  <div>
                    <Label>Error</Label>
                    <pre className="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {visibleJob.error_message}
                    </pre>
                  </div>
                )}

                {visibleJob.result && (
                  <div>
                    <Label>Result</Label>
                    <pre className="text-xs text-gray-900 dark:text-gray-100 bg-green-50 dark:bg-green-900/20 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {JSON.stringify(visibleJob.result, null, 2)}
                    </pre>
                  </div>
                )}

                {/* Formatted Job Metadata - Job-specific displays */}
                {visibleJob.meta && Object.keys(visibleJob.meta).length > 0 && (
                  <div>
                    <Label className="mb-2">Job Metadata</Label>

                    {/* open_conversation_job formatted metadata */}
                    {visibleJob.func_name?.includes('open_conversation_job') && (
                      <div className="bg-blue-50 dark:bg-blue-900/20 p-3 rounded mb-3 space-y-2">
                        {visibleJob.meta.word_count !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Word Count:</span> {visibleJob.meta.word_count}
                          </div>
                        )}
                        {visibleJob.meta.speakers && visibleJob.meta.speakers.length > 0 && (
                          <div className="text-sm">
                            <span className="font-medium">Speakers:</span> {visibleJob.meta.speakers.join(', ')}
                          </div>
                        )}
                        {visibleJob.meta.transcript_length !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Transcript Length:</span> {visibleJob.meta.transcript_length} chars
                          </div>
                        )}
                        {visibleJob.meta.duration_seconds !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Duration:</span> {visibleJob.meta.duration_seconds.toFixed(1)}s
                          </div>
                        )}
                        {visibleJob.meta.inactivity_seconds !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Idle Time:</span> {Math.floor(visibleJob.meta.inactivity_seconds)}s
                          </div>
                        )}
                        {visibleJob.meta.chunks_processed !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Chunks Processed:</span> {visibleJob.meta.chunks_processed}
                          </div>
                        )}
                        {visibleJob.meta.transcript && (
                          <div className="mt-2">
                            <div className="text-sm font-medium mb-1">Transcript:</div>
                            <div className="text-sm italic text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 p-2 rounded border border-gray-200 dark:border-gray-700 max-h-32 overflow-y-auto">
                              "{visibleJob.meta.transcript}"
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {/* process_memory_job formatted metadata */}
                    {visibleJob.func_name?.includes('process_memory_job') && visibleJob.meta.memory_details && visibleJob.meta.memory_details.length > 0 && (
                      <div className="bg-pink-50 dark:bg-pink-900/20 p-3 rounded mb-3 space-y-2">
                        <div className="text-sm">
                          <span className="font-medium">Memories Created:</span> {visibleJob.meta.memories_created || visibleJob.meta.memory_details.length}
                        </div>
                        {visibleJob.meta.processing_time !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Processing Time:</span> {visibleJob.meta.processing_time.toFixed(1)}s
                          </div>
                        )}
                        <div className="mt-2">
                          <div className="text-sm font-medium mb-1">Memory Details:</div>
                          <div className="space-y-1">
                            {visibleJob.meta.memory_details.map((mem: any, idx: number) => (
                              <div key={idx} className="text-xs bg-pink-100 dark:bg-pink-900/30 text-gray-800 dark:text-gray-200 p-2 rounded border border-pink-200 dark:border-pink-800">
                                {mem.text}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}

                    {/* stream_speech_detection_job formatted metadata */}
                    {visibleJob.func_name?.includes('stream_speech_detection_job') && (
                      <div className="bg-yellow-50 dark:bg-yellow-900/20 p-3 rounded mb-3 space-y-2">
                        {visibleJob.meta.speech_detected_at && (
                          <div className="text-sm">
                            <span className="font-medium">Speech Detected At:</span> {queueDate(visibleJob.meta.speech_detected_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}
                          </div>
                        )}
                        {visibleJob.meta.detected_speakers && visibleJob.meta.detected_speakers.length > 0 && (
                          <div className="text-sm">
                            <span className="font-medium">Detected Speakers:</span> {visibleJob.meta.detected_speakers.join(', ')}
                          </div>
                        )}
                        {visibleJob.meta.conversation_job_id && (
                          <div className="text-sm">
                            <span className="font-medium">Conversation Job:</span> {visibleJob.meta.conversation_job_id}
                          </div>
                        )}
                      </div>
                    )}

                    {/* transcribe_full_audio_job formatted metadata */}
                    {visibleJob.func_name?.includes('transcribe_full_audio_job') && (visibleJob.meta.title || visibleJob.meta.summary) && (
                      <div className="bg-purple-50 dark:bg-purple-900/20 p-3 rounded mb-3 space-y-2">
                        {visibleJob.meta.title && (
                          <div className="text-sm">
                            <span className="font-medium">Title:</span> {visibleJob.meta.title}
                          </div>
                        )}
                        {visibleJob.meta.summary && (
                          <div className="text-sm">
                            <span className="font-medium">Summary:</span> {visibleJob.meta.summary}
                          </div>
                        )}
                        {visibleJob.meta.transcript_length !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Transcript Length:</span> {visibleJob.meta.transcript_length} chars
                          </div>
                        )}
                        {visibleJob.meta.word_count !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Word Count:</span> {visibleJob.meta.word_count}
                          </div>
                        )}
                        {visibleJob.meta.processing_time !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Processing Time:</span> {visibleJob.meta.processing_time.toFixed(1)}s
                          </div>
                        )}
                      </div>
                    )}

                    {/* Raw JSON metadata (collapsible) */}
                    <details className="mt-2">
                      <summary className="text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer hover:text-gray-900 dark:hover:text-gray-100">
                        Raw Metadata JSON
                      </summary>
                      <pre className="text-xs text-gray-900 dark:text-gray-100 bg-blue-50 dark:bg-blue-900/20 p-2 rounded overflow-auto max-h-64 mt-2 whitespace-pre-wrap break-words">
                        {JSON.stringify(visibleJob.meta, null, 2)}
                      </pre>
                    </details>
                  </div>
                )}
              </div>
            )}
        </Modal>
      )}

      {/* Event Detail Modal */}
      {visibleEvent && (
        <Modal
          open
          onClose={() => setSelectedEvent(null)}
          title="Event Details"
          maxWidthClassName="max-w-3xl"
          className="max-h-[90vh] overflow-y-auto"
          footer={
            <Button variant="secondary" onClick={() => setSelectedEvent(null)}>
              Close
            </Button>
          }
        >
            <div className="space-y-4">
              {visibleEvent.privacy_held && <p role="status">Private or unscreened event details held</p>}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label>Time</Label>
                  <p className="text-sm text-gray-900 dark:text-gray-100">{queueDate(visibleEvent.timestamp * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', timeZoneName: 'short' })}</p>
                </div>
                <div>
                  <Label>Event</Label>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${getEventColor(visibleEvent.event)}`}>
                    {visibleEvent.event}
                  </span>
                </div>
                <div>
                  <Label>User</Label>
                  <p className="text-sm text-gray-900 dark:text-gray-100 font-mono">{visibleEvent.user_id}</p>
                </div>
                {visibleEvent.metadata?.client_id && (
                  <div>
                    <Label>Client</Label>
                    <p className="text-sm text-gray-900 dark:text-gray-100 font-mono">{visibleEvent.metadata.client_id}</p>
                  </div>
                )}
              </div>

              <div>
                <Label className="mb-2">Plugin Results</Label>
                <div className="space-y-2">
                  {(visibleEvent.plugins_executed || []).map((p, i) => {
                    const skipped = !!p.data?.skipped;
                    const tone: { card: string; badge: StateTone; text: string; label: string } = skipped
                      ? { card: 'bg-gray-50 border-gray-200 dark:bg-gray-900/40 dark:border-gray-700', badge: 'neutral', text: 'text-gray-700 dark:text-gray-300', label: 'Skipped' }
                      : p.success
                        ? { card: 'bg-green-50 border-green-200 dark:bg-green-900/20 dark:border-green-800', badge: 'success', text: 'text-green-800 dark:text-green-300', label: 'OK' }
                        : { card: 'bg-red-50 border-red-200 dark:bg-red-900/20 dark:border-red-800', badge: 'danger', text: 'text-red-800 dark:text-red-300', label: 'Error' };
                    // Show the plugin's structured output minus the skip flags we
                    // already render via the badge/detail.
                    const { skipped: _s, skip_reason: _r, detail, ...restData } = p.data || {};
                    return (
                      <div key={i} className={`p-3 rounded-lg border ${tone.card}`}>
                        <div className="flex items-center space-x-2 mb-1">
                          {skipped
                            ? <MinusCircle className="w-4 h-4 text-gray-500 dark:text-gray-400" />
                            : p.success
                              ? <CheckCircle className="w-4 h-4 text-green-600 dark:text-green-400" />
                              : <XCircle className="w-4 h-4 text-red-600 dark:text-red-400" />
                          }
                          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{p.plugin_id}</span>
                          <StateBadge tone={tone.badge}>{tone.label}</StateBadge>
                        </div>
                        {(p.message || detail) && (
                          <p className={`text-sm ml-6 ${tone.text}`}>
                            {p.message || detail}
                          </p>
                        )}
                        {Object.keys(restData).length > 0 && (
                          <pre className="text-xs text-gray-700 dark:text-gray-300 bg-white/60 dark:bg-gray-900/50 border border-gray-200 dark:border-gray-700 rounded p-2 mt-2 ml-6 overflow-auto max-h-40 whitespace-pre-wrap break-words">
                            {JSON.stringify(restData, null, 2)}
                          </pre>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>

              {visibleEvent.metadata && Object.keys(visibleEvent.metadata).length > 0 && (
                <details>
                  <summary className="text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer hover:text-gray-900 dark:hover:text-gray-100">
                    Raw Metadata
                  </summary>
                  <pre className="text-xs text-gray-900 dark:text-gray-100 bg-gray-50 dark:bg-gray-900 p-2 rounded overflow-auto max-h-40 mt-2 whitespace-pre-wrap break-words">
                    {JSON.stringify(visibleEvent.metadata, null, 2)}
                  </pre>
                </details>
              )}
            </div>
        </Modal>
      )}

      {/* Flush Jobs Modal */}
      {showFlushModal && (
        <Modal
          open
          onClose={() => { setShowFlushModal(false); setFlushPreview(null); }}
          title="Flush Jobs"
          icon={<Trash2 className="w-5 h-5" />}
          maxWidthClassName="max-w-lg"
          className="max-h-[90vh] overflow-y-auto"
          footer={
            <>
              <Button
                variant="secondary"
                size="md"
                className="flex-1"
                onClick={() => { setShowFlushModal(false); setFlushPreview(null); }}
              >
                Cancel
              </Button>
              <Button
                variant="secondary"
                size="md"
                className="flex-1"
                onClick={previewFlush}
                disabled={previewing || (!flushSettings.flush_all && flushSettings.statuses.length === 0)}
                icon={previewing ? <RotateCcw className="w-4 h-4 animate-spin" /> : <Eye className="w-4 h-4" />}
              >
                {previewing ? 'Previewing...' : 'Preview'}
              </Button>
              <Button
                variant={flushSettings.flush_all ? 'danger' : 'primary'}
                size="md"
                className="flex-1"
                onClick={flushJobs}
                disabled={flushing || (!flushSettings.flush_all && flushSettings.statuses.length === 0)}
                icon={flushing ? <RotateCcw className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
              >
                {flushing ? 'Flushing...' : flushSettings.flush_all ? 'Flush ALL Jobs' : 'Flush Selected Jobs'}
              </Button>
            </>
          }
        >
            <div className="space-y-4">
              <Alert tone="warning" icon={<AlertTriangle className="w-5 h-5 flex-shrink-0" />}>
                This will permanently remove jobs from the database
              </Alert>

              <div className="space-y-3">
                <div>
                  <label className="flex items-center space-x-2">
                    <input
                      type="radio"
                      name="flushType"
                      checked={!flushSettings.flush_all}
                      onChange={() => setFlushSettings(prev => ({ ...prev, flush_all: false }))}
                      className="text-blue-600"
                    />
                    <span className="text-sm font-medium text-gray-900 dark:text-gray-100">Flush old inactive jobs (recommended)</span>
                  </label>

                  {!flushSettings.flush_all && (
                    <div className="ml-6 mt-2 space-y-2">
                      <div>
                        <Label htmlFor="flush-older-than" className="mb-1 text-xs font-normal text-gray-600 dark:text-gray-400">Remove jobs older than:</Label>
                        <Select
                          id="flush-older-than"
                          value={flushSettings.older_than_hours}
                          onChange={(e) => setFlushSettings(prev => ({ ...prev, older_than_hours: parseInt(e.target.value) }))}
                        >
                          <option value={1}>1 hour</option>
                          <option value={6}>6 hours</option>
                          <option value={12}>12 hours</option>
                          <option value={24}>24 hours</option>
                          <option value={72}>3 days</option>
                          <option value={168}>1 week</option>
                        </Select>
                      </div>

                      <div>
                        <span className="mb-1 block text-xs text-gray-600 dark:text-gray-400">Job statuses to remove:</span>
                        {/* Checkbox renders an inline-flex label, so the group needs
                            an explicit flex column — space-y-* alone does not separate them. */}
                        <div className="flex flex-col items-start gap-1">
                          {['finished', 'failed', 'canceled'].map(status => (
                            <Checkbox
                              key={status}
                              checked={flushSettings.statuses.includes(status)}
                              onChange={(e) => {
                                if (e.target.checked) {
                                  setFlushSettings(prev => ({
                                    ...prev,
                                    statuses: [...prev.statuses, status]
                                  }));
                                } else {
                                  setFlushSettings(prev => ({
                                    ...prev,
                                    statuses: prev.statuses.filter(s => s !== status)
                                  }));
                                }
                              }}
                              label={<span className="text-xs capitalize">{status}</span>}
                            />
                          ))}
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                <div>
                  <label className="flex items-center space-x-2">
                    <input
                      type="radio"
                      name="flushType"
                      checked={flushSettings.flush_all}
                      onChange={() => setFlushSettings(prev => ({ ...prev, flush_all: true }))}
                      className="text-red-600"
                    />
                    <span className="text-sm font-medium text-red-600 dark:text-red-400">Flush ALL jobs (DANGER!)</span>
                  </label>

                  {flushSettings.flush_all && (
                    <div className="ml-6 mt-2 space-y-2">
                      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded p-2">
                        <p className="text-xs text-red-800 dark:text-red-300">
                          ⚠️ This will flush queued, started, deferred, scheduled, and canceled jobs.
                          {!flushSettings.include_failed && !flushSettings.include_finished &&
                            " Failed and finished jobs preserved for debugging."}
                        </p>
                      </div>

                      <div className="flex flex-col items-start gap-1">
                        <Checkbox
                          checked={flushSettings.include_failed}
                          onChange={(e) => setFlushSettings(prev => ({ ...prev, include_failed: e.target.checked }))}
                          label={<span className="text-xs">Also flush failed jobs</span>}
                        />

                        <Checkbox
                          checked={flushSettings.include_finished}
                          onChange={(e) => setFlushSettings(prev => ({ ...prev, include_finished: e.target.checked }))}
                          label={<span className="text-xs">Also flush finished jobs</span>}
                        />
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* Preview (dry run) of exactly what this flush would remove */}
              {flushPreview && (
                <div className="mt-2 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                  <div className="px-3 py-2 bg-gray-50 dark:bg-gray-900/40 border-b border-gray-200 dark:border-gray-700 text-xs font-medium text-gray-700 dark:text-gray-300 flex justify-between items-center">
                    <span>
                      {flushPreview.total_matched} job{flushPreview.total_matched === 1 ? '' : 's'} will be removed
                      {typeof flushPreview.redis_keys_matched === 'number' &&
                        ` + ${flushPreview.redis_keys_matched} Redis key${flushPreview.redis_keys_matched === 1 ? '' : 's'}`}
                    </span>
                    {!!flushPreview.skipped_session_level && (
                      <span className="text-gray-500 dark:text-gray-400">{flushPreview.skipped_session_level} session-level skipped</span>
                    )}
                  </div>
                  {flushPreview.jobs.length === 0 ? (
                    <div className="px-3 py-4 text-center text-xs text-gray-500 dark:text-gray-400">Nothing matches these settings.</div>
                  ) : (
                    <div className="max-h-48 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700">
                      {flushPreview.jobs.map((job: any) => (
                        <div key={job.job_id} className="px-3 py-1.5 text-xs flex items-center justify-between">
                          <div className="min-w-0 truncate">
                            <span className="font-medium text-gray-800 dark:text-gray-200">{job.job_type}</span>
                            <span className="text-gray-400 dark:text-gray-500 ml-2 font-mono">{job.job_id?.substring(0, 8)}</span>
                            {job.client_id && <span className="text-gray-500 dark:text-gray-400 ml-2">{job.client_id}</span>}
                          </div>
                          <div className="flex items-center space-x-2 flex-shrink-0 ml-2">
                            <StateBadge tone={getStatusTone(job.status)}>{job.status}</StateBadge>
                            {typeof job.age_hours === 'number' && <span className="text-gray-500 dark:text-gray-400">{job.age_hours}h</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
        </Modal>
      )}
    </div>
  );
};

export default Queue;
