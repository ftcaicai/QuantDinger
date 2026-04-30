# 03 · 行情数据 & 交易所/经纪商集成

数据 / 执行层是这套代码里**适配器最多**的部分（10+ CCXT 交易所 + IBKR + MT5 + 5 个股票数据源）。本文给出这套适配器系统的契约、派发逻辑、防护层与陷阱。

## 三层切分（再强调一次）

| 层 | 目录 | 输入 | 输出 | 状态 |
|---|------|------|------|------|
| **行情读** | [data_sources/](../../backend_api_python/app/data_sources/) | symbol + timeframe | OHLCV / ticker | 缓存 5 分钟 |
| **仪表盘聚合** | [data_providers/](../../backend_api_python/app/data_providers/) | 多 symbol、新闻、情绪 | 聚合 dict | 独立 2–5 分钟 缓存 |
| **执行写** | [services/live_trading/](../../backend_api_python/app/services/live_trading/) [services/ibkr_trading/](../../backend_api_python/app/services/ibkr_trading/) [services/mt5_trading/](../../backend_api_python/app/services/mt5_trading/) | exchange + order | 成交回执 | 实时，无缓存 |

不要把读和写写到同一个适配器里。

## 行情读：`data_sources/`

### 契约

`data_sources/base.py` 定义所有适配器必须实现的最小接口：

```python
get_kline(symbol, timeframe, limit, before_time=None, after_time=None) -> List[Dict]
# 返回： [{"time": <UTC秒>, "open": float, "high": float, "low": float, "close": float, "volume": float}, ...]

get_ticker(symbol) -> Dict   # 可选，CCXT 风格 {"last": float, ...}
```

**必须遵守的两个隐式约定**：

1. **时间戳是 UTC 秒**，不是毫秒。CCXT 默认毫秒，要在适配器里 `// 1000`。前端依赖此约定画图。
2. **过期判定**：`base.py::log_result()` 自带 staleness 警告，1m/小时级 2 根，日 5 天，周 21 天。这些阈值给 holiday/weekend 留了余量；新增 timeframe 时记得对齐阈值。

### 派发：按市场类型，不是 symbol

`data_sources/factory.py::DataSourceFactory.get_source(market)` 按 **market** 字段路由：

| market | 实现 | 来源 |
|--------|------|------|
| `Crypto` | [crypto.py](../../backend_api_python/app/data_sources/crypto.py) | CCXT (Binance/OKX/Bybit/Kraken/...) |
| `USStock` | [us_stock.py](../../backend_api_python/app/data_sources/us_stock.py) | yfinance / Tiingo / Finnhub |
| `CNStock` | [cn_stock.py](../../backend_api_python/app/data_sources/cn_stock.py) | 腾讯 / AKShare / 东方财富 |
| `HKStock` | [hk_stock.py](../../backend_api_python/app/data_sources/hk_stock.py) | 腾讯 / 东方财富 |
| `Forex` | [forex.py](../../backend_api_python/app/data_sources/forex.py) | yfinance / AKShare |
| `Futures` | [futures.py](../../backend_api_python/app/data_sources/futures.py) | CCXT futures |
| `Polymarket` | [polymarket.py](../../backend_api_python/app/data_sources/polymarket.py) | Polymarket Gamma/Data API |
| `AsiaStockKline` | [asia_stock_kline.py](../../backend_api_python/app/data_sources/asia_stock_kline.py) | 日韩台亚太股 |

并附带别名归一化：`stock` → `USStock`，`cn_stock` → `CNStock`，`fx` → `Forex`，等等。新增市场时**先在 factory.py 注册 alias**，再写适配器，否则前端传过来的 `market` 字符串会 lookup 失败。

### 防护层（`cache_manager` + `circuit_breaker` + `rate_limiter`）

数据层对外接 **REST/CCXT**，要扛限流和不稳定。三件套配合使用：

**[cache_manager.py](../../backend_api_python/app/data_sources/cache_manager.py)**
- TTL + LRU。realtime 20 分钟、kline 5 分钟、stock\_info 24 小时。
- 后端：`CACHE_ENABLED=true` 用 Redis，否则进程内 dict。
- 命中率会被记录，可以用 `/api/health` 关联端点观察。

