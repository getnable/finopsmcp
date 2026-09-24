# SPDX-License-Identifier: Apache-2.0
"""One price per LLM model, in one place.

The same fact lived in five tables. recommendations/bedrock_routing.py,
connectors/llm_costs.py, connectors/saas/anthropic_usage.py and
connectors/saas/openai_usage.py each carried their own per-million-token rates,
and ai_budget.py priced every Claude Code token at one blended $3/$15 whatever
model produced it. They had drifted the way copies do: none of them knew Claude
5, Opus 4.5 through 4.8 or Sonnet 4.6 by id, bedrock_routing mapped any "haiku"
it did not recognise to Claude 3 Haiku ($0.25/$1.25, a quarter of Haiku 4.5's
rate) and any "opus" to Opus 4 ($15/$75, three times Opus 4.5 and later), and
openai_usage had o3 at $10/$40, five times the $2/$8 it is listed at now.

The rule this module enforces, with tests/test_llm_price_tables.py behind it:
a model is priced here or it is unpriced. A consumer that meets a model this
table does not know reports it as unpriced; it does not borrow a sibling's rate.
A price goes in only when it was read from the provider's own published page,
and every row below says which page and when.

Rates are USD per million tokens at standard (non-batch, global) list price,
the basis every estimate in nable is quoted on. They are not anyone's bill:
negotiated discounts, batch, and credits all move the real number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The day every row below was last read from its source.
AS_OF = "2026-09-24"

ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
# Read through a search index of the model pages under
# https://developers.openai.com/api/docs/models/ because openai.com was not
# reachable from the environment the table was built in. Re-read the pricing
# page itself on the next update.
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"

SOURCES = {"anthropic": ANTHROPIC_SOURCE, "openai": OPENAI_SOURCE}


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens for one model.

    cache_write_5m / cache_write_1h are None where the provider has no separate
    write charge (OpenAI caches automatically and bills the write as ordinary
    input). cache_read is None where no cached-input rate is published, and
    those tokens are then billed as ordinary input.
    """
    model: str
    provider: str
    input: float
    output: float
    cache_write_5m: float | None
    cache_write_1h: float | None
    cache_read: float | None

    def cost(self, *, input_tokens: int = 0, output_tokens: int = 0,
             cache_write_5m_tokens: int = 0, cache_write_1h_tokens: int = 0,
             cache_read_tokens: int = 0, fast: bool = False,
             us_only: bool = False) -> float:
        """Unrounded USD for one usage record.

        fast: Anthropic fast mode, which replaces the input and output rates
        for the models in FAST_MODE and scales the cache rates with them.
        us_only: `inference_geo: "us"`, 1.1x on every category.
        """
        scale = 1.0
        inp, out = self.input, self.output
        if fast and self.model in FAST_MODE:
            f_in, f_out = FAST_MODE[self.model]
            scale = f_in / self.input
            inp, out = f_in, f_out
        w5 = (self.cache_write_5m if self.cache_write_5m is not None else self.input) * scale
        w1 = (self.cache_write_1h if self.cache_write_1h is not None else self.input) * scale
        rd = (self.cache_read if self.cache_read is not None else self.input) * scale
        usd = (input_tokens * inp + output_tokens * out + cache_write_5m_tokens * w5
               + cache_write_1h_tokens * w1 + cache_read_tokens * rd) / 1_000_000
        return usd * (US_ONLY_MULTIPLIER if us_only else 1.0)


# ── Anthropic ─────────────────────────────────────────────────────────────────
#
# (input, output, 5-minute cache write, 1-hour cache write, cache read), the
# columns of the "Model pricing" table at ANTHROPIC_SOURCE. Writes are 1.25x and
# 2x input on every model. Reads are 0.1x input except Claude Fable 5.1 and
# Mythos 5.1 (0.025x) and Claude Opus 5.5 (0.05x), which is why the read column
# is carried rather than derived.
#
# Opus 4, Opus 4.1, Sonnet 4 and Haiku 3.5 are retired on the Claude API and
# still served on Bedrock or Google Cloud, which is where nable meets them.
# Claude 3 Opus, 3 Sonnet, 3.5 Sonnet, 3.7 Sonnet and 3 Haiku are no longer on
# the page, so they are not here and read as unpriced.
_ANTHROPIC: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1":  (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-fable-5":    (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-mythos-5":   (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-opus-5-5":   (4.00,  20.00, 5.00,  8.00,  0.20),
    "claude-opus-5":     (5.00,  25.00, 6.25,  10.00, 0.50),
    "claude-opus-4-8":   (5.00,  25.00, 6.25,  10.00, 0.50),
    "claude-opus-4-7":   (5.00,  25.00, 6.25,  10.00, 0.50),
    "claude-opus-4-6":   (5.00,  25.00, 6.25,  10.00, 0.50),
    "claude-opus-4-5":   (5.00,  25.00, 6.25,  10.00, 0.50),
    "claude-opus-4-1":   (15.00, 75.00, 18.75, 30.00, 1.50),
    "claude-opus-4":     (15.00, 75.00, 18.75, 30.00, 1.50),
    "claude-sonnet-5":   (2.00,  10.00, 2.50,  4.00,  0.20),
    "claude-sonnet-4-6": (3.00,  15.00, 3.75,  6.00,  0.30),
    "claude-sonnet-4-5": (3.00,  15.00, 3.75,  6.00,  0.30),
    "claude-sonnet-4":   (3.00,  15.00, 3.75,  6.00,  0.30),
    "claude-haiku-4-5":  (1.00,  5.00,  1.25,  2.00,  0.10),
    "claude-3-5-haiku":  (0.80,  4.00,  1.00,  1.60,  0.08),
}

