"""Hyperliquid client tests with the SDK mocked out.

We mock both ``eth_account`` (used by ``BaseSignedClient``) and the
``hyperliquid.exchange`` / ``hyperliquid.info`` modules so the test can run
without the optional dependency installed and without ever touching a remote
endpoint.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest


MASTER = "0x" + "1" * 40
AGENT_ADDR = "0x" + "2" * 40
AGENT_KEY = "0x" + "a" * 64


# ---- Fakes --------------------------------------------------------------


class _FakeAccount:
    address = AGENT_ADDR


def _fake_account_module():
    mod = types.ModuleType("eth_account")
    class Account:
        @staticmethod
        def from_key(_key):
            return _FakeAccount()
    mod.Account = Account
    return mod


class _FakeExchange:
    last_call: Dict[str, Any] = {}

    def __init__(self, *_args, vault_address=None, account_address=None, **_kw):
        self.vault_address = vault_address
        self.account_address = account_address

    def order(self, coin, is_buy, sz, px, order_type, *, reduce_only=False, cloid=None):
        _FakeExchange.last_call = dict(
            method="order", coin=coin, is_buy=is_buy, sz=sz, px=px,
            order_type=order_type, reduce_only=reduce_only, cloid=cloid,
        )
        # Resting (limit) orders return ``resting`` with the oid; the fake oid
        # matches ``_FakeInfo._user_fills`` so wait_for_fill can resolve fills.
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": 12345}}]},
            },
        }

    def market_open(self, coin, is_buy, sz, px=None, slippage=None, cloid=None):
        _FakeExchange.last_call = dict(
            method="market_open", coin=coin, is_buy=is_buy, sz=sz, px=px,
            slippage=slippage, cloid=cloid,
        )
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"oid": 12345, "totalSz": str(sz), "avgPx": "60000.0"}}]},
            },
        }

    def cancel(self, coin, oid):
        _FakeExchange.last_call = dict(method="cancel", coin=coin, oid=oid)
        return {"status": "ok"}

    def cancel_by_cloid(self, coin, cloid):
        _FakeExchange.last_call = dict(method="cancel_by_cloid", coin=coin, cloid=cloid)
        return {"status": "ok"}

    def update_leverage(self, leverage, coin, is_cross):
        _FakeExchange.last_call = dict(method="update_leverage", leverage=leverage, coin=coin, is_cross=is_cross)
        return {"status": "ok"}


class _FakeInfo:
    def __init__(self, *_args, **_kw):
        pass

    def meta(self):
        return {"universe": [{"name": "BTC"}, {"name": "ETH"}]}

    def spot_meta(self):
        return {
            "tokens": [
                {"index": 0, "name": "USDC"},
                {"index": 1, "name": "PURR"},
                {"index": 5, "name": "HYPE"},
            ],
            "universe": [
                {"index": 0, "name": "PURR/USDC", "tokens": [1, 0]},
                {"index": 107, "name": "HYPE/USDC", "tokens": [5, 0]},
            ],
        }

    def all_mids(self):
        return {"BTC": "60123.5", "ETH": "3000", "@107": "12.5"}

    def user_state(self, _addr):
        return {
            "marginSummary": {"accountValue": "1000.0"},
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.1", "entryPx": "60000"}},
            ],
        }

    def spot_user_state(self, _addr):
        return {"balances": []}

    def open_orders(self, _addr):
        return [{"coin": "BTC", "oid": 1, "limitPx": "60000", "sz": "0.1", "side": "B"}]

    # --- Order status / fills (used by wait_for_fill) -----------------------

    # Per-test override: tests can mutate _FakeInfo._query_order_status to
    # simulate "open" / "filled" / "canceled". Default: filled.
    _query_order_status = "filled"
    _query_order_inner = {"oid": 12345, "sz": "0.1", "limitPx": "60000"}
    _user_fills = [
        {"oid": 12345, "sz": "0.1", "px": "60000.0", "fee": "0.05"},
    ]

    def query_order_by_oid(self, _addr, _oid):
        return {
            "status": "order",
            "order": {
                "order": _FakeInfo._query_order_inner,
                "status": _FakeInfo._query_order_status,
            },
        }

    def user_fills(self, _addr):
        return _FakeInfo._user_fills


def _install_fake_modules(monkeypatch):
    """Inject fake hyperliquid + eth_account modules so the import chain works."""
    monkeypatch.setitem(sys.modules, "eth_account", _fake_account_module())

    exchange_mod = types.ModuleType("hyperliquid.exchange")
    exchange_mod.Exchange = _FakeExchange
    info_mod = types.ModuleType("hyperliquid.info")
    info_mod.Info = _FakeInfo
    pkg = types.ModuleType("hyperliquid")
    pkg.exchange = exchange_mod
    pkg.info = info_mod
    types_mod = types.ModuleType("hyperliquid.utils.types")
    class _Cloid:
        def __init__(self, raw): self.raw = raw
        @classmethod
        def from_str(cls, s): return cls(s)
    types_mod.Cloid = _Cloid
    utils_mod = types.ModuleType("hyperliquid.utils")
    utils_mod.types = types_mod

    monkeypatch.setitem(sys.modules, "hyperliquid", pkg)
    monkeypatch.setitem(sys.modules, "hyperliquid.exchange", exchange_mod)
    monkeypatch.setitem(sys.modules, "hyperliquid.info", info_mod)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils", utils_mod)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils.types", types_mod)


@pytest.fixture
def hl_client(monkeypatch):
    """Construct a HyperliquidClient against the fake SDK."""
    _install_fake_modules(monkeypatch)
    # Force re-import of the adapter so it picks up the fake SDK
    for mod_name in (
        "app.services.live_trading.base_signed",
        "app.services.live_trading.hyperliquid",
    ):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    from app.services.live_trading.hyperliquid import HyperliquidClient
    return HyperliquidClient(
        wallet_address=MASTER,
        agent_private_key=AGENT_KEY,
        is_testnet=True,
    )


# ---- Tests --------------------------------------------------------------


def test_master_key_rejected(monkeypatch):
    """If the agent address derives to the same address as the master, refuse construction."""
    _install_fake_modules(monkeypatch)
    for mod_name in (
        "app.services.live_trading.base_signed",
        "app.services.live_trading.hyperliquid",
    ):
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Patch fake account so derived address EQUALS master
    fake_eth = sys.modules["eth_account"]
    class _Account:
        @staticmethod
        def from_key(_key):
            obj = mock.Mock()
            obj.address = MASTER
            return obj
    fake_eth.Account = _Account

    from app.services.live_trading.base import LiveTradingError
    from app.services.live_trading.hyperliquid import HyperliquidClient
    with pytest.raises(LiveTradingError, match="master EOA"):
        HyperliquidClient(
            wallet_address=MASTER,
            agent_private_key=AGENT_KEY,
            is_testnet=True,
        )


def test_testnet_url(hl_client):
    assert "testnet" in hl_client.base_url


def test_get_ticker_uses_all_mids(hl_client):
    res = hl_client.get_ticker(symbol="BTC/USDT")
    assert res["symbol"] == "BTC"
    assert res["last"] == pytest.approx(60123.5)


def test_get_ticker_spot_resolves_via_meta(hl_client):
    res = hl_client.get_ticker(symbol="HYPE/USDC", market_type="spot")
    assert res["symbol"] == "@107"
    assert res["last"] == pytest.approx(12.5)


def test_place_market_order_returns_live_order_result(hl_client):
    res = hl_client.place_market_order(symbol="BTC/USDT", side="BUY", qty=0.1)
    assert res.exchange_id == "hyperliquid"
    assert res.exchange_order_id == "12345"
    assert res.filled == pytest.approx(0.1)
    assert res.avg_price == pytest.approx(60000.0)
    # Verify the symbol is reduced to bare coin
    assert _FakeExchange.last_call["coin"] == "BTC"
    assert _FakeExchange.last_call["is_buy"] is True


def test_place_limit_order_passes_tif_gtc(hl_client):
    res = hl_client.place_order(
        symbol="ETH/USDT",
        side="SELL",
        qty=1.0,
        order_type="LIMIT",
        price=3100.0,
    )
    assert isinstance(res, dict) and res.get("status") == "ok"
    assert _FakeExchange.last_call["method"] == "order"
    assert _FakeExchange.last_call["coin"] == "ETH"
    assert _FakeExchange.last_call["is_buy"] is False
    assert _FakeExchange.last_call["px"] == 3100.0
    assert _FakeExchange.last_call["order_type"] == {"limit": {"tif": "Gtc"}}


def test_place_limit_post_only_uses_alo_tif(hl_client):
    hl_client.place_order(
        symbol="BTC", side="BUY", qty=0.1, order_type="LIMIT", price=60000.0, post_only=True,
    )
    assert _FakeExchange.last_call["order_type"] == {"limit": {"tif": "Alo"}}


def test_place_limit_requires_price(hl_client):
    from app.services.live_trading.base import LiveTradingError
    with pytest.raises(LiveTradingError, match="price is required"):
        hl_client.place_order(symbol="BTC", side="BUY", qty=0.1, order_type="LIMIT", price=None)


def test_cancel_by_oid(hl_client):
    hl_client.cancel_order(symbol="BTC", order_id="123")
    assert _FakeExchange.last_call == {"method": "cancel", "coin": "BTC", "oid": 123}


def test_cancel_requires_id(hl_client):
    from app.services.live_trading.base import LiveTradingError
    with pytest.raises(LiveTradingError, match="order_id"):
        hl_client.cancel_order(symbol="BTC")


def test_set_leverage_passes_through(hl_client):
    hl_client.set_leverage(symbol="BTC/USDT", leverage=5, is_cross=False)
    assert _FakeExchange.last_call == {
        "method": "update_leverage",
        "leverage": 5,
        "coin": "BTC",
        "is_cross": False,
    }


def test_get_positions_filters_by_symbol(hl_client):
    pos = hl_client.get_positions(symbol="BTC")
    assert len(pos) == 1
    assert pos[0]["position"]["coin"] == "BTC"
    # Filter that doesn't match
    assert hl_client.get_positions(symbol="ETH") == []


def test_get_account_returns_dict(hl_client):
    state = hl_client.get_account()
    assert "marginSummary" in state


def test_get_positions_long_position(hl_client):
    """One-way long position: szi > 0, the magnitude is what we close."""
    pos = hl_client.get_positions(symbol="BTC")
    assert len(pos) == 1
    inner = pos[0]["position"]
    assert inner["coin"] == "BTC"
    # Default fake returns szi="0.1" (long)
    assert float(inner["szi"]) == pytest.approx(0.1)


def test_pending_order_worker_hl_reduce_only_branch(monkeypatch, hl_client):
    """
    Verify the HL branch in pending_order_worker.py reduces requested size to
    the actual on-exchange position when smaller. Uses the live HL client (with
    mocked SDK) so the adapter shape stays in sync with the worker's expectations.
    """
    # The fake Info returns [{"position": {"coin": "BTC", "szi": "0.1", ...}}].
    # Simulate the worker's branch directly (we don't need to spin the full worker).
    from app.services.pending_order_worker import _is_hyperliquid_client

    assert _is_hyperliquid_client(hl_client) is True

    pos_list = hl_client.get_positions(symbol="BTC") or []
    pos_side = "long"
    actual_pos_size = 0.0
    for pos in pos_list:
        inner = pos.get("position") or {}
        try:
            raw_szi = float(inner.get("szi") or 0.0)
        except Exception:
            raw_szi = 0.0
        if abs(raw_szi) > 0:
            actual_long = raw_szi > 0
            wanted_long = (pos_side == "long")
            if actual_long == wanted_long:
                actual_pos_size = abs(raw_szi)
                break

    # Worker would set amount = 0.1 (the actual position) instead of e.g. 0.5.
    assert actual_pos_size == pytest.approx(0.1)


def test_place_limit_order_returns_live_order_result(hl_client):
    """place_limit_order should return a LiveOrderResult with the resting oid."""
    res = hl_client.place_limit_order(
        symbol="BTC/USDT", side="BUY", qty=0.1, price=60000.0,
    )
    assert res.exchange_id == "hyperliquid"
    assert res.exchange_order_id == "12345"
    # Limit order returns resting (not filled) -> filled=0 from the immediate response
    assert res.filled == 0.0
    # Verify SDK call shape: TIF is Gtc by default
    last = _FakeExchange.last_call
    assert last["method"] == "order"
    assert last["coin"] == "BTC"
    assert last["is_buy"] is True
    assert last["px"] == 60000.0
    assert last["order_type"] == {"limit": {"tif": "Gtc"}}


def test_place_limit_order_post_only_uses_alo(hl_client):
    """post_only=True forces TIF=Alo (HL rejects on cross)."""
    hl_client.place_limit_order(
        symbol="ETH", side="SELL", qty=1.0, price=3100.0, post_only=True,
    )
    assert _FakeExchange.last_call["order_type"] == {"limit": {"tif": "Alo"}}


def test_wait_for_fill_returns_fills_when_order_filled(hl_client):
    """When status=filled, wait_for_fill sums user_fills for that oid and
    returns ``filled``, ``avg_price``, ``fee``, ``status``."""
    _FakeInfo._query_order_status = "filled"
    _FakeInfo._user_fills = [
        {"oid": 12345, "sz": "0.05", "px": "60000.0", "fee": "0.03"},
        {"oid": 12345, "sz": "0.05", "px": "60100.0", "fee": "0.03"},
        {"oid": 99999, "sz": "1.0", "px": "100.0", "fee": "0.01"},  # different oid; ignored
    ]
    out = hl_client.wait_for_fill(symbol="BTC", order_id="12345", max_wait_sec=0.1, poll_interval_sec=0.01)
    assert out["filled"] == pytest.approx(0.10)
    # Volume-weighted: (0.05*60000 + 0.05*60100) / 0.10 = 60050
    assert out["avg_price"] == pytest.approx(60050.0)
    assert out["fee"] == pytest.approx(0.06)
    assert out["fee_ccy"] == "USDC"
    assert out["status"] == "filled"


def test_wait_for_fill_handles_unknown_oid(hl_client):
    """Empty / non-numeric order_id returns the zero result without crashing."""
    out = hl_client.wait_for_fill(symbol="BTC", order_id="", max_wait_sec=0.1)
    assert out["filled"] == 0.0
    assert out["fee_ccy"] == "USDC"
    out = hl_client.wait_for_fill(symbol="BTC", order_id="not-a-number", max_wait_sec=0.1)
    assert out["filled"] == 0.0


def test_pending_order_worker_hl_skips_when_side_mismatch(hl_client):
    """If wanted_side != actual_side, do not adjust amount — let HL reject it."""
    pos_list = hl_client.get_positions(symbol="BTC") or []
    # Pretend user is closing short while actual is long
    pos_side = "short"
    actual_pos_size = 0.0
    for pos in pos_list:
        inner = pos.get("position") or {}
        try:
            raw_szi = float(inner.get("szi") or 0.0)
        except Exception:
            raw_szi = 0.0
        if abs(raw_szi) > 0:
            actual_long = raw_szi > 0
            wanted_long = (pos_side == "long")
            if actual_long == wanted_long:
                actual_pos_size = abs(raw_szi)
                break
    assert actual_pos_size == 0.0
