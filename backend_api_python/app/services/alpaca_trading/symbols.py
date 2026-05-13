"""
Alpaca symbol normalization.

Alpaca uses standard US ticker symbols for stocks/ETFs (e.g., "SPY", "AAPL")
and slash-separated pairs for crypto (e.g., "BTC/USD", "ETH/USD").
"""

from typing import Tuple


def normalize_symbol(symbol: str, asset_class: str = "us_equity") -> str:
    """
    Normalize a symbol to Alpaca's expected format.

    Args:
        symbol: User-provided symbol (e.g., "AAPL", "BTCUSD", "BTC/USD")
        asset_class: "us_equity" or "crypto"

    Returns:
        Normalized symbol for Alpaca API
    """
    s = symbol.upper().strip()
    if asset_class == "crypto":
        # Crypto pairs: ensure slash format ("BTC/USD")
        if "/" in s:
            return s
        # Convert BTCUSD -> BTC/USD, ETHUSDT -> ETH/USDT
        for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
            if s.endswith(quote) and len(s) > len(quote):
                base = s[:-len(quote)]
                return f"{base}/{quote}"
        return s
    # Equities: just uppercase ticker
    return s.replace("/", ".")  # BRK/B -> BRK.B


def parse_symbol(symbol: str) -> Tuple[str, str]:
    """
    Parse a symbol into (base, asset_class).

    Returns:
        Tuple of (normalized_symbol, asset_class)
    """
    s = symbol.upper().strip()
    if "/" in s or any(s.endswith(q) for q in ("USD", "USDT", "USDC")):
        return normalize_symbol(s, "crypto"), "crypto"
    return normalize_symbol(s, "us_equity"), "us_equity"


def format_display_symbol(symbol: str, asset_class: str = "us_equity") -> str:
    """Format symbol for UI display."""
    if asset_class == "crypto":
        return normalize_symbol(symbol, "crypto")
    return symbol.upper()
