# 04 · AI / LLM 层

QuantDinger 的 AI 层不是简单的 "chat over LLM"，而是一套**带记忆、能反思、自校准**的分析闭环：

```
   user click
      │
      ▼
  /api/fast-analysis    ──► FastAnalysisService.analyze()
                                   │
                                   │ 1) MarketDataCollector 取齐价格/宏观/新闻
                                   │ 2) LLMService.call_llm_api(messages, model)
                                   │     ├─ provider 路由（OpenRouter / OpenAI / Gemini / DeepSeek / Grok / 自定义 / MiniMax）
                                   │     └─ JSON 模式 + fallback model + 错误降级
                                   │ 3) AnalysisMemory.store()
                                   │     └─► qd_analysis_memory（带 task_status）
                                   ▼
                           response → 前端

  --- 异步闭环 ---

  ReflectionWorker (每天)
      │
      ├─► AnalysisMemory.validate_unvalidated_older_than(7 days)
      │      用当前价对照旧 decision，写回 was_correct / actual_return_pct
      │
      └─► AICalibrationService.calibrate(market)
             从已验证记录里搜 consensus_score 阈值 → qd_ai_calibration
             FastAnalysisService 下次会读最新阈值再决定 BUY/SELL/HOLD
```

## LLM Provider 抽象 — `services/llm.py`

支持 7 个 provider：

| provider | base\_url | env |
|----------|-----------|-----|
| OpenRouter（默认） | openrouter.ai | `OPENROUTER_API_KEY` |
| OpenAI | api.openai.com | `OPENAI_API_KEY` |
| Google Gemini | generativelanguage.googleapis.com | `GOOGLE_API_KEY` |
| DeepSeek | api.deepseek.com | `DEEPSEEK_API_KEY` |
| Grok / xAI | api.x.ai | `XAI_API_KEY` |
| Custom | 自填 base\_url | `CUSTOM_LLM_*` |
| MiniMax | api.minimax.chat | `MINIMAX_API_KEY` |

### Provider 选择
- 显式：`LLM_PROVIDER` env 或调用方传 `provider`。
- 隐式（自动检测）按优先级：DeepSeek > Grok > MiniMax > OpenAI > Google > OpenRouter > Custom。
- **静默切换**：配置的 provider 没有 API key 时会按优先级自动 fallback，**不会向用户提示**。排障时先看日志确认实际用了哪个 provider。

### Model 名归一化
模型名带斜杠（`openai/gpt-4o`、`anthropic/claude-...`）默认认为是 OpenRouter 格式。直连 provider 时会 strip 前缀。新增 provider 时记得在 `_normalize_model_for_provider()` 里加 case，否则 prefix 不匹配会被静默替换成 provider 默认模型。

### 失败降级链
`call_llm_api()` 遇到 402/403/404/429 时：
1. 先试 `fallback_model`（同 provider 备用）。
2. 仍失败 → 切换到优先级里下一个有 key 的 provider。
3. 全部失败 → 抛错给上层。

`safe_call_llm()` 包裹 `call_llm_api`，如果用了 JSON 模式但解析失败，返回**默认结构 + error 字段**而不是抛错，便于上层至少有一个可序列化的回包。

## Fast Analysis — `services/fast_analysis.py`

**单次 LLM 调用**完成市场分析（非 multi-agent）。文件 ~2.8k 行。入口：

```python
analyze(market, symbol, language='zh-CN'|'en-US',
        model=None, timeframe='1h'|'4h'|'1d'|'1w', user_id=None)
```

返回结构：

```json
{
  "decision": "BUY|SELL|HOLD",
  "confidence": 1-100,
  "price_at_analysis": 1.2345,
  "indicators_snapshot": {...},
  "consensus": {"consensus_score": -10..10, "agreement_ratio": 0..1, ...},
  "trend_outlook": {"next_24h": ..., "next_3d": ..., "next_1w": ..., "next_1m": ...},
  "scores": {...},
  "reasons": [...]
}
```

**两条隐藏规则**：
- **地缘政治情绪扣分**硬编码：严重事件 -42，中度 -18 直接打到 sentiment 分数。没有外部配置。
- **没有重试**：LLM 错误直接传播给路由层。

