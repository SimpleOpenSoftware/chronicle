// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Chat from "./Chat";
import AskAboutSource from "../components/AskAboutSource";
import { CitedSourceText } from "../components/ChatSourceEvidence";
import { api, chatApi, ChatSourceContext } from "../services/api";

const ref = {
  kind: "recording" as const,
  key: "recording-a",
  local_date: null,
  timezone: "Asia/Kolkata",
};
const session = {
  session_id: "chat-a",
  title: "Therapy session",
  sources: [ref],
  interaction_version: 2,
  updated_at: "2026-09-04T07:00:00Z",
  context_changes: [],
};
const context: ChatSourceContext = {
  ref,
  title: "Therapy session",
  started_at: null,
  revision: "r1",
  coverage: "All available source passages included.",
  total_passages: 1,
  url: "/recordings/recording-a",
  passages: [
    {
      id: "C123_S1",
      text: "Ankush: I will journal daily.",
      label: "Ankush · 0:10",
      url: "/recordings/recording-a?start=10&end=20",
      revision: "v1",
    },
  ],
};
const message = {
  message_id: "m1",
  role: "assistant",
  content: "Journal daily [C123_S1]",
  timestamp: "2026-09-04T07:00:00Z",
  evidence: { conversations: [context], vault_notes: [], retrievals: [] },
  run_id: "run-a",
};

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  vi.spyOn(chatApi, "getRuns").mockResolvedValue({ data: [] } as never);
  vi.spyOn(chatApi, "getSessions").mockResolvedValue({
    data: [session],
  } as never);
  vi.spyOn(chatApi, "getSession").mockResolvedValue({ data: session } as never);
  vi.spyOn(chatApi, "getMessages").mockResolvedValue({ data: [] } as never);
  vi.spyOn(chatApi, "getSources").mockResolvedValue({
    data: { sources: [context] },
  } as never);
  vi.spyOn(chatApi, "getSaveProposal").mockResolvedValue({
    data: null,
  } as never);
  vi.spyOn(api, "get").mockResolvedValue({
    data: {
      items: [
        {
          ...context.ref,
          title: context.title,
          url: context.url,
          started_at: null,
          excerpt: "Journal daily",
          highlights: [],
        },
      ],
      total: 1,
      indexing: { initialized: true },
    },
  } as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function mount(ui: React.ReactNode = <Chat />, url = "/chat?session=chat-a") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[url]}>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}
function response(chunks: unknown[]) {
  return {
    ok: true,
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            chunks.map((c) => "data: " + JSON.stringify(c) + "\n\n").join("") +
              "data: [DONE]\n\n",
          ),
        );
        controller.close();
      },
    }),
  } as Response;
}

it("offers an immediate composer and source picker without creating a chat on navigation", () => {
  const create = vi.spyOn(chatApi, "createSession");
  mount(<Chat />, "/chat");
  expect(screen.getByRole("textbox", { name: "Chat message" })).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Add conversations" }),
  ).toBeVisible();
  expect(screen.queryByRole("slider")).not.toBeInTheDocument();
  expect(screen.queryByText("Search options")).not.toBeInTheDocument();
  expect(create).not.toHaveBeenCalled();
});

it("opens a selected chat, keeps its draft through evidence preview, and shows its destination", async () => {
  mount();
  const draft = await screen.findByRole("textbox", { name: "Chat message" });
  fireEvent.change(draft, { target: { value: "My next question" } });
  fireEvent.click(
    await screen.findByRole("button", { name: "Therapy session" }),
  );
  expect(
    await screen.findByText("Ankush: I will journal daily."),
  ).toBeVisible();
  fireEvent.click(
    screen.getByRole("button", { name: "Close Therapy session" }),
  );
  expect(draft).toHaveValue("My next question");
  expect(screen.getByText("Vault: Main")).toBeVisible();
});

