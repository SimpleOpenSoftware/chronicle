import { CheckCircle2, GitMerge, Loader2, PackageOpen, UserCheck, VolumeX } from 'lucide-react'
import { Button } from '../ui'

interface Props {
  total: number
  selectedCount: number
  mergeEligible: boolean
  // Conversations still lacking cached VAD analysis; null = not loaded yet.
  unanalyzedCount: number | null
  analyzing: boolean
  // Pending speaker-triage decisions and how many conversations they span.
  triagePendingCount: number
  triageConversationCount: number
  applyingTriage: boolean
  onApplyTriage: () => void
  onAnalyze: () => void
  onMerge: () => void
  onExport: () => void
}

export default function AuditToolbar({
  total,
  selectedCount,
  mergeEligible,
  unanalyzedCount,
  analyzing,
  triagePendingCount,
  triageConversationCount,
  applyingTriage,
  onApplyTriage,
  onAnalyze,
  onMerge,
  onExport,
}: Props) {
  const nothingToAnalyze = unanalyzedCount === 0 && !analyzing
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="text-sm text-gray-500 dark:text-gray-400">
        {total} match{total === 1 ? '' : 'es'} · {selectedCount} selected
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {triagePendingCount > 0 && (
          <Button
            variant="primary"
            size="md"
            onClick={onApplyTriage}
            disabled={applyingTriage}
            title="Apply speaker corrections and queue memory processing"
            icon={
              applyingTriage ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <UserCheck className="h-4 w-4" />
              )
            }
          >
            {applyingTriage
              ? 'Applying…'
              : `Apply triage (${triagePendingCount} across ${triageConversationCount})`}
          </Button>
        )}
        <Button
          variant="secondary"
          size="md"
          onClick={onAnalyze}
          disabled={analyzing || nothingToAnalyze}
          title={
            nothingToAnalyze
              ? 'All conversations already have cached VAD analysis'
              : 'Run VAD over conversations without cached analysis'
          }
          icon={
            analyzing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : nothingToAnalyze ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <VolumeX className="h-4 w-4" />
            )
          }
        >
          {analyzing
            ? 'Analyzing…'
            : nothingToAnalyze
              ? 'Audio analyzed'
              : unanalyzedCount != null
                ? `Analyze audio (${unanalyzedCount})`
                : 'Analyze audio'}
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onExport}
          title="Export speech-cropped clips + transcripts for annotation"
          icon={<PackageOpen className="h-4 w-4" />}
        >
          Export…
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onMerge}
          disabled={!mergeEligible}
          title={
            mergeEligible
              ? 'Merge the selected adjacent conversations'
              : 'Select 2+ conversations from the same device to merge'
          }
          icon={<GitMerge className="h-4 w-4" />}
        >
          Merge selected
        </Button>
        <span className="text-xs text-gray-500 dark:text-gray-400" title="Raw capture audio cannot be archived until a capture-retention policy is configured.">
          Audio archival unavailable
        </span>
      </div>
    </div>
  )
}
