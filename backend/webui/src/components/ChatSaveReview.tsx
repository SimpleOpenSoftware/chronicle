import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { chatApi } from "../services/api";
import ChatDialog from "./ChatDialog";

export default function ChatSaveReview({
  sessionId,
  destination,
  onClose,
  viewRun,
}: {
  sessionId: string;
  destination: string;
  onClose: () => void;
  viewRun: (id: string) => void;
}) {
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ["chat", "save", sessionId],
    queryFn: () => chatApi.getSaveProposal(sessionId).then((r) => r.data),
    refetchInterval: (q) =>
      ["queued", "generating", "applying"].includes(q.state.data?.state)
        ? 1500
        : false,
  });
  const proposal = query.data;
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setSelected([]);
  }, [proposal?.generation]);
  const act = async (kind: "generate" | "approve" | "discard" | "retry") => {
    setBusy(true);
    setError("");
    try {
      const { data } =
        kind === "generate"
          ? await chatApi.createSaveProposal(sessionId)
          : await chatApi.decideSaveProposal(
              sessionId,
              proposal.proposal_id,
              proposal.generation,
              selected,
              kind,
            );
      cache.setQueryData(["chat", "save", sessionId], data);
      void cache.invalidateQueries({ queryKey: ["chat", "runs", sessionId] });
    } catch (e: any) {
      setError(e.response?.data?.detail || "Could not update this review");
    } finally {
      setBusy(false);
    }
  };
  return (
    <ChatDialog title="Review and save" onClose={onClose}>
      <p>
        Destination: <strong>{destination}</strong>
      </p>
      <p className="my-3 text-sm text-[var(--tape-activity)]">
        Review proposed notes from the whole chat. Only changes you select and
        save will update your vault.
      </p>
      {query.isLoading && <p role="status">Loading review…</p>}
      {query.isError && (
        <p role="alert">
          Could not load this review.{" "}
          <button onClick={() => void query.refetch()}>Retry</button>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {proposal && (
        <p className="my-3" role="status">
          {
            (
              {
                queued: "Waiting to draft changes…",
                generating: "Reading the chat and drafting changes…",
                pending: "Ready to review",
                applying: "Saving your selected changes…",
                applied: "Selected changes saved",
                discarded: "Preview discarded",
                failed: "Review needs attention",
              } as Record<string, string>
            )[proposal.state]
          }
        </p>
      )}
      {proposal?.error && (
        <p role="alert" className="my-3 text-red-700 dark:text-red-300">
          {proposal.error}
        </p>
      )}
      {proposal?.run_id && (
        <button
          className="min-h-10 text-sm underline"
          onClick={() => viewRun(proposal.run_id)}
        >
          View run
        </button>
      )}
      {proposal?.messages && (
        <details className="my-3">
          <summary className="cursor-pointer py-2">
            Chat included in this preview · {proposal.messages.length} messages
          </summary>
          <p className="text-xs">
            Messages sent after this snapshot are not included.
          </p>
          {proposal.messages.map((m: any) => (
            <div key={m.message_id} className="my-3">
              <strong>{m.role === "user" ? "You" : "Chronicle"}</strong>
              <p className="whitespace-pre-wrap break-words text-sm">
                {m.content}
              </p>
            </div>
          ))}
        </details>
      )}
      {proposal?.state === "pending" && !proposal.changes.length && (
        <p>No note changes were proposed.</p>
      )}
      {proposal?.state === "applied" && (
        <div className="my-3 text-sm">
          <p>Saved in {destination}:</p>
          <ul className="list-disc pl-5">
            {proposal.changes
              .filter((c: any) =>
                proposal.applied_change_ids.includes(c.change_id),
              )
              .map((c: any) => (
                <li key={c.change_id} className="break-all">
                  {c.note_path}
                </li>
              ))}
          </ul>
        </div>
      )}
      {proposal?.changes?.map((c: any) => (
        <article
          key={c.change_id}
          className="my-3 rounded border border-[var(--tape-line)] p-3"
        >
          <label className="flex min-h-10 items-center gap-2">
            <input
              type="checkbox"
              checked={
                proposal.state === "applied"
                  ? proposal.applied_change_ids.includes(c.change_id)
                  : selected.includes(c.change_id)
              }
              disabled={busy || proposal.state !== "pending"}
              onChange={(e) =>
                setSelected((previous) =>
                  e.target.checked
                    ? [...previous, c.change_id]
                    : previous.filter((id) => id !== c.change_id),
                )
              }
            />
            <strong className="break-all">{c.note_path}</strong>
          </label>
          <p className="text-sm text-[var(--tape-activity)]">{c.summary}</p>
          <details>
            <summary className="cursor-pointer py-2">Before and after</summary>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <h4 className="text-sm font-medium">Before</h4>
                <pre className="whitespace-pre-wrap break-words text-xs">
                  {c.before_text || "New note"}
                </pre>
              </div>
              <div>
                <h4 className="text-sm font-medium">After</h4>
                <pre className="whitespace-pre-wrap break-words text-xs">
                  {c.after_text || "Removed"}
                </pre>
              </div>
            </div>
          </details>
          <details className="text-sm">
            <summary className="cursor-pointer py-2">
              Chat evidence reviewed
            </summary>
            <p className="text-xs text-[var(--tape-activity)]">
              Check the original attribution. Assistant suggestions are not
              established facts.
            </p>
            {(proposal.messages || [])
              .filter((m: any) => c.message_ids?.includes(m.message_id))
              .map((m: any) => (
                <div key={m.message_id} className="my-3">
                  <p className="font-medium">
                    {m.role === "user" ? "You" : "Chronicle"}
                  </p>
                  <p className="whitespace-pre-wrap break-words">{m.content}</p>
                  {m.metadata?.evidence?.conversations.map((source: any) => (
                    <div
                      key={source.ref.key}
                      className="mt-2 border-l border-[var(--tape-line)] pl-3"
                    >
                      <p className="font-medium">Source: {source.title}</p>
                      {source.passages.map((passage: any) => (
                        <blockquote
                          key={passage.id}
                          className="my-2 whitespace-pre-wrap break-words"
                        >
                          {passage.text}
                        </blockquote>
                      ))}
                    </div>
                  ))}
                </div>
              ))}
          </details>
        </article>
      ))}
      <div className="mt-4 flex flex-wrap gap-3">
        {(!proposal || ["applied", "discarded"].includes(proposal.state)) &&
          !query.isLoading &&
          !query.isError && (
            <button
              disabled={busy}
              className="min-h-11 rounded bg-[var(--tape-focus)] px-4 py-2 text-white"
              onClick={() => void act("generate")}
            >
              {busy ? "Preparing…" : "Preview whole-chat changes"}
            </button>
          )}
        {proposal?.state === "pending" && (
          <button
            disabled={busy || !selected.length}
            className="min-h-11 rounded bg-[var(--tape-focus)] px-4 py-2 text-white disabled:opacity-50"
            onClick={() => void act("approve")}
          >
            Save selected changes ({selected.length})
          </button>
        )}
        {proposal?.state === "failed" && (
          <button
            disabled={busy}
            className="min-h-11 px-3 underline"
            onClick={() => void act("retry")}
          >
            Retry saved operation
          </button>
        )}
        {proposal &&
          !proposal.has_applied_changes &&
          ["queued", "pending", "failed"].includes(proposal.state) && (
            <button
              disabled={busy}
              className="min-h-11 px-3 underline"
              onClick={() => void act("discard")}
            >
              Discard preview
            </button>
          )}
      </div>
    </ChatDialog>
  );
}
