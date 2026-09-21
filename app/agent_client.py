from __future__ import annotations

import json
import logging
import os
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

logger = logging.getLogger("workload-analyzer.agent")

MODEL_DEPLOYMENT = os.getenv("AI_ANALYZER_MODEL_DEPLOYMENT", "gpt-4o")

INSTRUCTIONS = """You are the AI Workload Classification Agent for a Kubernetes governance platform.

You receive structured evidence about a single Kubernetes workload: namespace, workload name,
kind, container images, pod labels, environment variable NAMES ONLY (never values), ports,
command, args, GPU requests, and replica count. You never receive secret values.

Decide whether this workload is an AI/LLM workload, and if so, your best determination of the
AI provider and model family, using only the evidence given. Do not invent details that are not
supported by the evidence. If the evidence is ambiguous, lower your confidence and set provider
or model to null rather than guessing.

Respond with ONLY a single JSON object, no prose, no markdown fences, in exactly this shape:
{
  "is_ai_workload": <bool>,
  "confidence": <float between 0 and 1>,
  "provider": <string or null, e.g. "azure-openai", "azure-ai-foundry", "openai", "anthropic", "self-hosted">,
  "model": <string or null, e.g. "gpt-4o", "gpt-5", "llama-3.3-70b">,
  "reasoning": <string, at most 200 characters, citing the evidence fields you used>
}
"""

POLICY_GENERATION_INSTRUCTIONS = """You are the AI Policy Generation Agent for a Kubernetes governance platform.

You decide the content of a draft AIPolicy for one Kubernetes workload, using ONLY the evidence
given to you:
- observed_images: container image references actually running for this workload
- classification: the provider/model already determined for this workload, and how confidently
  (source "runtime_observed" means it was confirmed from real traffic; "agent_inferred" means it
  was inferred from Kubernetes manifest evidence only)
- token_stats: observed input/output token percentiles from the last 7 days of real traffic, or
  null if no traffic has been observed yet
- gpu_requested: GPU count actually requested by the workload's containers
- reviewer_guidance: optional free-text instructions from a human reviewer

There are exactly two modes. Determine which one applies before deciding any field:

MODE 1 - NO GUIDANCE (reviewer_guidance is null or empty):
Generate a complete evidence-based baseline. Populate every field from the evidence:
- allowedRegistries: registry host of observed_images (e.g. "myregistry.io/app:v1" implies
  "myregistry.io/*").
- allowedProviders / allowedModels: exactly the classification's provider/model.
- maxInputTokens / maxOutputTokens: if token_stats.successful.requests_analyzed >= 30, use its
  recommended_limit values (already computed, P99 x1.10). If between 1 and 29, use them but say
  the sample is preliminary. If token_stats is null or zero requests, use 4000 input / 1000 output
  and say plainly no traffic has been observed yet.
- action: "audit".
- maxGPUs: gpu_requested as-is. allowedGPUTypes: [].

MODE 2 - GUIDANCE PROVIDED (reviewer_guidance is non-empty):
Interpret ONLY what the reviewer actually asked for. This is a strict-scoping mode: a field the
reviewer did not discuss must be left UNRESTRICTED, not filled from evidence, even though the
evidence exists. Concretely:
- If reviewer_guidance discusses registries, set allowedRegistries to exactly what they asked
  (e.g. "allow only docker.io" -> ["docker.io/*"] alone, replacing any evidence-based registry;
  "also allow docker.io" -> evidence-based registries PLUS docker.io/*). If they did not mention
  registries at all, set allowedRegistries to [] (unrestricted).
- The same logic applies independently to allowedProviders/allowedModels, maxInputTokens/
  maxOutputTokens, action, and maxGPUs/allowedGPUTypes: only populate a field if reviewer_guidance
  actually discusses that specific dimension. Otherwise return null for that field (scalars) or []
  (lists) to mean "left unrestricted, not part of this request".
- Never fill an untouched field from token_stats, classification, or gpu_requested in this mode.
  Evidence may still appear in your rationale text as context, but must not silently populate a
  field the reviewer never asked about.

Rationale requirements (both modes):
- Every rationale entry must say which evidence field or reviewer instruction drove that specific
  decision, and must explicitly say when a field was left unrestricted because it was out of scope
  for the reviewer's request.
- If allowedProviders, allowedModels, maxInputTokens, maxOutputTokens, or action end up non-empty
  (any runtime restriction is actually being set), add one final rationale entry reminding the
  reviewer that: (1) enforcing these requires the runtime-sidecar, which is only injected into
  pods labeled "ai-policy.io/runtime: enabled" — add that label to the workload's pod template if
  it is missing; and (2) the sidecar is injected at pod creation only, so existing pods must be
  restarted (e.g. a rollout restart) after applying this policy before enforcement takes effect.
  If no runtime restriction was set at all, state instead that only the registry check applies and
  no sidecar or restart is required.

Respond with ONLY a single JSON object, no prose, no markdown fences, in exactly this shape.
Scalars use JSON null (not the string "null") when a field is intentionally left unset in Mode 2:
{
  "allowedRegistries": [<string>, ...],
  "allowedProviders": [<string>, ...],
  "allowedModels": [<string>, ...],
  "maxInputTokens": <positive integer> | null,
  "maxOutputTokens": <positive integer> | null,
  "action": "audit" | "warn" | "block" | null,
  "maxGPUs": <integer >= 0> | null,
  "allowedGPUTypes": [<string>, ...],
  "rationale": [<string>, ...]
}
"""

