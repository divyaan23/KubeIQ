---
title: KubeIQ
description: AI workload discovery, governance insights, and policy recommendations for Kubernetes
ms.date: 2026-09-21
ms.topic: overview
keywords:
  - kubernetes
  - ai governance
  - azure ai foundry
  - token optimization
---

KubeIQ is a read-only Kubernetes analysis dashboard that discovers workloads,
identifies likely AI applications, and highlights gaps in `AIPolicy` coverage.
It combines Kubernetes manifest evidence, optional runtime telemetry, and an
Azure AI Foundry model to produce reviewable policy and token-limit
recommendations.

## Capabilities

* Inventory Deployments, StatefulSets, DaemonSets, and Jobs across namespaces
* Classify AI workloads without collecting environment variable values or
  Kubernetes Secret contents
* Enrich model and provider classifications with observed runtime traffic
* Show policy coverage, enforcement state, and recent violations
* Generate draft `AIPolicy` manifests from workload evidence and reviewer input
* Recommend input and output token limits from observed usage and pricing
* Report GPU requests and propose workload-specific GPU limits

> [!IMPORTANT]
> KubeIQ generates draft policies but never applies them. Review every generated
> manifest before using `kubectl apply`.

## Prerequisites

* Python 3.12 or Docker
* Access to a Kubernetes cluster through the active kubeconfig context
* An Azure AI Foundry project and deployed chat-completions model
* Azure credentials supported by `DefaultAzureCredential`, such as Azure CLI
  authentication or Azure Workload Identity
* Optional access to an AI policy telemetry dashboard for runtime enrichment

## Run locally

Create a virtual environment and install the dependencies from PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `AI_PROJECT_ENDPOINT` in `.env`, confirm that your current kubeconfig context
points to the intended cluster, and authenticate to Azure:

```powershell
az login
kubectl config current-context
uvicorn app.main:app --host 0.0.0.0 --port 8091
```

Open <http://localhost:8091>. FastAPI API documentation is available at
<http://localhost:8091/docs>.

## Configuration

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `AI_PROJECT_ENDPOINT` | Yes | None | Azure AI Foundry project endpoint |
| `AI_ANALYZER_MODEL_DEPLOYMENT` | No | `gpt-4o` | Model deployment used for classification and recommendations |
| `AI_ANALYZER_EXCLUDED_NAMESPACES` | No | Kubernetes and policy system namespaces | Comma-separated namespaces omitted from workload discovery |
| `TELEMETRY_DASHBOARD_URL` | No | In-cluster telemetry service URL | Source for observed models, violations, and token statistics; set to an empty value to disable enrichment |
| `LOG_LEVEL` | No | `INFO` | Python logging level |

The checked-in `.env.example` contains no credentials. Do not commit a populated
`.env` file.

## Run with Docker

Build the image and pass your Azure configuration at runtime:

```powershell
docker build -t kubeiq:local .
docker run --rm -p 8091:8091 --env-file .env kubeiq:local
```

The container must also be able to authenticate to Azure and reach a Kubernetes
API server. For cluster use, deploy the image with Workload Identity and the
RBAC resources in the provided manifest.

## Deploy to Kubernetes

Before deployment, update [k8s/deployment.yaml](k8s/deployment.yaml) with your
container image, Azure Workload Identity client ID, Foundry project endpoint,
model deployment, and telemetry URL. Then apply the manifest:

```powershell
kubectl create namespace ai-policy-system --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/deployment.yaml
kubectl rollout status deployment/ai-workload-analyzer -n ai-policy-system
```

The manifest creates a service account, read-only cluster RBAC, the analyzer
Deployment, and a `LoadBalancer` Service on port `8091`.

For a local connection instead of a public load balancer, run:

```powershell
kubectl port-forward service/ai-workload-analyzer 8091:8091 -n ai-policy-system
```

## API endpoints

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/healthz` | Return application health |
| `GET` | `/api/workloads` | List discovered workloads and AI classifications |
| `GET` | `/api/policy-governance` | Summarize policy coverage and violations |
| `GET` | `/api/policy-recommendations` | Generate a draft policy for one workload |
| `GET` | `/api/token-recommendation` | Recommend token limits from observed traffic |

Use `refresh=true` with `/api/workloads` to bypass the in-memory classification
cache. The recommendation endpoints require `namespace` and `workload` query
parameters.

## Demo resources

The [k8s/demo-unmanaged-workload.yaml](k8s/demo-unmanaged-workload.yaml) manifest
creates a sample workload without policy coverage. The
[k8s/policy-test-default.yaml](k8s/policy-test-default.yaml) manifest contains an
example `AIPolicy` custom resource definition and policy for testing.
