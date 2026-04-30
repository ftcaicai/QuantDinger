# 01 · 架构总览

## 进程模型

QuantDinger 是 **monolith Flask 应用 + 同进程后台线程**。除了 PostgreSQL 与 Redis 这两个独立服务，所有业务（API 路由、策略运行时、订单调度、AI 校准、Polymarket worker、USDT 链上对账等）都在**同一个 Python 进程**里以**线程**形式运行。这意味着：

- **横向扩展（多实例）会重复跑 worker**。当前没有分布式锁，多副本部署会出现重复发单、重复扣费、OAuth state 冲突（state 已落库可缓解）。
- gunicorn 默认配置是 `GUNICORN_WORKERS=1` + `GUNICORN_THREADS=8`（见 [docker-compose.yml](../../docker-compose.yml)）。**保持 workers=1**，否则 `_trading_executor` 这种模块级单例会被复制 N 份。
- 启动钩子集中在 [backend_api_python/app/\_\_init\_\_.py](../../backend_api_python/app/__init__.py) 的 `create_app()` 末尾，按顺序拉起：
  1. `start_pending_order_worker()` — 默认开 (`ENABLE_PENDING_ORDER_WORKER=true`)
  2. `start_portfolio_monitor()` — 默认开 (`ENABLE_PORTFOLIO_MONITOR=true`)
  3. `start_usdt_order_worker()` — 仅当 `USDT_PAY_ENABLED=true`
  4. `start_polymarket_worker()` — 默认开
  5. `start_ai_calibration_worker()` — best-effort
  6. `start_reflection_worker()` — best-effort，~24h 周期
  7. `restore_running_strategies()` — 把 DB 里 `status='running'` 的 `IndicatorStrategy` 重新挂回 `TradingExecutor`（`ScriptStrategy` 不会自动恢复，会被显式跳过）

> 这里的 best-effort 表示 import 或启动失败只 `logger.error` 不抛异常。

## `run.py` 做的隐式事情

[run.py](../../backend_api_python/run.py) 在 `create_app()` 之前还做了几件容易被忽视的事：

1. **强制 UTF-8 stdout/stderr**（Windows PowerShell 默认 GBK，否则中文日志会 `UnicodeEncodeError`）。
2. **加载 `.env`**：先读 `backend_api_python/.env`，再读仓库根 `.env`，**`override=False`**，即环境变量优先于文件（容器内 `docker-compose` 注入的环境变量会覆盖文件值）。
3. **统一代理**：若设置了 `PROXY_URL`，会把它写入 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。**同时把一组中国境内财经域名写进 `NO_PROXY`**（东方财富、新浪、10jqka、深交所、AKShare、Baostock 等），避免境内数据源走海外代理。新增国内数据源时，如果它有自己的域名，要把域名加进 `_CN_FINANCIAL_DOMAINS` 列表，否则会被代理拖慢/阻断。
4. `TQDM_DISABLE=1` — AKShare 等库会输出进度条污染日志，全局禁用。
5. **SECRET_KEY 兜底**：若 `DEBUG=False` 且 SECRET\_KEY 仍是默认值 `quantdinger-secret-key-change-me`，自动生成一次性密钥并打 WARN（生产部署 docker-compose 层会直接拒绝启动）。

## JSON 安全边界：`SafeJSONProvider`

Flask 的默认 JSON 编码器会输出字面 `NaN` / `Infinity`（不符合 RFC 8259），导致前端 `JSON.parse` 抛错。[app/\_\_init\_\_.py](../../backend_api_python/app/__init__.py) 注册了 `SafeJSONProvider`，会**递归地**把 `float('nan')` / `inf` 替换为 `None`。

**约束**：路由里**不要直接 `json.dumps(...)`**，而是 `return jsonify(...)` 或返回原生 dict / list 让 Flask 自己序列化，否则绕过了消毒。曾经的 heatmap "暂无数据" bug 就是因为 yfinance NaN 直接进 `json.dumps`。

## 三层切分

```
backend_api_python/app/
├── routes/         # Flask Blueprints（HTTP 边界）
├── services/       # 业务逻辑（线程/IO/DB）
│   ├── live_trading/   # 交易所执行适配器（写）
│   ├── ibkr_trading/   # IBKR 客户端
│   ├── mt5_trading/    # MT5 客户端
│   └── experiment/     # 策略演化/打分/regime
├── data_sources/   # 行情读适配器（按市场切分）
├── data_providers/ # 仪表盘高层取数（多 source 合并、新闻、情绪、热力图）
├── config/         # settings / api_keys / database / data_sources 配置
└── utils/          # 公共：auth/db/cache/logger/safe_exec…
```

### 路由层（routes/）
- 一个文件 = 一个业务域。22 个 Blueprint 在 [routes/\_\_init\_\_.py::register_routes()](../../backend_api_python/app/routes/__init__.py) 集中注册。
- 大多数前缀是 `/api/<domain>`；`indicator_bp` 同时挂 `/api/indicator`，与 `kline_bp`、`backtest_bp` 共享前缀（合并使用）。
- 健康检查 `health_bp` 不带前缀，直接 `/api/health`。

