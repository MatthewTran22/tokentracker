"""Per-model Codex pricing.

Rates are USD per 1,000,000 tokens. Source: OpenAI pricing page.
When updating, change the `verified` date in the comment below.

Unknown / missing model -> compute_cost_usd returns None. The row still
inserts (so request counts and token totals are preserved), but contributes
nothing to leaderboard cost. The price table is intentionally small and
explicit; auditing it is an ops task, not a runtime concern.
"""
import logging

log = logging.getLogger("tokentracker.codex_pricing")

# verified 2026-05-20 (illustrative — confirm against current OpenAI pricing
# before merging this change).
PRICES: dict[str, dict[str, float]] = {
    "gpt-5.1": {
        "input": 1.25,
        "cached_input": 0.125,
        "output": 10.00,
        "reasoning_output": 10.00,
    },
    # Add other Codex-supported models here.
}


def compute_cost_usd(
    model: str | None,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> float | None:
    """Return USD cost, or None if the model is unknown or missing.

    `tool_tokens` is intentionally excluded — OpenAI does not bill tool
    tokens separately from input/output, so including them would risk
    double-counting.
    """
    if not model:
        return None
    rates = PRICES.get(model)
    if rates is None:
        log.warning("codex_pricing: unknown model %r", model)
        return None
    return (
        (input_tokens * rates["input"])
        + (cached_input_tokens * rates["cached_input"])
        + (output_tokens * rates["output"])
        + (reasoning_output_tokens * rates["reasoning_output"])
    ) / 1_000_000
