import pytest

from tokentracker.codex_pricing import compute_cost_usd


def test_known_model_computes_cost():
    # gpt-5.1: input $1.25/M, output $10/M
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
    # cached_input rate per OpenAI pricing is 10% of input rate.
    fresh = compute_cost_usd("gpt-5.1", 1_000_000, 0, 0, 0)
    cached = compute_cost_usd("gpt-5.1", 1_000_000, 1_000_000, 0, 0)
    assert cached is not None and fresh is not None
    assert cached == pytest.approx(fresh * 0.1)


def test_reasoning_is_already_in_output_total():
    # Reasoning is a subset of the inclusive output count.
    only_output = compute_cost_usd("gpt-5.1", 0, 0, 100_000, 0)
    only_reasoning = compute_cost_usd("gpt-5.1", 0, 0, 100_000, 100_000)
    assert only_output == only_reasoning


def test_zero_tokens_returns_zero_for_known_model():
    assert compute_cost_usd("gpt-5.1", 0, 0, 0, 0) == 0.0


def test_mixed_input_and_reasoning_are_not_double_counted():
    assert compute_cost_usd('gpt-5.1', 1000, 800, 200, 100) == pytest.approx(0.00235)


@pytest.mark.parametrize('model, expected', [('gpt-6-astra', 1.5), ('gpt-5.6-sol', .6), ('gpt-5.6-terra', .32), ('gpt-5.6-luna', .032), ('gpt-5.2-codex', .315)])
def test_verified_standard_prices(model, expected):
    assert compute_cost_usd(model, 100_000, 0, 10_000, 0) == pytest.approx(expected)


def test_long_context_rates():
    assert compute_cost_usd('gpt-6-astra', 300_000, 100_000, 10_000, 5_000) == pytest.approx(4.95)


@pytest.mark.parametrize('values', [(-1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 0, 1)])
def test_invalid_counts_do_not_produce_misleading_cost(values):
    with pytest.raises(ValueError):
        compute_cost_usd('gpt-5.1', *values)


def test_cache_writes_replace_uncached_input_rate():
    # 500 fresh * $4 + 300 cached * $.4 + 200 writes * $5 + 100 output * $20.
    assert compute_cost_usd('gpt-5.6-sol', 1000, 300, 100, 50, cache_write_tokens=200) == pytest.approx(.00512)


def test_cache_writes_receive_long_context_multiplier():
    assert compute_cost_usd('gpt-6-astra', 300_000, 100_000, 10_000, 0, cache_write_tokens=100_000) == pytest.approx(5.45)


def test_cache_writes_on_earlier_models_have_no_additional_charge():
    assert compute_cost_usd('gpt-5.1', 1000, 0, 0, 0, cache_write_tokens=1000) == pytest.approx(.00125)


def test_cache_write_and_read_subsets_must_fit_input():
    with pytest.raises(ValueError):
        compute_cost_usd('gpt-5.6-sol', 1000, 800, 0, 0, cache_write_tokens=300)


@pytest.mark.parametrize('model, expected', [('gpt-5.5', .8), ('gpt-5.4', .4), ('gpt-5.3-codex', .315)])
def test_earlier_codex_model_prices(model, expected):
    assert compute_cost_usd(model, 100_000, 0, 10_000, 0) == pytest.approx(expected)


def test_gpt_55_long_context_has_no_cache_write_surcharge():
    assert compute_cost_usd('gpt-5.5', 300_000, 0, 10_000, 0, cache_write_tokens=100_000) == pytest.approx(3.45)


def test_long_context_threshold_is_strict_and_applies_to_whole_request():
    assert compute_cost_usd('gpt-6-astra', 272_000, 0, 1000, 0) == pytest.approx(2.77)
    assert compute_cost_usd('gpt-6-astra', 272_001, 0, 1000, 0) == pytest.approx(5.51502)
