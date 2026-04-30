# 07 · 数据库表索引

PostgreSQL 16，schema 定义在 [backend\_api\_python/migrations/init.sql](../../backend_api_python/migrations/init.sql)（容器启动时通过 `docker-entrypoint-initdb.d` 自动执行）。共 **33 张表**，全部以 `qd_` 前缀（除一个历史遗留的 `pending_orders`）。

> 本页**只列职责**，字段不重复 init.sql 内容。改 schema 时直接读 SQL；增表时同时更新本表索引。

## 用户 / 鉴权

| 表 | 行 | 职责 |
|----|----|------|
| `qd_users` | 8 | 用户主表：username、email、password\_hash、role、credits（Decimal）、vip\_level、vip\_expires\_at、token\_version、notification\_settings JSONB |
| `qd_oauth_links` | 155 | user ↔ OAuth provider 绑定（Google/GitHub） |
| `qd_oauth_states` | 104 | 一次性 CSRF state（20 分钟 TTL） |
| `qd_verification_codes` | 117 | 邮件验证码（注册/登录/改密，10 分钟 TTL） |
| `qd_login_attempts` | 138 | 爆破跟踪（IP + account 维度，7 天保留） |
| `qd_security_logs` | 177 | 审计：登录、注册、改密、OAuth、API key 操作 |

## 计费 / 商城

| 表 | 职责 |
|----|------|
| `qd_credits_log` | **不可变**积分流水（consume / add / refund / membership / admin / register\_bonus） |
| `qd_membership_orders` | 旧版 mock 支付（前端模拟结账，非链上） |
| `qd_usdt_orders` | USDT TRC20 链上订单：address (HD 派生)、amount、status、tx\_hash、expiry、address\_index |
| `qd_indicator_purchases` | 社区指标购买记录 |
| `qd_indicator_comments` | 社区指标评论 |
| `qd_quick_trades` | quick-trade 历史（与 strategy\_trades 区分，纯手工下单） |

## 策略运行时

| 表 | 职责 |
|----|------|
| `qd_strategies_trading` | **策略主表**：market、symbol、timeframe、status、initial\_capital、exchange\_config、indicator\_config、trading\_config、notification\_config、strategy\_type、code、bot\_runtime\_stats、script\_runtime\_state、last\_signal\_time、group\_id |
| `qd_strategy_positions` | 实时仓位：side、size、entry\_price、unrealized\_pnl、**highest\_price** / **lowest\_price**（trailing stop 关键） |
| `qd_strategy_trades` | 已成交回执（每条对应 pending\_orders 的 fill） |
| `pending_orders` | 信号 → 成交的桥接表（status: pending / filled / rejected / cancelled），**无 qd\_ 前缀** |
| `qd_strategy_notifications` | 策略级浏览器通知（in-app 红点） |
| `qd_strategy_logs` | 策略运行日志（fire-and-forget 写入） |
| `qd_indicator_codes` | 指标代码库（用户自建 + 内置 + 社区） |

## 回测

| 表 | 职责 |
|----|------|
| `qd_backtest_runs` | 一次回测：run\_type (indicator/script)、strategy\_id、strategy\_name、config\_snapshot、engine\_version、code\_hash |
| `qd_backtest_trades` | 回测中每笔模拟成交 |
| `qd_backtest_equity_points` | 资金曲线点 |

## 行情 / 自选 / 监控

| 表 | 职责 |
|----|------|
| `qd_watchlist` | 用户自选股 |
| `qd_market_symbols` | 全市场 symbol 字典（名称、行业、市值，加速搜索） |
| `qd_manual_positions` | 手工录入持仓（不自动下单） |
| `qd_position_alerts` | 价格告警 |
| `qd_position_monitors` | 多策略组合监控规则 |

## AI

| 表 | 职责 |
|----|------|
| `qd_analysis_memory` | FastAnalysis 决策历史 + 校验结果（task\_status、validated\_at、actual\_return\_pct、was\_correct、raw\_result JSONB） |
| `qd_analysis_tasks` | 长任务跟踪（与 analysis\_memory 配合） |

> Calibration 写入的表名实际是 `qd_ai_calibration`（在 ai\_calibration 服务里 ensure），但 init.sql 里没列出（运行时按需建表）。如有迁移工具需求，留意这一点。

## 凭据

| 表 | 职责 |
|----|------|
| `qd_exchange_credentials` | Fernet 加密的交易所 API key（exchange\_id + name + encrypted\_config） |

## Polymarket

| 表 | 职责 |
|----|------|
| `qd_polymarket_markets` | 市场快照（缓存，30 分钟） |
| `qd_polymarket_ai_analysis` | 单市场 AI 分析结果 |
| `qd_polymarket_asset_opportunities` | Worker 批量打分后的 top 机会 |

## 命名约定 & 实践

- **大多数 PK 是 `BIGSERIAL id`**，少数业务表用 `UUID`。
- **JSONB**：`exchange_config` / `indicator_config` / `notification_config` / `raw_result` / `bot_runtime_stats` 等灵活字段一律 JSONB；建索引时用 `->>` 表达式索引。
- **时区**：所有时间戳列是 `TIMESTAMP WITH TIME ZONE` 或 epoch seconds（`BIGINT`）。**K 线和信号时间用 epoch 秒**，业务事件时间用 `TIMESTAMPTZ`。注意混用风险。
- **Decimal**：金额字段一律 `NUMERIC(20, 8)`（加密资产精度）或 `NUMERIC(20, 2)`（积分），不要用 `FLOAT`。
- **状态枚举**：用 `VARCHAR` + 应用层校验，不用 PG enum 类型（迁移友好）。
- **建表语句**全部用 `CREATE TABLE IF NOT EXISTS`，**没有 down migration**。schema 演进靠 ALTER + 应用启动时 best-effort 自检 (`init_database` / `ensure_storage_schema`)。

## 改 schema 的工作流

1. 改 [backend\_api\_python/migrations/init.sql](../../backend_api_python/migrations/init.sql)：仅追加，不修改既有列。
2. 老库需要的字段：在对应 service 的 `ensure_*_schema()` 函数里加 `ALTER TABLE IF NOT EXISTS ... ADD COLUMN IF NOT EXISTS ...`。
3. 测试用 fresh PG（清掉 docker volume `quantdinger_postgres_data`）验证 init 路径。
4. 生产升级**先备份再升级**——没有自动迁移工具。

## 表关系总览

```
qd_users ──┬── qd_credits_log
           ├── qd_usdt_orders
           ├── qd_oauth_links ── qd_oauth_states
           ├── qd_strategies_trading ──┬── qd_strategy_positions
           │                            ├── qd_strategy_trades
           │                            ├── pending_orders
           │                            ├── qd_strategy_notifications
           │                            └── qd_strategy_logs
           ├── qd_backtest_runs ──┬── qd_backtest_trades
           │                       └── qd_backtest_equity_points
           ├── qd_indicator_codes ── qd_indicator_purchases
           ├── qd_exchange_credentials
           ├── qd_watchlist
           ├── qd_manual_positions ──┬── qd_position_alerts
           │                          └── qd_position_monitors
           ├── qd_analysis_memory ── qd_analysis_tasks
           └── qd_quick_trades
```

`qd_polymarket_*` 是市场维度的全局表，与 user 无强 FK。
