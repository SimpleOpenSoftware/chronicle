// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SourceSearch from "./SourceSearch";
import ContextRefreshes from "./ContextRefreshes";
import { api } from "../services/api";
import { sourceDate } from "../utils/sourceTime";

function show(ui: React.ReactNode, url = "/recordings") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[url]}>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const result = (title: string, timed = false) => ({
  data: {
    total: 1,
    indexing: { initialized: true },
    items: [
      {
        kind: "recording",
        key: "intro",
        title,
        url: "/recordings/intro",
        excerpt: "A workshop about pottery",
        highlights: ["Pottery"],
        started_at: null,
        match_start: timed ? 20 : null,
        match_end: timed ? 30 : null,
      },
    ],
  },
});

describe("source discovery", () => {
  it("defaults to all source types and resets pagination when narrowing scope", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(result("Workshop") as never);
    show(<SourceSearch />, "/recordings?q=pottery&offset=20&fields=summary");
    await screen.findByText("Workshop");
    expect(screen.getByRole("combobox", { name: "Search scope" })).toHaveValue("recording,episode,session");
    expect(get.mock.calls[0][1]?.params.kinds).toEqual(["recording", "episode", "session"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Search scope" }), { target: { value: "episode" } });
    await waitFor(() => expect(get).toHaveBeenLastCalledWith("/api/search", expect.objectContaining({ params: expect.objectContaining({ kinds: ["episode"], fields: ["summary"], offset: 0 }) })));
  });
  it("labels episodes and sessions separately and preserves their own destinations", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ data: { total: 2, indexing: { initialized: true }, items: [
      { ...result("Workshop").data.items[0], kind: "episode", key: "episode-1", title: "Clay shaping", url: "/timeline/episode-1" },
      { ...result("Workshop").data.items[0], kind: "session", key: "session-1", title: "Pottery afternoon", url: "/timeline?date=2026-09-04&session=session-1" },
    ] } } as never);
    show(<SourceSearch />, "/recordings?q=pottery");
    expect(await screen.findByRole("link", { name: /Episode.*Clay shaping/ })).toHaveAttribute("href", "/timeline/episode-1");
    expect(screen.getByRole("link", { name: /Conversation.*Pottery afternoon/ })).toHaveAttribute("href", "/timeline?date=2026-09-04&session=session-1");
  });
  it("shows the matching passage instead of clipping it behind unrelated opening text", async () => {
    const response = result("Workshop");
    response.data.items[0].excerpt = `${"Opening remarks. ".repeat(60)}We learned pottery techniques today.`;
    vi.spyOn(api, "get").mockResolvedValue(response as never);
    show(<SourceSearch />, "/recordings?q=pottery");
    const link = await screen.findByRole("link", { name: /Workshop/ });
    expect(link.querySelector("mark")).toHaveTextContent("pottery");
    expect(link.textContent).toContain("techniques today");
    expect(link.textContent!.length).toBeLessThan(500);
  });
  it("keeps field controls behind a disclosure and handles an empty selection", async () => {
    const get = vi
      .spyOn(api, "get")
      .mockResolvedValue(result("Workshop") as never);
    show(<SourceSearch />, "/recordings?q=pottery&fields=transcript&offset=20");
    await screen.findByText("Workshop");
    const disclosure = screen.getByRole("button", { name: /Filters/ });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    fireEvent.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("checkbox", { name: "Transcript" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Select a field to search.",
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Titles" }));
    await waitFor(() =>
      expect(get).toHaveBeenLastCalledWith(
        "/api/search",
        expect.objectContaining({
          params: expect.objectContaining({ fields: ["title"], offset: 0 }),
        }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(screen.queryByText("Workshop")).not.toBeInTheDocument();
  });
  it("preserves UTC source timestamps in a browser using IST", () => {
    expect(sourceDate("2026-06-13T05:56:23.917").toISOString()).toBe(
      "2026-06-13T05:56:23.917Z",
    );
    expect(sourceDate("2026-06-13T11:26:23.917+05:30").toISOString()).toBe(
      "2026-06-13T05:56:23.917Z",
    );
  });
  it("debounces typing, cancels obsolete requests and rejects stale results", async () => {
    let resolveOld!: (value: unknown) => void;
    const get = vi.spyOn(api, "get").mockImplementation((_url, config) =>
      config?.params.q === "intro"
        ? (new Promise((resolve) => {
            resolveOld = resolve;
          }) as never)
        : (Promise.resolve(result("Current match")) as never),
    );
    show(<SourceSearch />, "/recordings?q=intro");
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    const oldSignal = get.mock.calls[0][1]?.signal as AbortSignal;
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "pottery" },
    });
    expect(get).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Searching");
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2), {
      timeout: 2000,
    });
    expect(oldSignal.aborted).toBe(true);
    resolveOld(result("Obsolete match"));
    expect(await screen.findByText("Current match")).toBeVisible();
    expect(screen.queryByText("Obsolete match")).not.toBeInTheDocument();
  });
  it("preserves selected fields and navigation without inventing untimed seeks", async () => {
    const get = vi
      .spyOn(api, "get")
      .mockResolvedValue(result("Introduction") as never);
    show(
      <SourceSearch />,
      "/recordings?q=pottery&fields=transcript&types=recording,session&offset=20",
    );
    const link = await screen.findByRole("link", { name: /Introduction/ });
    expect(link).toHaveAttribute("href", "/recordings/intro");
    expect(get.mock.calls[0][1]?.params).toEqual(
      expect.objectContaining({
        fields: ["transcript"],
        kinds: ["recording", "session"],
        offset: 20,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /Filters/ }));
    expect(screen.getByRole("checkbox", { name: "Titles" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Transcript" })).toBeChecked();
  });
  it("seeks only when the source supplied a matching timed passage", async () => {
    vi.spyOn(api, "get").mockResolvedValue(
      result("Introduction", true) as never,
    );
    show(<SourceSearch />, "/recordings?q=pottery");
    expect(
      await screen.findByRole("link", { name: /Introduction/ }),
    ).toHaveAttribute("href", "/recordings/intro?start=20&end=30");
  });
  it("offers useful refreshes but generates only after explicit selection", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      data: {
        items: [
          {
            proposal_id: "p",
            session_key: "s",
            title: "Friday call",
            local_date: "2026-09-04",
            assessment: {
              verdict: "useful",
              reason: "Your introduction may answer this identity question.",
              relevant_paths: ["People/Pottery.md"],
            },
          },
        ],
      },
    } as never);
    const post = vi.spyOn(api, "post").mockResolvedValue({ data: {} } as never);
    show(<ContextRefreshes day="2026-09-04" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Review suggested refreshes" }),
    );
    expect(
      post.mock.calls.every((c) => c[0] === "/api/context-refreshes/check"),
    ).toBe(true);
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh this session" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/api/context-refreshes/p/refresh"),
    );
  });
});
