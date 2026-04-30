# 06 · 路由速查

全部 Blueprint 端点（来自 [routes/](../../backend_api_python/app/routes/) 实际 grep）。前缀来自 [routes/\_\_init\_\_.py](../../backend_api_python/app/routes/__init__.py)。

> 默认所有非 `auth` / `health` 路由都需要 JWT。带 ⚙ 的需 `manager` 或更高，带 ★ 的需 `admin`。

## health (`/`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/` | root |
| GET | `/health` | 简易 ping |
| GET | `/api/health` | 完整健康检查（用于 Docker healthcheck） |

## auth (`/api/auth`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/security-config` | 前端读 Turnstile/OAuth 配置 |
| POST | `/login` | 密码登录 |
| POST | `/login-code` | 邮箱验证码登录 |
| POST | `/send-code` | 发邮箱验证码（注册/登录/改密复用） |
| POST | `/register` | 注册 |
| POST | `/reset-password` | 重置密码（验证码） |
| POST | `/change-password` | 改密（旧密码） |
| GET | `/oauth/google` / `/oauth/github` | 启动 OAuth |
| GET | `/oauth/google/callback` / `/oauth/github/callback` | OAuth 回调 |
| POST | `/logout` | 注销（清 token\_version） |
| GET | `/info` | 当前用户信息 |

## user (`/api/users`)

普通用户：

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/profile` | 个人资料 |
| PUT | `/profile/update` | 更新资料 |
| POST | `/change-password` | 改密 |
| GET | `/my-credits-log` | 自己的积分流水 |
| GET | `/my-referrals` | 邀请记录 |
| GET / PUT | `/notification-settings` | 通知设置 |
| POST | `/notification-settings/test` | 测试发送 |
| GET / POST / DELETE | `/chart-templates` | 图表模板 |
| GET | `/system-strategies` | 系统策略库 |

管理员（★）：

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/list` / `/export` / `/detail` | 用户管理 |
| POST | `/create` | 建用户 |
| PUT | `/update` / DELETE `/delete` | CRUD |
| POST | `/reset-password` / `/set-credits` / `/set-vip` | 后台操作 |
| GET | `/roles` / `/credits-log` / `/admin-orders` / `/admin-ai-stats` | 报表 |

## strategy (`/api`)

策略本身：

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/templates` / `/templates/<key>` | 策略模板库 |
| GET | `/strategies` / `/strategies/detail` | 列表 / 详情 |
| POST | `/strategies/create` | 创建 |
| POST | `/strategies/batch-create` | 多 symbol 批量建 |
| POST | `/strategies/batch-start` / `/batch-stop` | 批量起/停 |
| DELETE | `/strategies/batch-delete` | 批量删 |
| PUT | `/strategies/update` | 更新 |
| DELETE | `/strategies/delete` | 删 |
| POST | `/strategies/start` / `/stop` | 单条起/停 |
| GET | `/strategies/trades` / `/positions` / `/equityCurve` | 运行时数据 |
| POST | `/strategies/test-connection` | 测试交易所连接 |
| POST | `/strategies/get-symbols` | 列交易所 symbol（用于建仓 UI） |
| POST | `/strategies/preview-compile` | 预编译 IndicatorStrategy 看错误 |
| POST | `/strategies/verify-code` | 沙箱验证用户代码 |
| POST | `/strategies/ai-generate` | LLM 生成策略代码 |
| GET | `/strategies/performance` | 总览指标 |
| GET | `/strategies/logs` | 运行日志 |

回测：

| Method | Path | 用途 |
|--------|------|-----|
| POST | `/strategies/backtest` | 用 strategy 行的 snapshot 回测 |
| GET | `/strategies/backtest/history` / `/get` | 回测历史 |

策略级通知：

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/strategies/notifications` / `/unread-count` | 列表 / 未读数 |
| POST | `/strategies/notifications/read` / `/read-all` | 标读 |
| DELETE | `/strategies/notifications/clear` | 清空 |

## indicator / kline / backtest (`/api/indicator`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/getIndicators` | 列指标（个人 + 社区） |
| POST | `/saveIndicator` | 保存（新建/更新） |
| POST | `/deleteIndicator` | 删 |
| GET | `/getIndicatorParams` | 解析 `@param` 注释 |
| POST | `/verifyCode` | 沙箱验证 |
| POST | `/aiGenerate` | LLM 生成指标代码 |
| POST | `/codeQualityHints` | 代码质量提示 |
| POST | `/parseStrategyConfig` | 解析 entry\_rules |
| POST | `/callIndicator` | 试运行指标产出图表 |
| GET | `/kline` | K 线（带缓存） |
| GET | `/price` | 当前价 |
| GET | `/backtest/precision-info` | UI 显示执行 TF + K 线数 |
| POST | `/backtest` | 跑回测 |
| GET | `/backtest/history` / `/get` | 历史 / 单条 |
| POST | `/backtest/aiAnalyze` | LLM 解读回测结果 |

## market (`/api/market`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/config` / `/types` / `/menuFooterConfig` | 前端配置（可选市场、底部菜单） |
| GET | `/symbols/search` | 搜索 symbol |
| GET | `/symbols/hot` | 热门 |
| GET / POST | `/watchlist/get` `/add` `/remove` | 自选 |
| GET | `/watchlist/prices` | 自选并发取价 |
| GET | `/price` | 单 symbol 当前价 |
| POST | `/stock/name` | 解析 ticker → 公司名 |

## global-market (`/api/global-market`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/overview` / `/heatmap` / `/news` / `/calendar` / `/sentiment` / `/adanos-sentiment` / `/opportunities` | 仪表盘聚合 |
| POST | `/refresh` | 强制刷新 cache |

## quick-trade (`/api/quick-trade`)

