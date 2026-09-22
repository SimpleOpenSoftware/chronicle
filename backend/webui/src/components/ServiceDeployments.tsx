import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../services/api";
import { Button } from "./ui";

type Endpoint = {
  url: string;
  health_url: string;
  readiness: Record<string, string | number | boolean>;
  identity_url?: string | null;
  identity?: Record<string, string | number | boolean>;
};
type Instance = { node: string; endpoints: Record<string, Endpoint> };
type Deployment = {
  mode: "single" | "ha";
  state: "stateless" | "speaker_catalog";
  instances: Instance[];
};
type Plan = {
  revision: number;
  nodes: Record<string, string>;
  deployments: Record<string, Deployment>;
};
type Route = {
  service: string;
  endpoint: string;
  mode: string;
  selected_node: string | null;
  instances: { node: string; healthy: boolean; reason: string }[];
};
type Status = {
  routes: Route[];
  violations: { service: string; node: string; reason: string }[];
};
const names: Record<string, string> = {
  "speaker-recognition": "Speaker recognition",
  "llm-services": "LLM and embeddings",
  tts: "Text to speech",
  "asr-services": "Speech to text",
};
const roles: Record<string, string[]> = {
  "speaker-recognition": ["speaker"],
  "llm-services": ["chat", "embeddings"],
  tts: ["tts"],
  "asr-services": ["batch"],
};
const inputClass =
  "w-full rounded border border-gray-300 bg-transparent px-3 py-2 text-sm dark:border-gray-600";
function errorText(error: unknown) {
  const e = error as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = e.response?.data?.detail;
  return typeof detail === "string"
    ? detail
    : detail
      ? JSON.stringify(detail)
      : e.message || "Request failed";
}

