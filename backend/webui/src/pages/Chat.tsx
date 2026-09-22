import { useEffect, useRef, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  chatApi,
  memorySpacesApi,
  ChatSourceRef,
  ChatSourceContext,
  SourcePassage,
} from "../services/api";
import { useChatSessions, useChatMessages } from "../hooks/useChat";
import ChatRunViewer, { ChatRun } from "../components/ChatRunViewer";
import { CitedSourceText } from "../components/ChatSourceEvidence";
import ChatImages from "../components/ChatImages";
import ChatDialog from "../components/ChatDialog";
import ChatSourcePicker from "../components/ChatSourcePicker";
import ChatSaveReview from "../components/ChatSaveReview";
import DialogueTasks from "../components/DialogueTasks";
import { refKey } from "../components/SourceResults";
import { sourceDate } from "../utils/sourceTime";

interface VaultNote {
  id: string;
  path: string;
  title: string;
  text: string;
  revision: string;
  coverage: string;
}
interface Evidence {
  conversations: ChatSourceContext[];
  vault_notes: VaultNote[];
  retrievals: { query: string; coverage: string }[];
}
interface Message {
  message_id: string;
  role: string;
  content: string;
  timestamp: string;
  run_id?: string;
  evidence?: Evidence;
  source_citations?: SourcePassage[];
  source_coverage?: string;
  memories_used?: string[];
}
const stamp = (value: string) =>
  sourceDate(value).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }) + " IST";
const button =
  "min-h-10 rounded px-3 py-2 text-sm hover:bg-[var(--tape-chip)] disabled:opacity-50";
const failure = (e: any) =>
  e.response?.data?.detail ||
  e.message ||
  "Something went wrong. Please retry.";
function citations(m: Message): SourcePassage[] {
  return [
    ...(m.source_citations || []),
    ...(m.evidence?.conversations.flatMap((c) => c.passages) || []),
    ...(m.evidence?.vault_notes.map((n) => ({
      id: n.id,
      label: n.title,
      text: n.text,
      revision: n.revision,
      url: "",
    })) || []),
  ];
}

