"""Tests covering Hyperliquid as a top-level market type (added per user request)."""

from __future__ import annotations

from unittest import mock

import pytest

from app.data_sources.factory import DataSourceFactory


def test_market_normalize_hyperliquid_aliases_to_crypto():
    """``Hyperliquid`` market routes to the Crypto data source for the v1 fallback."""
    assert DataSourceFactory.normalize_market("Hyperliquid") == "Crypto"
    assert DataSourceFactory.normalize_market("hyperliquid") == "Crypto"
    assert DataSourceFactory.normalize_market("HYPERLIQUID") == "Crypto"


def test_factory_get_kline_market_hyperliquid_transforms_symbol():
    """Picking ``Hyperliquid`` market in the UI without binding a credential
    still triggers HL→Binance symbol fallback (so HYPE etc. raise instead
    of returning empty)."""
    captured = {}

    class _StubSource:
        def get_kline(self, symbol, timeframe, limit, before_time, after_time):
            captured["symbol"] = symbol
            return []

    with mock.patch.object(DataSourceFactory, "get_source", return_value=_StubSource()):
        DataSourceFactory.get_kline(
            market="Hyperliquid", symbol="BTC", timeframe="1h", limit=10,
        )

    assert captured["symbol"] == "BTC/USDT"


def test_factory_get_kline_market_hyperliquid_raises_on_exclusive():
    """HYPE without binance equivalent → KlineSymbolError."""
    from app.services.live_trading.hyperliquid_symbols import KlineSymbolError
    with pytest.raises(KlineSymbolError):
        DataSourceFactory.get_kline(
            market="Hyperliquid", symbol="HYPE", timeframe="1h", limit=10,
        )


def test_factory_get_kline_market_crypto_no_exchange_id_unchanged():
    """Regression: ``Crypto`` market without exchange_id must NOT trigger HL transform."""
    captured = {}

    class _StubSource:
        def get_kline(self, symbol, timeframe, limit, before_time, after_time):
            captured["symbol"] = symbol
            return []

    with mock.patch.object(DataSourceFactory, "get_source", return_value=_StubSource()):
        DataSourceFactory.get_kline(
            market="Crypto", symbol="HYPE", timeframe="1h", limit=10,
        )

    # HYPE passed through to crypto source unchanged (which would then
    # itself fail upstream; that's the existing behavior, not regressed)
    assert captured["symbol"] == "HYPE"
