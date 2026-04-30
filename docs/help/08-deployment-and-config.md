# 08 · 部署与配置

## 容器拓扑

```
┌──────────────────────────────────────────────┐
│  quantdinger-network (docker bridge)         │
│                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │ frontend │──▶│ backend  │──▶│ postgres │  │
│  │  nginx   │   │ flask+gu │   │   16     │  │
│  │  :8888   │   │  :5000   │   │  :5432   │  │
│  └──────────┘   └────┬─────┘   └──────────┘  │
│                       │                      │
│                       └────────▶┌──────────┐ │
│                                 │  redis   │ │
│                                 │   7      │ │
│                                 │ :6379    │ │
│                                 └──────────┘ │
└──────────────────────────────────────────────┘
```

定义在仓库根 [docker-compose.yml](../../docker-compose.yml)。**全部容器宿主端口默认绑定 `127.0.0.1`**（除 `frontend` 是 `0.0.0.0:8888`），生产部署用 nginx/Caddy 反代到 `frontend`。

## 启动入口

| 模式 | 命令 |
|------|------|
| 完整 Docker 栈 | `docker-compose up -d --build` |
| 仅启 backend（dev） | `cd backend_api_python && python run.py` |
| 容器内 backend 真正命令 | `gunicorn -c gunicorn_config.py "run:app"`（来自 `start.sh` / `docker-entrypoint.sh`） |

`backend` 容器把宿主的 `./backend_api_python/.env` **bind-mount** 进 `/app/.env`，所以 admin 在前端"系统设置"里改 env 是直接落到宿主文件的——重启 backend 后生效。

## 关键 env 总览

完整模板：[backend\_api\_python/env.example](../../backend_api_python/env.example)。这里挑出**容易踩坑或最常调整**的子集。

### 必填（生产）

| env | 说明 |
|-----|------|
| `SECRET_KEY` | JWT + Fernet 凭据加密派生密钥。**必须 random 32 字节以上**；脚本：`./scripts/generate-secret-key.sh`。改后所有已加密凭据失效。 |
| `ADMIN_USER` / `ADMIN_PASSWORD` | 首次启动会自动建 admin |
| `DATABASE_URL` | `postgresql://user:pass@host:port/dbname`，docker-compose 已自动注入 |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | PG 容器初始化用 |

### 资源 / 并发

| env | 默认 | 说明 |
|-----|-----|------|
| `GUNICORN_WORKERS` | 1 | **保持 1**，否则模块级单例（TradingExecutor 等）会复制 |
| `GUNICORN_THREADS` | 8 | 单 worker 内的线程数，处理 HTTP |
| `DB_POOL_MIN` / `DB_POOL_MAX` | 5 / 50 | PG 连接池 |
| `PG_MAX_CONNECTIONS` | 150 | PG 容器侧上限，必须 ≥ POOL\_MAX + 余量 |
| `MARKET_EXECUTOR_WORKERS` | 6 | 市场行情并发 worker |
| `PORTFOLIO_EXECUTOR_WORKERS` | 3 | 持仓并发 |
| `STRATEGY_MAX_THREADS` | 64 | 同时运行的策略上限 |

### 后台 worker 开关

| env | 默认 | 说明 |
|-----|-----|------|
| `ENABLE_PENDING_ORDER_WORKER` | true | 关闭即 paper 模式（信号生成但不下单） |
| `ENABLE_PORTFOLIO_MONITOR` | true | 持仓监控线程 |
| `ENABLE_REFLECTION_WORKER` | true | AI 反思 + 校准 |
| `ENABLE_OFFLINE_AI_CALIBRATION` | true | 周期校准阈值 |
| `DISABLE_RESTORE_RUNNING_STRATEGIES` | false | 进程重启时不自动恢复 running 策略（低资源主机/调试用） |

### LLM

| env | 说明 |
|-----|------|
| `LLM_PROVIDER` | `openrouter` / `openai` / `gemini` / `deepseek` / `grok` / `custom` / `minimax`；空时按优先级自动检测 |
| `OPENROUTER_API_KEY` 等 | 各 provider 的 key，至少配一个 |
| `ENABLE_AI_ENSEMBLE` | env 已留位，**主流程未实现完整聚合**（见 [04-ai-llm-layer.md](04-ai-llm-layer.md)） |
| `AI_ENSEMBLE_MODELS` | 同上 |
| `AI_CALIBRATION_LOOKBACK_DAYS` | 30 |
| `AI_CALIBRATION_MIN_SAMPLES` | 80 |

### 网络 / 代理

| env | 说明 |
|-----|------|
| `PROXY_URL` | 一处配置传染全部出站请求；`run.py` 自动写 `HTTP(S)_PROXY` / `ALL_PROXY` |
| `LIVE_TRADING_CA_BUNDLE` | 自签 / 内网 CA |
| `LIVE_TRADING_SSL_VERIFY` | dev 关；生产**永远 true** |

> **国内部署**：`PROXY_URL` 设了海外代理时，`run.py` 已自动把 `.eastmoney.com` / `.sina.com.cn` / AKShare 等加进 `NO_PROXY`，新加国内数据源记得扩展 `_CN_FINANCIAL_DOMAINS` 列表（在 run.py 顶部）。

### 桌面 broker 闸门

| env | 说明 |
|-----|------|
| `ALLOW_LOCAL_DESKTOP_BROKERS` | 默认 `true`。**云 SaaS 必须设 false**——前端 IBKR/MT5 入口被禁、后端拒绝相关连接 |

### 计费

