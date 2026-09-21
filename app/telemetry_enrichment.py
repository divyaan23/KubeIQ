from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("workload-analyzer.telemetry")

# In-cluster service DNS for the existing telemetry-dashboard, which already
# observes provider/model directly from live sidecar traffic.
TELEMETRY_DASHBOARD_URL = os.getenv(
    "TELEMETRY_DASHBOARD_URL",
    "http://aipolicy-telemetry-dashboard.ai-policy-system.svc.cluster.local:8090",
)


def fetch_observed_models() -> dict[tuple[str, str], dict]:
    """Maps (namespace, workload) -> the dominant provider/model seen in live traffic.

    Best-effort only: returns an empty map if the telemetry-dashboard is unreachable
    or disabled, so the agent's manifest-based inference remains the fallback.
    """
    if not TELEMETRY_DASHBOARD_URL:
        return {}

    try:
        response = httpx.get(f"{TELEMETRY_DASHBOARD_URL}/api/inventory", timeout=5)
        response.raise_for_status()
        rows = response.json().get("workloads", [])
    except Exception:
        logger.warning("Could not reach telemetry-dashboard for model enrichment", exc_info=True)
        return {}

    # Rows are ordered by request volume descending, so the first row per
    # (namespace, workload) is the dominant provider/model for that workload.
    observed: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.get("namespace"), row.get("workload"))
        if key not in observed:
            observed[key] = {
                "provider": row.get("provider"),
                "model": row.get("model"),
                "requests": row.get("requests", 0),
            }
    return observed


def fetch_violations(limit: int = 100) -> list[dict]:
    """Recent policy violations from live traffic, grouped by policy in the caller.

    Best-effort only: returns an empty list if the telemetry-dashboard is unreachable,
    so the governance panel still renders (just without violation history).
    """
    if not TELEMETRY_DASHBOARD_URL:
        return []

    try:
        response = httpx.get(f"{TELEMETRY_DASHBOARD_URL}/api/violations", params={"limit": limit}, timeout=5)
        response.raise_for_status()
        return response.json().get("items", [])
    except Exception:
        logger.warning("Could not reach telemetry-dashboard for violation enrichment", exc_info=True)
        return []
