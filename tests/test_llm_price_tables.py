"""LLM token prices have one source: finops.llm_prices.

The rows are pinned to the published figures so a typo is a failing test, not
a quietly wrong estimate, and the resolver is pinned on the spellings nable
actually meets: API ids with a date, Bedrock profiles, Vertex ids and Cost
Explorer SKU names. The last section fails if a new model-keyed price table
appears outside llm_prices.
"""
from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

from finops import llm_prices
from finops.llm_prices import (
    FAST_MODE,
    MODEL_PRICES,
    US_ONLY_MULTIPLIER,
    canonical_model,
    price_for,
)

# (input, output, 5m write, 1h write, cache read) per million tokens, as the
# Claude pricing page lists them on AS_OF.
_ANTHROPIC_PUBLISHED = {
    "claude-fable-5-1": (10, 50, 12.50, 20, 0.25),
    "claude-fable-5": (10, 50, 12.50, 20, 1.00),
    "claude-opus-5-5": (4, 20, 5, 8, 0.20),
    "claude-opus-5": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-8": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-5": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-1": (15, 75, 18.75, 30, 1.50),
    "claude-sonnet-5": (2, 10, 2.50, 4, 0.20),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.30),
    "claude-haiku-4-5": (1, 5, 1.25, 2, 0.10),
    "claude-3-5-haiku": (0.80, 4, 1, 1.60, 0.08),
}


@pytest.mark.parametrize("model,row", sorted(_ANTHROPIC_PUBLISHED.items()))
def test_anthropic_rows_are_the_published_rates(model, row):
    p = MODEL_PRICES[model]
    assert (p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read) == row
    assert p.provider == "anthropic"


def test_anthropic_cache_writes_follow_the_published_multipliers():
    # 1.25x input for a 5-minute write and 2x for a 1-hour write, on every model.
    for m, p in MODEL_PRICES.items():
        if p.provider != "anthropic":
            continue
        assert p.cache_write_5m == pytest.approx(p.input * 1.25), m
        assert p.cache_write_1h == pytest.approx(p.input * 2), m


def test_cache_reads_are_a_tenth_of_input_except_the_three_published_exceptions():
    exceptions = {"claude-fable-5-1": 0.025, "claude-mythos-5-1": 0.025, "claude-opus-5-5": 0.05}
    for m, p in MODEL_PRICES.items():
        if p.provider != "anthropic":
            continue
        assert p.cache_read == pytest.approx(p.input * exceptions.get(m, 0.1)), m


def test_openai_rows():
    assert (MODEL_PRICES["gpt-4o"].input, MODEL_PRICES["gpt-4o"].output) == (2.50, 10.00)
    assert MODEL_PRICES["gpt-4o"].cache_read == 1.25
    # o3 was carried at $10/$40 in openai_usage; it lists at $2/$8.
    assert (MODEL_PRICES["o3"].input, MODEL_PRICES["o3"].output) == (2.00, 8.00)
    assert MODEL_PRICES["o4-mini"].cache_read == 0.275
    for p in MODEL_PRICES.values():
        if p.provider == "openai":
            assert p.cache_write_5m is None and p.cache_write_1h is None


# (input, output, cached input) per million tokens, standard tier.
_OPENAI_LISTED = {
    "gpt-5": (1.25, 10, 0.125),
    "gpt-5-codex": (1.25, 10, 0.125),
    "gpt-5-mini": (0.25, 2, 0.025),
    "gpt-5-nano": (0.05, 0.40, 0.005),
    "gpt-4.1": (2, 8, 0.50),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "gpt-4.1-nano": (0.10, 0.40, 0.025),
}


@pytest.mark.parametrize("model,row", sorted(_OPENAI_LISTED.items()))
def test_the_gpt_5_and_gpt_4_1_families_are_priced(model, row):
    p = MODEL_PRICES[model]
    assert (p.input, p.output, p.cache_read) == row and p.provider == "openai"


@pytest.mark.parametrize("snapshot,alias", [
    # The two snapshots the old openai_usage table priced, and the ones the
    # connectors meet most, each billed at its alias's rate.
    ("gpt-4o-2024-11-20", "gpt-4o"), ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
    ("gpt-4o-2024-08-06", "gpt-4o"), ("o3-2025-04-16", "o3"),
    ("o4-mini-2025-04-16", "o4-mini"), ("gpt-4.1-2025-04-14", "gpt-4.1"),
    ("gpt-5-2025-08-07", "gpt-5"), ("openai/gpt-5-mini-2025-08-07", "gpt-5-mini"),
])
def test_same_price_dated_snapshots_are_named_one_by_one(snapshot, alias):
    assert price_for(snapshot) is MODEL_PRICES[alias]


def test_an_openai_cache_write_is_billed_as_input():
    # OpenAI has no cache-write premium; Anthropic's 1.25x must not leak in.
    p = MODEL_PRICES["gpt-5"]
    assert p.cost(cache_write_5m_tokens=1_000_000) == pytest.approx(1.25)
    assert p.cost(cache_write_1h_tokens=1_000_000) == pytest.approx(1.25)


