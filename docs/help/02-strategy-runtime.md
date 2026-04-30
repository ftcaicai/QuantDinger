# 02 · 策略运行时

策略子系统是这套代码最复杂、文件最多、也最容易踩坑的部分。本文按"**一行 `qd_strategies_trading` 的生命周期**"组织，先讲数据流，再讲两种策略类型的差异，最后列陷阱。

## 全景图

```
                 ┌──────────────┐    ┌──────────────────┐
   /strategies/* │ StrategyService │ ←→│ qd_strategies_trading │
   HTTP routes   └──────┬───────┘    └──────────────────┘
                        │ status='running'
                        ▼
                ┌──────────────────┐ 周期轮询    ┌──────────────────┐
                │ TradingExecutor  │←──────────│ singleton getter │
                │  (后台线程池)    │            └──────────────────┘
                └──────┬───────────┘
        per-strategy daemon thread
                        │
        ┌───────────────┼─────────────────────┐
        │               │                     │
        ▼               ▼                     ▼
  KlineService    StrategyCompiler   StrategyScriptRuntime
  (行情读)        (IndicatorStrategy)  (ScriptStrategy)
        │               │                     │
        └───── 信号去重（process-local）──────┘
                        │
                        ▼
              INSERT pending_orders          ────────► SignalNotifier
                        │                                  │
                        ▼                                  ▼
             PendingOrderWorker (另一线程)      email/webhook/discord/
                        │                       telegram/sms/browser
                        ▼
        live_trading.factory → exchange client
                        │
                        ▼
        交易所成交 → qd_strategy_trades
                       qd_strategy_positions
```

## 涉及的核心模块

| 模块 | 行数 | 职责 |
|------|------|------|
| [services/strategy.py](../../backend_api_python/app/services/strategy.py) | ~1.4k | DB-facing：CRUD、状态机、运行时指标聚合 |
| [services/trading_executor.py](../../backend_api_python/app/services/trading_executor.py) | ~3.8k | 线程池 + 主循环 + 信号生成 |
| [services/strategy_compiler.py](../../backend_api_python/app/services/strategy_compiler.py) | ~700 | IndicatorStrategy JSON → Python |
| [services/strategy_script_runtime.py](../../backend_api_python/app/services/strategy_script_runtime.py) | ~190 | ScriptStrategy `on_init` / `on_bar` 上下文 |
| [services/strategy_snapshot.py](../../backend_api_python/app/services/strategy_snapshot.py) | — | 把策略行打包成回测可消费的 snapshot |
| [services/backtest.py](../../backend_api_python/app/services/backtest.py) | ~5k | 回测引擎（Indicator/Script 共用） |
| [services/pending_order_worker.py](../../backend_api_python/app/services/pending_order_worker.py) | ~2.4k | 把 pending\_orders 推到交易所 |
| [services/signal_notifier.py](../../backend_api_python/app/services/signal_notifier.py) | — | 多通道通知分发 |
| [services/exchange_execution.py](../../backend_api_python/app/services/exchange_execution.py) | — | 凭据合并、敏感字段脱敏 |
| [utils/safe_exec.py](../../backend_api_python/app/utils/safe_exec.py) | — | 用户代码沙箱 |
| [utils/strategy_runtime_logs.py](../../backend_api_python/app/utils/strategy_runtime_logs.py) | — | 策略运行日志 fire-and-forget 写库 |

## 状态机（极简）

`qd_strategies_trading.status` 只有两个状态：`stopped` ↔ `running`。**没有 paused / starting / stopping 中间态**。

- `start_strategy(id)` → `update_strategy_status(id, 'running')` → `TradingExecutor` 在下一轮轮询（5–10 秒）spawn 线程。
- `stop_strategy(id)` → `update_strategy_status(id, 'stopped')` → `TradingExecutor` 检测到状态变化即退出循环；**不等已挂出的 pending\_orders 处理完**，可能留下未成交的挂单。
- 进程重启时 `restore_running_strategies()` 把所有 `running` 行重新挂上线，但**只对 `IndicatorStrategy` 生效**，`ScriptStrategy` 行被显式跳过（运行时状态丢失，作者认为 script 类无法安全恢复）。失败的恢复会反向把 status 改回 `stopped`，避免僵尸。