| env | 说明 |
|-----|------|
| `BILLING_ENABLED` | **false 时全部静默 bypass**，部署易踩 |
| `BILLING_COST_AI_ANALYSIS` 等 | 单 feature 价格，0 表示不扣 |
| `MEMBERSHIP_MONTHLY_PRICE_USD` 等 | 套餐价格 / 时长 / 积分 |
| `USDT_PAY_ENABLED` | 启用链上支付 worker |
| `USDT_TRC20_XPUB` | HD wallet xpub（account 或 change 级都行） |
| `TRONGRID_API_KEY` | 链上对账请求频率配额 |
| `USDT_PAY_CONFIRM_SECONDS` | 默认 30 |
| `USDT_PAY_EXPIRE_MINUTES` | 默认 30 |

### 安全 / 风控

| env | 说明 |
|-----|------|
| `ENABLE_REGISTRATION` | 关闭后只允许 admin 建账号 |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | Cloudflare Turnstile，**fail-closed** |
| `CREDITS_REGISTER_BONUS` | 注册赠送积分（含 OAuth） |

### 数据 API

| env | 说明 |
|-----|------|
| `TWELVE_DATA_API_KEY` | 外汇/商品备选数据；中国股票要付费版 |
| `FINNHUB_API_KEY` | 美股新闻 / 名称解析 |
| `TIINGO_API_KEY` | 美股备选 K 线 |
| `ADANOS_API_KEY` | Adanos Sentiment（美股情绪） |

### 缓存

| env | 默认 | 说明 |
|-----|-----|------|
| `CACHE_ENABLED` | docker 里 true | 走 Redis；本地 dev 设 false 退回内存 |
| `REDIS_HOST` / `REDIS_PORT` | redis / 6379 | docker-compose 注入 |

## 常见生产部署清单

**最小生产验证**：

- [ ] `SECRET_KEY` 已生成且**不会再改**
- [ ] `ADMIN_PASSWORD` 已改
- [ ] `BILLING_ENABLED=true`（如果开商业）
- [ ] `ALLOW_LOCAL_DESKTOP_BROKERS=false`（云 SaaS）
- [ ] LLM provider key 至少 1 个
- [ ] `TURNSTILE_*` 配齐（开公网注册时）
- [ ] PG 已挂持久化 volume，备份策略已就位
- [ ] nginx/Caddy 反代加 HTTPS（front 容器对外不能裸 HTTP）
- [ ] 监控 `/api/health` 接 alarm
- [ ] `docker-compose logs -f backend` 看启动期 worker WARN
- [ ] 设置 `TZ`（默认 `Asia/Shanghai`），影响日志时间和通知文案

## 反代示例（nginx）

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> 默认 `frontend` 容器把 `/api/*` 转到 `backend:5000`，所以反代不需要分路径。

## 常见故障排查

| 现象 | 起因 | 处理 |
|------|------|------|
| backend 容器启动后立刻退出 | `SECRET_KEY` 还是默认值 | 用 `./scripts/generate-secret-key.sh` 生成 |
| 前端 "暂无数据" / 热力图空白 | 数据源熔断、yfinance NaN | 看 `circuit_breaker` 日志；`SafeJSONProvider` 应已处理 NaN |
| `connection pool exhausted` | `DB_POOL_MAX` 不够 | 调高 `DB_POOL_MAX`，同时检查 `PG_MAX_CONNECTIONS` |
| 策略点了 start 但没动静 | `TradingExecutor` 线程超过 `STRATEGY_MAX_THREADS` | 看后端日志 `RuntimeError: can't start new thread` |
| AI 分析永远 processing | `task_status` 没被改回 | 看 worker 是否崩；可手动 `UPDATE qd_analysis_memory SET task_status='failed' WHERE id=...` |
| 通知不到 | SMTP/webhook 配置缺 | 看 `qd_strategy_notifications` 表；或用 `/api/users/notification-settings/test` |
| OAuth 报 "Invalid state" | 多副本未共享 state | state 已落库，正常应通；检查时间是否过 20 分钟 |
| MT5 无法连接 | 非 Windows / MT5 终端未启动 | 仅 Windows，且 `ALLOW_LOCAL_DESKTOP_BROKERS=true`，且 MT5 终端登录中 |
| IBKR 间歇断开 | TWS 闲置超 15 分钟 | 上层 retry 或调 TWS 配置 |
| Twelve Data "apikey is incorrect" | key 过期或免费额度 | 中国 stock 要付费 |
| Redis refused | redis 容器没起 | `docker-compose up -d redis`；或 `CACHE_ENABLED=false` 退回内存 |
| 改了 .env 不生效 | env 优先级 = OS env > .env 文件 | docker-compose 直接注入的 env 会覆盖文件，要在 compose 里改或 unset |

## 备份建议

- **PostgreSQL volume** (`postgres_data`)：定时 `pg_dump` 到对象存储。`qd_credits_log`、`qd_usdt_orders`、`qd_strategies_trading` 是核心。
- **`backend_api_python/.env`**：里面有 `SECRET_KEY` + USDT xpub + 第三方 key，**单独备份并加密**（丢失 SECRET\_KEY 会让所有交易所凭据永久作废）。
- **`backend_logs` / `backend_data` volume**：低优先级，丢了不影响业务，但调查问题需要。

## 升级 schema

仓库**没有显式 migration 工具**。流程：

1. 改 [migrations/init.sql](../../backend_api_python/migrations/init.sql)，新表 / 新列用 `IF NOT EXISTS`。
2. 老部署：相关 service 的 `ensure_*_schema()` / `init_database()` 中加 `ALTER TABLE IF NOT EXISTS ... ADD COLUMN IF NOT EXISTS ...`。
3. **备份 → 重启 backend → 看启动日志**确认无报错。

> 不要直接用 ORM migrations 工具（如 alembic）覆盖现有 schema——本仓库没用 ORM，全是裸 SQL。