| Method | Path | 用途 |
|--------|------|-----|
| POST | `/place-order` | 一键下单（多家交易所） |
| GET | `/balance` | 余额 |
| GET | `/position` | 当前持仓 |
| POST | `/close-position` | 平仓 |
| GET | `/history` | 交易历史 |

## portfolio (`/api/portfolio`)

| Method | Path | 用途 |
|--------|------|-----|
| GET / POST / PUT / DELETE | `/positions` | 手工持仓 CRUD |
| GET | `/summary` | 汇总 |
| GET / POST / PUT / DELETE | `/monitors` | 监控规则 |
| POST | `/monitors/<id>/run` | 手动跑一次 |
| GET / POST / PUT / DELETE | `/alerts` | 告警 |
| GET / POST | `/groups` `/groups/rename` | 分组 |

## ibkr (`/api/ibkr`) — 需 `ALLOW_LOCAL_DESKTOP_BROKERS=true`

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/status` | 是否已连接 TWS |
| POST | `/connect` / `/disconnect` | 连接生命周期 |
| GET | `/account` / `/positions` / `/orders` / `/quote` | 账户信息 |
| POST | `/order` | 下单 |
| DELETE | `/order/<id>` | 撤单 |

## mt5 (`/api/mt5`) — 仅 Windows

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/status` | 终端状态 |
| POST | `/connect` / `/disconnect` | 终端连接 |
| GET | `/account` / `/positions` / `/orders` / `/symbols` / `/quote` | 数据 |
| POST | `/order` / `/close` | 下/平 |
| DELETE | `/order/<ticket>` | 撤 |

## credentials (`/api/credentials`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/desktop-brokers-policy` | 检查桌面 broker 闸门 |
| GET | `/list` | 凭据列表（仅 mask） |
| GET | `/egress-ip` | 出口 IP（白名单设置参考） |
| POST | `/create` | 新增（Fernet 加密入库） |
| GET | `/get` | 单条详情（仍是 mask） |
| DELETE | `/delete` | 删 |

## ai\_chat (`/api/ai`) — 兼容 stub

| Method | Path | 用途 |
|--------|------|-----|
| POST | `/chat/message` / `/chat/history/save` | no-op，保持前端兼容 |
| GET | `/chat/history` | 同上 |

## fast-analysis (`/api/fast-analysis`)

| Method | Path | 用途 |
|--------|------|-----|
| POST | `/analyze` | 主入口（异步，扣 credit） |
| POST | `/analyze-legacy` | 旧版 multi-hop（兼容） |
| GET | `/history` / `/history/all` | 历史决策 |
| DELETE | `/history/<id>` | 删 |
| POST | `/feedback` | 用户反馈 |
| GET | `/performance` | 准确率统计 |
| GET | `/similar-patterns` | 相似 K 线 few-shot |

## polymarket (`/api/polymarket`)

| Method | Path | 用途 |
|--------|------|-----|
| POST | `/analyze` | 单市场分析 |
| GET | `/history` | 历史 |

## experiment (`/api/experiment`)

| Method | Path | 用途 |
|--------|------|-----|
| POST | `/regime/detect` | 市场状态检测 |
| POST | `/pipeline/run` | 全流程跑（演化+回测+评分） |
| POST | `/ai-optimize` / `/ai-optimize-sync` | AI 调参 |
| POST | `/structured-tune` | 结构化网格搜索 |
| POST | `/save-strategy` | 把最佳变体存为新策略 |

## community (`/api/community`)

普通用户：

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/indicators` / `/indicators/<id>` | 浏览 |
| POST | `/indicators/<id>/purchase` | 购买（扣 credit） |
| POST | `/indicators/<id>/sync` | 同步本地副本 |
| GET | `/my-purchases` | 已购列表 |
| GET / POST / PUT | `/indicators/<id>/comments` | 评论 |
| GET | `/indicators/<id>/my-comment` | 我的评论 |
| GET | `/indicators/<id>/performance` | 表现 |

管理员（★）：

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/admin/pending-indicators` / `/admin/review-stats` | 审核入口 |
| POST | `/admin/indicators/<id>/review` / `/unpublish` | 审核动作 |
| DELETE | `/admin/indicators/<id>` | 删 |

## billing (`/api/billing`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/plans` | 套餐列表 |
| POST | `/purchase` | mock 支付（开发用） |
| POST | `/usdt/create` | 建 USDT TRC20 订单 |
| GET | `/usdt/order/<id>` | 查链上确认状态 |

## dashboard (`/api/dashboard`)

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/summary` | 首页大盘 |
| GET / DELETE | `/pendingOrders` `/pendingOrders/<id>` | 挂单管理 |

## settings (`/api/settings`) — 多数 ⚙

| Method | Path | 用途 |
|--------|------|-----|
| GET | `/schema` | 配置模式（前端渲染用） |
| GET | `/public-config` | 公开配置（不含 secret） |
| GET / POST | `/values` `/save` | 系统配置（写 .env，热生效） |
| GET | `/openrouter-balance` | 查 OpenRouter 余额 |
| POST | `/test-connection` | 测试外部服务连接 |

---

## 路由层公共行为

- **JWT 解析**集中在 `utils/auth.py` 的装饰器；不在各 route 自己写。
- **i18n**：很多接口接受 `lang=zh-CN|en-US|...`，路由里通常用 `_request_lang()` 提取。
- **响应**：统一返回 `{success: bool, data?: ..., message?: ...}`；前端按 `success` 判断。
- **错误**：HTTP 状态码大多是 200 + `success=false`（前端老约定），少数硬错（401/403/404/500）。
- **CORS**：`flask_cors.CORS(app)` 全开，没有 origin 白名单。生产部署若用反代记得 nginx 自己加 `Access-Control-Allow-Origin`。
