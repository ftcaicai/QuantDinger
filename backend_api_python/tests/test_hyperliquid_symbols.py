"""Hyperliquid symbol normalization tests.

These don't import the SDK; ``hyperliquid_symbols`` is a pure-Python module
that operates on dicts and strings.
"""

from app.services.live_trading.hyperliquid_symbols import (
    to_hl_perp_coin,
    to_hl_spot_index,
    from_hl_to_binance_equivalent,
)


# Sample spot_meta as returned by Info(...).spot_meta(). Trimmed for tests.
SAMPLE_SPOT_META = {
    "tokens": [
        {"index": 0, "name": "USDC"},
        {"index": 1, "name": "PURR"},
        {"index": 5, "name": "HYPE"},
        {"index": 10, "name": "SOL"},
    ],
    "universe": [
        {"index": 0, "name": "PURR/USDC", "tokens": [1, 0]},
        {"index": 107, "name": "HYPE/USDC", "tokens": [5, 0]},
        {"index": 211, "name": "SOL/USDC", "tokens": [10, 0]},
    ],
}


# ---- to_hl_perp_coin --------------------------------------------------------

def test_perp_coin_strips_quote():
    assert to_hl_perp_coin("BTC/USDT") == "BTC"
    assert to_hl_perp_coin("BTC/USDT:USDT") == "BTC"
    assert to_hl_perp_coin("BTCUSDT") == "BTC"
    assert to_hl_perp_coin("BTC") == "BTC"


def test_perp_coin_handles_lowercase_and_whitespace():
    assert to_hl_perp_coin("  eth/usdt  ") == "ETH"
    assert to_hl_perp_coin("solusdt") == "SOL"


def test_perp_coin_handles_empty():
    assert to_hl_perp_coin("") == ""
    assert to_hl_perp_coin(None) == ""


# ---- to_hl_spot_index -------------------------------------------------------

def test_spot_index_pass_through_when_already_wire():
    assert to_hl_spot_index("@107", SAMPLE_SPOT_META) == "@107"
    assert to_hl_spot_index("PURR/USDC", SAMPLE_SPOT_META) == "PURR/USDC"


def test_spot_index_resolves_known_token():
    assert to_hl_spot_index("HYPE/USDC", SAMPLE_SPOT_META) == "@107"
    assert to_hl_spot_index("SOL/USDC", SAMPLE_SPOT_META) == "@211"


def test_spot_index_purr_special_case_without_meta():
    # PURR/USDC is the historical exception accepted by name even with no meta
    assert to_hl_spot_index("PURR", None) == "PURR/USDC"
    assert to_hl_spot_index("PURR/USDC", None) == "PURR/USDC"


def test_spot_index_unknown_token_returns_input():
    # Unknown token without meta — preserve original (caller surfaces error)
    assert to_hl_spot_index("UNKNOWN/USDC", SAMPLE_SPOT_META) == "UNKNOWN/USDC"


# ---- from_hl_to_binance_equivalent -----------------------------------------

def test_binance_equiv_perp_basic():
    assert from_hl_to_binance_equivalent("BTC") == "BTC/USDT"
    assert from_hl_to_binance_equivalent("ETH") == "ETH/USDT"
    assert from_hl_to_binance_equivalent("SOL") == "SOL/USDT"


def test_binance_equiv_hl_exclusive_returns_none():
    # PURR is HL-only (not on Binance)
    assert from_hl_to_binance_equivalent("PURR") is None
    # HYPE is HL-only as of v1
    assert from_hl_to_binance_equivalent("HYPE") is None


def test_binance_equiv_at_index_resolves_to_base():
    # @107 -> HYPE -> None (HL-exclusive)
    assert from_hl_to_binance_equivalent("@107", spot_meta=SAMPLE_SPOT_META) is None
    # @211 -> SOL -> SOL/USDT
    assert from_hl_to_binance_equivalent("@211", spot_meta=SAMPLE_SPOT_META) == "SOL/USDT"


def test_binance_equiv_at_index_without_meta_returns_none():
    # Cannot resolve @<idx> without meta -> safest is None (caller errors out)
    assert from_hl_to_binance_equivalent("@107") is None


def test_binance_equiv_handles_compound_input():
    # "PURR/USDC" -> base PURR -> None
    assert from_hl_to_binance_equivalent("PURR/USDC") is None
    # "BTC/USDT" -> base BTC -> BTC/USDT
    assert from_hl_to_binance_equivalent("BTC/USDT") == "BTC/USDT"


def test_binance_equiv_empty():
    assert from_hl_to_binance_equivalent("") is None
    assert from_hl_to_binance_equivalent(None) is None
