"""Tests for /api/market/symbols/search with the Hyperliquid exchange.

Hyperliquid is a Crypto-market exchange (peer of Binance / OKX), not a
top-level market type. Frontend must pass ``market=Crypto`` plus an
``exchange_id=hyperliquid`` hint when searching from an HL context. With
that hint the backend reuses the Binance USDT pair list but rewrites the
display quote to USDC so the UI matches HL's actual market convention.
"""

from __future__ import annotations

import sys
import types

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


def test_search_with_hl_exchange_hint_returns_usdc_quote(client, app, monkeypatch):
    """``market=Crypto`` + ``exchange_id=hyperliquid`` -> BASE/USDC results."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get(
        "/api/market/symbols/search?market=Crypto&exchange_id=hyperliquid&keyword=BTC&limit=20"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 1
    items = body["data"]
    assert len(items) >= 1
    btc_match = next((r for r in items if r["symbol"] == "BTC/USDC"), None)
    assert btc_match is not None, items
    # Market label remains Crypto — HL is a Crypto exchange, not a market.
    assert btc_match["market"] == "Crypto"
    assert btc_match["name"] == "BTC"


def test_search_with_hl_exchange_hint_slash_in_keyword(client, app, monkeypatch):
    """Searching ``BTC/USDC`` directly must still match (the keyword
    normalization strips both /USDT and /USDC)."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get(
        "/api/market/symbols/search?market=Crypto&exchange_id=hyperliquid&keyword=BTC%2FUSDC&limit=20"
    )
    assert resp.status_code == 200
    items = resp.get_json()["data"]
    assert any(r["symbol"] == "BTC/USDC" and r["market"] == "Crypto" for r in items)


def test_search_crypto_without_hl_hint_unchanged(client, app, monkeypatch):
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


def test_search_hl_exclusive_token_returns_empty(client, app, monkeypatch):
    """v1 limit: HL-exclusive tokens (HYPE, PURR) have no Binance equivalent
    so the search returns empty rather than a misleading hit. P2 (independent
    HL data source) will pull HL's own universe and surface them."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get(
        "/api/market/symbols/search?market=Crypto&exchange_id=hyperliquid&keyword=HYPE&limit=20"
    )
    assert resp.status_code == 200
    items = resp.get_json()["data"]
    assert items == []


def test_search_market_hyperliquid_no_longer_treated_as_market(client, app, monkeypatch):
    """Regression guard: a stale frontend that still sends ``market=Hyperliquid``
    must not crash; it just gets the seed-table-empty default (i.e. ``[]``).
    HL is no longer a market type."""
    _patch_ccxt(monkeypatch)
    monkeypatch.setattr(
        "app.routes.market.seed_search_symbols",
        lambda **_: [],
    )

    resp = client.get("/api/market/symbols/search?market=Hyperliquid&keyword=BTC&limit=20")
    assert resp.status_code == 200
    # No HL-specific code path is taken; seed is empty -> []. The new
    # contract is: send market=Crypto + exchange_id=hyperliquid.
    assert resp.get_json()["data"] == []
