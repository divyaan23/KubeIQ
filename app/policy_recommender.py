from __future__ import annotations

from typing import Any

import httpx

from app.agent_client import generate_policy_recommendation
from app.telemetry_enrichment import TELEMETRY_DASHBOARD_URL

# Only used if the agent call itself fails outright (network/auth error) — never
# used as a silent substitute for the agent's own reasoning.
FALLBACK_MAX_INPUT_TOKENS = 4000
FALLBACK_MAX_OUTPUT_TOKENS = 1000

# The CRD is just a schema — safe to embed verbatim and apply to any cluster.
# Kept in sync with ai-policy-platform/charts/aipolicy-admission/crds/aipolicy-crd.yaml.
AIPOLICY_CRD_YAML = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: aipolicies.ai.policy.io
spec:
  group: ai.policy.io
  scope: Namespaced
  names:
    kind: AIPolicy
    plural: aipolicies
    singular: aipolicy
    shortNames:
      - aip
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                selector:
                  type: object
                  properties:
                    matchLabels:
                      type: object
                      additionalProperties:
                        type: string
                admission:
                  type: object
                  properties:
                    allowedRegistries:
                      type: array
                      items:
                        type: string
                    requireSidecar:
                      type: boolean
                runtime:
                  type: object
                  properties:
                    allowedProviders:
                      type: array
                      items:
                        type: string
                    allowedModels:
                      type: array
                      items:
                        type: string
                    maxInputTokens:
                      type: integer
                    maxOutputTokens:
                      type: integer
                    action:
                      type: string
                      enum: [block, warn, audit]
                pricing:
                  type: object
                  properties:
                    inputPerMillion:
                      type: number
                    outputPerMillion:
                      type: number
                thresholds:
                  type: object
                  properties:
                    inputWarningPct:
                      type: integer
                    outputWarningPct:
                      type: integer
                    outputCriticalPct:
                      type: integer
                gpu:
                  type: object
                  properties:
                    maxGPUs:
                      type: integer
                    allowedTypes:
                      type: array
                      items:
                        type: string
      additionalPrinterColumns:
        - name: Max-Input
          type: integer
          jsonPath: .spec.runtime.maxInputTokens
        - name: Max-Output
          type: integer
          jsonPath: .spec.runtime.maxOutputTokens
        - name: Action
          type: string
          jsonPath: .spec.runtime.action
        - name: Sidecar
          type: boolean
          jsonPath: .spec.admission.requireSidecar
        - name: Max-GPUs
          type: integer
          jsonPath: .spec.gpu.maxGPUs
        - name: Age
          type: date
          jsonPath: .metadata.creationTimestamp
"""

BOOTSTRAP_HEADER = """# ============================================================
# AI Governance Bootstrap Manifest
# ============================================================
# This file contains everything that is safe to ship as static YAML:
#   1. The AIPolicy CustomResourceDefinition (a schema — apply to any cluster)
#   2. The generated AIPolicy resource for this workload
#
# NOT included, because these are running services, not static YAML:
#   - The admission-controller (validates registries, injects the runtime sidecar)
#   - The runtime-sidecar (enforces provider/model/token limits per request)
# Without those deployed, this AIPolicy resource will be ACCEPTED by the
# cluster but NOT ENFORCED. Deploy them first, e.g.:
#   helm install aipolicy-admission ./ai-policy-platform/charts/aipolicy-admission \\
#     --namespace ai-policy-system --create-namespace
# ============================================================