# Fast mode (research preview, Claude API only): (input, output). Prompt caching
# multipliers apply on top, so the cache rates scale by the same factor.
FAST_MODE: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (8.00, 40.00),
    "claude-opus-5":   (10.00, 50.00),
    "claude-opus-4-8": (10.00, 50.00),
}

# `inference_geo: "us"` on Claude 4.6 and later: 1.1x on input, output, cache
# writes and cache reads. Earlier models reject the parameter.
US_ONLY_MULTIPLIER = 1.1

# ── OpenAI ────────────────────────────────────────────────────────────────────
#
# (input, output, cached input). Only the aliases whose standard rate could be
# confirmed. Dated snapshots are deliberately absent: gpt-4o-2024-05-13 is
# $5/$15 while later gpt-4o snapshots are $2.50/$10, so stripping a date is not
# safe for OpenAI the way it is for Claude. gpt-4-turbo and the dated snapshots
# the connectors see read as unpriced until someone confirms them.
_OPENAI: dict[str, tuple[float, float, float | None]] = {
    "gpt-4o":        (2.50,  10.00, 1.25),
    "gpt-4o-mini":   (0.15,  0.60,  0.075),
    "o1":            (15.00, 60.00, 7.50),
    "o1-mini":       (1.10,  4.40,  0.55),
    "o3":            (2.00,  8.00,  0.50),
    "o3-mini":       (1.10,  4.40,  0.55),
    "o4-mini":       (1.10,  4.40,  0.275),
    "gpt-3.5-turbo": (0.50,  1.50,  None),
}

MODEL_PRICES: dict[str, ModelPrice] = {
    **{m: ModelPrice(m, "anthropic", i, o, w5, w1, r)
       for m, (i, o, w5, w1, r) in _ANTHROPIC.items()},
    **{m: ModelPrice(m, "openai", i, o, None, None, r)
       for m, (i, o, r) in _OPENAI.items()},
}

# Other spellings of a model in the table. Not prices: each maps to a key above.
ALIASES: dict[str, str] = {
    "claude-opus-4-0": "claude-opus-4",
    "claude-sonnet-4-0": "claude-sonnet-4",
    # bedrock_routing and llm_costs have always said Haiku 3.5 this way round.
    "claude-haiku-3-5": "claude-3-5-haiku",
}

# ── resolving a model id ─────────────────────────────────────────────────────

_PAREN = re.compile(r"\s*\([^)]*\)")
_CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")                 # claude-opus-4-6[1m]
_GEO_PREFIX = re.compile(r"^(us|eu|apac|jp|au|ca|global|us-gov)\.")
_BEDROCK_VERSION = re.compile(r"-v\d+(:\d+)?$")               # -v1:0, -v2:0
_VERTEX_VERSION = re.compile(r"@[a-z0-9]+$")                  # @20250929
_DATE_SUFFIX = re.compile(r"-20\d{6}$")                       # -20250929


def canonical_model(raw: str) -> str:
    """The id a model is known by in MODEL_PRICES, whether or not it is priced.

    Accepts what nable actually meets: API ids with a date suffix
    (claude-sonnet-4-5-20250929), Bedrock ids and inference profiles
    (us.anthropic.claude-haiku-4-5-20251001-v1:0), Vertex ids
    (claude-sonnet-4-5@20250929), provider-routed ids (bedrock/anthropic...,
    openai/gpt-4o) and Cost Explorer SKU names ("Claude Sonnet 4.5 (Amazon
    Bedrock Edition)").
    """
    s = _PAREN.sub("", str(raw or "")).strip().lower()
    s = s.split("/")[-1]
    s = _CONTEXT_SUFFIX.sub("", s)
    s = _GEO_PREFIX.sub("", s)
    if s.startswith("anthropic."):
        s = s[len("anthropic."):]
    if "claude" in s:
        # Display names write the version with spaces and dots ("Claude Haiku
        # 4.5"); ids use dashes. OpenAI ids keep their dots (gpt-3.5-turbo).
        s = re.sub(r"[\s.]+", "-", s)
        s = _BEDROCK_VERSION.sub("", s)
        s = _VERTEX_VERSION.sub("", s)
        if s.endswith("-latest"):
            s = s[: -len("-latest")]
        s = _DATE_SUFFIX.sub("", s)
    return ALIASES.get(s, s)


def price_for(raw: str) -> ModelPrice | None:
    """The price row for a model id in any of the spellings canonical_model
    accepts, or None when nable has no confirmed price for it."""
    return MODEL_PRICES.get(canonical_model(raw))