**[circuit_breaker.py](../../backend_api_python/app/data_sources/circuit_breaker.py)**
- 状态机：`CLOSED → OPEN（3 分钟冷却）→ HALF_OPEN（探测）→ CLOSED`。
- 失败阈值 2–3 次。**走熔断的源会被绕过，不抛异常**——前端可能拿到空 K 线而不报错。

**[rate_limiter.py](../../backend_api_python/app/data_sources/rate_limiter.py)**
- 单源限频：腾讯 1 req/s，东方财富 0.5 req/s，AKShare 0.5 req/s。
- 指数退避 + ±20% jitter，最多 30s。
- User-Agent 池（11 个），模拟浏览器流量。
- **限流器是模块级单例**：跨路由共享同一队列，不要 per-request 实例化。

### 国内数据源 & 代理隔离

`run.py` 已经把 `.eastmoney.com` / `.sina.com.cn` / `.akshare.xyz` 等域加进 `NO_PROXY`（见 [01-architecture-overview.md](01-architecture-overview.md)）。**新增国内数据源时，把它的域也加进 `_CN_FINANCIAL_DOMAINS`**，否则在配了境外代理的部署上会被拖慢甚至阻断。

## 仪表盘聚合：`data_providers/`

[data_providers/](../../backend_api_python/app/data_providers/) 不是替代品，而是**消费 data_sources 的二级层**：

| 文件 | 用途 |
|------|------|
| `crypto.py` / `forex.py` / `commodities.py` / `indices.py` | 多 symbol 行情聚合，全局市场 dashboard 用 |
| `heatmap.py` | 板块/市值热力图，扇区分组 |
| `news.py` | 多源财经新闻聚合 |
| `sentiment.py` | 综合情绪（恐惧贪婪、市场宽度…） |
| `adanos_sentiment.py` | Adanos Market Sentiment（美股，需要 `ADANOS_API_KEY`） |
| `opportunities.py` | "机会雷达"——筛选异动 symbol |

**消费方**：`MarketDataCollector`（AI 分析单调用前的统一取数器）、`/api/global-market/*` 路由、Polymarket 分析里取关联资产。

## 执行写：`services/live_trading/` (CCXT-style)

### 派发

`services/live_trading/factory.py` 按 `exchange_id` 创建客户端。已支持的：

| exchange | 文件 | 现货 / 永续 / 期货 |
|----------|------|------|
| Binance | binance.py + binance_spot.py | 现货 / 永续 / 杠杆 |
| OKX | okx.py | 现货 / 永续 / 期权 |
| Bitget | bitget.py + bitget_spot.py | 现货 / 永续 / 跟单 |
| Bybit | bybit.py | 现货 / Linear |
| Coinbase | coinbase_exchange.py | 现货 |
| Kraken / Kraken Futures | kraken.py + kraken_futures.py | 现货 / 期货 |
| KuCoin | kucoin.py | 现货 / 期货 |
| Gate | gate.py | 现货 / 期货 |
| Deepcoin | deepcoin.py | 衍生品 |
| HTX | htx.py | 现货 / U 本位 |

每个客户端继承 `base.py::BaseRestClient`：15s 超时、SSL verify 解析、HMAC/签名工具方法。**Demo / sandbox 模式**通过 `isTestnet` / `sandbox` / `simulatedTrading` 标志识别（前端在 body 顶层或 nested `exchange_config` 都可能传，factory 合并）。

### 符号归一化

`services/live_trading/symbols.py` 是各家变体的转换函数集合：

```
BTC/USDT 入站
  → Binance spot:    BTC_USDT (下划线)
  → Binance futures: BTCUSDT
  → OKX swap:        BTC-USDT-SWAP
  → Coinbase:        BTC-USDT
  → Kraken:          XBT/USD（注意 BTC→XBT 重命名）
```

**`_symbols_match_quick_trade()`** 是模糊匹配的最后一道防线（用户在 quick-trade 输入的 symbol 与交易所返回的 position id 对齐），但**优先用交易所原生符号**比依赖模糊匹配安全。

### 凭据装载

`pending_order_worker` 不直接用前端传过来的 keys。流程：

