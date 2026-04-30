# Hyperliquid 交易接入指南

本文带你把 [Hyperliquid](https://app.hyperliquid.xyz/trade) 接入 QuantDinger，并跑通第一笔策略。Hyperliquid 是去中心化的永续 + 现货交易所，自带杠杆与 Vault 子账户。

> **TL;DR.** 在 Hyperliquid 官网生成 *agent wallet*（**不是**你主钱包的私钥），把它填进 QuantDinger 的凭据，然后用「策略」下单。Quick-Trade 在 v1 暂不支持。

## 为什么 Hyperliquid 与 CEX 不同

- **鉴权**：HL 用以太坊 **EIP-712** 签名而不是 HMAC API key。你填进 QuantDinger 的"key"是一段 32 字节十六进制私钥，且来自 *agent 钱包*，不是你的主钱包。
- **钱包模型**：**主钱包 (master EOA)** 持有资金。你授权一个或多个 **agent wallet**，agent 可以下单/撤单，**但无法提现或转账**。**agent 180 天后过期。**
- **位置模式**：**只有 one-way（单向）**。没有 hedge。下买单会先冲抵已有空头，反之亦然。
- **符号**：
  - 永续：裸币名 `BTC`、`ETH`、`HYPE`、`SOL`。
  - 现货：位置索引 `@1`、`@107` 等（仅 `PURR/USDC` 是命名特例）。
- **杠杆**：分币、分保证金模式（cross 或 per-asset isolated）。主流币最高 **40×**，仓位增大时分级保证金会自动下调有效杠杆。
- **没有真正的 market 单**：HL 把 market 实现为深价 IOC limit + 滑点限制。QuantDinger 的适配器自动处理。

## 第 1 步：在 Hyperliquid 生成 agent wallet

1. 在 [app.hyperliquid.xyz](https://app.hyperliquid.xyz) 连接钱包，存入 USDC。
2. 进入 **Profile → API**（[直链](https://app.hyperliquid.xyz/API)）。
3. 点 **Generate** 创建一个新的 agent。
4. **复制 agent 私钥**（32 字节 hex，类似 `0xabc123…`）。**这是你要粘进 QuantDinger 的"key"。**
5. **链上 approve** 该 agent。这是整个流程里**唯一**会用到主钱包的步骤。

> ⚠ **agent 私钥是半热钱包密钥**。它无法提币，但泄露后攻击者可以用对手盘 wash 你的资金来榨取 PnL。当作热钱包密钥保管。

> ⚠ **永远不要粘贴主钱包 (master EOA) 的私钥。** QuantDinger 会校验 agent 推导地址不能等于 `wallet_address`，但若绕过这个校验，账户里的所有资金都将暴露。

## 第 2 步：（可选）先用测试网

测试网完全镜像主网：[app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz)。

1. 在 [app.hyperliquid-testnet.xyz/drip](https://app.hyperliquid-testnet.xyz/drip) 领 mock USDC。**官方 faucet 要求该地址在主网上有过任何金额的入金记录**（防 sybil）。如果被卡，可用 QuickNode 或 Chainstack 的第三方 faucet 拿测试网 HYPE。
2. 在测试网 UI 上重复**第 1 步**，创建一个独立的测试网 agent。

## 第 3 步：在 QuantDinger 添加凭据

UI：**设置 → 交易所凭据 → 新增**。

| 字段 | 填什么 | 必填 |
|------|--------|------|
| `exchange_id` | `hyperliquid` | ✓ |
| `wallet_address` | 主钱包地址（`0x…` 42 字符） | ✓ |
| `agent_private_key` | 第 1 步得到的 64 hex 私钥 | ✓ |
| `vault_address` | 你交易的 vault 地址（可选） | ✗ |
| `account_address` | agent 服务的子账户地址（可选） | ✗ |
| `isTestnet` | 用了测试网就勾选，否则不勾 | ✓ |

agent 私钥落库前会用 **Fernet**（密钥派生自 `SECRET_KEY`）加密。凭据列表只显示打码的钱包地址（`0x1234…abcd`），私钥不会再次回显。

## 第 4 步：测试连接

UI：**策略 → 新建 → 交易所选 Hyperliquid → 测试连接**。

后端会调 `Info.user_state(wallet_address)` 并验证 agent 是否被 approve。

- **成功**：返回账户保证金概览。
- **失败**：最常见的原因是 agent 还没在链上 approve（第 1.5 步）。重新 approve 后再试。

## 第 5 步：跑策略

任何 IndicatorStrategy / ScriptStrategy 都可以选 Hyperliquid 凭据。注意点：

- **符号**：直接填 `BTC`、`ETH`。适配器接受 `BTC/USDT` / `BTCUSDT` 也会自动归一为裸币名。现货建议用 `PURR/USDC` 这类命名，适配器会通过缓存的 `spotMeta` 自动转 `@<idx>`。
- **K 线 / 回测数据**：v1 复用 **Binance** 的 K 线（暂未实现 HL 原生数据源）。**HL 独占 token**（`HYPE`、`PURR`）回测和 AI 分析会显示"symbol not supported"——但实盘下单不受影响。
- **Order mode**：v1 跳过 limit-first / maker 流程，HL 一律走 market。即使把 `order_mode` 设成 `maker`，仍然是 market 单。Maker / post-only 留待后续版本。
- **杠杆**：策略层设置即可，HL 会按当前 tier 上限静默封顶。
- **Hedge mode**：不存在。从 Binance 双向持仓策略迁移过来时，要重新评估关仓逻辑——已有空头时下买单会**净额对冲**而不是开新多头。

## 第 6 步：Quick-Trade（v1 部分支持）

| 端点 | 状态 | 说明 |
|------|------|------|
| `GET /api/quick-trade/balance` | ✓ 支持 | 返回 `{available, total, currency: "USDC"}`，从 `marginSummary.accountValue` + `withdrawable` 解析 |
| `GET /api/quick-trade/position` | ✓ 支持 | 把 HL `[{position: {...}}]` 展平成与其它交易所一致的结构 |
| `POST /api/quick-trade/close-position` | ✓ 支持 | 通过 `place_order_from_signal` 下 reduce-only IOC 单 |
| `POST /api/quick-trade/place-order`（开仓） | ✗ 返回 400 | USDT→qty 反算 + 每个交易所的 filter 还没接 HL，请通过策略开仓 |

"一键平仓"（在 QuantDinger UI 里手动关掉 HL 仓位）这个最常用的应急场景在 v1 已经能用。

## v1 限制一览

| 项目 | v1 状态 |
|------|---------|
| 永续交易 | ✓ 支持 |
| 现货交易 | ✓ 通过 `spotMeta` 解析支持 |
| Vault 地址 | ✓ 支持 |
| 子账户 (`account_address`) | ✓ 支持 |
| 单向持仓 | ✓ HL 唯一模式 |
| Hedge 模式 | ✗ HL 本身不存在 |
| Hyperliquid 作为顶级市场出现在 UI 选项 | ✓ 已加 |
| Quick-Trade 余额 / 持仓 / 一键平仓 | ✓ 支持 |
| Quick-Trade 开仓 (place-order) | ✗ 返回 400，请用策略开仓 |
| Limit-first / maker 模式 | ✗ 强制 market |
| 策略关仓时按交易所真实仓位自动修正 amount | ✓ 支持 |
| HL 独占 token 回测 | ✗ "symbol not supported" |
| WebSocket 行情 | ✗ 仅 REST 轮询 |
| Builder code（HL 推广分润） | ✗ 后续版本 |
| HL 原生 K 线 | ✗ v1 复用 Binance |
| Agent 续期提醒 | ✗ 暂需手动（180 天后失效） |

## 故障排查

**保存凭据时报 "agent_private_key matches wallet_address"** → 你粘的是主钱包私钥。**停！** 去 HL UI 生成真正的 agent。

**测试连接报 `User does not exist` 或类似错误** → agent 还没在链上 approve，或者你填的 `wallet_address` 不是 approve 这个 agent 的那个主钱包。两个字段都核对一遍。

**下单提示保证金 / 杠杆错误** → HL 的分级保证金把有效杠杆压低了。减小仓位或调低策略里的杠杆。

**回测显示 "symbol not supported"** → 你在回测 HYPE / PURR / 其他 HL 独占资产。v1 请改用有 Binance 等价市场的币（BTC、ETH、SOL、ARB 等）。

**agent 过期（180 天）** → 下单静默失败。在 HL UI 生成新 agent，更新凭据即可。

## 安全清单

- [ ] 粘到 QuantDinger 的是 **agent 私钥**，不是主钱包私钥。
- [ ] `wallet_address` 是**主钱包**地址，不是 agent。
- [ ] 用测试网就勾上 `isTestnet`。
- [ ] QuantDinger 后端的 `SECRET_KEY` 是一段强随机值（Fernet 加密派生自它，事后修改会让已存凭据无法解密）。
- [ ] 175 天后日历提醒：续 agent。

## 参考

- HL 文档：[hyperliquid.gitbook.io](https://hyperliquid.gitbook.io/hyperliquid-docs)
- API wallet / nonce 机制：[hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)
- 测试网 faucet：[app.hyperliquid-testnet.xyz/drip](https://app.hyperliquid-testnet.xyz/drip)
- 后端使用的官方 Python SDK：[github.com/hyperliquid-dex/hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
