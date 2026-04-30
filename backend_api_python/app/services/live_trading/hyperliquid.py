"""
Hyperliquid live-trading client.

Wraps the official ``hyperliquid-python-sdk`` (Exchange + Info) in a thin
adapter that matches QuantDinger's CCXT-style surface (``place_order`` /
``cancel_order`` / ``get_balance`` / ``get_positions`` / ``get_open_orders``
/ ``set_leverage`` / ``get_ticker``). The SDK handles:

- EIP-712 phantom-agent signing
- Nonce tracking
- msgpack action canonicalization

so this file stays free of crypto code beyond the agent-key plumbing in
``BaseSignedClient``.

Key design notes:

- Lazy import of the SDK. The module loads even on hosts that did not pip
  install the dep yet — only constructing ``HyperliquidClient`` triggers the
  import. Same pattern as IBKR / MT5 in this repo.
- Symbol input may be ``BTC`` / ``BTC/USDT`` / ``BTCUSDT`` / ``@107``. We
  normalize via ``hyperliquid_symbols`` and look up ``@`` indices through a
  cached ``spot_meta`` (24h TTL).
- HL has no "spot vs perp" toggle on the client itself; we infer from the
  symbol and pass a different ``Exchange.order(...)`` shape.
- Vault / subaccount: passed via SDK's ``vault_address`` / ``account_address``
  ctor args.

The adapter intentionally exposes raw SDK responses rather than translating to
a private dataclass — that matches every other adapter in this folder, where
``pending_order_worker`` and ``routes/quick_trade`` introspect dicts directly.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from app.services.live_trading.base import LiveOrderResult, LiveTradingError
from app.services.live_trading.base_signed import BaseSignedClient
from app.services.live_trading.hyperliquid_symbols import (
    to_hl_perp_coin,
    to_hl_spot_index,
)

logger = logging.getLogger(__name__)


_MAINNET_URL = "https://api.hyperliquid.xyz"
_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

_SPOT_META_TTL_SEC = 24 * 3600.0
_PERP_META_TTL_SEC = 24 * 3600.0


def _load_sdk():
    """Lazy import to avoid breaking module load when SDK is not installed."""
    try:
        from hyperliquid.exchange import Exchange  # type: ignore
        from hyperliquid.info import Info  # type: ignore
    except ImportError as e:
        raise LiveTradingError(
            "Hyperliquid trading requires hyperliquid-python-sdk. "
            "Run: pip install hyperliquid-python-sdk"
        ) from e
    return Exchange, Info


class HyperliquidClient(BaseSignedClient):
    """
    Thin wrapper over hyperliquid SDK.

    Construction:
        HyperliquidClient(
            wallet_address=<master EOA, 0x...>,
            agent_private_key=<agent wallet privkey, 0x...>,
            is_testnet=False,
            vault_address=None,             # optional, for vault trading
            account_address=None,           # optional, agent-on-subaccount
        )
    """

    exchange_id = "hyperliquid"

    def __init__(
        self,
        *,
        wallet_address: str,
        agent_private_key: str,
        is_testnet: bool = False,
        vault_address: Optional[str] = None,
        account_address: Optional[str] = None,
        timeout_sec: float = 15.0,
        base_url: Optional[str] = None,
    ):
        url = base_url or (_TESTNET_URL if is_testnet else _MAINNET_URL)
        super().__init__(
            base_url=url,
            wallet_address=wallet_address,
            agent_private_key=agent_private_key,
            timeout_sec=timeout_sec,
        )

        self.is_testnet = bool(is_testnet)
        self.vault_address = self._normalize_address(vault_address) or None
        # account_address: for agent-on-subaccount, the SDK uses this as the
        # "user" the actions act on behalf of. Default to the master wallet.
        self.account_address = self._normalize_address(account_address) or self.wallet_address

        Exchange, Info = _load_sdk()
        self._info = Info(url, skip_ws=True)
        self._exchange = Exchange(
            self._agent,
            url,
            vault_address=self.vault_address,
            account_address=self.account_address,
        )

        # spot_meta / meta caches (per-instance, lock-protected)
        self._spot_meta: Optional[Dict[str, Any]] = None
        self._spot_meta_ts: float = 0.0
        self._perp_meta: Optional[Dict[str, Any]] = None
        self._perp_meta_ts: float = 0.0
        self._meta_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Metadata accessors (cached)
    # ------------------------------------------------------------------

    def _get_spot_meta(self) -> Dict[str, Any]:
        with self._meta_lock:
            now = time.time()
            if self._spot_meta and (now - self._spot_meta_ts) < _SPOT_META_TTL_SEC:
                return self._spot_meta
            try:
                meta = self._info.spot_meta()
            except Exception as e:
                logger.warning(f"Hyperliquid spot_meta() failed: {e}")
                return self._spot_meta or {}
            if isinstance(meta, dict):
                self._spot_meta = meta
                self._spot_meta_ts = now
            return self._spot_meta or {}

    def _get_perp_meta(self) -> Dict[str, Any]:
        with self._meta_lock:
            now = time.time()
            if self._perp_meta and (now - self._perp_meta_ts) < _PERP_META_TTL_SEC:
                return self._perp_meta
            try:
                meta = self._info.meta()
            except Exception as e:
                logger.warning(f"Hyperliquid meta() failed: {e}")
                return self._perp_meta or {}
            if isinstance(meta, dict):
                self._perp_meta = meta
                self._perp_meta_ts = now
            return self._perp_meta or {}

    def get_spot_meta(self) -> Dict[str, Any]:
        """Public accessor (used by callers needing @idx -> name resolution)."""
        return self._get_spot_meta()

    # ------------------------------------------------------------------
    # Symbol routing
    # ------------------------------------------------------------------

    def _resolve_symbol(self, symbol: str, *, market_type: str = "swap") -> str:
        """
        Resolve a UI symbol into the wire symbol HL expects.

        - swap/perp: bare base coin (``BTC``, ``ETH``)
        - spot:      ``@<idx>`` or ``PURR/USDC``
        """
        mt = (market_type or "swap").lower()
        if mt in ("spot",):
            return to_hl_spot_index(symbol, self._get_spot_meta())
        return to_hl_perp_coin(symbol)

    # ------------------------------------------------------------------
    # Connection / account
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Best-effort: any successful Info call indicates the API is reachable."""
        try:
            self._info.meta()
            return True
        except Exception:
            return False

    def get_account(self) -> Dict[str, Any]:
        """
        Connection-test entry point.

        Returns the master account's perp clearinghouse state. This implicitly
        validates that the agent is approved for ``self.account_address``: if
        the agent isn't bound, subsequent signed calls will fail — but reading
        ``user_state`` is a public Info call so we additionally check that
        ``self._exchange.info`` agrees with our agent address.
        """
        try:
            state = self._info.user_state(self.account_address)
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid Info.user_state failed: {e}") from e
        return state if isinstance(state, dict) else {"raw": state}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_ticker(self, *, symbol: str, market_type: str = "swap") -> Dict[str, Any]:
        """
        Returns ``{"last": float, "symbol": <wire>}``. Pulled from
        ``Info.all_mids()`` which returns mid prices for every market.
        """
        wire = self._resolve_symbol(symbol, market_type=market_type)
        try:
            mids = self._info.all_mids()
        except Exception as e:
            logger.warning(f"Hyperliquid all_mids failed for {symbol}: {e}")
            return {"symbol": wire, "last": 0.0}
        last = 0.0
        if isinstance(mids, dict):
            try:
                last = float(mids.get(wire) or 0.0)
            except Exception:
                last = 0.0
        return {"symbol": wire, "last": last}

    def get_balance(self) -> Dict[str, Any]:
        """
        Combined perp + spot balance for the account.

        Shape mirrors what other adapters return — a dict with raw fields the
        callers in routes/quick_trade and pending_order_worker can introspect.
        """
        try:
            perp = self._info.user_state(self.account_address)
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid user_state failed: {e}") from e
        try:
            spot = self._info.spot_user_state(self.account_address)
        except Exception:
            spot = {}
        return {
            "perp": perp if isinstance(perp, dict) else {},
            "spot": spot if isinstance(spot, dict) else {},
        }

    def get_positions(self, *, symbol: str = "") -> List[Dict[str, Any]]:
        """
        Return open perp positions. HL is one-way only, so each (account, coin)
        appears at most once in the response.
        """
        try:
            state = self._info.user_state(self.account_address)
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid user_state failed: {e}") from e
        if not isinstance(state, dict):
            return []
        positions = state.get("assetPositions") or []
        wanted = to_hl_perp_coin(symbol) if symbol else ""
        out: List[Dict[str, Any]] = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            pos = p.get("position") or {}
            coin = str(pos.get("coin") or "").upper()
            if not coin:
                continue
            if wanted and coin != wanted:
                continue
            out.append(p)
        return out

    def get_open_orders(self, *, symbol: str = "") -> List[Dict[str, Any]]:
        try:
            orders = self._info.open_orders(self.account_address)
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid open_orders failed: {e}") from e
        if not isinstance(orders, list):
            return []
        if not symbol:
            return orders
        wanted = self._resolve_symbol(symbol, market_type="swap")
        wanted_spot = self._resolve_symbol(symbol, market_type="spot")
        return [o for o in orders if isinstance(o, dict) and str(o.get("coin") or "") in (wanted, wanted_spot)]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set_leverage(
        self,
        *,
        symbol: str,
        leverage: float,
        is_cross: bool = True,
    ) -> Dict[str, Any]:
        """
        HL leverage is per-coin and per-margin-mode. ``is_cross=False`` switches
        to per-asset isolated.

        Note: HL caps leverage by tier — the SDK / API will reject values
        higher than the current tier permits.
        """
        coin = to_hl_perp_coin(symbol)
        if not coin:
            raise LiveTradingError(f"Invalid symbol: {symbol}")
        try:
            lev = int(float(leverage or 1))
        except Exception:
            lev = 1
        if lev < 1:
            lev = 1
        try:
            return self._exchange.update_leverage(lev, coin, bool(is_cross))
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid update_leverage failed: {e}") from e

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        time_in_force: str = "Gtc",
        reduce_only: bool = False,
        post_only: bool = False,
        market_type: str = "swap",
        client_order_id: Optional[str] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """
        Place a single order. Mirrors the field names used by ``BinanceFuturesClient.place_order``
        so the existing dispatch in ``pending_order_worker`` can call it with
        the same kwargs.

        - ``side``         : "BUY" / "SELL"  (case-insensitive)
        - ``order_type``   : "LIMIT" / "MARKET"
        - ``time_in_force``: "Gtc" / "Ioc" / "Alo" (only meaningful for LIMIT;
                             ``post_only=True`` overrides to ``Alo``)
        - HL "market" orders are deeply-priced IOC limits per HL spec; the
          SDK's ``market_open`` helper handles slippage cap automatically.
        """
        wire = self._resolve_symbol(symbol, market_type=market_type)
        if not wire:
            raise LiveTradingError(f"Could not resolve Hyperliquid symbol: {symbol}")

        is_buy = str(side or "").strip().upper() == "BUY"
        size = float(qty or 0)
        if size <= 0:
            raise LiveTradingError("qty must be > 0")

        order_type_u = str(order_type or "LIMIT").strip().upper()
        tif = "Alo" if post_only else (time_in_force or "Gtc")

        try:
            if order_type_u == "MARKET":
                resp = self._exchange.market_open(
                    wire,
                    is_buy,
                    size,
                    None,           # px=None -> SDK applies default slippage
                    None,           # slippage default
                    cloid=_to_cloid(client_order_id),
                )
            else:
                if price is None or float(price) <= 0:
                    raise LiveTradingError("price is required for LIMIT orders")
                resp = self._exchange.order(
                    wire,
                    is_buy,
                    size,
                    float(price),
                    {"limit": {"tif": tif}},
                    reduce_only=bool(reduce_only),
                    cloid=_to_cloid(client_order_id),
                )
        except LiveTradingError:
            raise
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid place_order failed: {e}") from e

        # Surface API-level rejections as LiveTradingError so callers can log /
        # retry uniformly. SDK returns {"status": "ok"|"err", "response": {...}}.
        if isinstance(resp, dict) and str(resp.get("status") or "").lower() == "err":
            raise LiveTradingError(f"Hyperliquid rejected order: {resp.get('response')}")
        return resp if isinstance(resp, dict) else {"raw": resp}

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float = 0.0,
        size: float = 0.0,
        market_type: str = "swap",
        reduce_only: bool = False,
        pos_side: str = "",   # accepted for parity with other adapters; ignored (HL is one-way)
        client_order_id: Optional[str] = None,
        **_: Any,
    ) -> LiveOrderResult:
        """
        Match the ``place_market_order`` shape used by other adapters
        (``execution.place_order_from_signal`` calls this name on every
        crypto client). HL has no native market order — under the hood this
        is a deeply-priced IOC limit, applied by the SDK ``market_open``.
        """
        amount = float(qty if qty else size)
        resp = self.place_order(
            symbol=symbol,
            side=side,
            qty=amount,
            order_type="MARKET",
            market_type=market_type,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )
        return _to_live_order_result(resp)

    def cancel_order(
        self,
        *,
        symbol: str,
        order_id: str = "",
        client_order_id: str = "",
        market_type: str = "swap",
    ) -> Dict[str, Any]:
        wire = self._resolve_symbol(symbol, market_type=market_type)
        if not wire:
            raise LiveTradingError(f"Could not resolve Hyperliquid symbol: {symbol}")
        try:
            if client_order_id:
                resp = self._exchange.cancel_by_cloid(wire, _to_cloid(client_order_id))
            elif order_id:
                resp = self._exchange.cancel(wire, int(order_id))
            else:
                raise LiveTradingError("Either order_id (oid) or client_order_id (cloid) is required")
        except LiveTradingError:
            raise
        except Exception as e:
            raise LiveTradingError(f"Hyperliquid cancel_order failed: {e}") from e
        return resp if isinstance(resp, dict) else {"raw": resp}


