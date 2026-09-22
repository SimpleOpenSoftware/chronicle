# Explicit service placement and request failover

## Decision

Chronicle-managed compute deployments have one authoritative placement plan.
`single` assigns exactly one node. `ha` lists a primary followed by warm standbys.
The configured order selects the destination for each new HTTP request.
Single mode never promotes another node automatically.

The deployment authority is a node agent so container startup does not depend on
the backend becoming ready first. Its SQLite store serializes configuration and
activation reservations across processes. Every enrolled node names the same
coordinator URL and a stable, distinct node ID (including OS where hostnames overlap).
Discovery remains inventory and does not grant permission to run a service.

An instance belongs to a lifecycle group from `services.SERVICES`. For example,
`llm-services` contains chat and embedding endpoints: these share placement but
have independent readiness checks. A separately managed model deployment requires
a separately addressable lifecycle group; it must not implicitly start other groups.

## Admission and ownership changes

The common `services.run_compose_command` Interface guards activation from CLI,
boot, node-agent actions, provider changes and updates. A down/up restart reserves
before stopping and retains permission through startup. Concurrent activations
of one deployment serialize. Configuration changes require the current revision
and no outstanding activation reservations.

Admission checks the positively observed container state of all registered nodes.
Unknown/unreachable is not stopped. A single-owner move requires the excluded
instance to be stopped and idle before saving; configuration does not itself
start or stop containers. Removing enforcement also requires stopped instances.
No timeout expires a reservation: elapsed time cannot fence an orphaned process.
A reservation abandoned by a crashed CLI must be explicitly released only after
an operator establishes that the originating process cannot continue activation.

If the authority is unavailable, new managed activations fail closed. Existing
instances continue. The node watchdog re-reads authoritative policy and stops a
confirmed excluded group through the normal logged operation path. If the policy
cannot be read, it leaves running processes untouched. Direct Docker/Podman
commands bypass Chronicle admission and can briefly create an unexpected process;
periodic reconciliation detects and removes it. This is not runtime-level fencing.
Only registered nodes participate in enforcement; enroll every service host.

## Routing

Clients configure a deployment reference rather than competing direct and
discovery URLs. Managed model definitions reject those conflicting sources.
Speaker clients likewise reject an explicit URL alongside their deployment.
A stable authenticated HTTP gateway selects readiness-qualified replicas per
request, preserving multipart uploads, query strings and streamed responses.
Gateway credentials are not forwarded to inference servers.

The gateway probes all configured instances and selects the first ready one.
HA requires a separate `identity_url` and `identity` contract, usually from
`/v1/models` with `data.0.id`. Health-only equivalence is rejected. The primary is preferred again as soon as its readiness passes.
There is no automatic retry after submission, response timeout or partial stream.
The OpenAI client factory disables SDK retries for managed URLs. Gateway responses
also prohibit SDK automatic replay using `x-should-retry: false`. Higher-level job
retries remain separately observable logical attempts.

This provides compute-service failover, not HA for the backend, deployment authority,
or gateway. Those must remain available. Long-running streams are pinned to one
instance; WebSocket protocols and transparent stream continuation are not supported
by this HTTP gateway. Cloud providers may remain explicitly configured direct models.

## Speaker catalog contract

Speaker identity lives in node-local SQLite records, enrollment audio and an
in-memory FAISS index. Two healthy processes need not recognize the same people.
Speaker HA therefore serves **read-only catalog snapshots**. Every replica must
report `read_only: true` and the exact configured `catalog_fingerprint`, derived
from enrolled IDs, names, embeddings and the embedding-space identity. Health
mismatch excludes a replica. The service rejects mutations and WebSockets in this
mode, independently of the gateway. The gateway permits only declared read-only
inference endpoints. Enrollment remains available in single mode.

To publish a new snapshot: switch to a positively verified single owner, update
its catalog, stop it, copy a verified snapshot to stopped replicas, configure them
read-only, then save HA with the new fingerprint. Never synchronize a live SQLite
file between running writers. Automatic catalog replication is not implied by HA.

## Configuration

On each node, in machine-local `config/config.yml`:

```yaml
service_placement:
  node_id: gpu-linux
  coordinator_url: http://control.example.ts.net:8775
  authority: false  # true on exactly the coordinator
  reconcile_excluded: true
```

The authority stores the plan in `config/service-deployments.sqlite3`. Use the
Network page or `./services deployments --apply /path/to/plan.json`. Export with
`./services deployments`; inspect readiness and exclusions with
`./services deployments --status`. A plan carries its expected `revision`:

```json
{
  "revision": 0,
  "nodes": {
    "control-linux": "http://control.example.ts.net:8775",
    "gpu-linux": "http://gpu.example.ts.net:8775"
  },
  "deployments": {
    "speaker-recognition": {
      "mode": "single",
      "state": "speaker_catalog",
      "instances": [{
        "node": "gpu-linux",
        "endpoints": {"speaker": {
          "url": "http://gpu.example.ts.net:8085",
          "health_url": "http://gpu.example.ts.net:8085/readiness",
          "readiness": {"status": "ok"}
        }}
      }]
    }
  }
}
```

Set `SERVICE_PLACEMENT_TOKEN` in each node's `backend/.env` for authenticated
control, including localhost and Tailnet-trust-disabled installations. It is a
shared fleet control secret; do not put it in the deployment plan. Register all
node IDs before starting services on enrolled nodes.

Backend/worker environment: `SERVICE_GATEWAY_URL` points at the authority and
`SERVICE_GATEWAY_TOKEN` is its service-manager token. Keep secrets in `.env`.
Speaker config uses `speaker_recognition.deployment: speaker-recognition` with
`service_url: null` and no `SPEAKER_SERVICE_URL`. A managed model uses
`deployment: llm-services`, `deployment_endpoint: chat` (or `embeddings`) and no
direct/discovery URL. Replica URLs include API base paths such as `/v1`.
Model identity expectations are mandatory for stateless HA and must agree across replicas.
Use the actual model identity, not merely HTTP 200 or `status: ok`.

## Alternatives considered

- Discovery plus peer scans cannot serialize concurrent starts or distinguish an
  unreachable node from a stopped instance.
- Expiring leases without runtime fencing can leave two live owners; static
  ownership and durable activation reservations provide clearer failure behavior.
- Per-client endpoint selection would distribute health, routing and retry logic
  across Python SDKs, embedded agents and future consumers. A stable gateway keeps
  that behavior in one Module, at the cost of a central data-path dependency.

## Validation

Tests exercise registered node-agent routes, the actual Compose and CLI restart
entry points, concurrent reservations, revision conflicts, partition refusals,
HTTP request failover/recovery, model mismatch, streaming, no ambiguous replay,
speaker snapshot mutation rejection, and backend client configuration.

The speech-output client uses `TTS_DEPLOYMENT=tts` in its environment; remove
`CHRONICLE_TTS_URL` and `TTS_URL` overrides for managed synthesis.
