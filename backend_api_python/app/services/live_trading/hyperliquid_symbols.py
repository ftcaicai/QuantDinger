"""
Hyperliquid symbol normalization.

Hyperliquid splits perpetuals and spot:

- Perpetual symbols are bare base coins. ``BTC``, ``ETH``, ``HYPE``. The wire
  format never contains the quote token; HL perps are USD-margined under the
  hood so any incoming ``BTC/USDT`` / ``BTCUSDT`` from QuantDinger UI must be
  reduced to its base.
- Spot symbols are positional indexes into ``spotMeta.universe``: ``@1``,
  ``@107``... The historical exception is ``PURR/USDC`` which is also accepted
  by name. Mapping requires fetching ``spotMeta`` once (cached).

For "复用 Binance 价格" (reuse Binance K-line for backtest / AI), we map HL
coin → Binance symbol best-effort; HL-exclusive tokens (no Binance equivalent)
return ``None`` so callers can surface a clear "not supported" error rather
than feed bogus data.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


class KlineSymbolError(Exception):
    """
    Raised when a Hyperliquid symbol cannot be resolved to a K-line source.

    This typically means an HL-exclusive token (PURR, HYPE, ...) that has no
    Binance equivalent for the v1 "reuse Binance prices" fallback.

    Callers (routes, backtest, AI analysis) should catch this and surface a
    user-friendly message rather than feeding empty K-line data into a
    strategy or model.
    """


def _split_base_quote(symbol: str) -> Tuple[str, str]:
    """Local copy of symbols._split_base_quote to avoid a circular import."""
    s = (symbol or "").strip()
    if ":" in s:
        s = s.split(":", 1)[0]
    if "/" not in s:
        s_upper = s.upper()
        common_quotes = ['USDT', 'USD', 'BTC', 'ETH', 'BUSD', 'USDC', 'BNB']
        for quote in common_quotes:
            if s_upper.endswith(quote) and len(s_upper) > len(quote):
                base = s_upper[:-len(quote)]
                if base:
                    return base, quote
        return s_upper, ""
    base, quote = s.split("/", 1)
    return base.strip().upper(), quote.strip().upper()


# HL symbols that don't map cleanly via plain base/USDT on Binance.
# Most top tokens (BTC, ETH, SOL, ARB, ...) work directly, so the table only
# lists known divergences. If a coin isn't here we try ``<COIN>/USDT`` first
# and fall back to ``None`` if Binance has no such market.
_HL_TO_BINANCE_OVERRIDES: Dict[str, Optional[str]] = {
    # HL-exclusive tokens with no Binance equivalent
    "PURR": None,
    "HYPE": None,
    # USDC-quoted on HL spot (rare): caller should use USDT equivalent
}


def to_hl_perp_coin(symbol: str) -> str:
    """
    UI/strategy symbol -> HL perp coin name.

    ``BTC/USDT:USDT`` / ``BTC/USDT`` / ``BTCUSDT`` / ``BTC`` -> ``BTC``
    """
    s = (symbol or "").strip()
    if not s:
        return ""
    base, _ = _split_base_quote(s)
    if base:
        return base.upper()
    return s.upper()


def to_hl_spot_index(symbol: str, spot_meta: Optional[Dict[str, Any]] = None) -> str:
    """
    UI symbol -> HL spot wire identifier (``@<idx>`` or ``PURR/USDC``).

    ``spot_meta`` is the dict returned by ``Info(...).spot_meta()``; pass
    ``None`` when only a name-based shortcut is needed.

    Returns the original ``symbol`` if no mapping can be resolved (caller
    should treat that as a "not found" error rather than blindly trusting).
    """
    s = (symbol or "").strip()
    if not s:
        return ""
    # Already a wire symbol
    if s.startswith("@") or s.upper() == "PURR/USDC":
        return s
    base, quote = _split_base_quote(s)
    base_u = base.upper()

    if base_u == "PURR" and quote.upper() in ("USDC", "USD", ""):
        return "PURR/USDC"

    if not isinstance(spot_meta, dict):
        return s

    universe = spot_meta.get("universe") or []
    tokens = spot_meta.get("tokens") or []
    if not isinstance(universe, list) or not isinstance(tokens, list):
        return s

    # Build a small index: token_name -> token_idx
    token_idx_by_name: Dict[str, int] = {}
    for t in tokens:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").upper()
        try:
            idx = int(t.get("index"))
        except Exception:
            continue
        if name:
            token_idx_by_name[name] = idx

    target_token_idx = token_idx_by_name.get(base_u)
    if target_token_idx is None:
        return s

    # Find universe entry whose first token is the target. Quote currency
    # matters only loosely on HL spot; we accept the first match.
    for entry in universe:
        if not isinstance(entry, dict):
            continue
        toks = entry.get("tokens")
        if not isinstance(toks, list) or not toks:
            continue
        try:
            first = int(toks[0])
        except Exception:
            continue
        if first == target_token_idx:
            try:
                idx = int(entry.get("index"))
            except Exception:
                continue
            return f"@{idx}"

    return s


def from_hl_to_binance_equivalent(
    coin: str,
    *,
    spot_meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Map a HL perp/spot symbol -> the Binance symbol QuantDinger should use for
    K-line / AI / backtest reads.

    - Perps: ``BTC`` -> ``BTC/USDT``
    - Spot @<idx>: resolve via ``spotMeta`` to base name, then ``BASE/USDT``
    - HL-exclusive tokens (no Binance market): return ``None`` so callers can
      surface a clear "symbol not supported for backtest" error.
    """
    s = (coin or "").strip()
    if not s:
        return None

    # @<idx> -> resolve to token name; cannot proceed without spot_meta
    if s.startswith("@"):
        if not isinstance(spot_meta, dict):
            return None
        try:
            idx = int(s[1:])
        except Exception:
            return None
        universe = spot_meta.get("universe") or []
        tokens = spot_meta.get("tokens") or []
        token_name_by_idx: Dict[int, str] = {}
        for t in tokens:
            if isinstance(t, dict):
                try:
                    token_name_by_idx[int(t.get("index"))] = str(t.get("name") or "").upper()
                except Exception:
                    continue
        s = ""
        for entry in universe:
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("index")) != idx:
                    continue
            except Exception:
                continue
            toks = entry.get("tokens")
            if isinstance(toks, list) and toks:
                try:
                    s = token_name_by_idx.get(int(toks[0]), "")
                except Exception:
                    s = ""
                break
        if not s:
            return None

    # PURR/USDC etc. — strip quote, treat as base name lookup
    if "/" in s:
        s, _ = s.split("/", 1)

    base = s.strip().upper()
    if not base:
        return None
    if base in _HL_TO_BINANCE_OVERRIDES:
        return _HL_TO_BINANCE_OVERRIDES[base]
    return f"{base}/USDT"