export default function ServiceDeployments() {
  const cache = useQueryClient();
  const [draft, setDraft] = useState<Plan | null>(null);
  const [newNode, setNewNode] = useState("");
  const [newNodeUrl, setNewNodeUrl] = useState("");
  const [formError, setFormError] = useState("");
  const plan = useQuery({
    queryKey: ["service-deployments"],
    queryFn: async () =>
      (
        await api.get<{ plan: Plan; activations: unknown[] }>(
          "/api/admin/service-deployments",
        )
      ).data,
    retry: false,
  });
  const status = useQuery({
    queryKey: ["service-deployment-status"],
    queryFn: async () =>
      (await api.get<Status>("/api/admin/service-deployments/status")).data,
    enabled: !!plan.data,
    refetchInterval: 15000,
    retry: false,
  });
  const save = useMutation({
    mutationFn: async (value: Plan) =>
      api.put("/api/admin/service-deployments", value),
    onSuccess: async () => {
      setDraft(null);
      await cache.invalidateQueries({ queryKey: ["service-deployments"] });
      await cache.invalidateQueries({
        queryKey: ["service-deployment-status"],
      });
    },
  });
  const change = (fn: (p: Plan) => void) =>
    setDraft((current) => {
      if (!current) return current;
      const copy = structuredClone(current);
      fn(copy);
      return copy;
    });
  const addInstance = (service: string) =>
    change((p) => {
      const d = p.deployments[service];
      const node =
        Object.keys(p.nodes).find(
          (n) => !d.instances.some((i) => i.node === n),
        ) || "";
      d.instances.push({
        node,
        endpoints: Object.fromEntries(
          (roles[service] || ["api"]).map((r) => [
            r,
            { url: "", health_url: "", readiness: {} },
          ]),
        ),
      });
    });

  return (
    <section
      className="mb-6 rounded-lg border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800"
      aria-labelledby="deployment-heading"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="deployment-heading" className="text-lg font-semibold">
            Service placement
          </h2>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
            Choose one owner, or a primary with warm standbys. Requests follow
            this plan.
          </p>
        </div>
        {plan.data && !draft && (
          <Button
            variant="secondary"
            onClick={() => {
              setDraft(structuredClone(plan.data.plan));
              save.reset();
              setFormError("");
            }}
          >
            Configure placement
          </Button>
        )}
      </div>
      {plan.isLoading && (
        <p className="mt-4 text-sm">Loading deployment plan…</p>
      )}
      {plan.error && (
        <p role="alert" className="mt-4 text-sm text-red-600">
          Deployment plan unavailable: {errorText(plan.error)}
        </p>
      )}
      {!draft && plan.data && (
        <div className="mt-4 space-y-3">
          {!Object.keys(plan.data.plan.deployments).length && (
            <p className="text-sm text-gray-500">
              No service placement configured.
            </p>
          )}
          {Object.entries(plan.data.plan.deployments).map(([name, d]) => (
            <div
              key={name}
              className="border-t border-gray-200 pt-3 dark:border-gray-700"
            >
              <div className="flex flex-wrap justify-between gap-2">
                <strong className="text-sm">{names[name] || name}</strong>
                <span className="text-sm text-gray-500">
                  {d.mode === "single" ? "Single owner" : "High availability"}
                </span>
              </div>
              <p className="mt-1 text-sm">
                {d.instances
                  .map(
                    (i, ix) =>
                      `${i.node}${d.mode === "ha" ? (ix === 0 ? " (primary)" : " (standby)") : ""}`,
                  )
                  .join(" → ")}
              </p>
              {(status.data?.routes || [])
                .filter((r) => r.service === name)
                .map((r) => (
                  <div key={r.endpoint} className="mt-2 text-sm">
                    <span>
                      {r.endpoint}:{" "}
                      {r.selected_node
                        ? `serving from ${r.selected_node}`
                        : "unavailable"}
                    </span>
                    <span className="ml-2 text-gray-500">
                      {r.instances
                        .map(
                          (i) => `${i.node}: ${i.healthy ? "ready" : i.reason}`,
                        )
                        .join(" · ")}
                    </span>
                  </div>
                ))}
            </div>
          ))}
          {status.error && (
            <p role="alert" className="text-sm text-red-600">
              Live placement status unavailable: {errorText(status.error)}
            </p>
          )}
          {status.data?.violations.map((v, ix) => (
            <p role="alert" key={ix} className="text-sm text-red-600">
              {names[v.service] || v.service} on {v.node}: {v.reason}
            </p>
          ))}
          {!!plan.data.activations.length && (
            <p role="status" className="text-sm text-amber-700">
              A service activation is reserved. Placement changes wait until it
              finishes.
            </p>
          )}
        </div>
      )}
      {draft && (
        <form
          className="mt-5 space-y-5"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(draft);
          }}
        >
          <fieldset className="space-y-3">
            <legend className="mb-2 text-sm font-semibold">
              Cluster nodes
            </legend>
            {Object.entries(draft.nodes).map(([node, url]) => (
              <div
                key={node}
                className="grid gap-2 sm:grid-cols-[1fr_3fr_auto]"
              >
                <span className="py-2 text-sm">{node}</span>
                <input
                  aria-label={`${node} agent URL`}
                  className={inputClass}
                  value={url}
                  onChange={(e) =>
                    change((p) => {
                      p.nodes[node] = e.target.value;
                    })
                  }
                />
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() =>
                    change((p) => {
                      delete p.nodes[node];
                    })
                  }
                >
                  Remove node
                </Button>
              </div>
            ))}
            <div className="grid gap-2 sm:grid-cols-[1fr_3fr_auto]">
              <input
                aria-label="New node ID"
                placeholder="Node ID"
                className={inputClass}
                value={newNode}
                onChange={(e) => setNewNode(e.target.value)}
              />
              <input
                aria-label="New node agent URL"
                placeholder="https://node.example:8775"
                className={inputClass}
                value={newNodeUrl}
                onChange={(e) => setNewNodeUrl(e.target.value)}
              />
              <Button
                type="button"
                variant="secondary"
                disabled={!newNode || !newNodeUrl || newNode in draft.nodes}
                onClick={() => {
                  change((p) => {
                    p.nodes[newNode] = newNodeUrl;
                  });
                  setNewNode("");
                  setNewNodeUrl("");
                }}
              >
                Add node
              </Button>
            </div>
          </fieldset>
          {Object.entries(draft.deployments).map(([name, d]) => (
            <fieldset
              key={name}
              className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700"
            >
              <legend className="text-sm font-semibold">
                {names[name] || name}
              </legend>
              <label className="block text-sm">
                Availability
                <select
                  className={`${inputClass} mt-1`}
                  value={d.mode}
                  onChange={(e) =>
                    change((p) => {
                      p.deployments[name].mode = e.target
                        .value as Deployment["mode"];
                    })
                  }
                >
                  <option value="single">Single owner</option>
                  <option value="ha">Primary + warm standby</option>
                </select>
              </label>
              {d.mode === "single" && d.instances.length !== 1 && (
                <p className="text-sm text-amber-700">
                  Keep exactly one instance for single mode.
                </p>
              )}
              {d.state === "speaker_catalog" && d.mode === "ha" && (
                <p className="text-sm text-amber-700">
                  Speaker HA requires matching read-only catalog snapshots.
                  Enrollment edits require single mode.
                </p>
              )}
              {d.instances.map((instance, ix) => (
                <div
                  key={`${instance.node}:${ix}`}
                  className="space-y-3 rounded border border-gray-200 p-3 dark:border-gray-700"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="flex-1 text-sm">
                      {d.mode === "single"
                        ? "Owner"
                        : ix === 0
                          ? "Primary"
                          : `Standby ${ix}`}
                      <select
                        aria-label={`${name} instance ${ix + 1} node`}
                        className={`${inputClass} mt-1`}
                        value={instance.node}
                        onChange={(e) =>
                          change((p) => {
                            p.deployments[name].instances[ix].node =
                              e.target.value;
                          })
                        }
                      >
                        <option value="">Choose a node</option>
                        {Object.keys(draft.nodes).map((n) => (
                          <option key={n}>{n}</option>
                        ))}
                      </select>
                    </label>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() =>
                        change((p) => {
                          p.deployments[name].instances.splice(ix, 1);
                        })
                      }
                    >
                      Remove instance
                    </Button>
                    {ix > 0 && (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() =>
                          change((p) => {
                            const list = p.deployments[name].instances;
                            [list[ix - 1], list[ix]] = [list[ix], list[ix - 1]];
                          })
                        }
                      >
                        Move earlier
                      </Button>
                    )}
                  </div>
                  {Object.entries(instance.endpoints).map(
                    ([role, endpoint]) => (
                      <div key={role} className="grid gap-2 sm:grid-cols-2">
                        <label className="text-sm">
                          {role} URL
                          <input
                            required
                            type="url"
                            className={`${inputClass} mt-1`}
                            value={endpoint.url}
                            onChange={(e) =>
                              change((p) => {
                                p.deployments[name].instances[ix].endpoints[
                                  role
                                ].url = e.target.value;
                              })
                            }
                          />
                        </label>
                        <label className="text-sm">
                          Readiness URL
                          <input
                            required
                            type="url"
                            className={`${inputClass} mt-1`}
                            value={endpoint.health_url}
                            onChange={(e) =>
                              change((p) => {
                                p.deployments[name].instances[ix].endpoints[
                                  role
                                ].health_url = e.target.value;
                              })
                            }
                          />
                        </label>
                        <label className="text-sm sm:col-span-2">
                          Expected readiness fields (JSON)
                          <input
                            className={`${inputClass} mt-1 font-mono`}
                            defaultValue={JSON.stringify(endpoint.readiness)}
                            onChange={(e) => {
                              try {
                                const value = JSON.parse(e.target.value);
                                if (
                                  !value ||
                                  Array.isArray(value) ||
                                  typeof value !== "object"
                                )
                                  throw new Error();
                                change((p) => {
                                  p.deployments[name].instances[ix].endpoints[
                                    role
                                  ].readiness = value;
                                });
                                e.currentTarget.setCustomValidity("");
                                setFormError("");
                              } catch {
                                e.currentTarget.setCustomValidity(
                                  "Enter a valid JSON object",
                                );
                                setFormError(
                                  "Readiness fields must be a JSON object.",
                                );
                              }
                            }}
                          />
                        </label>
                        <label className="text-sm">
                          Model identity URL
                          <input
                            type="url"
                            className={`${inputClass} mt-1`}
                            value={endpoint.identity_url || ""}
                            onChange={(e) =>
                              change((p) => {
                                p.deployments[name].instances[ix].endpoints[
                                  role
                                ].identity_url = e.target.value || null;
                              })
                            }
                          />
                        </label>
                        <label className="text-sm">
                          Expected model identity (JSON)
                          <input
                            className={`${inputClass} mt-1 font-mono`}
                            defaultValue={JSON.stringify(
                              endpoint.identity || {},
                            )}
                            onChange={(e) => {
                              try {
                                const value = JSON.parse(e.target.value);
                                if (
                                  !value ||
                                  Array.isArray(value) ||
                                  typeof value !== "object"
                                )
                                  throw new Error();
                                change((p) => {
                                  p.deployments[name].instances[ix].endpoints[
                                    role
                                  ].identity = value;
                                });
                                e.currentTarget.setCustomValidity("");
                                setFormError("");
                              } catch {
                                e.currentTarget.setCustomValidity(
                                  "Enter a valid JSON object",
                                );
                                setFormError(
                                  "Model identity must be a JSON object.",
                                );
                              }
                            }}
                          />
                        </label>
                      </div>
                    ),
                  )}
                </div>
              ))}
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => addInstance(name)}
                >
                  Add instance
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() =>
                    change((p) => {
                      delete p.deployments[name];
                    })
                  }
                >
                  Remove placement
                </Button>
              </div>
            </fieldset>
          ))}
          <label className="block text-sm">
            Configure another service
            <select
              className={`${inputClass} mt-1`}
              value=""
              onChange={(e) => {
                const name = e.target.value;
                if (name)
                  change((p) => {
                    p.deployments[name] = {
                      mode: "single",
                      state:
                        name === "speaker-recognition"
                          ? "speaker_catalog"
                          : "stateless",
                      instances: [],
                    };
                  });
              }}
            >
              <option value="">Choose a service</option>
              {Object.entries(names)
                .filter(([name]) => !draft.deployments[name])
                .map(([name, label]) => (
                  <option value={name} key={name}>
                    {label}
                  </option>
                ))}
            </select>
          </label>
          <p className="text-sm text-gray-500">
            Saving changes placement and routing. Stop excluded instances first;
            saving does not start or stop containers.
          </p>
          {(formError || save.error) && (
            <p role="alert" className="text-sm text-red-600">
              {formError || errorText(save.error)}
            </p>
          )}
          <div className="flex gap-2">
            <Button disabled={save.isPending || !!formError} type="submit">
              {save.isPending ? "Saving…" : "Save placement"}
            </Button>
            <Button
              type="button"
              variant="secondary"
              disabled={save.isPending}
              onClick={() => setDraft(null)}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}
    </section>
  );
}
