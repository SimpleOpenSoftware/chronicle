# Chronicle on Kubernetes

This guide deploys Chronicle to a MicroK8s cluster with Skaffold and Helm. All
deploy steps run from the repository root through `make`.

- **Build machine**: where you run `make`. It builds the images and pushes them
  to the cluster registry. It can be another architecture (for example an arm64
  Mac): the speaker images build for amd64 on a buildx builder you choose.
- **Cluster**: one or more MicroK8s nodes. One node hosts the image registry.

## Contents

1. [Prepare the cluster](#1-prepare-the-cluster)
2. [Prepare the build machine](#2-prepare-the-build-machine)
3. [Configure](#3-configure)
4. [First-time setup](#4-first-time-setup)
5. [Deploy](#5-deploy)
6. [Settings changed in the app](#6-settings-changed-in-the-app)
7. [Check and maintain](#7-check-and-maintain)
8. [Troubleshooting](#8-troubleshooting)
9. [Known limitations](#9-known-limitations)

## 1. Prepare the cluster

**Run on each cluster node.**

1. Install MicroK8s:
   ```bash
   sudo snap install microk8s --classic
   sudo usermod -a -G microk8s $USER
   newgrp microk8s
   microk8s status --wait-ready
   ```
2. To add a node, run `microk8s add-node` on an existing node, then run the
   printed `microk8s join ...` command on the new node.
3. On one node, enable the add-ons:
   ```bash
   microk8s enable dns ingress metrics-server registry
   ```
   The registry listens on port `32000`. Check it with
   `curl http://localhost:32000/v2/`.
4. Install the OpenEBS local hostpath provisioner
   ([OpenEBS docs](https://openebs.io/docs)). The charts use the storage class
   `openebs-hostpath`. Check it with `kubectl get storageclass`.
5. GPU nodes only: install the NVIDIA GPU operator with
   `scripts/k8s/gpu/setup-nvidia-operator.sh`, then test it with
   `scripts/k8s/gpu/test-gpu-pod.sh`.

## 2. Prepare the build machine

Install these tools:

| Tool | Used for |
|---|---|
| `kubectl` | talking to the cluster (needs a kubeconfig for it) |
| `skaffold` | building and deploying |
| `helm` | the charts that Skaffold deploys |
| Docker with `buildx` | building images |
| `uv` | running the config scripts (`make config-k8s`) |

Then clone the repository:
```bash
git clone https://github.com/SimpleOpenSoftware/chronicle.git
cd chronicle
```

## 3. Configure

All deploy settings live in one file, `config.env`. It is gitignored.

```bash
cp config.env.template config.env
```

Edit at least these values:

| Variable | Meaning |
|---|---|
| `CONTAINER_REGISTRY` | the cluster registry, for example `<node>:32000` |
| `INFRASTRUCTURE_NAMESPACE` | namespace of MongoDB and Redis |
| `APPLICATION_NAMESPACE` | namespace of the backend and WebUI |
| `DOMAIN`, `DOMAIN_PREFIX`, `EXTERNAL_DOMAIN` | host names for the ingress |
| `MONGO_ROOT_PASSWORD` | the backend logs in to MongoDB as `root` with it |
| `AUTH_SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` | login |
| `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` | LLM and speech-to-text providers |

For speaker recognition, also set:

| Variable | Meaning |
|---|---|
| `HF_TOKEN` | Hugging Face token for the pyannote models |
| `COMPUTE_MODE` | `cpu`, `cu126` or `cu128`. Use `cu126` for Pascal GPUs (GTX 10xx); `cu128` drops them |
| `SPEAKER_NODE` | the node with the NVIDIA GPU |
| `BUILDX_BUILDER` | an amd64 buildx builder (`docker buildx ls` lists them) |
| `SPEAKER_SERVICE_URL` | the backend's in-cluster address of the speaker service |

Optional. Leave a value empty to turn it off:

| Variable | Meaning |
|---|---|
| `APP_NODE` | pin the backend and WebUI pods to this node |
| `TAILSCALE_HOSTNAME` | serve the API and WebUI at `https://<name>.<tailnet>.ts.net` (needs the Tailscale Kubernetes operator) |
| `TAILSCALE_PROXY_GROUP` | the Tailscale ProxyGroup for that ingress. Empty: the operator makes a proxy for it |

Application settings (models, memory, plugins) live in `config/config.yml`,
`config/defaults.yml` and `config/plugins.yml`, the same as for Docker Compose.

`make config-k8s` turns these into the files the deploy uses. It runs before
every deploy, so you do not edit these files:

| Generated file | Contents |
|---|---|
| `k8s-manifests/configmap.yaml` | `config.env` values, plus the three `config/*.yml` files |
| `k8s-manifests/secrets.yaml` | values whose name contains `SECRET`, `KEY`, `TOKEN` or `PASSWORD` (mode 600) |
| `skaffold.env` | the `config.env` values that `skaffold.yaml` uses |

It applies the ConfigMap and Secret to `APPLICATION_NAMESPACE`, and copies them
to the `speech` namespace. All three files are gitignored.

## 4. First-time setup

```bash
make setup-k8s
```

It asks for the registry node's IP address, then:

1. lets that node pull from the insecure registry
   (`scripts/k8s/configure-insecure-registry-remote.sh <ip> [ssh_user]`; run it
   again for each other node);
2. prepares the shared model storage for speaker recognition
   (`scripts/k8s/setup-storage.sh`);
3. runs `make config-k8s`;
4. deploys MongoDB and Redis (`make setup-infrastructure`);
5. applies the cross-namespace RBAC (`make setup-rbac`);
6. creates the shared models PVC (`make setup-storage-pvc`).

For Tailscale access, set `TAILSCALE_HOSTNAME` in `config.env`. `make deploy`
then creates the ingress with the backend chart.

## 5. Deploy

| Command | What it deploys |
|---|---|
| `make deploy` | backend and WebUI (it runs `deploy-k8s` when `DEPLOYMENT_MODE = kubernetes`) |
| `make deploy-speaker` | speaker recognition, in the `speech` namespace |
| `make deploy-k8s-full` | MongoDB and Redis, then backend and WebUI |

`make deploy-apps` and `make deploy-k8s` are the same as `make deploy` in
Kubernetes mode.

Every deploy runs `make config-k8s` first, then `skaffold run` with a profile.
The backend profile builds two images:

- `backend` from `backend/Dockerfile.k8s`. It runs the API with the workers as
  a sidecar container in the same pod.
- `webui` from `backend/webui/Dockerfile.k8s`.

`make deploy-speaker` builds its two images for amd64 with
`scripts/k8s/buildx-push.sh` on `BUILDX_BUILDER`, and pins the pod to
`SPEAKER_NODE`.

The ASR services have no `make` target. Deploy them with Skaffold directly:
```bash
set -a; source skaffold.env; set +a
skaffold run --profile=parakeet-asr --default-repo=<CONTAINER_REGISTRY>   # or moonshine-asr
```

## 6. Settings changed in the app

Under Docker Compose, the Settings page writes to `config/` on the host. In
Kubernetes the backend and workers use `/app/data/config` on the data volume
(`CONFIG_DIR`). A ConfigMap is read-only, so it is only the seed.

Each time the pod starts, the `config-seed` init container
(`backend/src/backend/config_seed.py`) updates that folder from the ConfigMap:

- `defaults.yml` is always replaced. It holds code defaults, not settings.
- `config.yml` and `plugins.yml` are copied the first time. After that, every
  setting already in the live file keeps its live value. Only keys that are new
  in the repository are added.

So a redeploy never undoes a change made in the app. Two results of this:

- If you change an existing value in `config/config.yml`, a redeploy does not
  apply it. Change it in the app instead, or edit the live file (the image has
  no editor, so copy it out and back):
  ```bash
  POD=$(kubectl get pod -n <APPLICATION_NAMESPACE> -l app.kubernetes.io/name=backend -o name | head -1)
  kubectl cp -n <APPLICATION_NAMESPACE> -c backend ${POD#pod/}:/app/data/config/config.yml live-config.yml
  # edit live-config.yml, then:
  kubectl cp -n <APPLICATION_NAMESPACE> -c backend live-config.yml ${POD#pod/}:/app/data/config/config.yml
  kubectl rollout restart -n <APPLICATION_NAMESPACE> deploy/backend
  ```
- A list is one value. If the live file has a `models:` list, a model added to
  the repository's list does not appear.

## 7. Check and maintain

| Command | What it does |
|---|---|
| `make k8s-status` | cluster health (`scripts/k8s/cluster-status.sh [namespace]`) |
| `make check-apps` | pods and services in `APPLICATION_NAMESPACE` |
| `make check-infrastructure` | pods and services in `INFRASTRUCTURE_NAMESPACE` |
| `make k8s-cleanup` | frees registry storage |
| `make k8s-purge` | removes unused images from the registry and nodes |

Useful `kubectl` commands:
```bash
kubectl logs -n <APPLICATION_NAMESPACE> deploy/backend -c backend
kubectl logs -n <APPLICATION_NAMESPACE> deploy/backend -c backend-workers
kubectl logs -n <APPLICATION_NAMESPACE> deploy/backend -c config-seed
kubectl logs -n <APPLICATION_NAMESPACE> deploy/webui
kubectl port-forward -n <APPLICATION_NAMESPACE> svc/backend 8000:8000
kubectl rollout restart -n <APPLICATION_NAMESPACE> deploy/backend
```

Check the backend: `curl http://<backend-host>/health`.

## 8. Troubleshooting

- **Image pull fails**: the node does not trust the registry. Run
  `scripts/k8s/configure-insecure-registry-remote.sh <node-ip>` for that node.
- **PVC stays `Pending`**: check `kubectl get storageclass` for
  `openebs-hostpath`.
- **Settings page says the config is read-only**: the pod still mounts the
  ConfigMap. This happens only with `persistence.enabled=false`.
- **`make config-k8s` says "config.env is missing Skaffold variables"**: add the
  named variables to `config.env`. Run the script through `make`, which loads
  `config.env`; run alone, it sees no values.
- **GPU pod is `Pending`**: check
  `kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, gpu: .status.allocatable."nvidia.com/gpu"}'`
  and the GPU operator pods (`kubectl get pods -n gpu-operator`). Run
  `nvidia-smi` on the node. If the driver does not load on a Secure Boot
  machine, its signing key may not be enrolled.

## 9. Known limitations

- The `infrastructure` profile deploys MongoDB with authentication off, but the
  backend logs in as `root` with `MONGO_ROOT_PASSWORD`. On a new cluster, enable
  MongoDB authentication with that password, or use a MongoDB that has it.
- The backend data volume is `ReadWriteOnce`. The backend pod uses `Recreate`,
  so a deploy stops the old pod before it starts the new one.
- For Docker Compose instead of Kubernetes, use `./wizard.sh` and `./services`
  (see [quickstart.md](quickstart.md)).
