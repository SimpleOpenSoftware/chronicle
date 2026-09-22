import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../services/api";
import { Button } from "./ui";

interface Suggestion {
  proposal_id: string;
  title: string;
  session_key: string;
  local_date: string | null;
  recording_id: string | null;
  correction_required: boolean;
  assessment: { reason: string; verdict: string; relevant_paths: string[] };
}
interface Props {
  day?: string;
  memorySpace?: string;
  recordingId?: string;
}

export default function ContextRefreshes(props: Props) {
  // A different day/space owns a separate queueing interaction.
  return <ContextRefreshList key={JSON.stringify(props)} {...props} />;
}

function ContextRefreshList({ day, memorySpace, recordingId }: Props) {
  const [open, setOpen] = useState(false);
  const busy = useRef(false);
  const [queued, setQueued] = useState<Set<string>>(new Set());
  const [failures, setFailures] = useState<Record<string, string>>({});
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const client = useQueryClient();
  const check = useMutation({
    mutationFn: () =>
      api.post("/api/context-refreshes/check", {
        local_date: day,
        recording_id: recordingId,
        memory_space_id: memorySpace,
      }),
  });
  const runCheck = check.mutate;
  useEffect(() => {
    if (day || recordingId) runCheck();
  }, [day, recordingId, memorySpace, runCheck]);
  const query = useQuery({
    queryKey: ["context-refreshes", day, memorySpace, recordingId],
    queryFn: async () =>
      (
        await api.get<{ items: Suggestion[] }>("/api/context-refreshes", {
          params: { local_date: day, memory_space_id: memorySpace, recording_id: recordingId },
        })
      ).data,
    refetchInterval: 30000,
  });
  const refresh = useMutation({
    mutationFn: async (items: Suggestion[]) => {
      // Only enqueue here; the existing durable workers own generation.
      // Bound requests and retain per-item results so one failure cannot stop others.
      const pending = [...items];
      await Promise.all(
        Array.from({ length: Math.min(3, pending.length) }, async () => {
          while (pending.length) {
            const item = pending.shift()!;
            try {
              await api.post(
                `/api/context-refreshes/${item.proposal_id}/refresh`,
              );
              setQueued((previous) => new Set(previous).add(item.proposal_id));
            } catch (error) {
              const detail = (
                error as { response?: { data?: { detail?: unknown } } }
              ).response?.data?.detail;
              setFailures((previous) => ({
                ...previous,
                [item.proposal_id]:
                  typeof detail === "string"
                    ? detail
                    : "Could not queue this refresh. Try again.",
              }));
            } finally {
              setProgress((previous) => ({
                ...previous,
                completed: previous.completed + 1,
              }));
            }
          }
        }),
      );
    },
    onSettled: async () => {
      try {
        await Promise.all([
          client.invalidateQueries({ queryKey: ["context-refreshes"] }),
          client.invalidateQueries({ queryKey: ["timeline-sessions"] }),
          client.invalidateQueries({ queryKey: ["recording-context"] }),
        ]);
      } finally {
        busy.current = false;
      }
    },
  });
  const items = (query.data?.items ?? []).filter(
    (item) => !queued.has(item.proposal_id) && (!recordingId || item.recording_id === recordingId),
  );
  const startRefresh = (selection: Suggestion[]) => {
    if (busy.current || !selection.length) return;
    busy.current = true;
    const unique = [
      ...new Map(selection.map((item) => [item.proposal_id, item])).values(),
    ];
    setFailures((previous) =>
      Object.fromEntries(
        Object.entries(previous).filter(
          ([id]) => !unique.some((item) => item.proposal_id === id),
        ),
      ),
    );
    setProgress({ completed: 0, total: unique.length });
    refresh.mutate(unique);
  };
  if (check.isError || query.isError)
    return (
      <div
        role="alert"
        className="rounded border border-amber-300 p-3 text-sm text-amber-900 dark:text-amber-200"
      >
        Could not check whether new context helps these sessions.{" "}
        <Button
          size="sm"
          variant="secondary"
          disabled={check.isPending}
          onClick={() => {
            runCheck();
            void query.refetch();
          }}
        >
          Retry context check
        </Button>
      </div>
    );
  if (!items.length && !progress.total) return null;
  return (
    <section
      aria-label="Suggested memory refreshes"
      className="text-[var(--tape-ink)] rounded-lg border border-amber-300 bg-[var(--tape-paper-raised)] p-4 dark:border-amber-800"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="font-semibold">
            Suggested memory refreshes
          </h3>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
            {items.length > 0
              ? `${items.length} suggestion${items.length === 1 ? "" : "s"} · ${recordingId ? "This recording" : day ? day : "This memory space"}`
              : "No remaining suggestions."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {items.length > 0 && (
            <Button
              size="sm"
              variant="primary"
              icon={
                <RefreshCw
                  size={14}
                  aria-hidden="true"
                  className={
                    refresh.isPending
                      ? "animate-spin motion-reduce:animate-none"
                      : ""
                  }
                />
              }
              disabled={refresh.isPending}
              onClick={() => startRefresh(items)}
            >
              {refresh.isPending
                ? `Queuing ${progress.completed} of ${progress.total}…`
                : `Refresh all suggested (${items.length})`}
            </Button>
          )}
          {items.length > 0 && (
            <Button
              size="sm"
              variant="secondary"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
            >
              {open ? "Hide suggestions" : "Review suggested refreshes"}
            </Button>
          )}
        </div>
      </div>
      {progress.total > 0 && (
        <p role="status" className="mt-3 text-sm text-[var(--tape-ink)]">
          {refresh.isPending
            ? `Queuing refreshes: ${progress.completed} of ${progress.total} requests finished.`
            : `${queued.size} refresh${queued.size === 1 ? "" : "es"} queued. Proposed note changes still need your approval.`}
        </p>
      )}
      {Object.keys(failures).length > 0 && (
        <div
          role="alert"
          className="mt-3 text-sm text-red-700 dark:text-red-300"
        >
          <p>
            {Object.keys(failures).length} refresh
            {Object.keys(failures).length === 1 ? "" : "es"} could not be
            queued. You can retry the remaining suggestions.
          </p>
          <ul className="mt-1 space-y-1">
            {Object.entries(failures).map(([id, message]) => (
              <li key={id} className="break-words">
                {query.data?.items.find((item) => item.proposal_id === id)
                  ?.title || "Session"}
                : {message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {open && (
        <div className="mt-3 space-y-3">
          {items.map((item) => (
            <article
              key={item.proposal_id}
              className="border-t border-[var(--tape-line)] pt-3"
            >
              <Link
                className="font-medium text-[var(--tape-focus)]"
                to={
                  item.local_date
                    ? `/timeline?date=${item.local_date}&session=${item.session_key}`
                    : `/recordings/${item.recording_id}`
                }
              >
                {item.title}
              </Link>
              <p className="my-2 text-sm leading-relaxed">
                {item.assessment.reason}
              </p>
              <p className="mb-2 break-words text-xs text-gray-500">
                {item.assessment.relevant_paths.join(" · ")}
              </p>
              <Button
                size="sm"
                variant="primary"
                disabled={refresh.isPending}
                onClick={() => startRefresh([item])}
              >
                {refresh.isPending &&
                refresh.variables?.some(
                  (selected) => selected.proposal_id === item.proposal_id,
                )
                  ? "Queuing…"
                  : item.correction_required
                    ? "Prepare correction proposal"
                    : "Refresh this session"}
              </Button>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
