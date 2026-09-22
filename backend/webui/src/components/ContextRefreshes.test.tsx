// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import ContextRefreshes from "./ContextRefreshes";
import { api } from "../services/api";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const suggestions = [
  "Workshop",
  "Planning call",
  "Studio visit",
  "Book discussion",
].map((title, i) => ({
  proposal_id: `p${i}`,
  title,
  session_key: `s${i}`,
  local_date: "2026-09-04",
  correction_required: i === 1,
  assessment: {
    verdict: "useful",
    reason: "Accepted notes may answer an open question.",
    relevant_paths: ["Topics/Ceramics.md"],
  },
}));

function setup(items = suggestions) {
  vi.spyOn(api, "get").mockResolvedValue({ data: { items } });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const ui = (day: string) => (
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ContextRefreshes day={day} />
      </QueryClientProvider>
    </MemoryRouter>
  );
  return { ...render(ui("2026-09-04")), ui, client };
}

it("queues the displayed suggestions once, bounds concurrent requests and reports acceptance separately from generation", async () => {
  const finish: Array<() => void> = [];
  const post = vi
    .spyOn(api, "post")
    .mockImplementation((url) =>
      url.endsWith("/check")
        ? Promise.resolve({ data: {} })
        : new Promise((resolve) =>
            finish.push(() => resolve({ data: { state: "queued" } })),
          ),
    );
  const { client } = setup();
  const invalidation = vi.spyOn(client, "invalidateQueries");
  const button = await screen.findByRole("button", {
    name: "Refresh all suggested (4)",
  });
  expect(
    post.mock.calls.filter(([url]) => url.endsWith("/refresh")),
  ).toHaveLength(0);
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(() => expect(finish).toHaveLength(3));
  expect(button).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("0 of 4");
  await act(async () => finish[0]());
  await waitFor(() => expect(finish).toHaveLength(4));
  expect(screen.getByRole("status")).toHaveTextContent("1 of 4");
  await act(async () => finish.slice(1).forEach((resolve) => resolve()));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("4 refreshes queued"),
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    "still need your approval",
  );
  expect(
    post.mock.calls
      .filter(([url]) => url.endsWith("/refresh"))
      .map(([url]) => url),
  ).toEqual(
    suggestions.map(
      (item) => `/api/context-refreshes/${item.proposal_id}/refresh`,
    ),
  );
  await waitFor(() =>
    expect(invalidation).toHaveBeenCalledWith({
      queryKey: ["timeline-sessions"],
    }),
  );
  expect(
    screen.queryByRole("button", { name: /Refresh all/ }),
  ).not.toBeInTheDocument();
});

it("keeps partial failures visible and retries only unsuccessful suggestions", async () => {
  let fail = true;
  const post = vi
    .spyOn(api, "post")
    .mockImplementation((url) =>
      url === "/api/context-refreshes/p1/refresh" && fail
        ? Promise.reject({
            response: {
              data: { detail: "Sources changed. Reopen the session." },
            },
          })
        : Promise.resolve({ data: { state: "queued" } }),
    );
  setup();
  fireEvent.click(
    await screen.findByRole("button", { name: "Refresh all suggested (4)" }),
  );
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("3 refreshes queued"),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Planning call: Sources changed",
  );
  const retry = await screen.findByRole("button", {
    name: "Refresh all suggested (1)",
  });
  await waitFor(() => expect(retry).toBeEnabled());
  fail = false;
  fireEvent.click(retry);
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("4 refreshes queued"),
  );
  expect(
    post.mock.calls.filter(([url]) => url.endsWith("/refresh")),
  ).toHaveLength(5);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("resets batch feedback when navigating to another day and preserves individual correction actions", async () => {
  const post = vi.spyOn(api, "post").mockResolvedValue({ data: {} });
  const { rerender, ui } = setup([suggestions[1]]);
  fireEvent.click(
    await screen.findByRole("button", { name: "Review suggested refreshes" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Prepare correction proposal" }),
  );
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("1 refresh queued"),
  );
  expect(post).toHaveBeenCalledWith("/api/context-refreshes/p1/refresh");
  rerender(ui("2026-09-05"));
  await screen.findByRole("button", { name: "Refresh all suggested (1)" });
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Review suggested refreshes" }),
  ).toHaveAttribute("aria-expanded", "false");
});

it("fetches a separate suggestion scope when moving between recordings", async () => {
  vi.spyOn(api, "post").mockResolvedValue({ data: {} });
  const get = vi.spyOn(api, "get")
    .mockResolvedValueOnce({ data: { items: [{ ...suggestions[0], recording_id: "recording-a" }] } })
    .mockResolvedValueOnce({ data: { items: [{ ...suggestions[1], recording_id: "recording-b" }] } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (recordingId: string) => <MemoryRouter><QueryClientProvider client={client}>
    <ContextRefreshes memorySpace="space-a" recordingId={recordingId} />
  </QueryClientProvider></MemoryRouter>;
  const { rerender } = render(ui("recording-a"));
  fireEvent.click(await screen.findByRole("button", { name: "Review suggested refreshes" }));
  expect(screen.getByRole("link", { name: "Workshop" })).toBeVisible();
  expect(get).toHaveBeenCalledWith("/api/context-refreshes", {
    params: { local_date: undefined, memory_space_id: "space-a", recording_id: "recording-a" },
  });
  rerender(ui("recording-b"));
  fireEvent.click(await screen.findByRole("button", { name: "Review suggested refreshes" }));
  expect(screen.getByRole("link", { name: "Planning call" })).toBeVisible();
  expect(screen.queryByRole("link", { name: "Workshop" })).not.toBeInTheDocument();
  expect(get).toHaveBeenLastCalledWith("/api/context-refreshes", {
    params: { local_date: undefined, memory_space_id: "space-a", recording_id: "recording-b" },
  });
});
