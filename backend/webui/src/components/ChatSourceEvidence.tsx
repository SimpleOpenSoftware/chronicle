import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { X, ArrowUpRight } from "lucide-react";
import { ChatSourceContext, SourcePassage } from "../services/api";

export function CitedSourceText({
  text,
  citations,
  onSelect,
}: {
  text: string;
  citations: SourcePassage[];
  onSelect: (passage: SourcePassage) => void;
}) {
  const byId = new Map(citations.map((p) => [p.id, p]));
  const inline = (value: string) => (
    <>
      {value
        .split(
          /(\[(?:S\d+|C[a-f0-9]+_S\d+|V[a-f0-9]+)(?:,\s*(?:S\d+|C[a-f0-9]+_S\d+|V[a-f0-9]+))*\]|\*\*[^*]+\*\*|\*[^*\n]+\*)/g,
        )
        .map((part, index) => {
          if (part.startsWith("**") && part.endsWith("**"))
            return <strong key={index}>{part.slice(2, -2)}</strong>;
          if (part.startsWith("*") && part.endsWith("*"))
            return <em key={index}>{part.slice(1, -1)}</em>;
          if (
            !/^\[(?:S\d+|C[a-f0-9]+_S\d+|V[a-f0-9]+)(?:,\s*(?:S\d+|C[a-f0-9]+_S\d+|V[a-f0-9]+))*\]$/.test(
              part,
            )
          )
            return part;
          return (
            <span key={index}>
              {(part.match(/(?:S\d+|C[a-f0-9]+_S\d+|V[a-f0-9]+)/g) || []).map(
                (id) => {
                  const passage = byId.get(id);
                  return passage ? (
                    <button
                      key={id}
                      onClick={() => onSelect(passage)}
                      aria-label={`Read source ${id}: ${passage.label}`}
                      className="mx-0.5 inline-flex min-h-8 items-center rounded px-1 text-[var(--tape-focus)] underline underline-offset-2"
                      title={passage.label}
                    >
                      [{citations.findIndex((p) => p.id === id) + 1}]
                    </button>
                  ) : (
                    `[${id}]`
                  );
                },
              )}
            </span>
          );
        })}
    </>
  );
  const blocks: string[] = [];
  let lines: string[] = [],
    previousKind = "";
  const flush = () => {
    if (lines.length) blocks.push(lines.join("\n"));
    lines = [];
  };
  for (const line of text.split("\n")) {
    if (!line.trim()) {
      flush();
      previousKind = "";
      continue;
    }
    const kind = /^#{1,6} /.test(line)
      ? "heading"
      : /^[-*_]{3,}$/.test(line.trim())
        ? "rule"
        : /^[-*] /.test(line)
          ? "bullets"
          : /^\d+\. /.test(line)
            ? "numbered"
            : "paragraph";
    if (kind !== previousKind || kind === "heading" || kind === "rule") flush();
    lines.push(line);
    previousKind = kind;
  }
  flush();
  return (
    <div className="space-y-3 break-words">
      {blocks.map((block, index) => {
        if (/^#{1,6} /.test(block))
          return (
            <p key={index} className="font-semibold">
              {inline(block.replace(/^#{1,6} /, ""))}
            </p>
          );
        if (/^[-*_]{3,}$/.test(block.trim()))
          return <hr key={index} className="border-[var(--tape-line)]" />;
        const lines = block.split("\n");
        if (lines.every((line) => /^[-*] /.test(line)))
          return (
            <ul key={index} className="list-disc space-y-1 pl-5">
              {lines.map((line, i) => (
                <li key={i}>{inline(line.slice(2))}</li>
              ))}
            </ul>
          );
        if (lines.every((line) => /^\d+\. /.test(line)))
          return (
            <ol key={index} className="list-decimal space-y-1 pl-5">
              {lines.map((line, i) => (
                <li key={i}>{inline(line.replace(/^\d+\. /, ""))}</li>
              ))}
            </ol>
          );
        return <p key={index}>{inline(block)}</p>;
      })}
    </div>
  );
}

export default function ChatSourceEvidence({
  context,
  citation,
  onClose,
}: {
  context?: ChatSourceContext;
  citation: SourcePassage | null;
  onClose: () => void;
}) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    close.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("keydown", escape);
      previous?.focus();
    };
  }, [onClose]);
  const passages = citation ? [citation] : context?.passages || [];
  return (
    <aside
      aria-label="Source evidence"
      className="fixed inset-0 z-50 flex flex-col bg-[var(--tape-paper)] text-[var(--tape-ink)] lg:static lg:z-auto lg:w-96 lg:shrink-0 lg:border-l lg:border-[var(--tape-line)]"
    >
      <div className="flex items-start justify-between gap-3 border-b border-[var(--tape-line)] p-4">
        <div className="min-w-0">
          <h3 className="font-semibold">
            {citation ? "Cited passage" : "Source evidence"}
          </h3>
          <p className="mt-1 break-words text-sm text-[var(--tape-activity)]">
            {citation ? "Evidence retained with this answer" : context?.title}
          </p>
        </div>
        <button
          ref={close}
          onClick={onClose}
          aria-label="Close source evidence"
          className="rounded p-2 hover:bg-[var(--tape-chip)]"
        >
          <X className="h-5 w-5" />
        </button>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {!citation && context && (
          <p className="text-xs text-[var(--tape-activity)]">
            {context.coverage}
          </p>
        )}
        {passages.map((p) => (
          <article
            key={p.id}
            className="border-b border-[var(--tape-line)] pb-4"
          >
            <p className="mb-2 text-xs text-[var(--tape-activity)]">
              [{p.id}] {p.label}
            </p>
            <p className="whitespace-pre-wrap break-words text-sm leading-6">
              {p.text}
            </p>
            <Link
              className="mt-2 inline-flex min-h-10 items-center gap-1 text-sm text-[var(--tape-focus)] underline"
              to={p.url}
            >
              Open source <ArrowUpRight className="h-3.5 w-3.5" />
            </Link>
          </article>
        ))}
      </div>
    </aside>
  );
}