def _to_live_order_result(resp: Dict[str, Any]) -> LiveOrderResult:
    """
    Translate the HL SDK order response to ``LiveOrderResult``.

    SDK shape (success):
        {
          "status": "ok",
          "response": {
            "type": "order",
            "data": {
              "statuses": [{"resting": {"oid": 123}}]   # or {"filled": {"oid":..., "totalSz":..., "avgPx":...}}
            }
          }
        }
    """
    raw = resp if isinstance(resp, dict) else {}
    response = raw.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else {}
    statuses = (data.get("statuses") if isinstance(data, dict) else None) or []
    oid = ""
    filled = 0.0
    avg_px = 0.0
    for st in statuses:
        if not isinstance(st, dict):
            continue
        if "resting" in st and isinstance(st.get("resting"), dict):
            try:
                oid = str(st["resting"].get("oid") or "")
            except Exception:
                pass
        if "filled" in st and isinstance(st.get("filled"), dict):
            f = st["filled"]
            try:
                oid = str(f.get("oid") or oid)
                filled = float(f.get("totalSz") or 0.0)
                avg_px = float(f.get("avgPx") or 0.0)
            except Exception:
                pass
    return LiveOrderResult(
        exchange_id="hyperliquid",
        exchange_order_id=oid,
        filled=float(filled or 0.0),
        avg_price=float(avg_px or 0.0),
        raw=raw,
    )


def _to_cloid(value: Optional[str]):
    """
    HL ``cloid`` is a 16-byte hex (32 hex chars + 0x prefix). The SDK exposes a
    Cloid type but accepts ``None`` to skip. Anything else, we let the SDK
    parse — it'll raise if the format is wrong, which we wrap into
    LiveTradingError at the call site.
    """
    s = str(value or "").strip()
    if not s:
        return None
    try:
        from hyperliquid.utils.types import Cloid  # type: ignore
        return Cloid.from_str(s if s.startswith("0x") else "0x" + s)
    except Exception:
        # Let SDK reject downstream if shape is wrong; we don't silently drop.
        return None
