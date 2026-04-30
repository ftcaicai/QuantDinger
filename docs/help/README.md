# QuantDinger 代码导读 / Help

本目录是面向开发者（包括 AI 助手）的内部分析文档，目的是让任何人**不必通读 ~2 万行后端代码**就能快速上手二次开发、定位问题、添加适配器或扩展业务模块。

> 这些文档基于对 `backend_api_python/` 全量源码的分析整理，关注**机制、契约、生命周期与陷阱**，而不是 API 列表的逐字翻译。命名规范、文件位置等可直接读源码看到的内容刻意省略。

## 阅读顺序

| # | 文档 | 用途 |
|---|------|------|
| 0 | [README.md](README.md)（本页） | 索引与项目格局 |
| 1 | [01-architecture-overview.md](01-architecture-overview.md) | 整体架构、进程模型、启动钩子、三层切分（routes / services / data\_*） |
| 2 | [02-strategy-runtime.md](02-strategy-runtime.md) | 策略运行时：IndicatorStrategy vs ScriptStrategy、TradingExecutor 线程模型、信号→挂单→成交的桥接 |
| 3 | [03-data-and-brokers.md](03-data-and-brokers.md) | 行情数据层（data\_sources / data\_providers）、CCXT/IBKR/MT5 执行层、缓存/熔断/限流、符号归一化 |
| 4 | [04-ai-llm-layer.md](04-ai-llm-layer.md) | LLM Provider 抽象、FastAnalysis、AnalysisMemory、Calibration/Reflection 闭环、Polymarket、Experiment Pipeline |
| 5 | [05-auth-billing-notifications.md](05-auth-billing-notifications.md) | JWT/角色、OAuth、凭据加密、积分与会员、USDT TRC20 支付、通知通道 |
| 6 | [06-routes-reference.md](06-routes-reference.md) | 全部 Blueprint 端点速查 |
| 7 | [07-database-schema.md](07-database-schema.md) | 33 张 `qd_*` 表的职责索引（不是字段字典） |
| 8 | [08-deployment-and-config.md](08-deployment-and-config.md) | Docker Compose 拓扑、关键 env、生产部署清单、常见故障 |

## 项目格局速记

- **后端**（`backend_api_python/`）— Apache 2.0，Flask + gunicorn + PostgreSQL 16 + Redis 7。**所有后台 worker 与 web 进程跑在同一个 Python 进程里**（通过 `create_app()` 启动钩子拉起），不是独立 service。
- **前端**（`frontend/dist/`）— **仅预构建产物**。Vue 源码在另一个私有仓库 [QuantDinger-Vue](https://github.com/brokermr810/QuantDinger-Vue)。本仓库只能改 `nginx.conf` 与重新同步 `dist/`。
- **`docker-compose.yml`** 起 4 个容器：`frontend`（nginx）、`backend`（Flask）、`postgres`、`redis`。
- **入口**：开发用 `python run.py`（Flask dev server），生产用 `gunicorn -c gunicorn_config.py "run:app"`（容器 `docker-entrypoint.sh` 触发）。

## 写文档时的原则

- 不复述源码就能看到的内容（目录树、函数名列表、字段表）。
- 重点写：**为什么**这样设计、**线程/进程/生命周期**、**外部副作用**、**容易踩的坑**。
- 路径都用相对仓库根的格式，便于 IDE / Markdown 直接跳转。
- 中文为主，专有名词、env 名、表名保留英文原样。
