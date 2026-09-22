import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ChatSourceRef } from "../services/api";
import { sourceDate } from "../utils/sourceTime";
import type { ReactNode } from "react";

export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  const matched = new Set(terms.map((t) => t.toLocaleLowerCase()));
  return (
    <>
      {text.split(/([\p{L}\p{N}]+)/u).map((part, i) =>
        matched.has(part.toLocaleLowerCase()) ? (
          <mark
            key={i}
            className="rounded-sm bg-[var(--tape-selected)] text-[var(--tape-focus)]"
          >
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

function matchingExcerpt(text: string, terms: string[]) {
  const matched = new Set(terms.map((term) => term.toLocaleLowerCase()));
  const firstMatch = [...text.matchAll(/[\p{L}\p{N}]+/gu)].find((part) =>
    matched.has(part[0].toLocaleLowerCase()),
  );
  let start = Math.max(0, (firstMatch?.index ?? 0) - 80);
  if (start > 0) {
    // Preserve whole words and enough context to understand the matched passage.
    while (start > 0 && !/\s/u.test(text[start - 1])) start--;
  }
  const passage = text.slice(start, start + 420);
  return `${start ? "… " : ""}${passage}${start + 420 < text.length ? "…" : ""}`;
}

export interface SourceHit {
  kind: "recording" | "session" | "episode";
  key: string;
  title: string;
  url: string;
  participants?: string[];
  excerpt: string;
  highlights: string[];
  started_at: string | null;
  duration?: number;
  owner_date?: string | null;
  timezone?: string;
  match_start?: number;
  match_end?: number;
}
export const hitRef = (hit: SourceHit): ChatSourceRef => ({
  kind: hit.kind,
  key: hit.key,
  local_date: hit.owner_date,
  timezone: hit.timezone || "Asia/Kolkata",
});
export const refKey = (ref: ChatSourceRef) =>
  [
    ref.kind,
    ref.key,
    ref.local_date || "",
    ref.timezone || "Asia/Kolkata",
  ].join(":");

export function useSourceResults(
  query: string,
  space?: string,
  kinds = "recording,episode,session",
  fields = "title,summary,transcript,speakers,id",
  offset = 0,
  recent = true,
) {
  const result = useQuery({
    queryKey: ["source-search", query, kinds, fields, offset, space],
    queryFn: async ({ signal }) =>
      (
        await api.get<{
          items: SourceHit[];
          total: number;
          indexing: {
            initialized?: boolean;
            state: string;
            completed: number;
            error?: string;
          };
        }>("/api/search", {
          signal,
          params: {
            q: query,
            memory_space_id: space,
            kinds: kinds.split(","),
            fields: fields.split(","),
            offset,
            limit: 20,
          },
          paramsSerializer: { indexes: null },
        })
      ).data,
    enabled: (recent || !!query) && !!fields,
    refetchInterval: (q) =>
      q.state.data && !q.state.data.indexing.initialized ? 10000 : false,
  });
  // React Query retains old data after a failed refetch. A privacy hold must
  // remove those previews from every consumer, including chat attachments.
  return {
    ...result,
    data: result.isError || result.isFetching ? undefined : result.data,
  };
}

export function SourceResult({
  item,
  children,
  to,
  state,
  preview = false,
}: {
  item: SourceHit;
  children?: ReactNode;
  to?: string;
  state?: unknown;
  preview?: boolean;
}) {
  return (
    <article className="min-w-0 border-b border-[var(--tape-line)] px-2 py-3">
      <Link
        to={to || item.url}
        state={state}
        target={preview ? "_blank" : undefined}
        rel={preview ? "noreferrer" : undefined}
        className="block rounded py-1 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--tape-focus)]"
      >
        <p className="text-xs text-[var(--tape-activity)]">
          {item.kind === "session"
            ? "Conversation"
            : item.kind === "episode"
              ? "Episode"
              : "Recording"}{" "}
          ·{" "}
          {item.started_at
            ? sourceDate(item.started_at).toLocaleString("en-IN", {
                timeZone: "Asia/Kolkata",
                day: "numeric",
                month: "short",
                year: "numeric",
                hour: "numeric",
                minute: "2-digit",
              }) + " IST"
            : "Date unknown"}
        </p>
        <h3 className="mt-1 break-words font-medium">
          <Highlight text={item.title} terms={item.highlights || []} />
        </h3>
        {!!item.participants?.length && (
          <p className="mt-1 text-xs text-[var(--tape-activity)]">
            {item.participants.join(" · ")}
          </p>
        )}
        <p className="mt-1 line-clamp-3 break-words text-sm text-[var(--tape-activity)]">
          <Highlight
            text={matchingExcerpt(item.excerpt, item.highlights || [])}
            terms={item.highlights || []}
          />
        </p>
      </Link>
      <div className="mt-2 flex flex-wrap items-center gap-3">{children}</div>
    </article>
  );
}