## 单条策略运行循环（per-thread）

`TradingExecutor` 给每个 `running` 策略起一个 daemon 线程，循环里大致：

1. 调 `KlineService.get_klines(symbol, timeframe, limit)` 拉 K 线。底层走 `DataSourceFactory`，可能是 CCXT 或 REST（详见 [03-data-and-brokers.md](03-data-and-brokers.md)）。K 线缓存 5–30 分钟。
2. 根据 `strategy_type` 走两条路径之一：
   - **IndicatorStrategy**：`StrategyCompiler` 把 JSON entry\_rules 编译成 Python（指标计算 + 布尔条件 + 仓位状态机），直接 `exec` 在 pandas/numpy 命名空间里，最终读 `df['open_long']` / `df['add_long']` / `df['close_long']` 等列。
   - **ScriptStrategy**：`compile_strategy_script_handlers(code)` 通过 `safe_exec` 拿到 `on_init` / `on_bar` 两个 callable；构造 `StrategyScriptContext`（`ctx.bars_df`、`ctx.params`、`ctx.position`、`ctx.balance`、`ctx._orders`），按 bar 调 `on_bar(ctx, bar)`，回收 `ctx._orders`。
3. **进程内信号去重**：`(strategy_id, symbol, signal_type, candle_timestamp)` 作为 key，已发过的同 candle 信号丢弃。
4. INSERT 到 `pending_orders` 表（status='pending'）。
5. `SignalNotifier.notify_signal(...)` 多通道并行（详见 [05](05-auth-billing-notifications.md#通知)）。
6. `append_strategy_log()` 写 `qd_strategy_logs`（best-effort，失败不抛）。

> ⚠ 信号去重是**进程级 in-memory**，不是 DB 锁。多副本部署会重复下单。

## 仓位状态：`qd_strategy_positions`

- `TradingExecutor` 每轮算出 `unrealized_pnl`（按当前价 + entry\_price），写回 `qd_strategy_positions`。
- 同时维护 `highest_price`（多头）/`lowest_price`（空头），用于 trailing stop。**这两个字段一旦数据丢失/被人改了，移动止损会失效。**
- 已实现盈亏来自 `qd_strategy_trades`，由 `PendingOrderWorker` 在订单成交回执到达时累加。

## TradingExecutor → live\_trading 的桥接

注意这里的设计：`TradingExecutor` **只负责生成信号 + 落 pending\_orders**，不直接调交易所 API。**真正的下单**由独立的 `PendingOrderWorker` 完成：

- 轮询 `pending_orders` 表 status='pending' 的行；
- 通过 `services/exchange_execution.resolve_exchange_config()` 合并 `qd_exchange_credentials`（用 Fernet 解密 API key）和策略级覆盖；
- 调 `live_trading.factory.create_client(exchange_id, config)` 拿到 CCXT 风格客户端（详见 [03](03-data-and-brokers.md#live-trading)）；
- 执行限价/市价单，更新 `pending_orders.status` 为 `filled` / `rejected` / `cancelled`，把成交细节插入 `qd_strategy_trades`。

**意义**：
1. 信号与执行解耦 → 失败可重试，paper 模式直接关 `PendingOrderWorker` 即可。
2. 下单失败不会卡住信号生成线程。
3. 改造成多进程消费者也容易（DB 行级锁即可）。

## 策略代码沙箱：`utils/safe_exec.py`

用户写的策略 / 指标代码会被注入两层防护：

1. **`validate_code_safety()`**：AST + 正则黑名单。禁止：`os` / `subprocess` / `__import__` / `eval` / `exec` / `getattr` / `open` / `pickle` / `socket` / `http` / `ctypes` / `threading` / `asyncio` / `signal` / `resource` / `importlib`。
2. **`safe_exec_code()`**：白名单 builtins（数学、迭代、类型转换），允许 `import` 的模块只有 `numpy`, `pandas`, `math`, `json`, `datetime`, `time`, `collections`, `functools`, `itertools`, `statistics`, `decimal`, `fractions`, `operator`, `copy`。Unix 上用 `SIGALRM` 设超时；Windows 用 `threading.Timer`（注意 Windows 上**超时只是软限制**）。
3. 还有 `safe_exec_isolated()`（`multiprocessing` 子进程隔离），目前主要在回测路径上用。

**陷阱**：用户尝试通过 `__import__("os")`、`type(...).__bases__[0].__subclasses__()` 等姿势绕沙箱时会被 AST 阶段拒掉，但**不要**绕过 `validate_code_safety` 直接调 `exec` —— 这是审核重点。

## IndicatorStrategy vs ScriptStrategy 一览

| 维度 | IndicatorStrategy | ScriptStrategy |
|------|-------------------|----------------|
| 输入 | UI 选择的 entry\_rules JSON | 用户写的 Python：`on_init(ctx)` / `on_bar(ctx, bar)` |
| 引擎 | `StrategyCompiler` 生成代码后跑在 pandas DataFrame 上 | 事件驱动，per-bar 调用 `on_bar` |
| 信号产物 | `df['open_long']` / `df['close_long']` 等布尔列 | `ctx._orders` 里追加的 dict |
| 启动恢复 | `restore_running_strategies` 自动挂回 | **不会自动恢复**（运行时状态丢失） |
| 回测 | 与实盘共用 `StrategyCompiler` | 共用 `StrategyScriptRuntime` |
| 跨标的 (cross-sectional) | 解析支持，回测尚未实现（warn） | 当前未支持 |
| 时机 | `same_bar_close` / `next_bar_open` 可配 | 用户自行控制 |

## 回测：`services/backtest.py`

~5k 行的核心引擎（不要轻易 dump 全文）：

- `BacktestService` 是入口；`_KlineCache` 是模块内 K 线缓存，TTL 5–30 分钟。
- timeframe 拉取窗口硬编码：1m → 15 天，5m → 1 年，15m/30m → 1 年，1H 及以上 → 3 年。
- `MTF_CONFIG` 控制多周期回退（数据稀疏时 1m → 5m）。
- 落库：`qd_backtest_runs`（含 `engine_version`、`code_hash`，便于复现）+ `qd_backtest_trades` + `qd_backtest_equity_points`。
- 路由 `/api/indicator/backtest` → `services/strategy_snapshot.StrategySnapshotResolver` → `BacktestService.run_backtest_*`，**走的是 strategy snapshot 而不是直接读 strategies 行**，目的是回测可重放。

## 主要陷阱

- **状态机简陋**：只有 stopped/running，停止是硬中止，已挂出的 pending\_orders 不被取消。
- **进程级信号去重**：多副本部署会重复发单。多实例环境需要自己加一层（DB 唯一约束已在 schema 里准备好）。
- **`restore_running_strategies` 只救 IndicatorStrategy**：ScriptStrategy 进程重启后必须用户手动 start。
- **MT5/IBKR 策略只能在带桌面终端的机器上跑**：cloud SaaS 部署设 `ALLOW_LOCAL_DESKTOP_BROKERS=false`，相关连接测试会直接 403。
- **Trailing stop 依赖 `highest_price` / `lowest_price`**：这俩字段写在 `qd_strategy_positions`，被人手改或丢失则止损失效。
- **`STRATEGY_MAX_THREADS` 默认 64**：超过会 `RuntimeError: can't start new thread`，**只在日志里**，HTTP 接口仍可能返回 ok（启动是异步的）。
- **`SignalNotifier` 静默退化**：缺 SMTP\_HOST 不会让 `notify_signal` 失败，只是该通道返回 `{ok: false}`。要确认通知到达，看 `qd_strategy_notifications` 表，不要看 HTTP 响应。
- **回测 K 线缓存 TTL 较长**：连续调参容易吃到旧数据。要么重启容器要么改时间窗口让 cache key 变。
- **跨周期同 candle 重发**：当 timeframe 缩短到 1m 而 K 线源还在生成尾 candle 时，最后一根可能反复触发信号。`signal_dedup` 会拦，但要确认 `candle_timestamp` 的取值是收盘时间。
- **`exchange_execution.safe_exchange_config_for_log()`**：所有打日志的地方都要先过这个函数脱敏 API key。新加 broker 时记得扩展 mask 列表。
