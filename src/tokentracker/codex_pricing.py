"""Standard API-equivalent USD estimates, verified 2026-09-17.

Sources: https://developers.openai.com/api/docs/pricing and the individual
/api/docs/models/{model} pages for gpt-5.1, gpt-5.1-codex,
gpt-5.1-codex-max, gpt-5.2-codex, gpt-5.3-codex, gpt-5.4 and gpt-5.5. The gpt-6-astra and gpt-5.6-luna
model pages document the >272K input-token long-context threshold.

These are estimates, not subscription charges or invoices: service-tier and
regional adjustments and hosted-tool fees are not represented. Cache reads
and writes replace uncached input rates (not additive fees), documented at
https://developers.openai.com/api/docs/guides/prompt-caching. Unknown models stay unpriced.
Long-context estimates use each request input count; older models document
session-level premiums which cannot be reconstructed from isolated responses.
"""

PRICES: dict[str, dict[str, float]] = {
    'gpt-5.1': {'input': 1.25, 'cached_input': .125, 'output': 10.0},
    'gpt-5.1-codex': {'input': 1.25, 'cached_input': .125, 'output': 10.0},
    'gpt-5.1-codex-max': {'input': 1.25, 'cached_input': .125, 'output': 10.0},
    'gpt-5.2-codex': {'input': 1.75, 'cached_input': .175, 'output': 14.0},
    'gpt-5.3-codex': {'input': 1.75, 'cached_input': .175, 'output': 14.0},
    'gpt-5.4': {'input': 2.5, 'cached_input': .25, 'output': 15.0},
    'gpt-5.5': {'input': 5.0, 'cached_input': .5, 'output': 30.0},
    'gpt-6-astra': {'input': 10.0, 'cached_input': 1.0, 'output': 50.0},
    'gpt-5.6-sol': {'input': 4.0, 'cached_input': .4, 'output': 20.0},
    'gpt-5.6-terra': {'input': 2.0, 'cached_input': .2, 'output': 12.0},
    'gpt-5.6-luna': {'input': .2, 'cached_input': .02, 'output': 1.2},
}
_CACHE_WRITE_MODELS = {'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'}
_LONG_CONTEXT_MODELS = _CACHE_WRITE_MODELS | {'gpt-5.4', 'gpt-5.5'}


def compute_cost_usd(
    model: str | None,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    cache_write_tokens: int = 0,
) -> float | None:
    """Price inclusive input/output counts without counting subsets twice.

    Cached input is part of input_tokens; reasoning is part of output_tokens.
    Tool tokens are not separately billable and are deliberately excluded.
    """
    rates = PRICES.get(model) if model else None
    if rates is None:
        return None
    counts = (input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, cache_write_tokens)
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError('token counts must be nonnegative integers')
    if cached_input_tokens + cache_write_tokens > input_tokens or reasoning_output_tokens > output_tokens:
        raise ValueError('token subsets exceed totals')
    long_context = model in _LONG_CONTEXT_MODELS and input_tokens > 272_000
    input_multiplier = 2 if long_context else 1
    output_multiplier = 1.5 if long_context else 1
    write_rate = rates['input'] * (1.25 if model in _CACHE_WRITE_MODELS else 1)
    return (
        ((input_tokens - cached_input_tokens - cache_write_tokens) * rates['input']
         + cache_write_tokens * write_rate
         + cached_input_tokens * rates['cached_input']) * input_multiplier
        + output_tokens * rates['output'] * output_multiplier
    ) / 1_000_000
