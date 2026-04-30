"""Tests for /api/market/symbols/search?market=Hyperliquid.

The search endpoint had no Hyperliquid branch; HL queries fell through the
seed-table lookup (empty) and skipped the CCXT fallback (Crypto-only). Now
``Hyperliquid`` reuses the Binance USDT pair list but rewrites display
``market`` to ``Hyperliquid`` and quote to ``USDC`` so the UI matches HL's
actual USDC-denominated markets.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest


_FAKE_MARKETS = {
    "BTC/USDT": {"active": True, "base": "BTC", "quote": "USDT"},
    "ETH/USDT": {"active": True, "base": "ETH", "quote": "USDT"},
    "SOL/USDT": {"active": True, "base": "SOL", "quote": "USDT"},
    # An inactive pair must be filtered out
    "OLD/USDT": {"active": False, "base": "OLD", "quote": "USDT"},
    # A non-USDT pair must be filtered out
    "BTC/USDC": {"active": True, "base": "BTC", "quote": "USDC"},
}


@pytest.fixture(autouse=True)
def _reset_market_cache():
    """Reset the module-level cache between tests so each test sees a fresh load."""
    from app.routes import market as market_route
    market_route._crypto_markets_cache["data"] = None
    market_route._crypto_markets_cache["ts"] = 0
    yield
    market_route._crypto_markets_cache["data"] = None
    market_route._crypto_markets_cache["ts"] = 0


def _patch_ccxt(monkeypatch):
    """Replace ccxt.binance / ccxt.gate with a stub that returns our fake markets."""
    fake_ccxt = types.ModuleType("ccxt")

    class _FakeExchange:
        def __init__(self):
            self.markets = _FAKE_MARKETS

        def load_markets(self):
            return self.markets

    fake_ccxt.binance = _FakeExchange
    fake_ccxt.gate = _FakeExchange
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)


def test_search_hyperliquid_returns_results_with_usdc_quote(client, app, monkeypatch):
    _patch_ccxt(monkeypatch)
    # seed table is empty for Hyperliquid; we only care about the fallback
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get("/api/market/symbols/search?market=Hyperliquid&keyword=BTC&limit=20")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 1
    items = body["data"]
    assert len(items) >= 1
    # Every result should be displayed under Hyperliquid market with /USDC quote
    btc_match = next((r for r in items if r["symbol"] == "BTC/USDC"), None)
    assert btc_match is not None, items
    assert btc_match["market"] == "Hyperliquid"
    assert btc_match["name"] == "BTC"


def test_search_hyperliquid_with_slash_in_keyword(client, app, monkeypatch):
    """Searching "BTC/USDC" (with slash + HL quote) must still find the pair."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get("/api/market/symbols/search?market=Hyperliquid&keyword=BTC%2FUSDC&limit=20")
    assert resp.status_code == 200
    items = resp.get_json()["data"]
    assert any(r["symbol"] == "BTC/USDC" and r["market"] == "Hyperliquid" for r in items)


def test_search_crypto_regression_unaffected(client, app, monkeypatch):
    """Existing Crypto search must still return BASE/USDT under market=Crypto."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get("/api/market/symbols/search?market=Crypto&keyword=BTC&limit=20")
    assert resp.status_code == 200
    items = resp.get_json()["data"]
    btc = next((r for r in items if r["symbol"] == "BTC/USDT"), None)
    assert btc is not None
    assert btc["market"] == "Crypto"


def test_search_hyperliquid_exclusive_token_returns_empty(client, app, monkeypatch):
    """v1 limitation: HL-exclusive tokens (HYPE, PURR) have no Binance equivalent
    so the search returns empty rather than a misleading hit. This is documented
    in the trading guide as a v1 limit; P2 will pull HL's own universe."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get("/api/market/symbols/search?market=Hyperliquid&keyword=HYPE&limit=20")
    assert resp.status_code == 200
    items = resp.get_json()["data"]
    # HYPE isn't in our fake Binance markets — must return empty, not crash.
    assert items == []
