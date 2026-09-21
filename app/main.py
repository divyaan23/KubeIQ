from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from fastapi import FastAPI, HTTPException
from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from app.agent_client import classify_workload, generate_token_recommendation
from app.dashboard import router as dashboard_router
from app.k8s_collector import collect_workload_evidence
from app.policy_recommender import build_recommendation, fetch_token_stats
from app.pricing_reference import lookup_reference_pricing
from app.telemetry_enrichment import fetch_observed_models, fetch_violations

logger = logging.getLogger("workload-analyzer")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

app = FastAPI(title="AI Kubernetes Workload Analyzer", version="v1")
app.include_router(dashboard_router)
custom_api: client.CustomObjectsApi | None = None

# In-memory cache keyed by (namespace, workload) -> {"hash": evidence_hash, "classification": {...}}
# Avoids re-invoking the agent when a workload's evidence hasn't changed since the last scan.
_classification_cache: dict[tuple[str, str], dict[str, Any]] = {}


def _initialize_kubernetes_client() -> None:
    global custom_api
    try:
        config.load_incluster_config()
    except ConfigException:
        try:
            config.load_kube_config()
        except ConfigException:
            return
    custom_api = client.CustomObjectsApi()


@app.on_event("startup")
def startup() -> None:
    _initialize_kubernetes_client()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _list_policies() -> list[dict[str, Any]]:
    if custom_api is None:
        return []
    try:
        response = custom_api.list_cluster_custom_object(
            group="ai.policy.io",
            version="v1alpha1",
            plural="aipolicies",
        )
    except Exception:
        return []
    return response.get("items", [])


