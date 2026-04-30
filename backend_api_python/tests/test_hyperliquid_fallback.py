"""Tests for the v1 ``reuse Binance prices`` fallback that gates Hyperliquid
strategies through ``maybe_transform_kline_symbol`` before any K-line fetch.

The transform must:
  - pass non-HL traffic through unchanged;
  - rewrite HL perp symbols to Binance equivalents;
  - raise ``KlineSymbolError`` for HL-exclusive tokens (HYPE / PURR / ...);
  - be invoked at both ``DataSourceFactory.get_kline`` and ``KlineService.get_kline``.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.services.live_trading.hyperliquid_symbols import (
    KlineSymbolError,
    maybe_transform_kline_symbol,
)


# ---- maybe_transform_kline_symbol -----------------------------------------

def test_pass_through_when_not_hyperliquid():
    """Non-HL exchanges are no-ops regardless of symbol shape."""
    assert maybe_transform_kline_symbol(
        exchange_id="binance", market="Crypto", symbol="BTC/USDT",
    ) == "BTC/USDT"
    assert maybe_transform_kline_symbol(
        exchange_id=None, market="Crypto", symbol="HYPE",
    ) == "HYPE"


def test_pass_through_when_market_not_crypto():
    """HL transform should not interfere with stock / forex routes."""
    assert maybe_transform_kline_symbol(
        exchange_id="hyperliquid", market="USStock", symbol="AAPL",
    ) == "AAPL"


def test_already_binance_form_passes_through():
    """Caller already gave us a Binance-shaped symbol — don't double-transform."""
    for s in ("BTC/USDT", "ETH/USDT:USDT", "BTCUSDT", "SOLUSDT"):
        out = maybe_transform_kline_symbol(
            exchange_id="hyperliquid", market="Crypto", symbol=s,
        )
        assert out.upper() == s.upper(), s


def test_hl_perp_resolves_to_binance():
    assert maybe_transform_kline_symbol(
        exchange_id="hyperliquid", market="Crypto", symbol="BTC",
    ) == "BTC/USDT"
    assert maybe_transform_kline_symbol(
        exchange_id="hyperliquid", market="Crypto", symbol="ETH",
    ) == "ETH/USDT"


def test_hl_exclusive_raises():
    """HYPE / PURR have no Binance equivalent — must raise so caller can
    surface a clear error rather than silently feeding empty K-lines."""
    with pytest.raises(KlineSymbolError, match="HYPE"):
        maybe_transform_kline_symbol(
            exchange_id="hyperliquid", market="Crypto", symbol="HYPE",
        )
    with pytest.raises(KlineSymbolError, match="PURR"):
        maybe_transform_kline_symbol(
            exchange_id="hyperliquid", market="Crypto", symbol="PURR",
        )


def test_empty_symbol_passes_through():
    """Don't raise on empty / None — let downstream return its usual no-data."""
    assert maybe_transform_kline_symbol(
        exchange_id="hyperliquid", market="Crypto", symbol="",
    ) == ""


# ---- Integration: DataSourceFactory.get_kline ------------------------------

def test_factory_get_kline_transforms_hl_symbol():
    """Factory should call data source with the Binance-equivalent symbol."""
    from app.data_sources.factory import DataSourceFactory

    captured = {}

    class _StubSource:
        def get_kline(self, symbol, timeframe, limit, before_time, after_time):
            captured["symbol"] = symbol
            return []

    with mock.patch.object(DataSourceFactory, "get_source", return_value=_StubSource()):
        DataSourceFactory.get_kline(
            market="Crypto", symbol="BTC", timeframe="1h", limit=10,
            exchange_id="hyperliquid",
        )

    assert captured["symbol"] == "BTC/USDT"


def test_factory_get_kline_propagates_klinesymbolerror_for_hl_exclusive():
    """HL-exclusive tokens raise — ``DataSourceFactory.get_kline``'s catch-all
    must NOT swallow this into an empty list."""
    from app.data_sources.factory import DataSourceFactory

    with pytest.raises(KlineSymbolError):
        DataSourceFactory.get_kline(
            market="Crypto", symbol="HYPE", timeframe="1h", limit=10,
            exchange_id="hyperliquid",
        )


def test_factory_get_kline_passes_through_when_no_exchange_id():
    """Non-HL callers should not be affected by the new code path."""
    from app.data_sources.factory import DataSourceFactory

    captured = {}

    class _StubSource:
        def get_kline(self, symbol, timeframe, limit, before_time, after_time):
            captured["symbol"] = symbol
            return []

    with mock.patch.object(DataSourceFactory, "get_source", return_value=_StubSource()):
        DataSourceFactory.get_kline(
            market="Crypto", symbol="BTCUSDT", timeframe="1h", limit=10,
        )

    assert captured["symbol"] == "BTCUSDT"


# ---- Integration: KlineService.get_kline -----------------------------------

def test_kline_service_transforms_hl_symbol():
    """KlineService should resolve HL symbol up-front (so cache key + downstream agree)."""
    from app.services.kline import KlineService

    svc = KlineService()
    svc.cache = mock.MagicMock()
    svc.cache.get.return_value = None

    captured = {}

    def _stub_get(*, market, symbol, timeframe, limit, before_time):
        captured["symbol"] = symbol
        captured["market"] = market
        return [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}]

    with mock.patch("app.services.kline.DataSourceFactory.get_kline", side_effect=_stub_get):
        klines = svc.get_kline(
            market="Crypto", symbol="ETH", timeframe="1h", limit=5,
            exchange_id="hyperliquid",
        )

    assert klines and len(klines) == 1
    assert captured["symbol"] == "ETH/USDT"


def test_kline_service_raises_for_hl_exclusive():
    from app.services.kline import KlineService
    svc = KlineService()
    with pytest.raises(KlineSymbolError):
        svc.get_kline(
            market="Crypto", symbol="HYPE", timeframe="1h", limit=5,
            exchange_id="hyperliquid",
        )


# ---- Integration: backtest ContextVar --------------------------------------

def test_backtest_context_var_routes_through_transform(monkeypatch):
    """When run_strategy_snapshot sets the ContextVar, _fetch_kline_data
    should pick up exchange_id without explicit kwarg threading."""
    from app.services import backtest as bt_module

    captured = {}

    def _stub_factory_get_kline(market, symbol, timeframe, limit, before_time=None, after_time=None, exchange_id=None):
        captured["market"] = market
        captured["symbol"] = symbol
        captured["exchange_id"] = exchange_id
        return []

    monkeypatch.setattr(
        "app.services.backtest.DataSourceFactory.get_kline",
        _stub_factory_get_kline,
    )

    svc = bt_module.BacktestService()
    token = bt_module._current_exchange_id.set("hyperliquid")
    try:
        from datetime import datetime
        svc._fetch_kline_data(
            market="Crypto", symbol="BTC", timeframe="1h",
            start_date=datetime(2025, 1, 1), end_date=datetime(2025, 1, 2),
        )
    finally:
        bt_module._current_exchange_id.reset(token)

    # exchange_id was picked up from the ContextVar
    assert captured["exchange_id"] == "hyperliquid"
    # Transform happens inside DataSourceFactory.get_kline — _fetch_kline_data
    # passes the original "BTC" through, factory rewrites it. We're stubbing
    # factory entirely so we just verify the wiring carries exchange_id.
    assert captured["symbol"] == "BTC"