TOKEN_RECOMMENDATION_INSTRUCTIONS = """You are the Token Optimization Agent for a Kubernetes AI governance platform.
Apply AI token-economics practice (see: measuring token use per workload, setting budgets/limits
with headroom, watching for inefficient prompts, and forecasting cost from real usage) rather than
just curve-fitting a percentile.

You receive observed token usage for one workload's AI traffic over a window of window_days:
- namespace, workload, window_days
- requests_analyzed: count of successful requests the input/output stats below are based on
- input / output: each an object with average, p50, p95, p99, max observed token counts from
  SUCCESSFUL requests only
- output_input_ratio_pct: output tokens as a percent of input tokens across successful traffic
- current_max_input_tokens / current_max_output_tokens: the workload's existing AIPolicy runtime
  limits, or null if the workload has no limits configured yet
- input_price_per_million_usd / output_price_per_million_usd: token pricing to use for cost
  estimates, or null if no pricing is available at all
- pricing_source: "policy" when the reviewer explicitly configured this price on the AIPolicy,
  "reference_estimate" when it's a built-in public-list-price fallback (not this workload's
  actual contracted rate), or null when no price is available
- blocked_requests_count: how many requests were rejected by the current policy for exceeding a
  token limit in this window
- blocked_requests: up to 10 of those blocked requests, each with the input_tokens/output_tokens
  that were rejected and the violation_reason (e.g. "Input token limit exceeded")

Recommend maxInputTokens and maxOutputTokens for this workload's AIPolicy runtime enforcement,
using ALL of the signals together, not just the successful-traffic percentiles:
- Blocked requests are direct evidence the current limit is too tight for real traffic. If any
  blocked_requests exist, your suggested limit for that dimension (input or output) MUST be at
  least as high as the highest blocked value for that dimension, with headroom above it — do not
  recommend a limit that would still reject those same requests.
- If there are no blocked requests, favor limits with headroom above observed P99 successful
  traffic so normal requests are not blocked, while still capping runaway/outlier prompts.
- Cost awareness: if input_price_per_million_usd or output_price_per_million_usd is not null,
  estimate the USD cost of one average request and of the P99 request, and reason about whether
  raising a limit meaningfully changes worst-case per-request cost. Include this as a rationale
  entry. If pricing_source is "reference_estimate", explicitly label the estimate as based on
  public list pricing, not this workload's actual contracted rate, and suggest the reviewer
  configure spec.pricing on the AIPolicy for an accurate figure. If pricing is null, say cost
  impact cannot be estimated because no pricing is available, instead of inventing a price.
- Prompt efficiency: if output_input_ratio_pct is low (well under ~25%) while average input tokens
  is large (over ~1500), call this out as a sign of prompt/context bloat (e.g. unneeded system
  instructions, retrieved context, or untrimmed conversation history) and recommend optimizing the
  prompt/context INSTEAD OF, or in addition to, just raising maxInputTokens — token economics
  favors reducing unnecessary tokens over only expanding limits to absorb them.
- Scale awareness: use requests_analyzed and window_days to describe whether this looks like
  low-volume/pilot traffic (fewer than ~30 requests over the window) or steadier production
  traffic, and note that pilot-stage limits may need revisiting once volume grows — don't
  over-commit to a permanent number from a small sample.
- Flag when the sample size is small (fewer than 30 total requests, successful + blocked) since
  that lowers confidence in the exact numbers, but still give your best concrete recommendation
  rather than refusing to answer.
- Do not invent values that contradict the evidence.

Respond with ONLY a single JSON object, no prose, no markdown fences, in exactly this shape:
{
  "suggested_max_input_tokens": <positive integer or null>,
  "suggested_max_output_tokens": <positive integer or null>,
  "confidence": <float between 0 and 1>,
  "rationale": [<string>, ...]
}
"""