def _matching_policy_name(policies: list[dict[str, Any]], namespace: str, labels: dict[str, str]) -> str:
    for policy in policies:
        metadata = policy.get("metadata") or {}
        if metadata.get("namespace") not in (namespace, None, ""):
            continue
        selector_labels = ((policy.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector_labels:
            continue
        if all(
            fnmatchcase(labels.get(key, ""), pattern)
            for key, pattern in selector_labels.items()
        ):
            return metadata.get("name", "unknown")
    return "Missing"


def _matching_policy(policies: list[dict[str, Any]], namespace: str, labels: dict[str, str]) -> dict[str, Any] | None:
    for policy in policies:
        metadata = policy.get("metadata") or {}
        if metadata.get("namespace") not in (namespace, None, ""):
            continue
        selector_labels = ((policy.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector_labels:
            continue
        if all(
            fnmatchcase(labels.get(key, ""), pattern)
            for key, pattern in selector_labels.items()
        ):
            return policy
    return None


def _evidence_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _apply_observed_traffic(classification: dict[str, Any], observed: dict[str, Any] | None) -> dict[str, Any]:
    """Runtime-observed provider/model outrank the agent's manifest-only guess."""
    if not observed or not observed.get("model"):
        return classification
    return {
        **classification,
        "is_ai_workload": True,
        "confidence": 1.0,
        "provider": observed.get("provider") or classification.get("provider"),
        "model": observed.get("model"),
        "reasoning": f"Confirmed from {observed.get('requests', 0)} observed runtime request(s) via the sidecar, not inferred from manifest evidence.",
        "source": "runtime_observed",
    }


@app.get("/api/workloads")
def list_workloads(refresh: bool = False) -> dict[str, Any]:
    policies = _list_policies()
    observed_models = fetch_observed_models()
    workloads = []
    for evidence in collect_workload_evidence():
        payload = evidence.to_agent_payload()
        cache_key = (evidence.namespace, evidence.workload)
        payload_hash = _evidence_hash(payload)
        cached = _classification_cache.get(cache_key)

        if not refresh and cached and cached["hash"] == payload_hash:
            classification = cached["classification"]
        else:
            classification = classify_workload(payload)
            classification.setdefault("source", "agent_inferred")
            _classification_cache[cache_key] = {"hash": payload_hash, "classification": classification}

        classification = _apply_observed_traffic(classification, observed_models.get(cache_key))

        workloads.append(
            {
                "namespace": evidence.namespace,
                "workload": evidence.workload,
                "kind": evidence.kind,
                "images": evidence.images,
                "replicas": evidence.replica_count,
                "gpu_requested": evidence.gpu_requested,
                "policy": _matching_policy_name(policies, evidence.namespace, evidence.labels),
                "ai_classification": classification,
            }
        )

    return {"workloads": workloads}


@app.get("/api/policy-governance")
def policy_governance() -> dict[str, Any]:
    """Read-only snapshot of AIPolicy resources currently applied in the cluster,
    for the status popover — separate from the per-workload generator flow."""
    policies = _list_policies()
    total = 0
    governed = 0
    workloads_by_policy: dict[str, list[dict[str, str]]] = {}
    for evidence in collect_workload_evidence():
        total += 1
        policy_name = _matching_policy_name(policies, evidence.namespace, evidence.labels)
        if policy_name != "Missing":
            governed += 1
            workloads_by_policy.setdefault(policy_name, []).append(
                {"namespace": evidence.namespace, "workload": evidence.workload}
            )

    violations_by_policy: dict[str, list[dict[str, Any]]] = {}
    for item in fetch_violations():
        policy_name = item.get("policy_name") or "unknown"
        violation = item.get("violation") or {}
        violations_by_policy.setdefault(policy_name, []).append(
            {
                "observed_at": item.get("observed_at"),
                "namespace": item.get("namespace"),
                "workload": item.get("workload"),
                "violation_reason": violation.get("message") or violation.get("code"),
                "decision": item.get("decision"),
            }
        )

    standards = [
        {
            "namespace": (policy.get("metadata") or {}).get("namespace", "default"),
            "name": (policy.get("metadata") or {}).get("name", "unknown"),
            "scope": ((policy.get("spec") or {}).get("selector") or {}).get("matchLabels") or {},
            "allowed_registries": ((policy.get("spec") or {}).get("admission") or {}).get("allowedRegistries") or [],
            "require_sidecar": bool(((policy.get("spec") or {}).get("admission") or {}).get("requireSidecar", False)),
            "allowed_providers": ((policy.get("spec") or {}).get("runtime") or {}).get("allowedProviders") or [],
            "allowed_models": ((policy.get("spec") or {}).get("runtime") or {}).get("allowedModels") or [],
            "max_input_tokens": ((policy.get("spec") or {}).get("runtime") or {}).get("maxInputTokens"),
            "max_output_tokens": ((policy.get("spec") or {}).get("runtime") or {}).get("maxOutputTokens"),
            "runtime_action": ((policy.get("spec") or {}).get("runtime") or {}).get("action", "audit"),
            "enforcement_active": ((policy.get("spec") or {}).get("runtime") or {}).get("action") == "block",
            "max_gpus": ((policy.get("spec") or {}).get("gpu") or {}).get("maxGPUs"),
            "allowed_gpu_types": ((policy.get("spec") or {}).get("gpu") or {}).get("allowedTypes") or [],
            "generation": (policy.get("metadata") or {}).get("generation", 1),
            "updated_at": (policy.get("metadata") or {}).get("creationTimestamp", ""),
            "governed_workloads": workloads_by_policy.get((policy.get("metadata") or {}).get("name", "unknown"), []),
            "violations_count": len(violations_by_policy.get((policy.get("metadata") or {}).get("name", "unknown"), [])),
            "violations": violations_by_policy.get((policy.get("metadata") or {}).get("name", "unknown"), [])[:5],
        }
        for policy in policies
    ]

    return {
        "posture": {"total": total, "governed": governed, "missing": total - governed},
        "standards": standards,
    }


@app.get("/api/token-recommendation")
def token_recommendation(namespace: str, workload: str) -> dict[str, Any]:
    """Agent-generated token limit recommendation from observed usage — a narrative
    counterpart to the deterministic P99+10% calculation on the telemetry dashboard."""
    evidence = next(
        (
            item
            for item in collect_workload_evidence()
            if item.namespace == namespace and item.workload == workload
        ),
        None,
    )
    if evidence is None:
        raise HTTPException(status_code=404, detail="Workload not found in current cluster scan")

    token_stats = fetch_token_stats(namespace, workload)
    current_policy = _matching_policy(_list_policies(), namespace, evidence.labels)
    runtime_spec = ((current_policy or {}).get("spec") or {}).get("runtime") or {}
    pricing_spec = ((current_policy or {}).get("spec") or {}).get("pricing") or {}

    # Reviewer-configured pricing on the AIPolicy always wins; only fall back to
    # the built-in public-list reference table when the policy has none.
    input_price = pricing_spec.get("inputPerMillion")
    output_price = pricing_spec.get("outputPerMillion")
    pricing_source = "policy" if (input_price is not None or output_price is not None) else None
    if pricing_source is None:
        cache_key = (namespace, workload)
        cached_classification = (_classification_cache.get(cache_key) or {}).get("classification") or {}
        provider = (token_stats or {}).get("provider") or cached_classification.get("provider")
        model = (token_stats or {}).get("model") or cached_classification.get("model")
        reference = lookup_reference_pricing(provider, model)
        if reference:
            input_price = reference["input_per_million_usd"]
            output_price = reference["output_per_million_usd"]
            pricing_source = "reference_estimate"

    successful = (token_stats or {}).get("successful") or {}
    blocked_requests = (token_stats or {}).get("blocked_requests") or []
    if not token_stats or (not successful.get("requests_analyzed") and not blocked_requests):
        return {
            "namespace": namespace,
            "workload": workload,
            "kind": evidence.kind,
            "generated_at": datetime.now(UTC).isoformat(),
            "token_stats": token_stats,
            "recommendation": None,
            "cold_start": True,
        }

    context = {
        "namespace": namespace,
        "workload": workload,
        "window_days": token_stats.get("window_days"),
        "requests_analyzed": successful.get("requests_analyzed"),
        "input": successful.get("input"),
        "output": successful.get("output"),
        "output_input_ratio_pct": successful.get("output_input_ratio_pct"),
        "current_max_input_tokens": runtime_spec.get("maxInputTokens"),
        "current_max_output_tokens": runtime_spec.get("maxOutputTokens"),
        "input_price_per_million_usd": input_price,
        "output_price_per_million_usd": output_price,
        "pricing_source": pricing_source,
        "blocked_requests_count": len(blocked_requests),
        "blocked_requests": [
            {
                "input_tokens": item.get("input_tokens"),
                "output_tokens": item.get("output_tokens"),
                "violation_reason": item.get("violation_label") or item.get("violation_reason"),
            }
            for item in blocked_requests[:10]
        ],
    }
    recommendation = generate_token_recommendation(context)

    return {
        "namespace": namespace,
        "workload": workload,
        "kind": evidence.kind,
        "generated_at": datetime.now(UTC).isoformat(),
        "token_stats": token_stats,
        "pricing": {
            "input_per_million_usd": input_price,
            "output_per_million_usd": output_price,
            "source": pricing_source,
        },
        "recommendation": recommendation,
        "cold_start": False,
    }


@app.get("/api/policy-recommendations")
def policy_recommendation(namespace: str, workload: str, guidance: str | None = None) -> dict[str, Any]:
    evidence = next(
        (
            item
            for item in collect_workload_evidence()
            if item.namespace == namespace and item.workload == workload
        ),
        None,
    )
    if evidence is None:
        raise HTTPException(status_code=404, detail="Workload not found in current cluster scan")

    payload = evidence.to_agent_payload()
    cache_key = (namespace, workload)
    cached = _classification_cache.get(cache_key)
    payload_hash = _evidence_hash(payload)
    if cached and cached["hash"] == payload_hash:
        classification = cached["classification"]
    else:
        classification = classify_workload(payload)
        classification.setdefault("source", "agent_inferred")
        _classification_cache[cache_key] = {"hash": payload_hash, "classification": classification}

    observed = fetch_observed_models().get(cache_key)
    classification = _apply_observed_traffic(classification, observed)

    return build_recommendation(
        namespace=namespace,
        workload=workload,
        images=evidence.images,
        labels=evidence.labels,
        gpu_requested=evidence.gpu_requested,
        classification=classification,
        user_guidance=guidance,
        kind=evidence.kind,
    )