it("keeps historical messages and evidence readable with no send or save controls", async () => {
  vi.mocked(chatApi.getSession).mockResolvedValue({
    data: { ...session, interaction_version: undefined, source: ref },
  } as never);
  vi.mocked(chatApi.getMessages).mockResolvedValue({
    data: [
      { ...message, evidence: undefined, source_citations: context.passages },
    ],
  } as never);
  mount();
  expect(
    await screen.findByText(/This historical chat is read-only/),
  ).toBeVisible();
  expect(
    screen.queryByRole("textbox", { name: "Chat message" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Review and save" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Sources" }));
  expect(
    await screen.findByText("Ankush: I will journal daily."),
  ).toBeVisible();
});

it("retains source citations and vault evidence after loading saved messages", async () => {
  vi.mocked(chatApi.getMessages).mockResolvedValue({
    data: [
      {
        ...message,
        evidence: {
          ...message.evidence,
          vault_notes: [
            {
              id: "Vabc",
              path: "People/Alex.md",
              title: "Alex",
              text: "Background note",
              coverage: "Full retrieved note",
              revision: "r2",
            },
          ],
        },
      },
    ],
  } as never);
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: /Read source C123_S1/ }),
  );
  expect(screen.getByRole("dialog", { name: "Sources" })).toBeVisible();
  expect(screen.getByText("Ankush: I will journal daily.")).toBeVisible();
  fireEvent.click(screen.getByText("Alex", { exact: true }));
  expect(screen.getByText("Background note")).toBeVisible();
});

it("renders each grouped namespaced citation", () => {
  const select = vi.fn(),
    second = { ...context.passages[0], id: "C456_S2" };
  render(
    <CitedSourceText
      text="**Agreed** [C123_S1, C456_S2]"
      citations={[context.passages[0], second]}
      onSelect={select}
    />,
  );
  expect(screen.getByText("Agreed").tagName).toBe("STRONG");
  fireEvent.click(
    screen.getByRole("button", { name: "Read source C456_S2: Ankush · 0:10" }),
  );
  expect(select).toHaveBeenCalledWith(second);
});

it("opens the shared recent picker and saves an explicit empty attachment selection", async () => {
  const update = vi
    .spyOn(chatApi, "setSources")
    .mockResolvedValue({ data: { ...session, sources: [] } } as never);
  mount();
  await screen.findByRole("button", { name: "Therapy session" });
  fireEvent.click(screen.getByRole("button", { name: "Add conversations" }));
  expect(await screen.findByText(/Recent conversations/)).toBeVisible();
  fireEvent.click(
    await screen.findByRole("checkbox", { name: "Include in chat" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Use selected conversations" }),
  );
  await waitFor(() => expect(update).toHaveBeenCalledWith("chat-a", []));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
});

it("blocks sending on unavailable evidence but still permits removal", async () => {
  vi.mocked(chatApi.getSources).mockRejectedValue(new Error("unavailable"));
  mount();
  expect(
    await screen.findByText(/An attached conversation is unavailable/),
  ).toBeVisible();
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "What happened?" },
  });
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Remove recording" }),
  ).toBeEnabled();
});

it("preserves draft and displays the actual streamed backend error after narration reset", async () => {
  vi.spyOn(chatApi, "sendMessage").mockResolvedValue(
    response([
      { choices: [{ delta: { content: "Tool narration" } }] },
      { chronicle_metadata: { reset_content: true }, choices: [{ delta: {} }] },
      { error: { message: "Source changed during this turn" } },
    ]),
  );
  mount();
  await screen.findByRole("button", { name: "Therapy session" });
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "What happened?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Source changed during this turn",
  );
  expect(screen.getByRole("textbox")).toHaveValue("What happened?");
  expect(screen.queryByText("Tool narration")).not.toBeInTheDocument();
});