"""


def _fetch_token_stats(namespace: str, workload: str) -> dict[str, Any] | None:
    if not TELEMETRY_DASHBOARD_URL:
        return None
    try:
        response = httpx.get(
            f"{TELEMETRY_DASHBOARD_URL}/api/workload-token-stats",
            params={"namespace": namespace, "workload": workload},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


# Public alias — the Tokenomics endpoint in main.py reuses this same fetch.
fetch_token_stats = _fetch_token_stats


def _apply_instructions(
    namespace: str,
    workload: str,
    kind: str,
    selector_key: str,
    selector_value: str,
    runtime_enforced: bool,
) -> str:
    """Deterministic, workload-specific footer — exact names/kind are known facts here,
    not something to leave to the agent to guess."""
    restart_kind = kind.lower() if kind.lower() in ("deployment", "statefulset", "daemonset") else None
    lines = [
        "",
        "# ============================================================",
        "# Apply Instructions",
        "# ============================================================",
        "# 1. Apply this file (registers the CRD if needed, plus the policy):",
        "#      kubectl apply -f <this-file>.yaml",
        "#",
        f"# 2. This policy only governs workloads in namespace '{namespace}' whose pod",
        f"#    labels match {selector_key}: {selector_value}. Confirm your workload's pod",
        "#    template already carries that label (it does for the workload this was",
        "#    generated from).",
    ]
    if runtime_enforced:
        lines += [
            "#",
            "# 3. This policy sets runtime restrictions (provider/model/token/GPU), which are",
            "#    enforced by a sidecar container the admission-controller injects. That",
            "#    injection ONLY happens on pods labeled:",
            "#      ai-policy.io/runtime: \"enabled\"",
            f"#    Add that label to the pod template of your {kind or 'workload'} if it isn't",
            f"#    already present — edit the {kind or 'workload'} '{workload}' manifest itself",
            "#    (NOT this file) to include:",
            "#      spec:",
            "#        template:",
            "#          metadata:",
            "#            labels:",
            '#              ai-policy.io/runtime: "enabled"',
            "#    then apply that change and continue to step 4.",
            "#",
            "# 4. The sidecar is injected at pod CREATION time only — it will not retroactively",
            "#    attach to already-running pods. After applying this policy, restart the",
            "#    workload so new pods pick up the sidecar and its config:",
        ]
        if restart_kind:
            lines.append(f"#      kubectl rollout restart {restart_kind}/{workload} -n {namespace}")
        else:
            lines.append(f"#      Recreate the {kind or 'workload'} '{workload}' (kind '{kind}' has no rollout restart)")
    else:
        lines += [
            "#",
            "# 3. This policy only restricts container registries at admission time — no",
            "#    runtime sidecar is required for that check to take effect.",
        ]
    lines.append("# ============================================================")
    return "\n".join(lines) + "\n"


def build_recommendation(
    namespace: str,
    workload: str,
    images: list[str],
    labels: dict[str, str],
    gpu_requested: int,
    classification: dict[str, Any],
    user_guidance: str | None = None,
    kind: str = "Deployment",
) -> dict[str, Any]:
    """Generates a draft AIPolicy for a workload with no matching policy yet.

    The agent decides every policy value (registries, provider/model scope, token
    limits, GPU limit, enforcement mode) from raw evidence plus optional reviewer
    guidance. Python only fetches the raw evidence and renders the final YAML, so a
    model output can never produce syntactically invalid YAML. Never applies
    anything; the caller must review and `kubectl apply`.
    """
    token_stats = _fetch_token_stats(namespace, workload)
    selector_key = "app" if "app" in labels else next(iter(labels), "app")
    selector_value = labels.get(selector_key, workload)

    agent_input = {
        "observed_images": images,
        "classification": classification,
        "token_stats": token_stats,
        "gpu_requested": gpu_requested,
        "reviewer_guidance": (user_guidance or "").strip() or None,
    }
    decision = generate_policy_recommendation(agent_input)
    if decision is None:
        decision = {
            "allowedRegistries": [],
            "allowedProviders": [p for p in [classification.get("provider")] if p],
            "allowedModels": [m for m in [classification.get("model")] if m],
            "maxInputTokens": FALLBACK_MAX_INPUT_TOKENS,
            "maxOutputTokens": FALLBACK_MAX_OUTPUT_TOKENS,
            "action": "audit",
            "maxGPUs": gpu_requested,
            "allowedGPUTypes": [],
            "rationale": ["Agent policy generation was unavailable; showing a minimal safe fallback."],
        }

    policy_name = f"{workload}-policy"
    runtime_lines = "\n".join(
        [
            "    allowedProviders:",
            _yaml_list(decision["allowedProviders"], indent=6),
            "    allowedModels:",
            _yaml_list(decision["allowedModels"], indent=6),
            *_optional_scalar_line("maxInputTokens", decision["maxInputTokens"], indent=4),
            *_optional_scalar_line("maxOutputTokens", decision["maxOutputTokens"], indent=4),
            *_optional_scalar_line("action", decision["action"], indent=4, quoted=False),
        ]
    )
    gpu_lines = "\n".join(
        [
            *_optional_scalar_line("maxGPUs", decision["maxGPUs"], indent=4),
            "    allowedTypes:",
            _yaml_list(decision["allowedGPUTypes"], indent=6),
        ]
    )
    runtime_enforced = any(
        [
            decision["allowedProviders"],
            decision["allowedModels"],
            decision["maxInputTokens"] is not None,
            decision["maxOutputTokens"] is not None,
            decision["action"] is not None,
        ]
    )
    policy_yaml = BOOTSTRAP_HEADER + AIPOLICY_CRD_YAML + f"""---
apiVersion: ai.policy.io/v1alpha1
kind: AIPolicy
metadata:
  name: {policy_name}
  namespace: {namespace}
spec:
  selector:
    matchLabels:
      {selector_key}: {selector_value}
  admission:
    allowedRegistries:
{_yaml_list(decision["allowedRegistries"], indent=6)}
    requireSidecar: false
  runtime:
{runtime_lines}
  gpu:
{gpu_lines}
""" + _apply_instructions(namespace, workload, kind, selector_key, selector_value, runtime_enforced)

    return {
        "policy_name": policy_name,
        "policy_yaml": policy_yaml,
        "rationale": decision["rationale"],
        "cold_start": token_stats is None or (token_stats.get("successful") or {}).get("requests_analyzed", 0) == 0,
        "requires_human_review": True,
    }


def _yaml_list(values: list[str], *, indent: int) -> str:
    if not values:
        return " " * indent + "[]"
    pad = " " * indent
    return "\n".join(f"{pad}- {value}" for value in values)


def _optional_scalar_line(key: str, value: Any, *, indent: int, quoted: bool = False) -> list[str]:
    """Omits the YAML line entirely when value is None, meaning 'left unrestricted' rather
    than a literal default — matches how the admission-controller/runtime-sidecar treat an
    absent field as 'no limit enforced'."""
    if value is None:
        return []
    pad = " " * indent
    rendered = f'"{value}"' if quoted else value
    return [f"{pad}{key}: {rendered}"]