_openai_client = None


def _client():
    """Reuses the same Foundry project as the chatbot; created lazily so import never fails."""
    global _openai_client
    if _openai_client is None:
        project = AIProjectClient(
            endpoint=os.getenv("AI_PROJECT_ENDPOINT", ""),
            credential=DefaultAzureCredential(),
        )
        _openai_client = project.get_openai_client(api_version="2024-10-21")
    return _openai_client


def classify_workload(evidence: dict) -> dict:
    """Sends one workload's evidence to the model and returns its structured verdict."""
    fallback = {
        "is_ai_workload": False,
        "confidence": 0.0,
        "provider": None,
        "model": None,
        "reasoning": "Agent classification unavailable.",
    }
    try:
        response = _client().chat.completions.create(
            model=MODEL_DEPLOYMENT,
            response_format={"type": "json_object"},
            timeout=30,
            messages=[
                {"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": json.dumps(evidence, separators=(",", ":"))},
            ],
        )
        parsed = json.loads(response.choices[0].message.content)
        return {
            "is_ai_workload": bool(parsed.get("is_ai_workload", False)),
            "confidence": float(parsed.get("confidence", 0.0)),
            "provider": parsed.get("provider"),
            "model": parsed.get("model"),
            "reasoning": str(parsed.get("reasoning", ""))[:200],
        }
    except Exception:
        logger.exception("Agent classification failed for workload evidence")
        return fallback


def _optional_int(value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    return max(minimum, int(value))


def generate_policy_recommendation(evidence: dict) -> dict | None:
    """Lets the agent decide the full policy content from raw evidence + optional reviewer
    guidance. Returns None on any failure so the caller can fall back to a safe minimal policy.

    Scalar fields (maxInputTokens/maxOutputTokens/maxGPUs/action) may come back as None when the
    reviewer's guidance didn't address that dimension — the caller must render those as omitted
    from the YAML, not as a literal zero/default.
    """
    try:
        response = _client().chat.completions.create(
            model=MODEL_DEPLOYMENT,
            response_format={"type": "json_object"},
            timeout=30,
            messages=[
                {"role": "system", "content": POLICY_GENERATION_INSTRUCTIONS},
                {"role": "user", "content": json.dumps(evidence, separators=(",", ":"))},
            ],
        )
        parsed = json.loads(response.choices[0].message.content)
        action = parsed.get("action")
        action = str(action).lower() if action is not None else None
        return {
            "allowedRegistries": [str(r) for r in parsed.get("allowedRegistries") or []],
            "allowedProviders": [str(p) for p in parsed.get("allowedProviders") or []],
            "allowedModels": [str(m) for m in parsed.get("allowedModels") or []],
            "maxInputTokens": _optional_int(parsed.get("maxInputTokens"), minimum=1),
            "maxOutputTokens": _optional_int(parsed.get("maxOutputTokens"), minimum=1),
            "action": action if action in ("audit", "warn", "block") else None,
            "maxGPUs": _optional_int(parsed.get("maxGPUs"), minimum=0),
            "allowedGPUTypes": [str(t) for t in parsed.get("allowedGPUTypes") or []],
            "rationale": [str(r) for r in parsed.get("rationale") or []],
        }
    except Exception:
        logger.exception("Agent policy generation failed; caller will fall back to a safe default")
        return None


def generate_token_recommendation(context: dict) -> dict | None:
    """Lets the agent recommend token limits from observed usage stats — a narrative
    counterpart to the telemetry dashboard's deterministic P99+10% calculation."""
    try:
        response = _client().chat.completions.create(
            model=MODEL_DEPLOYMENT,
            response_format={"type": "json_object"},
            timeout=30,
            messages=[
                {"role": "system", "content": TOKEN_RECOMMENDATION_INSTRUCTIONS},
                {"role": "user", "content": json.dumps(context, separators=(",", ":"))},
            ],
        )
        parsed = json.loads(response.choices[0].message.content)
        return {
            "suggested_max_input_tokens": _optional_int(parsed.get("suggested_max_input_tokens"), minimum=1),
            "suggested_max_output_tokens": _optional_int(parsed.get("suggested_max_output_tokens"), minimum=1),
            "confidence": float(parsed.get("confidence", 0.0)),
            "rationale": [str(r) for r in parsed.get("rationale") or []],
        }
    except Exception:
      logger.exception("Agent token recommendation failed")
      return None

