import { SourceResult, useSourceResults } from "./SourceResults";
import AskAboutSource from "./AskAboutSource";
import { useEffect, useState, ReactNode } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import {
  Search,
  SlidersHorizontal,
  ChevronDown,
  X,
} from "lucide-react";
import { api } from "../services/api";
import { Button } from "./ui";

const fields = ["title", "summary", "transcript", "speakers", "id"];
const fieldLabel: Record<string, string> = {
  title: "Titles",
  summary: "Summaries",
  transcript: "Transcript",
  speakers: "Speakers",
  id: "IDs",
};

export { Highlight } from "./SourceResults";

interface SourceSearchProps {
  browseActions?: ReactNode;
  browseFilters?: ReactNode;
  activeFilterCount?: number;
}
export default function SourceSearch({ browseActions, browseFilters, activeFilterCount = 0 }: SourceSearchProps) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const [filtersOpen, setFiltersOpen] = useState(false);
  const value = params.get("q") || "";
  const [debounced, setDebounced] = useState(value);
  const scope = params.get("types") || "recording,episode,session";
  const selected = (params.get("fields") || fields.join(","))
    .split(",")
    .filter((f) => fields.includes(f));
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value.trim()), 800);
    return () => clearTimeout(timer);
  }, [value]);
  const update = (key: string, next: string) => {
    const p = new URLSearchParams(params);
    next ? p.set(key, next) : p.delete(key);
    if (key !== "offset") p.delete("offset");
    setParams(p, { replace: true });
  };
  const query = useSourceResults(debounced, params.get("memory_space_id") || undefined, scope, selected.join(","), offset, false);
  const searching = query.isFetching || value.trim() !== debounced;
  const retryIndex = useMutation({
    mutationFn: () => api.post("/api/search/retry-index"),
    onSuccess: () => {
      void query.refetch();
    },
  });
  return (
    <section
      aria-label="Search recordings, episodes and sessions"
      className="space-y-3 text-[var(--tape-ink)]"
    >
      <div className="flex flex-wrap items-center gap-2">
      <div className="flex min-w-0 basis-64 flex-1 items-center rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] focus-within:ring-2 focus-within:ring-[var(--tape-focus)]">
        <label className="relative min-w-0 flex-1">
          <span className="sr-only">Search recordings, episodes and sessions</span>
          <Search
            aria-hidden
            className="absolute left-4 top-3.5 h-4 w-4 text-[var(--tape-activity)]"
          />
          <input
            value={value}
            onChange={(e) => update("q", e.target.value)}
            placeholder="Search words, speakers or IDs"
            className="h-11 w-full rounded-lg bg-transparent pl-11 pr-3 text-sm placeholder:text-[var(--tape-activity)] outline-none"
          />
        </label>
        {value && (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => update("q", "")}
            className="mr-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-md text-[var(--tape-activity)] hover:bg-[var(--tape-chip)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--tape-focus)]"
          >
            <X aria-hidden className="h-4 w-4" />
          </button>
        )}
      </div>
      <select
        aria-label="Search scope"
        title="Choose which source types to search"
        value={scope}
        onChange={(e) => update("types", e.target.value)}
        className="h-11 max-w-full rounded-md border border-[var(--tape-line)] bg-[var(--tape-paper-raised)] px-3 text-sm text-[var(--tape-ink)]"
      >
        <option value="recording,episode,session">Everything</option>
        <option value="recording">Recordings</option>
        <option value="episode">Episodes</option>
        <option value="session">Sessions</option>
        <option value="recording,session">Recordings &amp; sessions</option>
      </select>
      <button
        type="button"
        aria-expanded={filtersOpen}
        aria-controls="source-search-fields"
        onClick={() => setFiltersOpen(!filtersOpen)}
        className="flex min-h-11 items-center gap-2 rounded-md border border-[var(--tape-line)] px-3 text-sm text-[var(--tape-activity)] hover:bg-[var(--tape-chip)]"
      >
        <SlidersHorizontal aria-hidden className="h-4 w-4" />
        Filters
        {(selected.length !== fields.length || activeFilterCount > 0) &&
          ` (${(selected.length !== fields.length ? 1 : 0) + activeFilterCount})`}
        <ChevronDown aria-hidden className={`h-4 w-4 transition-transform ${filtersOpen ? "rotate-180" : ""}`} />
      </button>
      {browseActions}
      </div>
      {filtersOpen && (
        <fieldset
          id="source-search-fields"
          className="rounded-lg border border-[var(--tape-line)] p-3"
        >
          <legend className="px-1 text-xs text-[var(--tape-activity)]">
            Search options
          </legend>
          <p className="mb-3 text-sm text-[var(--tape-activity)]">
            Everything searches recordings (audio and transcripts), episodes (individual activities),
            and sessions (related episodes, or a prepared undated recording).
          </p>
          <p className="mb-1 text-xs font-medium text-[var(--tape-activity)]">Search within</p>
          <div className="grid grid-cols-2 gap-x-3 sm:flex sm:flex-wrap sm:gap-x-5">
            {fields.map((field) => (
              <label
                key={field}
                className="flex min-h-10 items-center gap-2 text-sm"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(field)}
                  onChange={() =>
                    update(
                      "fields",
                      (selected.includes(field)
                        ? selected.filter((f) => f !== field)
                        : [...selected, field]
                      ).join(",") || "none",
                    )
                  }
                  className="h-4 w-4 accent-[var(--tape-focus)]"
                />
                {fieldLabel[field]}
              </label>
            ))}
          </div>
          {browseFilters}
          {selected.length !== fields.length && <button type="button" className="min-h-11 text-sm text-[var(--tape-focus)]" onClick={() => update("fields", "")}>Reset search options</button>}
        </fieldset>
      )}
      {!!value && (
        <>
          <p role="status" className="text-sm text-gray-500 dark:text-gray-400">
            {!selected.length
              ? "Select a field to search."
              : searching
                ? "Searching…"
                : query.isError
                  ? "Search failed. Try again."
                  : `${query.data?.total || 0} results · best matches first`}
          </p>
          {query.isError && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => query.refetch()}
            >
              Retry search
            </Button>
          )}
          {query.data?.indexing.state === "failed" && (
            <Button
              size="sm"
              variant="secondary"
              disabled={retryIndex.isPending}
              onClick={() => retryIndex.mutate()}
            >
              Retry indexing
            </Button>
          )}
          {retryIndex.isError && (
            <p role="alert" className="text-sm text-red-700 dark:text-red-300">
              Could not restart indexing. Try again.
            </p>
          )}
          {query.data && !query.data.indexing.initialized && (
            <p
              role="status"
              className="rounded-lg border border-amber-300 px-3 py-2 text-sm text-amber-800 dark:text-amber-200"
            >
              {query.data.indexing.state === "failed"
                ? "Search indexing paused after an error. Existing results remain available."
                : "Preparing search history. Results are incomplete while recordings, episodes and sessions are indexed."}
            </p>
          )}
          {!searching &&
            !query.isError &&
            selected.length > 0 &&
            query.data?.total === 0 && (
              <div className="border-t border-[var(--tape-line)] py-8">
                <h3 className="font-medium">
                  No matching{" "}
                  {scope === "session"
                    ? "sessions"
                    : scope === "episode"
                      ? "episodes"
                    : scope === "recording"
                      ? "recordings"
                      : "sources"}
                </h3>
                <p className="mt-1 text-sm text-[var(--tape-activity)]">
                  Try fewer words or include more search fields.
                </p>
              </div>
            )}
          {!searching &&
            query.data?.items.map((item) => {
              const url = new URL(item.url, "https://chronicle.invalid");
              if (
                item.kind === "recording" &&
                typeof item.match_start === "number" &&
                typeof item.match_end === "number"
              ) {
                url.searchParams.set("start", String(item.match_start));
                url.searchParams.set("end", String(item.match_end));
              }
              const memorySpace = params.get("memory_space_id");
              if (memorySpace)
                url.searchParams.set("memory_space_id", memorySpace);
              return (
                <SourceResult key={item.kind + item.key} item={item} to={url.pathname + url.search} state={{ from: location.pathname + location.search }}>
                  <AskAboutSource source={{ kind: item.kind, key: item.key, local_date: item.owner_date, timezone: item.timezone }} />
                </SourceResult>
              );
            })}
          {!searching && query.data && query.data.total > 20 && (
            <div className="flex items-center justify-between">
              <Button
                size="sm"
                variant="secondary"
                disabled={!offset}
                onClick={() =>
                  update("offset", String(Math.max(0, offset - 20)))
                }
              >
                Previous
              </Button>
              <span className="text-xs">
                Page {Math.floor(offset / 20) + 1}
              </span>
              <Button
                size="sm"
                variant="secondary"
                disabled={offset + 20 >= query.data.total}
                onClick={() => update("offset", String(offset + 20))}
              >
                Next
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
