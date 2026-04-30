# Hyperliquid Trading Guide

This guide walks you through connecting [Hyperliquid](https://app.hyperliquid.xyz/trade) to QuantDinger and running your first strategy. Hyperliquid is a decentralized perpetuals + spot exchange with native leverage and Vault support.

> **TL;DR.** Generate an *agent wallet* on Hyperliquid (NOT your master EOA private key), paste it into QuantDinger as a credential, and trade through a Strategy. Quick-Trade is not yet supported in v1.

## Why Hyperliquid is different

- **Auth**: Hyperliquid signs every order with an Ethereum **EIP-712** signature, not an HMAC API key. The "key" you paste is a 32-byte hex private key from an *agent wallet*, not from your main wallet.
- **Wallet model**: your **master EOA** holds the funds. You delegate trading rights to one or more **agent wallets** that can place / cancel orders but **cannot withdraw or transfer**. Agents expire after **180 days**.
- **Position mode**: **one-way only**. There is no hedge mode. Net buys reduce existing shorts and vice-versa.
- **Symbols**:
  - Perp markets are bare coin names: `BTC`, `ETH`, `HYPE`, `SOL`.
  - Spot markets are positional indices: `@1`, `@107`, etc. (`PURR/USDC` is the only named exception.)
- **Leverage**: per-coin, per-margin-mode (cross or per-asset isolated). Maximum is **40×** on top markets and is automatically reduced as your position size grows (tiered margin).
- **No "market" order on the wire**: HL implements market orders as deeply-priced IOC limits with a slippage cap. The QuantDinger adapter handles this transparently.

## Step 1 — Create an agent wallet on Hyperliquid

1. Connect your wallet to [app.hyperliquid.xyz](https://app.hyperliquid.xyz) and deposit USDC if you haven't already.
2. Go to **Profile → API** ([direct link](https://app.hyperliquid.xyz/API)).
3. Click **Generate** to create a new agent wallet.
4. **Copy the agent's private key** (32 bytes hex, e.g. `0xabc123…`). Save it somewhere temporary — you'll paste it into QuantDinger next.
5. **Approve** the agent on-chain. This is the only step that uses your master EOA.

> ⚠ **The agent private key is semi-hot.** It cannot withdraw, but a leak lets an attacker place wash trades against your liquidity to drain your PnL. Treat it like a hot wallet key, not a read-only key.

> ⚠ **Never paste your master EOA private key.** QuantDinger will refuse to save it (the agent address must differ from `wallet_address`), but if you bypass that check, every dollar in the account is at risk.

## Step 2 — (Optional) Testnet first

Hyperliquid has a fully mirrored testnet at [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz). Recommended before mainnet:

1. Get test USDC from the faucet at [app.hyperliquid-testnet.xyz/drip](https://app.hyperliquid-testnet.xyz/drip). The official faucet requires a prior mainnet deposit from the same address (anti-sybil). If that gates you, third-party faucets like QuickNode or Chainstack provide testnet HYPE.
2. Repeat **Step 1** on the testnet UI to create a separate testnet agent wallet.

## Step 3 — Add the credential in QuantDinger

UI: **Settings → Exchange Credentials → New**.

| Field | What to paste | Required |
|-------|---------------|----------|
| `exchange_id` | `hyperliquid` | ✓ |
| `wallet_address` | your master EOA address (`0x…`, 42 chars) | ✓ |
| `agent_private_key` | the 64-hex-char private key from Step 1 | ✓ |
| `vault_address` | a vault address you trade for (optional) | ✗ |
| `account_address` | a subaccount address the agent acts on (optional) | ✗ |
| `isTestnet` | true if you used the testnet UI in Step 1, otherwise false | ✓ |

The agent key is encrypted with **Fernet** (key derived from `SECRET_KEY`) before it ever touches the database. The credentials list view shows only a masked wallet hint (`0x1234…abcd`) — the agent key is never displayed again.

## Step 4 — Test connection

UI: **Strategies → New → Exchange = Hyperliquid → Test connection**.

This calls `Info.user_state(wallet_address)` and confirms the agent is approved. Expect:

- **Success**: account margin summary returns.
- **Failure**: most common cause is the agent has not been approved on-chain yet (Step 1.5). Re-approve and retry.

## Step 5 — Run a strategy

Build any IndicatorStrategy or ScriptStrategy and select your Hyperliquid credential. Things to know:

- **Symbol**: enter `BTC`, `ETH`, etc. The adapter accepts `BTC/USDT` / `BTCUSDT` style and reduces them to bare coins automatically. For spot, prefer `PURR/USDC` notation; the adapter will resolve to `@<idx>` via cached `spotMeta`.
- **K-line / backtest data**: in v1, QuantDinger reuses **Binance** K-lines for HL symbols (no native HL data source yet). For HL-exclusive tokens like **HYPE** or **PURR**, backtest and AI analysis will report "symbol not supported" — live trading still works.
- **Order mode**: HL skips the limit-first / maker flow in v1 — every order is sent as a market (IOC) order. If you set `order_mode = maker`, the adapter still submits a market order. Maker / post-only support will land in a later release.
- **Leverage**: set per-strategy. Hyperliquid will silently cap to the current tier maximum.
- **Hedge mode**: not available. If you migrate a Binance dual-side strategy, evaluate the close logic carefully — buys against an existing short position will *net*, not open a new long.

## Step 6 — Quick-trade (fully supported in v1)

| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /api/quick-trade/balance` | ✓ Supported | Returns `{available, total, currency: "USDC"}` from `marginSummary.accountValue` + `withdrawable` |
| `GET /api/quick-trade/position` | ✓ Supported | Flattens HL `[{position: {...}}]` into the same shape as other exchanges |
| `POST /api/quick-trade/close-position` | ✓ Supported | Sends a reduce-only IOC order via `place_order_from_signal` |
| `POST /api/quick-trade/place-order` (open position) | ✓ Supported | USDT amount → coin qty via HL's `get_ticker` (same flow as Binance/OKX) |

Open + close + balance + position queries all behave like any other Crypto exchange.

## Limitations & gotchas

| Item | Status in v1 |
|------|--------------|
| Perp market trading | ✓ Supported |
| Spot trading | ✓ Supported (resolved via `spotMeta`) |
| Vault address | ✓ Supported |
| Subaccount (`account_address`) | ✓ Supported |
| One-way position mode | ✓ Only mode HL supports |
| Hedge mode | ✗ Not available on HL |
| Hyperliquid as a top-level market type in the UI | ✗ Not a market — Hyperliquid is a **Crypto exchange** (peer of Binance / OKX). UI selects Crypto + binds an HL credential. |
| `/api/market/symbols/search?market=Crypto&exchange_id=hyperliquid` | ✓ Returns Binance USDT pairs displayed as `BASE/USDC` (HL's quote convention). HL-exclusive tokens (HYPE, PURR) don't appear in v1; P2 will pull HL's own universe. |
| Quick-trade balance / position / close-position / place-order | ✓ Supported (all four) |
| Limit-first / maker order mode | ✓ Supported (`order_mode=maker`/`limit_first` uses HL's ALO TIF) |
| Strategy reduce-only auto-adjust to actual on-exchange position | ✓ Supported |
| Backtest on HL-exclusive tokens (HYPE, PURR) | ✗ "Symbol not supported" |
| WebSocket streams | ✗ REST polling only |
| Builder code (HL referral) | ✗ Future release |
| Native HL K-line / OHLC | ✗ Reuses Binance equivalent for v1 |
| Agent renewal reminder | ✗ Manual — agents expire 180 days after creation |

## Troubleshooting

**"agent_private_key matches wallet_address"** during credential save → you pasted your master EOA private key. Stop. Generate a real agent wallet in the HL UI.

**`User does not exist` / similar** during test-connection → the agent hasn't been approved on-chain yet, or the `wallet_address` you typed isn't the address that approved this agent. Re-check both fields.

**Order rejected with margin / leverage error** → HL's tiered margin lowered your effective max leverage. Reduce position size or lower leverage in the strategy config.

**Backtest says "symbol not supported"** → you're trying to backtest HYPE / PURR / another HL-exclusive. Pick a coin that has a Binance equivalent (BTC, ETH, SOL, ARB, …) for v1.

**Agent expired (180-day)** → silent failure on order submit. Generate a fresh agent in the HL UI, update the credential.

## Security checklist

- [ ] Agent key, not master key, pasted into QuantDinger.
- [ ] `wallet_address` is your *master* EOA, not the agent.
- [ ] If on testnet, `isTestnet=true` was checked.
- [ ] `SECRET_KEY` for the QuantDinger backend is a strong random value (the Fernet key for credential encryption derives from it; changing it after the fact makes saved keys unreadable).
- [ ] Agent rotation calendar reminder set for 175 days.

## Reference

- Hyperliquid docs: [hyperliquid.gitbook.io](https://hyperliquid.gitbook.io/hyperliquid-docs)
- API wallets / nonces: [hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)
- Testnet faucet: [app.hyperliquid-testnet.xyz/drip](https://app.hyperliquid-testnet.xyz/drip)
- Official Python SDK (used internally): [github.com/hyperliquid-dex/hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
