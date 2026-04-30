"""
Base class for live-trading clients that sign actions with an Ethereum private
key (e.g. Hyperliquid's EIP-712 / phantom-agent flow), as opposed to HMAC over
REST.

Kept deliberately thin: the actual signing is delegated to vendor SDKs
(``hyperliquid-python-sdk`` for Hyperliquid). This class only:

- Loads / validates the agent private key into an ``eth_account.LocalAccount``.
- Rejects accidental use of the master EOA private key (heuristic: any key
  matching the configured ``wallet_address``).
- Provides a common error type and ``base_url`` plumbing so the factory and
  ``pending_order_worker`` can treat signed clients alongside REST clients.

Anything stronger (replay protection, msgpack canonicalization, nonce
tracking) is the SDK's job.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Union

from app.services.live_trading.base import LiveTradingError

logger = logging.getLogger(__name__)


class BaseSignedClient:
    """Marker base class for signed (EIP-712) live-trading clients."""

    def __init__(
        self,
        *,
        base_url: str,
        wallet_address: str,
        agent_private_key: str,
        timeout_sec: float = 15.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.wallet_address = self._normalize_address(wallet_address)
        if not self.wallet_address:
            raise LiveTradingError("Missing wallet_address (master EOA)")
        if not (agent_private_key or "").strip():
            raise LiveTradingError("Missing agent_private_key")
        self._agent = self._load_agent(agent_private_key)
        self._reject_if_master_key(self._agent.address)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _normalize_address(addr: Any) -> str:
        s = str(addr or "").strip()
        if not s:
            return ""
        if not s.startswith("0x") and not s.startswith("0X"):
            s = "0x" + s
        # 0x + 40 hex chars = 42; allow user to paste with whitespace
        if len(s) != 42:
            return ""
        return s.lower()

    def _load_agent(self, private_key: str) -> Any:
        """
        Lazy-load eth_account so this module imports cleanly even when the
        Hyperliquid SDK (and its ``eth-account`` dep) is not installed.
        """
        try:
            from eth_account import Account  # type: ignore
        except ImportError as e:
            raise LiveTradingError(
                "Signed live-trading requires eth-account. Install hyperliquid-python-sdk."
            ) from e
        key = str(private_key or "").strip()
        if not key.startswith("0x") and not key.startswith("0X"):
            key = "0x" + key
        try:
            return Account.from_key(key)
        except Exception as e:
            raise LiveTradingError(f"Invalid agent_private_key: {e}") from e

    def _reject_if_master_key(self, agent_address: str) -> None:
        """
        Sanity check: the agent address derived from ``agent_private_key`` MUST
        differ from the master ``wallet_address``. If they match, the user
        almost certainly pasted their master EOA private key — refuse and tell
        them to create an agent in the UI instead.
        """
        if not agent_address:
            return
        if agent_address.lower() == self.wallet_address.lower():
            raise LiveTradingError(
                "agent_private_key derives the same address as wallet_address. "
                "Do NOT paste your master EOA private key — generate an agent "
                "wallet at app.hyperliquid.xyz/API and paste that key instead."
            )

    @property
    def agent_address(self) -> str:
        try:
            return str(self._agent.address)
        except Exception:
            return ""