`MarketDataCollector` 是 fast\_analysis 取数前的统一接入点（来自 [services/market\_data\_collector.py](../../backend_api_python/app/services/market_data_collector.py)），把 OHLCV、宏观、新闻、情绪打成一个 dict 再送 LLM。

### 路由：`/api/fast-analysis/analyze`

POST body 包含 `market` / `symbol` / `language` / `model` / `timeframe`。路由层关键设计：

1. **in-flight 防抖**：同一 user + symbol 已在跑 task 时直接复用 `memory_id`，避免快速点击重复扣费。
2. **预扣 credit**：先 `BillingService.check_and_consume(user_id, 'ai_analysis')`，再起后台线程跑 `analyze()`。
3. **async 任务**：HTTP 立刻返回 `{memory_id, task_status:'processing'}`，前端轮询 `qd_analysis_memory` 直到 `task_status='completed'` 或 `'failed'`。
4. **失败自动退款**：`task_status='failed'` 时反向写一条 `qd_credits_log.action='refund'`。

## Analysis Memory — `services/analysis_memory.py`

单表 [qd\_analysis\_memory](07-database-schema.md)。除了存原始结果，还有几个用于学习闭环的字段：

| 字段 | 用途 |
|------|------|
| `task_status` | `processing` / `completed` / `failed`（异步任务状态） |
| `validated_at` | 反思 worker 校验时间 |
| `actual_return_pct` | 校验时算出的实际收益 |
| `was_correct` | 校验结论 |
| `raw_result` | 完整 JSONB，含全部模型输出 |

关键查询：

- `get_recent(market, symbol, days, limit)` — 历史决策。
- `validate_unvalidated_older_than(min_age_days, limit)` — Reflection worker 校验旧决策的入口。
- `get_similar_patterns(market, symbol, indicators, limit)` — 加权相似度（RSI ±15 → 0.3，MACD signal 一致 → 0.3，MA 趋势 0.25，波动率档位 0.15），用于 prompt few-shot。
- `get_confidence_accuracy_by_bucket()` — confidence calibration 的输入。

## Reflection Worker — `services/reflection.py`

后台守护线程，默认开启，间隔 **86400s（1 天）**。每轮：

1. 调 `AnalysisMemory.validate_unvalidated_older_than(min_age_days=7, limit=200)`，对≥7 天前的决策计算 `actual_return_pct` / `was_correct`：
   - **BUY 正确**：实际收益 > +2%
   - **SELL 正确**：实际收益 < -2%
   - **HOLD 正确**：|实际收益| ≤ 5%
2. 触发 `AICalibrationService.calibrate(market)`（见下）。

> 闭环含义：今天的 AI 决策只有过 7 天才会被校验，对应的阈值更新会作用在 7 天后的新分析上。**改这个 min\_age\_days 之前要想清楚校验信号噪声会不会更大**。

## AI Calibration — `services/ai_calibration.py`

校准的是 `consensus_score` → decision 的阈值（注意：**不是直接调整模型权重**）。

- 默认搜索阈值候选范围 `[10, 30]`。
- 准确率最高者写入 `qd_ai_calibration`：`buy_threshold` / `sell_threshold` / `min_consensus_abs_override` / `quality_hold_threshold` 等。
- 入参 env：`ENABLE_OFFLINE_AI_CALIBRATION`、`AI_CALIBRATION_MARKET`、`AI_CALIBRATION_LOOKBACK_DAYS=30`、`AI_CALIBRATION_MIN_SAMPLES=80`。
- 平局时优先选 BUY+SELL coverage 更高的方案。

> ⚠ FastAnalysisService 本身**不会**自动应用最新阈值——目前是路由层调 `AICalibrationService.get_latest(market)` 后再做 decision 覆盖。如果你跳过路由直接调 service，决策不会被校准。

## 集成模式：`ENABLE_AI_ENSEMBLE`

`AI_ENSEMBLE_MODELS` 在 env 里有占位，**当前代码并没有完整 ensemble 聚合实现**。`LLMService` 是无状态的，可以并发调多个 provider，但聚合规则（投票/加权）尚未在主流程串起来。要做 ensemble 需要在 `FastAnalysisService.analyze` 周围自己加 fan-out / fan-in。

