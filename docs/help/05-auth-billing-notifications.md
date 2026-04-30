# 05 · 认证 / 账单 / 凭据 / 通知

## 鉴权模型

**JWT (HS256)** + 模块级单例的 `SECRET_KEY`：

- token TTL：7 天。
- payload：`user_id`、`username`、`role`、**`token_version`**。
- 装饰器：`@login_required`、`@admin_required`、`@manager_required`、`@permission_required('feature_name')`，全部在 [utils/auth.py](../../backend_api_python/app/utils/auth.py)。
- 校验通过后写入 Flask `g`：`g.user_id` / `g.user_role` / `g.user`。

### `token_version` 的作用：单设备登录

`qd_users.token_version` 在每次新 login 时自增；旧 token 因 payload 里的版本号不匹配而被拒绝。**代价**：每次请求都要查一次 `qd_users` 比较 version，增加 ~1 ms / req DB 开销。没有缓存。

### 角色矩阵（累积权限）

| 角色 | 权限 |
|------|------|
| `viewer` | 仪表盘只读 |
| `user` | + 指标、回测、策略、持仓 |
| `manager` | + 设置管理 |
| `admin` | + 用户管理 + 全局凭据 |

`UserService.get_user_permissions()` 把这套规则查表，`@permission_required` 用它做特性级判断。新加业务功能时：决定挂哪一档，写进 `ROLE_PERMISSIONS` map。

## 用户启动 & 密码

- `UserService.ensure_admin_exists()` 在 `create_app()` 初始化阶段跑，把 `ADMIN_USER` / `ADMIN_PASSWORD` 落库（如果没有 admin）。新建用户带默认自选股 + 内置指标（FTUE）。
- 密码：bcrypt 12 rounds 优先；老数据走 SHA256+salt fallback。
- `email_verified` 区分密码登录 vs 验证码登录路径。

## 凭据加密：`qd_exchange_credentials`

[utils/credential_crypto.py](../../backend_api_python/app/utils/credential_crypto.py) 用 **Fernet (AES-128 + HMAC)** 加密交易所 API key/secret/passphrase。

- 密钥派生：`SECRET_KEY` → SHA-256 → urlsafe base64 → Fernet key。
- `encrypt_credential_blob(dict) -> str` / `decrypt_credential_blob(str) -> dict`。
- 列表接口只回 `api_key_hint`（前 4 + 后 4 字符的 mask）。**plaintext 永远不出 backend**。
- 运行时解密：`pending_order_worker` 在每次取出新订单时通过 `services/exchange_execution.resolve_exchange_config(user_id, …)` 解密 → 合并策略级覆盖 → 返回最终 client config。**没有缓存**——高并发场景每次下单都要解密一次（CPU 略浪费但安全收益更高）。

> 改 `SECRET_KEY` 会让所有已加密凭据**永久不可解密**。生产部署务必从一开始就用强 random 值（`./scripts/generate-secret-key.sh`）。

## 计费：积分 / 会员

[services/billing\_service.py](../../backend_api_python/app/services/billing_service.py)，~750 行。

### 特性级扣费

`BillingService.check_and_consume(user_id, 'feature_name')`：

1. 读 `BILLING_COST_<FEATURE>` env（如 `BILLING_COST_AI_ANALYSIS`）。
2. 若 cost == 0 或 `BILLING_ENABLED=false` → 直接放行。
3. 否则查 `qd_users.credits`（`Decimal(20,2)` 防浮点误差）。
4. 不足返错。够 → 扣减 + INSERT `qd_credits_log` (action='consume')。

> ⚠ **`BILLING_ENABLED=false` 时所有扣费都被静默 bypass**。生产部署易踩坑：开发环境忘了开 → 上线后没扣费、没人发现，直到月底对账。

### 会员栈

三档（env 配置价格 / 积分 / 时长）：

| plan | 默认价 | credits | 时长 |
|------|------|---------|------|
| monthly | €19.9 | 500 | 30 天 |
| yearly | €199 | 8000 | 365 天 |
| lifetime | €499 | 800 / 月 | 终身 |

- **续订叠加**：未到期就续费会从当前 `vip_expires_at` 起加，不丢失剩余天数。
- **lifetime 月度积分**：`_grant_lifetime_monthly_credits_best_effort()` 在每次 VIP 状态查询时按需补发，最多补 6 个周期（防 worker 长期挂掉后一次性灌一大笔）。
- **VIP 不直接抵扣特性**：AI 分析、回测仍走 credit 扣费流。VIP 只对**社区指标**的 `vip_free` 字段生效（购买社区指标免费）。
- 管理员可走 `set_vip()` 手工置位，落 audit log。

## USDT TRC20 支付

[services/usdt\_payment\_service.py](../../backend_api_python/app/services/usdt_payment_service.py)：

### 一笔订单一个地址

