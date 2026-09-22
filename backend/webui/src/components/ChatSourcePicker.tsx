import { useEffect, useState } from "react";
import { ChatSourceRef } from "../services/api";
import ChatDialog from "./ChatDialog";
import {
  SourceResult,
  hitRef,
  refKey,
  useSourceResults,
} from "./SourceResults";

export default function ChatSourcePicker({
  selected,
  space,
  onSave,
  onClose,
}: {
  selected: ChatSourceRef[];
  space?: string;
  onSave: (refs: ChatSourceRef[]) => Promise<void>;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(selected);
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(text.trim());
      setOffset(0);
    }, 350);
    return () => clearTimeout(timer);
  }, [text]);
  const results = useSourceResults(query, space, undefined, undefined, offset);
  return (
    <ChatDialog title="Add conversations" onClose={onClose}>
      <label className="block text-sm">
        Find by title, person, or words you remember
        <input
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          className="mt-2 w-full rounded border border-[var(--tape-line)] bg-transparent p-3"
          placeholder="Search conversations…"
        />
      </label>
      <p className="my-3 text-sm text-[var(--tape-activity)]">
        {text ? "Matching conversations" : "Recent conversations"} ·{" "}
        {draft.length} of 10 selected
      </p>
      {(results.isFetching || text.trim() !== query) && (
        <p role="status">Finding conversations…</p>
      )}
      {results.isError && (
        <p role="alert">
          Search is unavailable.{" "}
          <button className="underline" onClick={() => void results.refetch()}>
            Retry
          </button>
        </p>
      )}
      {results.data?.items.map((item) => {
        const ref = hitRef(item);
        const previewUrl = space
          ? `${item.url}${item.url.includes("?") ? "&" : "?"}memory_space_id=${encodeURIComponent(space)}`
          : item.url;
        const checked = draft.some((r) => refKey(r) === refKey(ref));
        return (
          <SourceResult key={refKey(ref)} item={item} to={previewUrl} preview>
            <label className="inline-flex min-h-10 items-center gap-2">
              <input
                type="checkbox"
                checked={checked}
                disabled={saving || (!checked && draft.length >= 10)}
                onChange={() =>
                  setDraft((previous) =>
                    checked
                      ? previous.filter((r) => refKey(r) !== refKey(ref))
                      : [...previous, ref],
                  )
                }
              />
              Include in chat
            </label>
            <a
              className="text-sm underline"
              href={previewUrl}
              target="_blank"
              rel="noreferrer"
            >
              Preview conversation
            </a>
          </SourceResult>
        );
      })}
      {!results.isFetching && results.data?.items.length === 0 && (
        <p>No conversations found. Try fewer words or a different person.</p>
      )}
      <div className="my-3 flex justify-between">
        <button
          disabled={!offset}
          onClick={() => setOffset(Math.max(0, offset - 20))}
        >
          Previous
        </button>
        <button
          disabled={!results.data || offset + 20 >= results.data.total}
          onClick={() => setOffset(offset + 20)}
        >
          Next
        </button>
      </div>
      {error && (
        <p role="alert" className="my-2 text-red-700 dark:text-red-300">
          {error}
        </p>
      )}
      <button
        disabled={saving}
        className="min-h-11 rounded bg-[var(--tape-focus)] px-4 py-2 text-white"
        onClick={async () => {
          setSaving(true);
          setError("");
          try {
            await onSave(draft);
            onClose();
          } catch (e: any) {
            setError(
              e.response?.data?.detail ||
                e.message ||
                "Could not update conversations",
            );
          } finally {
            setSaving(false);
          }
        }}
      >
        {saving ? "Adding…" : "Use selected conversations"}
      </button>
    </ChatDialog>
  );
}
