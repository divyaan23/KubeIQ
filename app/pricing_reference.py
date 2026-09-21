"""Best-effort public list pricing, used only when an AIPolicy doesn't configure
its own spec.pricing. These are approximate USD-per-million-token rates and go
stale as providers change pricing — treat them as a fallback estimate, never as
a substitute for the reviewer's actual contracted/negotiated rates.

Keyed by (provider, model-prefix). Model lookup matches by prefix so version
suffixes (e.g. "gpt-4o-2024-08-06") still resolve to the right row.
"""

from __future__ import annotations

# (provider, model_prefix) -> (input_per_million_usd, output_per_million_usd)
_REFERENCE_PRICING: list[tuple[str, str, float, float]] = [
    ("azure-openai", "gpt-4o-mini", 0.15, 0.60),
    ("azure-openai", "gpt-4o", 2.50, 10.00),
    ("azure-openai", "gpt-4.1-mini", 0.40, 1.60),
    ("azure-openai", "gpt-4.1-nano", 0.10, 0.40),
    ("azure-openai", "gpt-4.1", 2.00, 8.00),
    ("azure-openai", "gpt-4-turbo", 10.00, 30.00),
    ("azure-openai", "gpt-35-turbo", 0.50, 1.50),
    ("azure-ai-foundry", "gpt-4o-mini", 0.15, 0.60),
    ("azure-ai-foundry", "gpt-4o", 2.50, 10.00),
    ("azure-ai-foundry", "gpt-4.1-mini", 0.40, 1.60),
    ("azure-ai-foundry", "gpt-4.1", 2.00, 8.00),
    ("openai", "gpt-4o-mini", 0.15, 0.60),
    ("openai", "gpt-4o", 2.50, 10.00),
    ("openai", "gpt-4.1-mini", 0.40, 1.60),
    ("openai", "gpt-4.1", 2.00, 8.00),
    ("openai", "gpt-4-turbo", 10.00, 30.00),
    ("anthropic", "claude-3.5-sonnet", 3.00, 15.00),
    ("anthropic", "claude-3.5-haiku", 0.80, 4.00),
    ("anthropic", "claude-3-opus", 15.00, 75.00),
    ("anthropic", "claude-3-haiku", 0.25, 1.25),
]


def lookup_reference_pricing(provider: str | None, model: str | None) -> dict[str, float] | None:
    """Returns {"input_per_million_usd", "output_per_million_usd"} for a known
    provider/model pair, or None when there's no reference entry — e.g. for
    self-hosted models, where cost is infrastructure-based, not per-token."""
    if not provider or not model:
        return None
    provider_key = provider.strip().lower()
    model_key = model.strip().lower()
    if provider_key in ("self-hosted", "unknown"):
        return None

    for ref_provider, ref_model_prefix, input_price, output_price in _REFERENCE_PRICING:
        if ref_provider != provider_key:
            continue
        if model_key.startswith(ref_model_prefix):
            return {"input_per_million_usd": input_price, "output_per_million_usd": output_price}
    return None