### 服务层（services/）
- 三个 **factory pattern** 入口：
  - `data_sources/factory.py::DataSourceFactory` — 按 `market` 字段（Crypto/USStock/CNStock/HKStock/Forex/Futures）派发读适配器。
  - `services/live_trading/factory.py` — 按 `exchange_id` 派发 CCXT 风格的执行客户端。
  - `services/mt5_trading/__init__.py` 与 `services/ibkr_trading/__init__.py` — 桌面端 broker，**不走** live_trading factory，独立路径。
- 几个**模块级单例**（[app/\_\_init\_\_.py](../../backend_api_python/app/__init__.py)）：
  - `_trading_executor`（`get_trading_executor()`）
  - `_pending_order_worker`（`get_pending_order_worker()`）
  - 还有 polymarket worker、usdt worker、calibration worker 各自的 `get_xxx()`
  - 任何会启动后台线程的代码**都必须**走 getter，否则会出现重复线程。

### 数据层（data_sources/ vs data_providers/）
两者职责不同，不要混用：
- **data_sources/** — 原子级行情读取（K 线、ticker），单 symbol、单 timeframe，5 分钟 TTL 缓存。
- **data_providers/** — 仪表盘聚合（热力图、指数、新闻、情绪、机会），多 symbol，独立 2–5 分钟 TTL。`MarketDataCollector` 与 global market 路由是其主要消费方。

### 工具层（utils/）
- [utils/db.py](../../backend_api_python/app/utils/db.py) 仅是对 `db_postgres.py` 的 re-export；**永远是 PostgreSQL**，`get_db_type()` 直接 hard-code 返回 `'postgresql'`。SQL 占位符是 `%s` 不是 `?`。
- [utils/auth.py](../../backend_api_python/app/utils/auth.py) — JWT 装饰器、角色检查、`g.user_id` 注入。
- [utils/safe_exec.py](../../backend_api_python/app/utils/safe_exec.py) — 用户 Python 代码沙箱（AST 黑名单 + 白名单 builtins + 可选超时/内存限制）。
- [utils/credential_crypto.py](../../backend_api_python/app/utils/credential_crypto.py) — 交易所 API key 的 Fernet 加解密（密钥派生自 `SECRET_KEY`）。
- [utils/cache.py](../../backend_api_python/app/utils/cache.py) — Redis 优先、内存 fallback；`CACHE_ENABLED` 切换。
- [utils/local_brokers.py](../../backend_api_python/app/utils/local_brokers.py) — 检查 `ALLOW_LOCAL_DESKTOP_BROKERS` 闸门，IBKR/MT5 路由必须先过这个检查。

## 数据库连接池

PostgreSQL 连接池由 [utils/db_postgres.py](../../backend_api_python/app/utils/db_postgres.py) 管理。关键 env：

| env | 默认 | 说明 |
|------|-----|------|
| `DB_POOL_MIN` | 5 | 池最小连接 |
| `DB_POOL_MAX` | 50 | 池最大连接 |
| `DB_POOL_ACQUIRE_TIMEOUT` | 10 | 获取超时（秒） |
| `DB_POOL_HEALTH_CHECK` | true | 取出时 ping |

`docker-compose.yml` 已为 PG 容器配置 `max_connections=150`，给 50 池 + 管理员/psql 留余量。**多 worker / 多策略并发**时常见报错 `connection pool exhausted` → 调高 `DB_POOL_MAX` 同时调高 PG 的 `PG_MAX_CONNECTIONS`。

使用模式（context manager）：

```python
from app.utils.db import get_db_connection
with get_db_connection() as db:
    cur = db.cursor()
    cur.execute("SELECT id FROM qd_users WHERE username = %s", (name,))
    row = cur.fetchone()    # row 是 RealDictRow，dict-like
    db.commit()
```

## 启动顺序与失败模式

```
container 启动
  ↓ docker-entrypoint.sh / start.sh
gunicorn worker fork
  ↓ run.py 顶部：UTF-8、.env、proxy、TQDM_DISABLE
create_app()
  ↓ Flask app + SafeJSONProvider + CORS + logger
  ↓ init_database() + ensure_admin_exists()      ← 这里失败只 WARN，不阻塞
  ↓ register_routes()
  ↓ start_*_worker() × 5    ← 任一失败只 WARN
  ↓ restore_running_strategies()    ← 把 IndicatorStrategy 重连
  ↓ gunicorn 开始接请求
```

**陷阱**：很多 worker 启动是 best-effort（catch all Exception），意味着 worker 没起来时 web 请求仍会接受，但相关功能静默失效。排障时优先 `docker-compose logs -f backend | grep -i "Failed to start"`。

## 与前端的契约

- 前端是预构建 SPA，所有交互都是 JSON over HTTP（路径以 `/api/` 开头，由 nginx 转发到 `backend:5000`）。
- WebSocket 当前**未使用**（即使是策略实时日志、行情流也是轮询）。
- 前端 i18n 在私有 Vue 仓里，[scripts/i18n-*.js](../../scripts/) 是用 LLM 自动补齐 locale 文件的工具，**只对私有 Vue 仓生效**，与本仓库 `frontend/dist` 无关。