@pytest.mark.parametrize("raw,provider", [
    ("gpt-9-turbo", "openai"), ("o7-mini", "openai"), ("codex-mini-latest", "openai"),
    ("gpt-5", "openai"), ("claude-nova-9", "anthropic"), ("claude-haiku-4-5", "anthropic"),
    ("llama-3-70b", None), ("unknown", None), ("opus-model", None),
])
def test_provider_of_tells_openai_from_anthropic_unpriced_or_not(raw, provider):
    assert llm_prices.provider_of(raw) == provider


def test_the_table_says_when_and_where():
    assert date.fromisoformat(llm_prices.AS_OF)
    assert set(llm_prices.SOURCES) == {p.provider for p in MODEL_PRICES.values()}
    assert all(u.startswith("https://") for u in llm_prices.SOURCES.values())


# ── resolving the ids nable meets ────────────────────────────────────────────

@pytest.mark.parametrize("raw,canonical", [
    ("claude-opus-5-5", "claude-opus-5-5"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("claude-opus-4-1-20250805", "claude-opus-4-1"),
    ("claude-opus-4-20250514", "claude-opus-4"),
    ("claude-opus-4-0", "claude-opus-4"),
    ("claude-opus-4-6[1m]", "claude-opus-4-6"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
    ("anthropic.claude-3-5-haiku-20241022-v1:0", "claude-3-5-haiku"),
    ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
    (("arn:aws:bedrock:us-east-1:000000000000:inference-profile/"
      "us.anthropic.claude-opus-4-1-20250805-v1:0"), "claude-opus-4-1"),
    ("bedrock/anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("claude-sonnet-4-5@20250929", "claude-sonnet-4-5"),
    ("Claude Sonnet 4.5 (Amazon Bedrock Edition)", "claude-sonnet-4-5"),
    ("Claude Haiku 4.5", "claude-haiku-4-5"),
    ("Claude 3.5 Haiku", "claude-3-5-haiku"),
    ("claude-haiku-3-5", "claude-3-5-haiku"),
    ("openai/gpt-4o", "gpt-4o"),
    ("gpt-3.5-turbo", "gpt-3.5-turbo"),
])
def test_canonical_model(raw, canonical):
    assert canonical_model(raw) == canonical
    assert price_for(raw) is MODEL_PRICES[canonical]


@pytest.mark.parametrize("raw", [
    # Not on the current Claude pricing page.
    "claude-3-opus-20240229", "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-haiku-20240307", "Claude 3 Opus",
    # A Claude model this table does not know. It must not borrow a sibling's
    # rate the way bedrock_routing's family match did.
    "claude-haiku-9-9",
    # OpenAI: unconfirmed, and a dated gpt-4o snapshot that is not the alias's price.
    "gpt-4-turbo", "gpt-4o-2024-05-13",
    "", "<synthetic>",
])
def test_unconfirmed_models_are_unpriced(raw):
    assert price_for(raw) is None


# ── cost ─────────────────────────────────────────────────────────────────────

def test_cost_prices_each_category_at_its_own_rate():
    p = MODEL_PRICES["claude-opus-5-5"]
    usd = p.cost(input_tokens=1_000_000, output_tokens=1_000_000,
                 cache_write_5m_tokens=1_000_000, cache_write_1h_tokens=1_000_000,
                 cache_read_tokens=1_000_000)
    assert usd == pytest.approx(4 + 20 + 5 + 8 + 0.20)


def test_fast_mode_uses_the_published_fast_rates_and_scales_cache():
    p = MODEL_PRICES["claude-opus-5"]
    assert p.cost(input_tokens=1_000_000, output_tokens=1_000_000, fast=True) == pytest.approx(60)
    # Caching multipliers apply on top of fast pricing: 1.25x of $10.
    assert p.cost(cache_write_5m_tokens=1_000_000, fast=True) == pytest.approx(12.50)
    # A model without fast mode is billed at standard rates.
    assert "claude-opus-4-7" not in FAST_MODE
    assert (MODEL_PRICES["claude-opus-4-7"].cost(output_tokens=1_000_000, fast=True)
            == pytest.approx(25))


def test_us_only_inference_is_ten_percent_on_everything():
    p = MODEL_PRICES["claude-sonnet-4-6"]
    base = p.cost(input_tokens=1000, output_tokens=1000, cache_read_tokens=1000)
    assert p.cost(input_tokens=1000, output_tokens=1000, cache_read_tokens=1000,
                  us_only=True) == pytest.approx(base * US_ONLY_MULTIPLIER)


def test_openai_cache_writes_bill_as_input_and_reads_at_the_cached_rate():
    p = MODEL_PRICES["gpt-4o"]
    assert p.cost(cache_write_5m_tokens=1_000_000) == pytest.approx(2.50)
    assert p.cost(cache_read_tokens=1_000_000) == pytest.approx(1.25)
    # No cached rate published: cached tokens bill as input.
    assert MODEL_PRICES["gpt-3.5-turbo"].cost(cache_read_tokens=1_000_000) == pytest.approx(0.50)


# ── no new copies ────────────────────────────────────────────────────────────
#
# A dict literal with an LLM model id for a key and a price for a value is a
# token-price table, whatever it is called. Four of them were merged into
# llm_prices, and every one had started as a reasonable local convenience, so
# the only durable rule is that none may exist outside llm_prices unless it is
# listed here with the reason it is not a copy of an LLM token price.

_SRC = Path(__file__).resolve().parents[1] / "src" / "finops"
_MODEL_KEY = re.compile(
    r"(claude|anthropic\.|gpt-|chatgpt|^o[1-9](-|$)|gemini|llama|mistral|mixtral|nova-|"
    r"titan|command-r|deepseek|qwen|embedding|dall-e|whisper|tts-|sonnet|haiku|opus|"
    r"bison|gecko|imagen)", re.IGNORECASE)
# Field names a per-model price row uses when it is a dict.
_PRICE_FIELDS = {"input", "output", "prompt", "completion", "cache_read", "cache_write",
                 "input_price", "output_price"}

_ALLOWED = {
    ("llm_prices.py", "_ANTHROPIC"): "the source of truth",
    ("llm_prices.py", "_OPENAI"): "the source of truth",
    ("llm_prices.py", "FAST_MODE"): "the source of truth",
    ("connectors/saas/vertex_costs.py", "_VERTEX_PRICING"):
        "Gemini and PaLM on Vertex: a provider llm_prices does not cover yet, "
        "and the only copy of those rates",
    ("analytics/ai_kpis.py", "_CONTEXT_WINDOWS"): "context window sizes in tokens, not prices",
}


def _is_number(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp):
        node = node.operand
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool))


def _is_price(node: ast.AST) -> bool:
    """A number, a row of numbers, an {"input": n, ...} dict, or a constructor
    called with numbers (a ModelPrice built somewhere else)."""
    if _is_number(node):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return len(node.elts) >= 2 and any(_is_number(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return any(isinstance(k, ast.Constant) and k.value in _PRICE_FIELDS and _is_number(v)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.Call):
        return sum(_is_number(a) for a in [*node.args, *(k.value for k in node.keywords)]) >= 2
    return False


def _llm_price_dicts(source: str) -> list[tuple[str, int]]:
    """(name, line) for each model-keyed price dict. The name is the variable it
    is assigned to, else the key it sits under in an enclosing dict, else "<dict>"."""
    tree = ast.parse(source)
    names: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names[id(node.value)] = t.id
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(v, ast.Dict) and isinstance(k, ast.Constant)
                        and isinstance(k.value, str)):
                    names.setdefault(id(v), k.value)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        if any(isinstance(k, ast.Constant) and isinstance(k.value, str)
               and _MODEL_KEY.search(k.value) and _is_price(v)
               for k, v in zip(node.keys, node.values)):
            found.append((names.get(id(node), "<dict>"), node.lineno))
    return found


def _scan() -> dict[tuple[str, str], int]:
    hits = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for name, line in _llm_price_dicts(path.read_text(encoding="utf-8")):
            hits[(rel, name)] = line
    return hits


def test_no_llm_price_table_outside_llm_prices():
    stray = {k: line for k, line in _scan().items() if k not in _ALLOWED}
    assert not stray, (
        "LLM token prices belong in finops/llm_prices.py; resolve a model with "
        "price_for() instead of adding a table. If this dict is not a price, add it "
        f"to _ALLOWED with the reason. Found: {stray}")


def test_the_allowlist_has_no_stale_entries():
    # An entry for a table that has moved or been renamed would silently allow
    # a new one under the old name.
    found = _scan()
    assert not [k for k in _ALLOWED if k not in found]


def test_the_scan_catches_a_copy_in_any_shape():
    src = (
        "PRICING = {'claude-opus-5': (5.0, 25.0)}\n"
        "_RATES: dict = {'gpt-4o': {'input': 2.5, 'output': 10.0}}\n"
        "BLEND = {'sonnet': 3.0}\n"
        "ROWS = {'claude-haiku-4-5': ModelPrice('x', 'anthropic', 1.0, 5.0, None, None, 0.1)}\n"
        "def f():\n"
        "    return {'anthropic.claude-3-haiku': [0.25, 1.25]}.get('x')\n"
        "NAMES = {'claude-opus-5': 'Claude Opus 5'}\n"      # a label, not a price
        "ROUTE = {'gpt-4o': 'gpt-4o-mini'}\n"               # a mapping, not a price
        "TIERS = {'gpt-4o': True}\n"                        # a flag, not a price
    )
    assert [n for n, _ in _llm_price_dicts(src)] == [
        "PRICING", "_RATES", "BLEND", "ROWS", "<dict>"]
