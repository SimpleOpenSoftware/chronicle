import { sourceDate } from "../utils/sourceTime";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  timelineApi,
  MemoryReviewProposal,
  MemorySession,
  SessionSource,
} from "../services/api";
import { Button } from "./ui";
import { CandidateChanges } from "./timeline/ReviewDesk";
import ContextRefreshes from "./ContextRefreshes";
import { sourceLabel } from "./timeline/SessionMemoryBoard";

interface Context {
  organization_requests?: { request_id?: string }[];
  event_date_known: boolean;
  dates: string[];
  uploaded_at: string;
  sessions: (MemorySession & { supporting_passages?: string[] })[];
  undated_session: {
    session_key: string;
    revision: number;
    title: string;
    sources: SessionSource[];
    stale?: boolean;
  } | null;
  proposal:
    | (MemoryReviewProposal & {
        stage: string;
        questions: string[];
        completed_sources: number;
        total_sources: number;
        source_scope: SessionSource[];
      })
    | null;
}
const busy = ["queued", "generating", "checking", "applying", "regenerating"];
const stageText: Record<string, string> = {
  queued: "Queued",
  account: "Reading session sources",
  checking_claims: "Checking claims",
  memory: "Drafting note changes",
  complete: "Preparation complete",
};

export default function RecordingMemoryContext({
  recordingId,
}: {
  recordingId: string;
}) {
  const client = useQueryClient();
  const [params] = useSearchParams();
  const space = params.get("memory_space_id") || undefined;
  const [clarification, setClarification] = useState("");
  const [requestId, setRequestId] = useState<string>();
  const [inspect, setInspect] = useState(false);
  const query = useQuery({
    queryKey: ["recording-context", recordingId, space],
    queryFn: async () =>
      (
        await api.get<Context>(`/api/recordings/${recordingId}/context`, {
          params: { timezone: "Asia/Kolkata", memory_space_id: space },
        })
      ).data,
    refetchInterval: 5000,
  });
  const reload = () =>
    client.invalidateQueries({ queryKey: ["recording-context", recordingId] });
  const prepare = useMutation({
    mutationFn: () =>
      api.post(`/api/recordings/${recordingId}/undated-session`, {
        memory_space_id: space,
      }),
    onSuccess: reload,
  });
  const organize = useMutation({
    mutationFn: (day: string) =>
      api.post(`/api/recordings/${recordingId}/organize-day`, {
        local_date: day,
        timezone: "Asia/Kolkata",
      }),
    onSuccess: (response) => {
      setRequestId(response.data.request_id);
      reload();
    },
  });
  const activeRequest =
    requestId ||
    query.data?.organization_requests?.find((r) => r.request_id)?.request_id;
  const progress = useQuery({
    queryKey: ["recording-organization", activeRequest],
    queryFn: async () =>
      (await timelineApi.getReconciliation(activeRequest!)).data,
    enabled: !!activeRequest,
    refetchInterval: 5000,
  });
  const data = query.data,
    session = data?.undated_session,
    proposal = data?.proposal;
  const questions = proposal?.state === "needs_attention" ? proposal.questions : [];
  const hasIncludedSources = session?.sources.some(source => ["supporting", "uncertain"].includes(source.participation));
  const generate = useMutation({
    mutationFn: () =>
      api.post(`/api/sessions/undated/${session!.session_key}/memory`, {
        revision: session!.revision,
        excluded_source_keys: [],
        memory_space_id: space,
      }),
    onSuccess: reload,
  });
  const correct = useMutation({
    mutationFn: () =>
      api.post(`/api/context-refreshes/${proposal!.proposal_id}/refresh`),
    onSuccess: reload,
  });
  const decide = useMutation({
    mutationFn: ({
      action,
      keys,
    }: {
      action: "exclude" | "include" | "clarify";
      keys: string[];
    }) =>
      api.post(`/api/sessions/undated/${session!.session_key}/decision`, {
        revision: session!.revision,
        memory_space_id: space,
        source_keys: keys,
        action,
        clarification: action === "clarify" ? clarification : undefined,
      }),
    onSuccess: async () => {
      setClarification("");
      await reload();
    },
  });
  const inference = useQuery({
    queryKey: ["session-memory-exchanges", proposal?.proposal_id],
    queryFn: async () =>
      (await timelineApi.getMemoryExchanges(proposal!.proposal_id)).data,
    enabled: inspect && !!proposal,
  });
  const error = [query, prepare, organize, generate, decide, correct].find(
    (q) => q.isError,
  )?.error as
    { response?: { data?: { detail?: string } }; message?: string } | undefined;
  return (
    <section
      aria-label="Timeline and memory"
      className="text-[var(--tape-ink)] space-y-3 rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-4"
    >
      <h2 className="font-semibold">Timeline & memory</h2>
      {query.isLoading && (
        <p role="status" className="text-sm">
          Finding this recording’s sessions…
        </p>
      )}
      {error && (
        <div role="alert" className="text-sm text-red-700 dark:text-red-300">
          {error.response?.data?.detail || error.message}
          <Button size="sm" variant="secondary" onClick={() => query.refetch()}>
            Reload
          </Button>
        </div>
      )}
      {data && (
        <>
          {!data.event_date_known && (
            <p className="text-sm text-gray-600 dark:text-gray-300">
              Recording date unknown. Uploaded{" "}
              {sourceDate(data.uploaded_at).toLocaleString("en-IN", {
                timeZone: "Asia/Kolkata",
              })}{" "}
              IST. This session stays undated.
            </p>
          )}
          {data.event_date_known && !data.sessions.length && (
            <p className="text-sm text-gray-600 dark:text-gray-300">
              No sessions organized yet.
            </p>
          )}
          {data.dates.map((day) => (
            <div className="flex flex-wrap items-center gap-3" key={day}>
              <Link
                className="text-sm font-medium text-[var(--tape-focus)]"
                to={`/timeline?date=${day}&recording=${recordingId}`}
              >
                View {day}
              </Link>
              {!data.sessions.length && (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={organize.isPending}
                  onClick={() => organize.mutate(day)}
                >
                  {organize.isPending ? "Queuing…" : "Organize this day"}
                </Button>
              )}
            </div>
          ))}
          {activeRequest && (
            <p role="status" className="text-sm">
              Day organization: {progress.data?.state || "queued"}.{" "}
              <Link
                className="text-[var(--tape-focus)]"
                to={`/timeline?date=${data.dates[0]}&recording=${recordingId}`}
              >
                Open progress in Timeline
              </Link>
            </p>
          )}
          {data.sessions.map((s) => (
            <article
              key={s.session_key}
              className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--tape-line)] pt-3"
            >
              <div className="min-w-0">
                <h3 className="break-words font-medium">{s.title}</h3>
                <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
                  {sourceDate(s.started_at).toLocaleTimeString("en-IN", {
                    timeZone: "Asia/Kolkata",
                    hour: "numeric",
                    minute: "2-digit",
                  })}
                  –
                  {sourceDate(s.ended_at).toLocaleTimeString("en-IN", {
                    timeZone: "Asia/Kolkata",
                    hour: "numeric",
                    minute: "2-digit",
                  })}{" "}
                  IST
                </p>
                <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
                  {s.summary}
                </p>
                {s.supporting_passages?.map((text, i) => (
                  <blockquote
                    key={i}
                    className="my-2 border-l-2 border-[var(--tape-line)] pl-2 text-sm"
                  >
                    {text}
                  </blockquote>
                ))}
                <p className="mt-1 text-xs">
                  {s.change_count
                    ? `${s.change_count} proposed changes`
                    : s.state.replace(/_/g, " ")}
                </p>
              </div>
              <Link
                className="rounded-md bg-[var(--tape-focus)] px-3 py-2 text-sm font-medium text-[var(--tape-paper)]"
                to={`/timeline?date=${s.owner_local_date}&session=${s.session_key}`}
              >
                Open session
              </Link>
            </article>
          ))}
          {!data.event_date_known && !session && (
            <Button
              variant="primary"
              size="sm"
              disabled={prepare.isPending}
              onClick={() => prepare.mutate()}
            >
              {prepare.isPending ? "Preparing…" : "Prepare undated session"}
            </Button>
          )}
          {session?.stale && (
            <Button
              size="sm"
              variant="secondary"
              disabled={prepare.isPending}
              onClick={() => prepare.mutate()}
            >
              Prepare current recording revision
            </Button>
          )}
          {session && (
            <div className="space-y-3 border-t border-[var(--tape-line)] pt-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="font-medium">{session.title}</h3>
                  <p className="text-xs text-gray-500">
                    Undated session · revision {session.revision}
                  </p>
                </div>
                {hasIncludedSources && (!proposal ||
                  ["failed", "stale"].includes(
                    proposal.state,
                  )) && (
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={generate.isPending || session.stale}
                    onClick={() => generate.mutate()}
                  >
                    {generate.isPending
                      ? "Queuing…"
                      : proposal?.state === "failed"
                        ? "Retry preparation"
                        : "Generate memory"}
                  </Button>
                )}
              </div>
              <details>
                <summary className="cursor-pointer text-sm">
                  Sources & exclusions
                </summary>
                {session.sources.map((s) => (
                  <div
                    key={s.key}
                    className="mt-2 rounded border border-[var(--tape-line)] p-3"
                  >
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="accent-[var(--tape-focus)]"
                        disabled={decide.isPending || s.kind === "annotation"}
                        checked={s.participation !== "excluded"}
                        onChange={(e) =>
                          decide.mutate({
                            action: e.target.checked ? "include" : "exclude",
                            keys: [s.key],
                          })
                        }
                      />
                      Include {sourceLabel(s).toLowerCase()} in selection ·{" "}
                      {s.locator.capture_source_id}
                    </label>
                    <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
                      {s.participation === "uncertain"
                        ? "Attribution needs clarification"
                        : s.participation === "excluded"
                          ? "Excluded from memory"
                          : s.participation === "background"
                            ? "Background context only"
                            : "Contributes to the account"}{" "}
                      ·{" "}
                      <a
                        href="#recording-transcript"
                        className="text-[var(--tape-focus)] underline"
                      >
                        Inspect transcript
                      </a>
                    </p>
                    <p className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-sm">
                      {s.excerpt}
                    </p>
                  </div>
                ))}
              </details>
              {proposal && (
                <>
                  <p role="status" className="text-sm">
                    {!hasIncludedSources ? "Excluded from memory" : busy.includes(proposal.state)
                      ? `${stageText[proposal.stage] || proposal.stage}${proposal.total_sources ? ` · ${proposal.completed_sources}/${proposal.total_sources} source groups` : ""}`
                      : proposal.state === "no_changes"
                        ? "No useful new changes"
                        : proposal.state === "applied"
                          ? "Memory saved"
                          : proposal.state === "pending"
                            ? `${proposal.changes?.length || 0} proposed changes`
                            : proposal.state === "failed" ? "Preparation failed. No notes were saved." : proposal.state === "needs_attention" ? "Clarification needed" : proposal.state === "stale" ? "Sources changed. Prepare a fresh draft." : proposal.state.replace(/_/g, " ")}
                  </p>
                  {busy.includes(proposal.state) && (
                    <div
                      role="progressbar"
                      aria-label="Completed source groups"
                      aria-valuemin={0}
                      aria-valuemax={Math.max(1, proposal.total_sources)}
                      aria-valuenow={proposal.completed_sources}
                      className="h-2 w-full overflow-hidden rounded bg-[var(--tape-track)]"
                    ><div className="h-full bg-[var(--tape-focus)]" style={{ width: `${Math.min(100, 100 * proposal.completed_sources / Math.max(1, proposal.total_sources))}%` }} /></div>
                  )}
                  {proposal.error && (
                    <details>
                      <summary className="cursor-pointer text-sm text-red-700 dark:text-red-300">
                        Preparation needs attention
                      </summary>
                      <pre className="mt-2 whitespace-pre-wrap break-words text-xs">
                        {proposal.error}
                      </pre>
                    </details>
                  )}
                  {questions.map((q, i) => (
                    <p
                      key={i}
                      className="rounded border border-amber-300 p-3 text-sm dark:border-amber-800"
                    >
                      {q}
                    </p>
                  ))}
                  {!!questions.length && (
                    <div className="space-y-2">
                      <label className="block text-sm">
                        Your clarification
                        <textarea
                          value={clarification}
                          onChange={(e) => setClarification(e.target.value)}
                          className="mt-1 block w-full rounded border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] p-2"
                        />
                      </label>
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={decide.isPending || !clarification.trim()}
                        onClick={() =>
                          decide.mutate({
                            action: "clarify",
                            keys: session.sources
                              .filter((s) => s.kind !== "annotation")
                              .map((s) => s.key),
                          })
                        }
                      >
                        Save clarification
                      </Button>
                    </div>
                  )}
                  <CandidateChanges
                    proposal={proposal}
                    day="undated"
                    timezone="Asia/Kolkata"
                  />
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setInspect(!inspect)}
                  >
                    {inspect
                      ? "Hide model details"
                      : "Inspect model input and output"}
                  </Button>
                  {inspect && (
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-[var(--tape-line)] p-3 text-xs">
                      {inference.isLoading
                        ? "Loading model details…"
                        : JSON.stringify(inference.data, null, 2)}
                    </pre>
                  )}
                </>
              )}
            </div>
          )}
          {proposal?.state === "correction_required" && (
            <Button
              size="sm"
              variant="primary"
              disabled={correct.isPending}
              onClick={() => correct.mutate()}
            >
              Prepare correction proposal
            </Button>
          )}
          <ContextRefreshes memorySpace={space} recordingId={recordingId} />
        </>
      )}
    </section>
  );
}