def maybe_transform_kline_symbol(
    *,
    exchange_id: Optional[str],
    market: str,
    symbol: str,
    spot_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    K-line entry-point hook for the v1 "reuse Binance prices" fallback.

    When a strategy is bound to Hyperliquid, the K-line / AI / backtest paths
    still pull data from the default crypto data source (Binance via CCXT).
    The strategy's symbol arrives in HL form (``BTC``, ``HYPE``, ``@107``);
    this function rewrites it to the Binance-equivalent (``BTC/USDT``) the
    data source actually understands, OR raises ``KlineSymbolError`` for
    HL-exclusive tokens that have no equivalent.

    Pass-through behavior (returns ``symbol`` unchanged):
    - ``exchange_id`` is anything other than ``hyperliquid``
    - ``market`` is anything other than ``Crypto``
    - the symbol already looks like a Binance market (``BTC/USDT``,
      ``BTCUSDT``, ``ETH/USDT:USDT``)

    Raises ``KlineSymbolError`` when the resolved coin has no Binance market
    (HYPE, PURR, ...).
    """
    eid = (exchange_id or "").strip().lower()
    market_str = (market or "").strip()
    market_lower = market_str.lower()
    # Trigger when EITHER the strategy is bound to a Hyperliquid credential OR
    # the user picked "Hyperliquid" from the market dropdown directly. After
    # ``DataSourceFactory.normalize_market`` aliases ``Hyperliquid`` -> ``Crypto``
    # we still see the original market value here (transform happens before
    # normalization in the factory).
    is_hl = (eid == "hyperliquid") or (market_lower == "hyperliquid")
    if not is_hl:
        return symbol
    # Once we know it's HL, the underlying data comes from the Crypto source.
    # Accept "Crypto" and "Hyperliquid" both — anything else is out of scope.
    if market_str not in ("Crypto", "Hyperliquid") and market_lower not in ("crypto", "hyperliquid"):
        return symbol

    s = (symbol or "").strip()
    if not s:
        return s

    # Already in Binance form? Treat anything that contains "/" or a known
    # quote suffix as already-resolved and pass through. The downstream data
    # source's own normalization handles the rest.
    if "/" in s:
        return s
    upper = s.upper()
    for q in ("USDT", "USDC", "USD", "BTC", "ETH", "BUSD", "BNB"):
        if upper.endswith(q) and len(upper) > len(q):
            return upper

    equivalent = from_hl_to_binance_equivalent(s, spot_meta=spot_meta)
    if equivalent is None:
        raise KlineSymbolError(
            f"Hyperliquid symbol '{s}' has no Binance-equivalent market for "
            f"backtest / AI analysis in v1. Live trading on HL still works."
        )
    return equivalent
