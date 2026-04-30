"""Hyperliquid Quick-Trade integration tests.

Covers the v1 surface that's now supported via Quick-Trade:
- ``_parse_balance`` understands HL ``{"perp": {...}, "spot": {...}}`` shape
- ``_fetch_exchange_positions_raw`` flattens HL ``[{"position": {...}}]`` into
  the CCXT-like list ``_parse_positions`` expects
- ``/place-order`` still rejects HL (open-position not supported in v1)

Tests for ``/types`` advertising Hyperliquid + factory aliasing live in
``test_hyperliquid_market_type.py``.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.routes.quick_trade import (
    _parse_balance,
    _parse_positions,
    _reject_quick_trade_open_if_hyperliquid,
    _fetch_exchange_positions_raw,
)


# ---- /balance --------------------------------------------------------------

def test_parse_balance_hl_perp_account_value():
    """HL perp.marginSummary.accountValue -> total; perp.withdrawable -> available."""
    raw = {
        "perp": {
            "marginSummary": {
                "accountValue": "1234.56",
                "totalNtlPos": "0",
                "totalRawUsd": "1234.56",
                "totalMarginUsed": "0",
            },
            "withdrawable": "999.99",
            "assetPositions": [],
        },
        "spot": {"balances": []},
    }
    out = _parse_balance(raw, "hyperliquid", "swap")
    assert out["total"] == pytest.approx(1234.56)
    assert out["available"] == pytest.approx(999.99)
    assert out["currency"] == "USDC"


def test_parse_balance_hl_falls_back_to_withdrawable_when_no_account_value():
    raw = {"perp": {"withdrawable": "500.0", "assetPositions": []}, "spot": {}}
    out = _parse_balance(raw, "hyperliquid", "swap")
    assert out["available"] == pytest.approx(500.0)
    assert out["total"] == pytest.approx(500.0)


def test_parse_balance_hl_empty_returns_zero():
    out = _parse_balance({"perp": {}, "spot": {}}, "hyperliquid", "swap")
    assert out["total"] == 0.0
    assert out["available"] == 0.0


def test_parse_balance_non_hl_unchanged():
    """Existing exchanges' balance parsing must not regress."""
    raw = {"availableBalance": "100", "totalWalletBalance": "150"}
    out = _parse_balance(raw, "binance", "swap")
    assert out["total"] == pytest.approx(150.0)
    assert out["available"] == pytest.approx(100.0)


# ---- /position -------------------------------------------------------------

class _FakeHL:
    """Stand-in for HyperliquidClient with the bits _fetch_exchange_positions_raw uses."""
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self, *, symbol=""):
        return self._positions


def _install_hl_class(monkeypatch, fake_class):
    """Make the routes module's HyperliquidClient isinstance() pick up our fake."""
    import sys
    import types
    mod = types.ModuleType("app.services.live_trading.hyperliquid")
    mod.HyperliquidClient = fake_class
    monkeypatch.setitem(sys.modules, "app.services.live_trading.hyperliquid", mod)


def test_fetch_positions_raw_hl_flatten_long(monkeypatch):
    """Long position: szi > 0, side='long', size=abs(szi)."""
    fake_client = _FakeHL([
        {"position": {"coin": "BTC", "szi": "0.5", "entryPx": "60000",
                       "unrealizedPnl": "100", "leverage": {"value": 5}}},
    ])
    _install_hl_class(monkeypatch, _FakeHL)

    out = _fetch_exchange_positions_raw(
        fake_client, exchange_config={}, symbol="BTC", market_type="swap",
    )
    assert isinstance(out, list) and len(out) == 1
    p = out[0]
    assert p["symbol"] == "BTC/USDT"
    assert p["side"] == "long"
    assert p["size"] == pytest.approx(0.5)
    assert p["entryPrice"] == pytest.approx(60000)
    assert p["leverage"] == pytest.approx(5)


def test_fetch_positions_raw_hl_flatten_short(monkeypatch):
    """Short position: szi < 0, side='short', size=abs(szi)."""
    fake_client = _FakeHL([
        {"position": {"coin": "ETH", "szi": "-2.0", "entryPx": "3000",
                       "unrealizedPnl": "-50", "leverage": {"value": 3}}},
    ])
    _install_hl_class(monkeypatch, _FakeHL)

    out = _fetch_exchange_positions_raw(
        fake_client, exchange_config={}, symbol="ETH", market_type="swap",
    )
    assert len(out) == 1
    assert out[0]["side"] == "short"
    assert out[0]["size"] == pytest.approx(2.0)


def test_fetch_positions_raw_hl_zero_position_filtered(monkeypatch):
    """Empty / zero position should be filtered out so /position returns []."""
    fake_client = _FakeHL([
        {"position": {"coin": "BTC", "szi": "0", "entryPx": "0"}},
    ])
    _install_hl_class(monkeypatch, _FakeHL)

    out = _fetch_exchange_positions_raw(
        fake_client, exchange_config={}, symbol="BTC", market_type="swap",
    )
    assert out == []


def test_parse_positions_understands_hl_flattened_shape(monkeypatch):
    """Round-trip: flattened HL list goes through _parse_positions cleanly."""
    fake_client = _FakeHL([
        {"position": {"coin": "BTC", "szi": "0.1", "entryPx": "60000",
                       "unrealizedPnl": "5", "leverage": {"value": 10}}},
    ])
    _install_hl_class(monkeypatch, _FakeHL)

    raw = _fetch_exchange_positions_raw(
        fake_client, exchange_config={}, symbol="BTC", market_type="swap",
    )
    parsed = _parse_positions(raw)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["symbol"] == "BTC/USDT"
    assert p["side"] == "long"
    assert p["size"] == pytest.approx(0.1)
    assert p["entry_price"] == pytest.approx(60000)
    assert p["leverage"] == pytest.approx(10)


# ---- HL gate (open-only) ---------------------------------------------------

def test_open_position_still_rejects_hl(app):
    """Open via Quick-Trade is intentionally still 400 in v1."""
    with app.app_context():
        out = _reject_quick_trade_open_if_hyperliquid("hyperliquid")
        assert out is not None
        resp, status = out
        assert status == 400
        body = resp.get_json()
        assert body["code"] == 0
        assert "Hyperliquid" in body["msg"]


def test_open_position_pass_through_for_other_exchanges(app):
    with app.app_context():
        assert _reject_quick_trade_open_if_hyperliquid("binance") is None
        assert _reject_quick_trade_open_if_hyperliquid("") is None
