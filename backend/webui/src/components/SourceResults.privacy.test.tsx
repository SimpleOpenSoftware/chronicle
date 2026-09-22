// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import SourceSearch from "./SourceSearch";
import ChatSourcePicker from "./ChatSourcePicker";
import { api, deviceInputApi } from "../services/api";
import PrivacyIntervals from "./timeline/PrivacyIntervals";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});

it("a saved exclusion revalidates existing search previews", async () => {
  const get = vi.spyOn(api, "get").mockResolvedValueOnce({ data: {
    items: [{ kind: "recording", key: "synthetic", title: "Synthetic old preview",
      excerpt: "Synthetic fixture", url: "/recordings/synthetic", highlights: [], started_at: null }],
    total: 1, indexing: { initialized: true, state: "complete" },
  }} as never).mockResolvedValue({ data: { items: [], total: 0, indexing: { initialized: true, state: "complete" } }} as never);
  vi.spyOn(deviceInputApi, "overridePrivacy").mockResolvedValue({ data: { revision: 2, decision: "excluded" } } as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<MemoryRouter initialEntries={["/recordings?q=synthetic"]}>
    <QueryClientProvider client={client}>
      <SourceSearch />
      <PrivacyIntervals timezone="Asia/Kolkata" intervals={[{
        source_id: "synthetic-source", source_name: "Test computer", revision: 1,
        started_at: "2026-01-01T10:00:00Z", ended_at: "2026-01-01T10:01:00Z",
        state: "excluded", label: "Private activity · excluded",
      }]} />
    </QueryClientProvider>
  </MemoryRouter>);
  await screen.findByText("Synthetic old preview");
  fireEvent.click(screen.getByRole("button", { name: "Keep excluded" }));
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText("Synthetic old preview")).not.toBeInTheDocument());
  client.clear();
});

it.each(["search", "chat-picker"])("%s removes cached previews when revalidation is held", async (entry) => {
  const get = vi.spyOn(api, "get").mockResolvedValue({ data: {
    items: [{ kind: "recording", key: "synthetic", title: "Synthetic held preview",
      excerpt: "Synthetic private fixture", url: "/recordings/synthetic", highlights: [], started_at: null }],
    total: 1, indexing: { initialized: true, state: "complete" },
  }} as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<MemoryRouter initialEntries={["/recordings?q=synthetic"]}>
    <QueryClientProvider client={client}>
      {entry === "search" ? <SourceSearch /> : <ChatSourcePicker selected={[]} onSave={vi.fn()} onClose={vi.fn()} />}
    </QueryClientProvider>
  </MemoryRouter>);
  await screen.findByText("Synthetic held preview");
  get.mockRejectedValue({ response: { status: 423, data: { detail: "Private or unscreened evidence is held from processing" } } });
  await act(async () => { await client.invalidateQueries({ queryKey: ["source-search"] }); });
  await waitFor(() => expect(screen.queryByText("Synthetic held preview")).not.toBeInTheDocument());
  expect(screen.queryByText("Synthetic private fixture")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: entry === "search" ? "Retry search" : "Retry" })).toBeVisible();
  client.clear();
});