## Polymarket 工作流

三个文件协作：

- **[services/polymarket\_analyzer.py](../../backend_api_python/app/services/polymarket_analyzer.py)** — 单市场分析。
  `analyze_market(market_id, user_id, use_cache, language, model)`：取市场详情 → 30 分钟 DB 缓存 → 收新闻 + 关联资产 → `_ai_predict_probability()` 调 LLM JSON 模式 → 算 divergence × confidence → 给 BUY/HOLD/SELL 推荐。
- **[services/polymarket\_batch\_analyzer.py](../../backend_api_python/app/services/polymarket_batch_analyzer.py)** — 批量打分。
  `batch_analyze_markets(markets, max_opportunities)`：把 500+ 市场摘要打到**一次 LLM 调用**里，返回前 N 个 score≥60 的机会。**不是每个市场各调一次**，省 token。
- **[services/polymarket\_worker.py](../../backend_api_python/app/services/polymarket_worker.py)** — 守护线程，每 30 分钟从 Gamma API 拉热门市场 → 去重 → 调 batch analyzer → 写 `qd_polymarket_asset_opportunities`。

路由 `POST /api/polymarket/analyze` 里：解析 URL/title → 找 market\_id → 扣 credit → 调 analyzer → 返回 `{market_id, ai_probability, market_probability, divergence, recommendation, confidence, opportunity_score}`。

## Experiment Pipeline — `services/experiment/`

策略**演化** + 自动调参。各文件分工：

| 文件 | 职责 |
|------|------|
| `regime.py` | `MarketRegimeService.detect(df)` — 用 30+ 根 K 线推断 bull/bear/range/high\_vol/transition，返回 `{regime, confidence, strategyFamilies, features, segments}` |
| `scoring.py` | `StrategyScoringService.score_result(backtest_result, regime)` — 加权（return 22%、annual 12%、sharpe 18%、profit\_factor 14%、winrate 9%、drawdown 15%、stability 10%）+ regime fit bonus，返回 0–100 + 等级 |
| `evolution.py` | `build_variants(snapshot, parameter_space, max_variants, method='grid'|'random')` 生成参数变体 |
| `prompts.py` | 构造 LLM prompts（注入指标代码、`@param` 注释、当前 regime、上一轮最佳） |
| `runner.py` | `ExperimentRunnerService.run_ai_pipeline(user_id, payload, on_progress)` — multi-round 主循环：detect → LLM 出 N 个候选参数 → 批回测 → 打分排序 → progress callback → 早停（默认 best\_score≥82） |

LLM 调用是**串行**的（一轮一次），目前没有并行 model evaluation。

## 路由速查（AI 相关）

| 路由 | 用途 |
|------|------|
| `POST /api/fast-analysis/analyze` | 启动一次分析（异步，扣 credit，自动退款） |
| `POST /api/polymarket/analyze` | Polymarket 单市场分析 |
| `POST /api/ai/chat/*` | 兼容性 stub，无后端持久化（实际聊天走 fast-analysis 或 strategy AI 生成） |

## 主要陷阱

- **Provider 静默 fallback**：少配 key 不会报错，会用别的 provider。要看日志确认。
- **decision 阈值未自动应用**：直接调 `FastAnalysisService.analyze` 不会读 calibration；要在路由层用 `AICalibrationService.get_latest()` 覆盖。
- **地缘扣分硬编码** -42 / -18，靠改 env 没用。
- **min\_age\_days=7**：新决策 7 天内不参与校准。
- **Polymarket 30 分钟缓存**：用户角度可能看不到刚刚的市场更新。
- **AI ensemble 未完整实现**：env 留了名字但聚合逻辑要自己补。
- **AnalysisMemory `task_status`** 一定要写完结，否则前端会一直 loading。崩溃路径里 `task_status` 不会自动改成 `failed`，建议在守护线程里加 timeout sweeper。
- **批量 Polymarket 分析**走单次大 prompt，token 上限可能截断市场列表，前 N 之外的机会会丢。
- **`reflection.py` 默认 24h cadence**：测试时改短，生产保持，否则历史样本不够新。
