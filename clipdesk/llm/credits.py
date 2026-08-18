"""Turning token counts into GitHub AI Credits.

GitHub moved Copilot to usage-based billing on 1 June 2026. Interactions are
priced per token against the model that answered, and the total is expressed in
GitHub AI Credits, where **1 credit = $0.01 USD**. So credits and tokens are not
alternatives: credits are what tokens cost, and the model decides the rate.

Two honesty notes, because the difference between our figure and the one on a
GitHub bill has to be explainable:

* **Cached input is not modelled.** Copilot charges a tenth of the input rate
  for tokens the model reuses. The bridge does not tell us how many were cached,
  so every input token is priced at the full rate and our figure is a ceiling.
* **The auto-selection discount is not applied.** GitHub gives 10% off when
  *its* auto model selection picks the model. ClipDesk picking the model is not
  the same feature, so claiming the discount would understate the bill.

Prices are per one million tokens and change; they live in one table so a change
is one edit, and history reprices because credits are computed on read rather
than frozen at the time of the call.
"""

from __future__ import annotations

import re
from typing import Any

#: What one GitHub AI Credit is worth, by definition.
CREDIT_USD = 0.01

#: Model -> (input $/1M tokens, output $/1M tokens), from GitHub's published
#: pricing. Long-context tiers cost more above 200-272K input tokens; ClipDesk's
#: largest window is far below that, so the default tier is what applies.
PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.3-codex": (1.75, 14.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
    # Anthropic
    "claude-haiku-4.5": (1.00, 5.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet-4.5": (3.00, 15.00),
    "claude-sonnet-4.6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4.5": (5.00, 25.00),
    "claude-opus-4.6": (5.00, 25.00),
    "claude-opus-4.7": (5.00, 25.00),
    "claude-opus-4.8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    # Google
    "gemini-3.1-pro": (2.00, 12.00),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    # xAI
    "grok-4.5": (2.00, 6.00),
    "grok-4.6": (2.00, 6.00),
    # Microsoft
    "mai-code-1-flash": (0.75, 4.50),
    "mai-code-1.1-flash": (0.20, 1.20),
    # GitHub
    "raptor-mini": (0.25, 2.00),
    # Moonshot AI
    "kimi-k2.7-code": (0.95, 4.00),
    "kimi-k3": (3.00, 15.00),
}

#: Suffixes providers add that do not change the price.
_NOISE = re.compile(r"-(preview|latest|stable|ga|beta|thinking)$")


def _normalise(model: str) -> str:
    name = model.strip().lower()
    while True:
        trimmed = _NOISE.sub("", name)
        if trimmed == name:
            return name
        name = trimmed


def price_for(model: str) -> tuple[float, float] | None:
    """Input and output price per million tokens, or None if unlisted.

    Unlisted is reported rather than guessed: a made-up rate on an internal or
    brand-new model would be worse than admitting the total is incomplete.
    """
    name = _normalise(model)
    if name in PRICES:
        return PRICES[name]
    # Longest first, so gpt-5.4-mini is not matched by gpt-5.4.
    for known in sorted(PRICES, key=len, reverse=True):
        if name.startswith(known):
            return PRICES[known]
    return None


def credits_for(by_model: dict[str, Any] | None) -> dict[str, Any]:
    """Price per-model token counts, and say which models could not be priced."""
    credits = 0.0
    unpriced: list[str] = []
    for model, entry in (by_model or {}).items():
        price = price_for(model)
        if price is None:
            unpriced.append(model)
            continue
        prompt = int(entry.get("prompt", 0) or 0)
        completion = int(entry.get("completion", 0) or 0)
        usd = (prompt * price[0] + completion * price[1]) / 1_000_000
        credits += usd / CREDIT_USD
    return {
        "credits": round(credits, 2),
        "usd": round(credits * CREDIT_USD, 4),
        "unpriced": sorted(unpriced),
    }


def credits_for_tokens(tokens: dict[str, Any] | None) -> dict[str, Any]:
    """Price a stored usage block, coping with totals recorded before this existed.

    Older projects kept only the models involved, not the tokens each one used.
    With a single model the split is not in doubt, so those price exactly. With
    several there is no honest way to divide the total, and reporting them as
    unpriced shows the reader a caveat instead of a confident zero.
    """
    if not tokens or not tokens.get("total_tokens"):
        return {"credits": 0.0, "usd": 0.0, "unpriced": []}

    by_model = tokens.get("by_model")
    if not by_model:
        models = [str(name) for name in (tokens.get("models") or [])]
        if len(models) != 1:
            return {"credits": 0.0, "usd": 0.0, "unpriced": sorted(models) or ["unknown"]}
        by_model = {
            models[0]: {
                "prompt": tokens.get("prompt_tokens", 0),
                "completion": tokens.get("completion_tokens", 0),
            }
        }
    return credits_for(by_model)
