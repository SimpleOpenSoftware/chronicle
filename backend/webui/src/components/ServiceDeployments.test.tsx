// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ServiceDeployments from "./ServiceDeployments";
const get = vi.fn();
const put = vi.fn();
vi.mock("../services/api", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}));
const plan = {
  revision: 3,
  nodes: { rainbow: "http://rainbow:8775", kraken: "http://kraken:8775" },
  deployments: {
    "speaker-recognition": {
      mode: "single",
      state: "speaker_catalog",
      instances: [
        {
          node: "rainbow",
          endpoints: {
            speaker: {
              url: "http://rainbow:8085",
              health_url: "http://rainbow:8085/readiness",
              readiness: { status: "ok" },
            },
          },
        },
      ],
    },
  },
};
beforeEach(() => {
  get.mockReset();
  put.mockReset();
  get.mockImplementation((url: string) =>
    Promise.resolve({
      data: url.endsWith("/status")
        ? {
            routes: [
              {
                service: "speaker-recognition",
                endpoint: "speaker",
                mode: "single",
                selected_node: "rainbow",
                instances: [
                  { node: "rainbow", healthy: true, reason: "Ready" },
                ],
              },
            ],
            violations: [],
          }
        : { plan, activations: [] },
    }),
  );
  put.mockResolvedValue({ data: {} });
});
function show() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ServiceDeployments />
    </QueryClientProvider>,
  );
}
it("shows configured owner and selected request destination", async () => {
  show();
  expect(await screen.findByText("Single owner")).toBeTruthy();
  expect(await screen.findByText("speaker: serving from rainbow")).toBeTruthy();
});
it("saves the expected revision and leaves container actions separate", async () => {
  show();
  fireEvent.click(await screen.findByText("Configure placement"));
  fireEvent.change(
    screen.getByLabelText("speaker-recognition instance 1 node"),
    { target: { value: "kraken" } },
  );
  fireEvent.click(screen.getByText("Save placement"));
  await waitFor(() => expect(put).toHaveBeenCalled());
  const payload = put.mock.calls[0][1];
  expect(payload.revision).toBe(3);
  expect(payload.deployments["speaker-recognition"].instances[0].node).toBe(
    "kraken",
  );
  expect(put.mock.calls[0][0]).toBe("/api/admin/service-deployments");
});
it("keeps conflict detail visible without dismissing unsaved edits", async () => {
  put.mockRejectedValue({
    response: { data: { detail: "Stop the old owner first" } },
  });
  show();
  fireEvent.click(await screen.findByText("Configure placement"));
  fireEvent.click(screen.getByText("Save placement"));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Stop the old owner first",
  );
  expect(screen.getByText("Save placement")).toBeTruthy();
});

afterEach(cleanup);

it('does not save when one JSON contract is invalid after another is edited', async () => {
  show();
  fireEvent.click(await screen.findByText('Configure placement'));
  fireEvent.change(screen.getByLabelText('Expected readiness fields (JSON)'), { target: { value: '{broken' } });
  fireEvent.change(screen.getByLabelText('Expected model identity (JSON)'), { target: { value: '{"model":"ready"}' } });
  fireEvent.click(screen.getByText('Save placement'));
  expect(put).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Expected readiness fields (JSON)')).toBeInvalid();
});
