from tokentracker.codex_pricing import compute_cost_usd


def test_known_model_computes_cost():
    # gpt-5.1: input $1.25/M, output $10/M (illustrative — must be verified)
    cost = compute_cost_usd(
        model="gpt-5.1",
        input_tokens=1_000_000,
        cached_input_tokens=0,
        output_tokens=100_000,
        reasoning_output_tokens=0,
    )
    # 1M input * $1.25 + 100k output * $10 = $1.25 + $1.00 = $2.25
    assert cost == 2.25


def test_unknown_model_returns_none():
    cost = compute_cost_usd(
        model="not-a-real-model",
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=1000,
        reasoning_output_tokens=0,
    )
    assert cost is None


def test_missing_model_returns_none():
    assert compute_cost_usd(
        model=None,
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=1000,
        reasoning_output_tokens=0,
    ) is None


def test_cached_input_uses_discount_rate():
    # All input is cached -> should be cheaper than the same volume of fresh input.
    fresh = compute_cost_usd("gpt-5.1", 1_000_000, 0, 0, 0)
    cached = compute_cost_usd("gpt-5.1", 0, 1_000_000, 0, 0)
    assert cached is not None and fresh is not None
    assert cached < fresh


def test_reasoning_billed_at_output_rate_by_default():
    # Until OpenAI distinguishes, reasoning_output rate == output rate.
    only_output = compute_cost_usd("gpt-5.1", 0, 0, 100_000, 0)
    only_reasoning = compute_cost_usd("gpt-5.1", 0, 0, 0, 100_000)
    assert only_output == only_reasoning


def test_zero_tokens_returns_zero_for_known_model():
    assert compute_cost_usd("gpt-5.1", 0, 0, 0, 0) == 0.0