- 通过 HD wallet xpub 派生：`USDT_TRC20_XPUB` 可以是 account 级 (`m/44'/195'/0'`) 或 change 级 (`m/44'/195'/0'/0`)，代码会归一化到 change 级，按 `address_index` 派生。
- 每条订单进 `qd_usdt_orders`，`(chain, address)` 唯一，避免地址撞车。
- 用 [bip_utils](https://github.com/ebellocchia/bip_utils) 做派生（项目唯一密码学依赖之一）。

### 链上对账 worker

`get_usdt_order_worker()` 每 `USDT_PAY_CONFIRM_SECONDS` 秒（默认 30）轮询 TronGrid：

```
GET https://api.trongrid.io/v1/accounts/{addr}/transactions?contract=<USDT_TRC20_CONTRACT>
```

发现充值且确认数 ≥ N（默认 30 块 ≈ 几分钟）：

1. `qd_usdt_orders.status = 'confirmed'`，记 `tx_hash` / `confirmed_at`。
2. 调 `BillingService.purchase_membership(...)` 给 user 发 VIP + credits。
3. 写 `qd_credits_log` (action `'membership_*'`，reference 指向 USDT order)。

订单 `USDT_PAY_EXPIRE_MINUTES`（默认 30）后过期；过期可重新下单。

> **没有 WebSocket**：纯轮询。高负载下会有秒级延迟，TronGrid 限流可能让确认更慢。

## OAuth（Google / GitHub）

`services/oauth_service.py` + 路由 `/api/auth/<provider>/authorize` & `/callback`。

1. `/authorize?redirect_url=...` 生成 32 byte state token，**入库** `qd_oauth_states`（TTL 20 分钟）。**入库不入内存**：保证多实例部署 state 不丢。
2. provider 回调带 code+state → consume state（一次性、过期校验）→ 换 token → 拉用户信息（10s 超时）。
3. `get_or_create_user_from_oauth()`：
   - 已绑定 → 返用户。
   - 邮箱已存在 → 绑定到现有 user。
   - 新建 user：随机密码 + 自动用户名 + 写 `qd_oauth_links`。
4. 新用户额外送 `CREDITS_REGISTER_BONUS` 积分（如果 > 0）。

> `qd_oauth_states` 过期清理是被动触发（下次 authorize 时清），没有定时任务，实际数据量很小不算问题。

## 风控与审计

**爆破防护**（[services/security\_service.py](../../backend_api_python/app/services/security_service.py)）：

| 维度 | 阈值 | 锁定 |
|------|------|------|
| IP 维度失败登录 | 10 次 / 5 分钟 | 15 分钟 IP block |
| 账号维度失败登录 | 5 次 / 60 分钟 | 30 分钟账号锁 |
| 邮件验证码 | 1 次 / 分钟 / email；10 次 / 小时 / IP | — |

数据写 `qd_login_attempts`，7 天后自动清。

**Cloudflare Turnstile** 可选，env：`TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY`。**fail-closed**：开启后 Turnstile 调用失败会拒绝登录，不会降级放行。

**审计日志** `qd_security_logs`：注册、登录、改密、OAuth 登录、API key CRUD…7 天保留。

## 通知

[services/signal\_notifier.py](../../backend_api_python/app/services/signal_notifier.py) 支持 6 个通道：

| 通道 | 配置 / 调用方式 |
|------|----------------|
| browser | INSERT `qd_strategy_notifications`，前端轮询消费 |
| email | SMTP TLS/SSL（`SMTP_HOST` / `SMTP_USER` / `SMTP_PASS`），**同步**调用 |
| sms | Twilio REST API |
| telegram | Bot HTTP API（`telegram_bot_token` 可写在策略 config 里） |
| discord | Incoming webhook，embed 格式 |
| webhook | 用户自定义 URL，支持 Bearer + HMAC-SHA256 签名 |

**通用模式**：`notify_signal(strategy_id, name, symbol, signal_type, price, stake, direction, notification_config, extra) -> Dict[channel → {ok, error}]`。每个通道**独立调用、独立失败**，不会互相影响。

> ⚠ **静默退化**：缺 SMTP 配置 → email 通道返回 `{ok:false, error:'no smtp'}`，**整体响应仍是 200**。要确认通知到达，看 `qd_strategy_notifications` / 各通道服务端日志，不要看 HTTP 响应。

测试发送：`send_profile_test_notifications()`（在 settings 页验证用户配置）。

## 关键表索引

| 表 | 作用 |
|----|------|
| `qd_users` | 用户、角色、credits、VIP 状态、`token_version` |
| `qd_credits_log` | 不可变积分流水（consume / add / refund / membership / admin） |
| `qd_usdt_orders` | USDT TRC20 订单：地址、金额、状态、tx\_hash、过期时间 |
| `qd_membership_orders` | 旧版 mock 支付记录（非 USDT 流） |
| `qd_oauth_links` | user ↔ provider 绑定 |
| `qd_oauth_states` | 一次性 CSRF state |
| `qd_verification_codes` | 邮件验证码（注册/登录/改密，10 分钟 TTL） |
| `qd_login_attempts` | 爆破跟踪 |
| `qd_security_logs` | 审计 |
| `qd_exchange_credentials` | Fernet 加密的交易所凭据 |

完整表清单见 [07-database-schema.md](07-database-schema.md)。

## 主要陷阱

- **`BILLING_ENABLED=false` 静默 bypass**：开发期不扣费 → 上线忘了开 → 没人发现。
- **VIP 不抵扣 AI 分析**：用户会以为 VIP 之后免费。文案里要写清楚。
- **改 SECRET\_KEY 永久毁凭据**：先迁移再改。
- **每请求 DB 查 token\_version**：不大但累积，QPS 高时考虑加缓存。
- **lifetime 月积分 best-effort**：worker 挂超过 6 个月再恢复，丢失的不会补。
- **USDT 是轮询不是事件**：高负载下确认延迟可能超 1 分钟。
- **凭据每次解密**：没缓存，单笔下单 + Fernet ~1ms，可承受但要注意。
- **OAuth state 不过期清理**：靠下次 authorize 触发；少量过期行长期存在不影响功能。
- **通知静默失败**：要看 `qd_strategy_notifications` 表，不能信 200 OK。
- **email 通道是同步调用**：SMTP 服务器卡顿会影响策略主循环节奏。需要异步可以包一层 thread pool。