1. 策略行的 `exchange_config` 里要么有 `credential_id` 引用 `qd_exchange_credentials`，要么有 inline 的 `api_key` / `secret_key`（也是合法的，例如 paper 模式）。
2. `services/exchange_execution.resolve_exchange_config(config, user_id)`：
   - 如果有 `credential_id`，读 `qd_exchange_credentials.encrypted_config`，用 Fernet 解密；
   - 合并策略级 override（demo flag、base\_url、leverage…）；
   - 返回最终 dict 给 factory。
3. **打日志前永远调 `safe_exchange_config_for_log()`**，否则 secret 进日志。

## 桌面经纪商：IBKR & MT5

桌面 broker 与 CCXT 完全不同：

| 维度 | CCXT (crypto) | IBKR | MT5 |
|------|---------------|------|-----|
| 通信 | REST + (部分) WS | TWS Socket via `ib_insync` | Win32 DLL via `MetaTrader5` |
| 认证 | api\_key / secret | TWS 登录态 | MT5 终端登录态 |
| 部署 | 任意服务器 | **必须有 TWS / Gateway 进程** | **必须 Windows + MT5 终端** |
| 闸门 env | 无 | `ALLOW_LOCAL_DESKTOP_BROKERS=true` | 同上 |
| 路由 | 不直接暴露 | `/api/ibkr/*` | `/api/mt5/*` |

**云 SaaS 部署必须设 `ALLOW_LOCAL_DESKTOP_BROKERS=false`**，否则前端会显示 IBKR/MT5 入口但任何下单调用都因连接失败超时。

- IBKR 默认连 `127.0.0.1:7497`（live）或 `7496`（paper）。TWS 闲置 15 分钟会自动断开，需要 `client.connect()` 重连——上层记得加重试。
- MT5 仅 Windows 可用。Linux/Mac 上 import `MetaTrader5` 会失败；factory 里有 lazy import 兜住，错误码会变成"未配置"而不是 500。
- MT5 factory 限制 `market_category == 'Forex'`。试图在 MT5 上交易加密 / 股票会被拒绝。

## SSL / 代理 / 证书

- `LIVE_TRADING_CA_BUNDLE` 指向 PEM 文件（用 SOCKS 代理或自签证书时常用）。
- `LIVE_TRADING_SSL_VERIFY=false` —— **仅本地开发**。
- 默认走 certifi 或系统 CA bundle（`/etc/ssl/certs/ca-certificates.crt`）。

## 路由层入口

| 路由 | 用途 | 走的层 |
|------|------|--------|
| `/api/market/*` | 自选股、symbol 搜索 | data\_sources |
| `/api/indicator/kline` | K 线（带缓存） | data\_sources（KlineService 包装） |
| `/api/global-market/*` | 全局仪表盘 | data\_providers |
| `/api/quick-trade/*` | 一键下单 | live\_trading |
| `/api/portfolio/*` | 持仓监控 | data\_sources（并发 ThreadPoolExecutor 3–6 worker） |
| `/api/ibkr/*` | IBKR 专用 | ibkr\_trading |
| `/api/mt5/*` | MT5 专用 | mt5\_trading |

## 主要陷阱

- **CCXT `fetch_ohlcv` 的最后一根可能是未收盘 candle**。回测/信号去重要用收盘时间作 key，否则会在尾 candle 上反复触发。
- **熔断器静默生效**：被熔断的源不抛异常，路由可能拿到空数据。前端"暂无数据"问题先看 `circuit_breaker` 状态。
- **不同交易所现货 vs 期货符号差别大**（Binance: 下划线 vs 无分隔；OKX: `BTC-USDT` vs `BTC-USDT-SWAP`），用错前缀直接 "symbol not found"。
- **限流器是单例**：跨实例部署时不共享状态，多副本下整体 QPS 会成倍增长。
- **国内数据源走代理**会被东方财富/腾讯频繁屏蔽。检查 `NO_PROXY`。
- **MT5 仅 Windows**：CI / Linux 容器里 import 失败被静默捕获，看不到错误，要在 health 端点单独探测。
- **Quick-Trade USDT-计价**：用户填 `$100` 时后端会用 `last` 价反算 `qty = 100 / price`，**用的是缓存的 ticker**，价格抖动大时 qty 会偏。
- **凭据日志泄露**：直接 `logger.info(config)` 会泄漏 API key。一律先 `safe_exchange_config_for_log(config)`。