export default function Chat() {
  const [params, setParams] = useSearchParams();
  const sid = params.get("session"),
    space = params.get("memory_space_id") || undefined;
  const cache = useQueryClient(),
    sessions = useChatSessions(space);
  const sessionQuery = useQuery({
    queryKey: ["chat", "session", sid],
    queryFn: () => chatApi.getSession(sid!).then((r) => r.data),
    enabled: !!sid,
  });
  const session = sessionQuery.data,
    destinationSpace = session?.memory_space_id || space;
  const spaceQuery = useQuery({
    queryKey: ["memory-space", destinationSpace],
    queryFn: () => memorySpacesApi.get(destinationSpace!).then((r) => r.data),
    enabled: !!destinationSpace,
  });
  const destination = destinationSpace
    ? spaceQuery.data?.name || "Memory Space"
    : "Main";
  const historical = !!session && session.interaction_version !== 2;
  const [draft, setDraft] = useState(""),
    [stream, setStream] = useState(""),
    [status, setStatus] = useState(""),
    [error, setError] = useState("");
  const [sending, setSending] = useState(false),
    [picker, setPicker] = useState(false),
    [review, setReview] = useState(false),
    [historyOpen, setHistoryOpen] = useState(false),
    [attachmentBusy, setAttachmentBusy] = useState(false);
  const [runId, setRunId] = useState<string | null>(null),
    [activeRun, setActiveRun] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Message | null>(null),
    [preview, setPreview] = useState<ChatSourceContext | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null),
    [elapsed, setElapsed] = useState(0);
  const controller = useRef<AbortController | null>(null),
    end = useRef<HTMLDivElement>(null),
    input = useRef<HTMLTextAreaElement>(null);
  const queryMessages = useChatMessages(sid),
    messages: Message[] = queryMessages.data || [];
  const refs: ChatSourceRef[] = session?.sources || [];
  const sourceQuery = useQuery({
    queryKey: ["chat", "sources", sid, refs],
    queryFn: () => chatApi.getSources(sid!).then((r) => r.data.sources),
    enabled: !!sid && !historical && refs.length > 0,
    retry: false,
  });
  const historicalSource = useQuery({
    queryKey: ["chat", "source", sid],
    queryFn: () => chatApi.getSource(sid!).then((r) => r.data),
    enabled: historical && !!session?.source,
    retry: false,
  });
  const runs = useQuery({
    queryKey: ["chat", "runs", sid],
    queryFn: () => chatApi.getRuns(sid!).then((r) => r.data),
    enabled: !!sid,
    refetchInterval: (q) =>
      sending || q.state.data?.some((r: ChatRun) => r.status === "running")
        ? 1500
        : false,
  });
  const externalReply =
    runs.data?.some(
      (r: ChatRun) =>
        r.status === "running" && !r.question.startsWith("Review and save:"),
    ) || false;
  const replying = sending || externalReply;
  const previousExternalReply = useRef(false);
  useEffect(() => {
    if (previousExternalReply.current && !externalReply && sid) {
      void cache.invalidateQueries({ queryKey: ["chat", "messages", sid] });
    }
    previousExternalReply.current = externalReply;
  }, [externalReply, sid, cache]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [sid, messages[messages.length - 1]?.message_id]);
  useEffect(() => {
    if (sending) end.current?.scrollIntoView({ block: "nearest" });
  }, [stream, sending]);
  useEffect(() => {
    if (!startedAt) return;
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [startedAt]);
  const navigateChat = (id?: string) => {
    if (replying || attachmentBusy) return;
    const next = new URLSearchParams();
    if (space) next.set("memory_space_id", space);
    if (id) next.set("session", id);
    setParams(next);
    setDraft("");
    setError("");
    setStream("");
    setHistoryOpen(false);
    setActiveRun(null);
    setEvidence(null);
    setPreview(null);
    requestAnimationFrame(() => input.current?.focus());
  };
  const ensureSession = async () => {
    if (sid) return sid;
    const { data } = await chatApi.createSession(undefined, [], space);
    cache.setQueryData(["chat", "session", data.session_id], data);
    const next = new URLSearchParams(params);
    next.set("session", data.session_id);
    setParams(next);
    void cache.invalidateQueries({ queryKey: ["chat", "sessions"] });
    return data.session_id as string;
  };
  const setSources = async (sources: ChatSourceRef[]) => {
    setAttachmentBusy(true);
    try {
      const id = await ensureSession();
      const { data } = await chatApi.setSources(id, sources);
      cache.setQueryData(["chat", "session", id], data);
      await cache.invalidateQueries({ queryKey: ["chat", "sources", id] });
    } finally {
      setAttachmentBusy(false);
    }
  };
  const send = async () => {
    if (
      !draft.trim() ||
      replying ||
      historical ||
      sourceQuery.isError ||
      sourceQuery.isFetching ||
      attachmentBusy
    )
      return;
    const text = draft.trim();
    setSending(true);
    setError("");
    setStream("");
    setActiveRun(null);
    setStatus("Thinking…");
    setStartedAt(Date.now());
    setElapsed(0);
    let id = sid,
      completed = false;
    try {
      id = await ensureSession();
      controller.current = new AbortController();
      const response = await chatApi.sendMessage(
        text,
        id!,
        controller.current.signal,
      );
      if (!response.ok) {
        const body = await response.json();
        throw new Error(body.detail || "Could not send your message");
      }
      const reader = response.body!.getReader(),
        decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ") || line.slice(6).trim() === "[DONE]")
            continue;
          const chunk = JSON.parse(line.slice(6)),
            meta = chunk.chronicle_metadata || {};
          if (meta.run_id) setActiveRun(meta.run_id);
          if (meta.dialogue) cache.setQueryData(["chat", "dialogue", id], meta.dialogue);
          if (chunk.error)
            throw new Error(chunk.error.message || "Reply failed");
          if (meta.error) throw new Error(meta.error);
          if (meta.reset_content) setStream("");
          if (meta.status)
            setStatus(
              (
                {
                  thinking: "Thinking…",
                  searching: "Reading relevant vault notes…",
                  searched: meta.status.failed
                    ? "Vault retrieval failed; checking available evidence…"
                    : "Reviewing the evidence…",
                  writing: "Writing…",
                } as Record<string, string>
              )[meta.status.stage] || "Working…",
            );
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) setStream((previous) => previous + delta);
          if (chunk.choices?.[0]?.finish_reason === "stop") completed = true;
        }
        if (done) break;
      }
      if (!completed)
        throw new Error(
          "The reply ended before completion. Inspect its run, then retry.",
        );
      setDraft("");
    } catch (e: any) {
      setError(
        e.name === "AbortError"
          ? "Reply stopped. Your draft is preserved."
          : failure(e),
      );
    } finally {
      if (id)
        await Promise.all([
          cache.invalidateQueries({ queryKey: ["chat", "messages", id] }),
          cache.invalidateQueries({ queryKey: ["chat", "runs", id] }),
          cache.invalidateQueries({ queryKey: ["chat", "session", id] }),
          cache.invalidateQueries({ queryKey: ["chat", "sessions"] }),
        ]);
      setSending(false);
      setStartedAt(null);
      setStatus("");
      if (completed) setStream("");
    }
  };
  const history = (
    <div className="space-y-1">
      {sessions.data?.map((s: any) => (
        <button
          disabled={replying || attachmentBusy}
          key={s.session_id}
          onClick={() => navigateChat(s.session_id)}
          className={`block w-full rounded p-3 text-left ${s.session_id === sid ? "bg-[var(--tape-chip)]" : "hover:bg-[var(--tape-paper-raised)]"}`}
        >
          <span className="line-clamp-2 text-sm font-medium">{s.title}</span>
          <span className="mt-1 block text-xs text-[var(--tape-activity)]">
            {stamp(s.updated_at)}
            {s.interaction_version !== 2 ? " · Read-only" : ""}
          </span>
        </button>
      ))}
      {sessions.isLoading && <p>Loading chats…</p>}
      {sessions.isError && (
        <div role="alert">
          <p>Chats could not be loaded. Please try again.</p>
          <button disabled={sessions.isFetching} onClick={() => void sessions.refetch()}>
            {sessions.isFetching ? "Loading chats…" : "Retry loading chats"}
          </button>
        </div>
      )}
    </div>
  );
  const renderEvidence = (m: Message) => (
    <>
      {m.evidence?.conversations.map((c) => (
        <section key={refKey(c.ref)} className="mb-5">
          <h3 className="font-medium">{c.title}</h3>
          <p className="my-2 text-xs text-[var(--tape-activity)]">
            {c.coverage}
          </p>
          {!c.passages.length && (
            <p className="text-sm">
              This conversation was available, but this reply did not cite a
              specific passage.
            </p>
          )}
          {c.passages.map((p) => (
            <div key={p.id} className="my-3">
              <p className="text-xs">{p.label}</p>
              <p className="whitespace-pre-wrap break-words text-sm">
                {p.text}
              </p>
              <Link to={p.url} className="inline-block py-2 text-sm underline">
                Open source
              </Link>
            </div>
          ))}
        </section>
      ))}
      {!!m.evidence?.vault_notes.length && (
        <h3 className="mb-2 font-medium">Vault notes consulted</h3>
      )}
      {m.evidence?.vault_notes.map((n) => (
        <details key={n.id} className="mb-3">
          <summary className="cursor-pointer py-2">{n.title}</summary>
          <p className="mb-2 text-xs text-[var(--tape-activity)]">
            {n.path} · {n.coverage}
          </p>
          <p className="whitespace-pre-wrap break-words text-sm">{n.text}</p>
        </details>
      ))}
      {m.evidence?.retrievals.map((r, i) => (
        <p key={i} className="my-2 text-xs text-[var(--tape-activity)]">
          {r.coverage}
        </p>
      ))}
      {m.source_citations?.map((p) => (
        <div key={p.id} className="mb-4">
          <p className="text-sm">{p.label}</p>
          <p className="whitespace-pre-wrap break-words">{p.text}</p>
          <Link className="underline" to={p.url}>
            Open source
          </Link>
        </div>
      ))}
      <ChatImages
        memoriesUsed={
          m.evidence?.vault_notes.map((n) => n.path) || m.memories_used || []
        }
      />
    </>
  );
  return (
    <div className="flex h-[calc(100dvh-10rem)] min-h-[28rem] min-w-0 text-[var(--tape-ink)] md:h-[calc(100dvh-12rem)]">
      <aside className="hidden w-64 shrink-0 overflow-y-auto border-r border-[var(--tape-line)] pr-3 md:block">
        <div className="mb-3 flex items-center justify-between">
          <h1 className="font-semibold">Chats</h1>
          <button
            className={button}
            disabled={replying}
            onClick={() => navigateChat()}
          >
            New chat
          </button>
        </div>
        {history}
      </aside>
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--tape-line)] px-2 pb-3 md:px-5">
          <div className="min-w-0">
            <h2 className="line-clamp-2 font-semibold">
              {session?.title || "Chat"}
            </h2>
            <p className="text-xs text-[var(--tape-activity)]">
              Vault: {destination}
            </p>
          </div>
          <div className="flex flex-wrap gap-1">
            <button
              className={`${button} md:hidden`}
              disabled={replying}
              onClick={() => setHistoryOpen(true)}
            >
              Chats
            </button>
            <button
              className={`${button} md:hidden`}
              disabled={replying}
              onClick={() => navigateChat()}
            >
              New chat
            </button>
            {sid && !historical && (
              <button
                className={button}
                disabled={
                  replying || !messages.some((m) => m.role === "assistant")
                }
                onClick={() => setReview(true)}
              >
                Review and save
              </button>
            )}
          </div>
        </header>
        {sessionQuery.isError && (
          <p role="alert" className="p-3">
            This chat is unavailable.{" "}
            <button
              className="underline"
              onClick={() => void sessionQuery.refetch()}
            >
              Retry
            </button>
          </p>
        )}
        {historical && (
          <div className="border-b border-[var(--tape-line)] p-3 text-sm">
            This historical chat is read-only. Its messages, sources, and runs
            are preserved.{" "}
            <button className="underline" onClick={() => navigateChat()}>
              Start a new chat
            </button>
            {historicalSource.data && (
              <button
                className={`${button} underline`}
                onClick={() => setPreview(historicalSource.data)}
              >
                View attached conversation
              </button>
            )}
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-4 md:px-5">
          {!messages.length && !sid && (
            <div className="mx-auto max-w-xl py-8">
              <h3 className="text-xl font-medium">
                What would you like to think through?
              </h3>
              <p className="mt-3 text-[var(--tape-activity)]">
                Ask about your life, or add conversations to focus on what
                happened. Chronicle can read relevant vault notes for
                background.
              </p>
            </div>
          )}
          {queryMessages.hasNextPage && (
            <button
              className={`${button} underline`}
              disabled={queryMessages.isFetchingNextPage}
              onClick={() => void queryMessages.fetchNextPage()}
            >
              {queryMessages.isFetchingNextPage
                ? "Loading earlier messages…"
                : "Load earlier messages"}
            </button>
          )}
          {queryMessages.isLoading && <p role="status">Loading discussion…</p>}
          {queryMessages.isError && (
            <p role="alert">
              Could not load messages.{" "}
              <button onClick={() => void queryMessages.refetch()}>
                Retry
              </button>
            </p>
          )}
          {messages.map((message, index) => (
            <div key={message.message_id}>
              {(session?.context_changes || [])
                .filter(
                  (c: any) =>
                    sourceDate(c.at) <= sourceDate(message.timestamp) &&
                    (index === 0 ||
                      sourceDate(c.at) >
                        sourceDate(messages[index - 1].timestamp)),
                )
                .map((c: any) => (
                  <p
                    key={c.at}
                    className="my-4 border-t border-[var(--tape-line)] pt-3 text-center text-xs text-[var(--tape-activity)]"
                  >
                    Context changed · {c.sources.length} conversations attached
                    · {stamp(c.at)}
                  </p>
                ))}
              <article
                className={`mb-5 rounded-lg p-3 ${message.role === "user" ? "ml-auto max-w-[90%] bg-[var(--tape-chip)] md:max-w-[80%]" : "bg-[var(--tape-paper-raised)]"}`}
              >
                <p className="mb-2 text-xs text-[var(--tape-activity)]">
                  {message.role === "user" ? "You" : "Chronicle"} ·{" "}
                  {stamp(message.timestamp)}
                </p>
                <CitedSourceText
                  text={message.content}
                  citations={citations(message)}
                  onSelect={() => setEvidence(message)}
                />
                <ChatImages
                  memoriesUsed={
                    message.evidence?.vault_notes.map((n) => n.path) ||
                    message.memories_used ||
                    []
                  }
                />
                {message.source_coverage && (
                  <p className="mt-2 text-xs">{message.source_coverage}</p>
                )}
                <div className="mt-2 flex flex-wrap gap-2">
                  {(message.evidence || !!message.source_citations?.length) && (
                    <button
                      className={`${button} underline`}
                      onClick={() => setEvidence(message)}
                    >
                      Sources
                    </button>
                  )}
                  {message.run_id && (
                    <button
                      className={`${button} underline`}
                      onClick={() => setRunId(message.run_id!)}
                    >
                      View run
                    </button>
                  )}
                </div>
              </article>
            </div>
          ))}
          {(session?.context_changes || [])
            .filter(
              (c: any) =>
                !messages.length ||
                sourceDate(c.at) >
                  sourceDate(messages[messages.length - 1].timestamp),
            )
            .map((c: any) => (
              <p
                key={c.at}
                className="my-3 text-center text-xs text-[var(--tape-activity)]"
              >
                Context changed · {c.sources.length} conversations attached ·{" "}
                {stamp(c.at)}
              </p>
            ))}
          {sid && !historical && <>
            <DialogueTasks sessionId={sid} />
            <Link className={`${button} inline-block underline`} to={`/live-record?thread=${encodeURIComponent(sid)}${session?.memory_space_id ? `&memory_space_id=${encodeURIComponent(session.memory_space_id)}` : ""}`}>Continue by voice</Link>
          </>}
          {externalReply && !sending && (
            <p role="status" className="my-3 text-sm">
              A reply is running. You can inspect its progress in Run history.
            </p>
          )}
          {sending && (
            <p role="status" className="my-3 text-sm">
              {status} · {elapsed}s{" "}
              <button
                className="underline"
                onClick={() => controller.current?.abort()}
              >
                Stop
              </button>
            </p>
          )}
          {stream && (
            <div className="mb-4 whitespace-pre-wrap break-words p-3">
              {stream}
            </div>
          )}
          {error && (
            <div
              role="alert"
              className="my-3 text-sm text-red-700 dark:text-red-300"
            >
              {error}
              {activeRun && (
                <button
                  className={`${button} underline`}
                  onClick={() => setRunId(activeRun)}
                >
                  View run
                </button>
              )}
            </div>
          )}
          <div ref={end} />
        </div>
        {!!runs.data?.length && (
          <details className="border-t border-[var(--tape-line)] px-3 text-xs">
            <summary className="cursor-pointer py-2">
              Run history · {runs.data.length}
            </summary>
            <div className="max-h-40 overflow-auto">
              {runs.data.map((r: ChatRun) => (
                <button
                  key={r.run_id}
                  className="block w-full py-2 text-left underline"
                  onClick={() => setRunId(r.run_id)}
                >
                  {r.status} · {r.question} · {stamp(r.started_at)}
                </button>
              ))}
            </div>
          </details>
        )}
        {!historical && (
          <div className="border-t border-[var(--tape-line)] px-2 pt-3 md:px-5">
            {!!refs.length && (
              <details
                open={
                  typeof window.matchMedia === "function"
                    ? window.matchMedia("(min-width: 768px)").matches
                    : true
                }
                className="mb-2"
              >
                <summary className="cursor-pointer text-sm">
                  Discussing {refs.length}{" "}
                  {refs.length === 1 ? "conversation" : "conversations"}
                </summary>
                <div className="mt-2 flex max-h-32 flex-wrap gap-2 overflow-y-auto">
                  {refs.map((ref) => {
                    const context = sourceQuery.data?.find(
                      (c) => refKey(c.ref) === refKey(ref),
                    );
                    return (
                      <div
                        key={refKey(ref)}
                        className="flex max-w-full items-center rounded border border-[var(--tape-line)] text-xs"
                      >
                        <button
                          className="min-h-10 min-w-0 break-words px-2 text-left underline"
                          disabled={!context}
                          onClick={() => setPreview(context!)}
                        >
                          {context?.title ||
                            `${ref.kind} · ${ref.key.slice(0, 8)}`}
                        </button>
                        <button
                          className="min-h-10 px-3"
                          disabled={replying || attachmentBusy}
                          aria-label={`Remove ${context?.title || ref.kind}`}
                          onClick={() =>
                            void setSources(
                              refs.filter((r) => refKey(r) !== refKey(ref)),
                            ).catch((e) => setError(failure(e)))
                          }
                        >
                          ×
                        </button>
                      </div>
                    );
                  })}
                </div>
              </details>
            )}
            {sourceQuery.isFetching && (
              <p role="status" className="mb-2 text-xs">
                Loading conversation evidence…
              </p>
            )}
            {sourceQuery.isError && (
              <p role="alert" className="mb-2 text-sm">
                An attached conversation is unavailable.{" "}
                <button
                  className="underline"
                  onClick={() => void sourceQuery.refetch()}
                >
                  Retry
                </button>{" "}
                or remove it before sending.
              </p>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void send();
              }}
            >
              <label className="sr-only" htmlFor="chat-message">
                Chat message
              </label>
              <div className="flex gap-2">
                <textarea
                  ref={input}
                  id="chat-message"
                  rows={2}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                  placeholder={
                    refs.length
                      ? "Ask about these conversations…"
                      : "Ask a question…"
                  }
                  className="min-w-0 flex-1 resize-y rounded border border-[var(--tape-line)] bg-transparent p-3"
                />
                <button
                  disabled={
                    !draft.trim() ||
                    replying ||
                    attachmentBusy ||
                    sourceQuery.isFetching ||
                    sourceQuery.isError ||
                    (!!sid && !session)
                  }
                  className="rounded bg-[var(--tape-focus)] px-3 text-white disabled:opacity-50"
                  type="submit"
                >
                  Send
                </button>
              </div>
            </form>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <button
                className={`${button} underline`}
                disabled={replying || attachmentBusy || (!!sid && !session)}
                onClick={() => setPicker(true)}
              >
                Add conversations
              </button>
              <p className="text-xs text-[var(--tape-activity)]">
                Relevant vault notes are available for background.
              </p>
            </div>
          </div>
        )}
      </main>
      {historyOpen && (
        <ChatDialog title="Chats" onClose={() => setHistoryOpen(false)}>
          {history}
        </ChatDialog>
      )}
      {picker && (
        <ChatSourcePicker
          selected={refs}
          space={destinationSpace}
          onSave={setSources}
          onClose={() => setPicker(false)}
        />
      )}
      {review && sid && (
        <ChatSaveReview
          sessionId={sid}
          destination={destination}
          onClose={() => setReview(false)}
          viewRun={(id) => {
            setReview(false);
            setRunId(id);
          }}
        />
      )}
      {evidence && (
        <ChatDialog title="Sources" onClose={() => setEvidence(null)}>
          {renderEvidence(evidence)}
        </ChatDialog>
      )}
      {preview && (
        <ChatDialog title={preview.title} onClose={() => setPreview(null)}>
          <p className="mb-3 text-sm">{preview.coverage}</p>
          <Link className="underline" to={preview.url}>
            Open conversation
          </Link>
          {preview.passages.map((p) => (
            <p
              key={p.id}
              className="my-3 whitespace-pre-wrap break-words text-sm"
            >
              {p.text}
            </p>
          ))}
        </ChatDialog>
      )}
      {runId && sid && (
        <ChatRunViewer
          sessionId={sid}
          runId={runId}
          onClose={() => setRunId(null)}
        />
      )}
    </div>
  );
}