it("previews the whole chat and applies only selected note changes", async () => {
  vi.mocked(chatApi.getMessages).mockResolvedValue({
    data: [message],
  } as never);
  const proposal = {
    proposal_id: "p1",
    generation: "g1",
    state: "pending",
    messages: [
      { message_id: "m1", role: "user", content: "A whole-chat statement" },
    ],
    changes: [
      {
        change_id: "c1",
        note_path: "People/Alex.md",
        before_text: "Before",
        after_text: "After",
        summary: "Update note",
      },
    ],
  };
  vi.mocked(chatApi.getSaveProposal).mockResolvedValue({
    data: proposal,
  } as never);
  const decide = vi.spyOn(chatApi, "decideSaveProposal").mockResolvedValue({
    data: { ...proposal, state: "applied", applied_change_ids: ["c1"] },
  } as never);
  mount();
  const open = await screen.findByRole("button", { name: "Review and save" });
  await waitFor(() => expect(open).toBeEnabled());
  fireEvent.click(open);
  const checkbox = await screen.findByRole("checkbox", {
    name: "People/Alex.md",
  });
  expect(
    screen.getByRole("button", { name: "Save selected changes (0)" }),
  ).toBeDisabled();
  fireEvent.click(checkbox);
  fireEvent.click(
    screen.getByRole("button", { name: "Save selected changes (1)" }),
  );
  await waitFor(() =>
    expect(decide).toHaveBeenCalledWith(
      "chat-a",
      "p1",
      "g1",
      ["c1"],
      "approve",
    ),
  );
  expect(await screen.findByText("Selected changes saved")).toBeVisible();
});

it("source entry points create a new plural-source chat in the same space", async () => {
  const create = vi
    .spyOn(chatApi, "createSession")
    .mockResolvedValue({ data: session } as never);
  function Destination() {
    return <p>{useLocation().search}</p>;
  }
  mount(
    <Routes>
      <Route path="/recordings" element={<AskAboutSource source={ref} />} />
      <Route path="/chat" element={<Destination />} />
    </Routes>,
    "/recordings?memory_space_id=space-a",
  );
  fireEvent.click(screen.getByRole("button", { name: "Ask about this" }));
  expect(
    await screen.findByText("?session=chat-a&memory_space_id=space-a"),
  ).toBeVisible();
  expect(create).toHaveBeenCalledWith(undefined, [ref], "space-a");
});

it("keeps attachment changes and sending disabled when reloading a running reply", async () => {
  vi.mocked(chatApi.getRuns).mockResolvedValue({
    data: [
      {
        run_id: "busy-run",
        status: "running",
        question: "What did we agree?",
        started_at: "2026-09-14T02:00:00Z",
      },
    ],
  } as never);
  mount();
  expect(await screen.findByText(/A reply is running/)).toBeVisible();
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "A new draft" },
  });
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Add conversations" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Remove Therapy session" }),
  ).toBeDisabled();
});

it("loads earlier historical messages without losing the latest page", async () => {
  vi.mocked(chatApi.getSession).mockResolvedValue({
    data: { ...session, interaction_version: null, sources: [] },
  } as never);
  vi.mocked(chatApi.getMessages).mockImplementation(
    async (_sid, _limit, offset) =>
      ({
        data: offset
          ? [
              {
                ...message,
                message_id: "earliest",
                content: "Earliest retained message",
              },
            ]
          : Array.from({ length: 100 }, (_, i) => ({
              ...message,
              message_id: `recent-${i}`,
              content: `Recent message ${i}`,
            })),
      }) as never,
  );
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "Load earlier messages" }),
  );
  expect(await screen.findByText("Earliest retained message")).toBeVisible();
  expect(screen.getByText("Recent message 99")).toBeVisible();
  expect(chatApi.getMessages).toHaveBeenCalledWith("chat-a", 100, 100);
});

it("keeps headings, paragraphs, lists and citation buttons distinct without blank lines", () => {
  const { container } = render(
    <CitedSourceText
      text={
        "## Commitments\nOnly these are agreed:\n- Send the agenda [C123_S1]\n- Lunch is *optional*"
      }
      citations={context.passages}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("Commitments", { exact: true })).toHaveClass(
    "font-semibold",
  );
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(container.querySelector("em")).toHaveTextContent("optional");
  expect(
    screen.getByRole("button", { name: /Read source C123_S1/ }),
  ).toHaveTextContent("[1]");
});
