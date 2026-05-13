"""
Community Service - 指标社区服务

处理指标市场、购买、评论等功能。
"""
import json
import time
import statistics
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple

from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.services.billing_service import get_billing_service
from app.services.experiment.scoring import StrategyScoringService

logger = get_logger(__name__)


# Field set we pull out of qd_backtest_runs.result_json. Centralised so
# get_market_indicators (list, N indicators) and get_indicator_performance
# (single indicator, deeper detail) agree on parsing & units.
#
# Units (matching what services/backtest.py:_run() writes):
#   totalReturn / annualReturn  – percent, e.g. 12.5 means +12.5%
#   maxDrawdown                 – percent, NEGATIVE, e.g. -8.3 means -8.3%
#   sharpeRatio                 – plain number
#   winRate                     – percent 0..100
#   profitFactor                – ratio, >1 means net winner
#   totalTrades                 – integer count of closing trades
def _parse_backtest_result(raw: str) -> Optional[Dict[str, Any]]:
    """Decode result_json string -> dict, returning None on any parse error.

    Returns the full result dict (not just KPI fields) because downstream
    callers may need ``equityCurve``, ``startDate``, etc.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        rj = json.loads(raw)
        return rj if isinstance(rj, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _summarise_indicator_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate one indicator's successful backtest runs into a single
    representative KPI block + a composite ``score``.

    Args:
        runs: list of qd_backtest_runs rows. Each row is expected to expose
            at least ``id``, ``symbol``, ``timeframe`` and ``result_json``.

    Strategy / why this is shaped like this:
      * We *score every run individually* through ``StrategyScoringService``
        (the same engine the parameter-optimisation page uses) and then
        take the **median** of those scores as the indicator's headline.
        Median, not mean, because one freak run on the most generous
        symbol shouldn't single-handedly drag a mediocre indicator into
        the top of the leaderboard.
      * For the KPI numbers shown on the card (return / sharpe / drawdown
        / win-rate) we also use the **median** across runs. Same reason.
      * ``best_run_id`` is the run with the highest *individual* score —
        that's the one whose equity curve we'll draw on the detail page,
        because picking by raw return is gameable (one lucky backtest
        with crazy DD wins it).
      * ``applicable_symbols`` / ``applicable_timeframes`` are the union
        of everywhere the author successfully ran a backtest. This is the
        "automatic inference" path the user explicitly chose over having
        authors hand-tag the publish form.
    """
    empty = {
        'score': 0.0,
        'total_return': 0.0,
        'annual_return': 0.0,
        'sharpe': 0.0,
        'max_drawdown': 0.0,
        'win_rate': 0.0,
        'profit_factor': 0.0,
        'sample_size': 0,
        'best_run_id': None,
        'symbols': [],
        'timeframes': [],
    }
    if not runs:
        return empty

    scorer = StrategyScoringService()
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    returns: List[float] = []
    annual_returns: List[float] = []
    sharpes: List[float] = []
    drawdowns: List[float] = []
    win_rates: List[float] = []
    profit_factors: List[float] = []
    symbols: List[str] = []
    timeframes: List[str] = []

    for run in runs:
        rj = _parse_backtest_result(run.get('result_json'))
        if not rj:
            continue
        try:
            score_info = scorer.score_result(rj)
            # StrategyScoringService returns 'overallScore' (not 'overall').
            # Reading the wrong key here previously made every indicator's
            # market-page composite score read 0, regardless of backtest quality.
            score_val = float(score_info.get('overallScore') or 0)
        except Exception:
            logger.debug("score_result failed for run %s", run.get('id'), exc_info=True)
            score_val = 0.0

        scored.append((score_val, int(run.get('id') or 0), rj))
        returns.append(float(rj.get('totalReturn') or 0))
        annual_returns.append(float(rj.get('annualReturn') or 0))
        sharpes.append(float(rj.get('sharpeRatio') or 0))
        drawdowns.append(float(rj.get('maxDrawdown') or 0))
        win_rates.append(float(rj.get('winRate') or 0))
        profit_factors.append(float(rj.get('profitFactor') or 0))

        sym = (run.get('symbol') or '').strip()
        tf = (run.get('timeframe') or '').strip()
        if sym:
            symbols.append(sym)
        if tf:
            timeframes.append(tf)

    if not scored:
        return empty

    score_values = [s for s, _, _ in scored]
    headline_score = round(statistics.median(score_values), 2)
    best = max(scored, key=lambda x: x[0])
    best_run_id = best[1] or None

    def _median(xs: List[float]) -> float:
        return round(statistics.median(xs), 2) if xs else 0.0

    # Dedupe preserving first-seen order so the UI shows the most-used
    # symbol first when truncating to "BTC/USDT +2 more".
    def _dedup(xs: List[str]) -> List[str]:
        seen = set()
        out = []
        for x in xs:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return {
        'score': headline_score,
        'total_return': _median(returns),
        'annual_return': _median(annual_returns),
        'sharpe': _median(sharpes),
        'max_drawdown': _median(drawdowns),  # already negative %, keep sign
        'win_rate': _median(win_rates),
        'profit_factor': _median(profit_factors),
        'sample_size': len(scored),
        'best_run_id': best_run_id,
        'symbols': _dedup(symbols),
        'timeframes': _dedup(timeframes),
    }


def _fetch_indicator_kpis(cur, indicator_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Batch-load KPI summary for several indicator ids in one round-trip.

    Returns ``{indicator_id: kpi_dict}``. Indicators with zero successful
    backtests get an empty kpi dict (score=0, symbols=[], etc.) — they
    still appear in the dict so callers don't have to do KeyError dances.

    Why batching: ``get_market_indicators`` returns up to ``page_size``
    indicators per request. Running one ``SELECT … FROM qd_backtest_runs``
    per indicator would scale linearly with page size and dominate the
    request budget; a single ``IN (…)`` query plus an in-Python group-by
    is O(N + M) where M is total runs across the page.
    """
    if not indicator_ids:
        return {}
    # Bucket runs by indicator_id first, then summarise each bucket.
    buckets: Dict[int, List[Dict[str, Any]]] = {iid: [] for iid in indicator_ids}
    placeholders = ','.join(['%s'] * len(indicator_ids))
    try:
        cur.execute(f"""
            SELECT id, indicator_id, symbol, timeframe, result_json
            FROM qd_backtest_runs
            WHERE indicator_id IN ({placeholders})
              AND status = 'success'
              AND result_json IS NOT NULL AND result_json != ''
        """, tuple(indicator_ids))
        for row in cur.fetchall() or []:
            iid = int(row.get('indicator_id') or 0)
            if iid in buckets:
                buckets[iid].append(dict(row))
    except Exception:
        logger.debug("Batch KPI query failed; returning empty KPIs", exc_info=True)
        return {iid: _summarise_indicator_runs([]) for iid in indicator_ids}

    return {iid: _summarise_indicator_runs(rows) for iid, rows in buckets.items()}


class CommunityService:
    """指标社区服务类"""
    
    def __init__(self):
        self.billing = get_billing_service()
        # Best-effort: ensure compatibility columns exist (for old databases)
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute("ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS vip_free BOOLEAN DEFAULT FALSE")
                # source_indicator_id links a buyer's local copy back to the published
                # original indicator so we can re-sync the latest code on demand.
                cur.execute("ALTER TABLE qd_indicator_codes ADD COLUMN IF NOT EXISTS source_indicator_id INTEGER")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_indicator_codes_source "
                    "ON qd_indicator_codes USING btree (source_indicator_id)"
                )
                db.commit()
                cur.close()
        except Exception:
            pass
    
    # ==========================================
    # 指标市场
    # ==========================================
    
    def get_market_indicators(
        self,
        page: int = 1,
        page_size: int = 12,
        keyword: str = None,
        pricing_type: str = None,  # 'free' / 'paid' / None(all)
        sort_by: str = 'score',    # 'score' / 'newest' / 'hot' / 'price_asc' / 'price_desc' / 'rating'
        user_id: int = None        # 当前用户ID，用于判断是否已购买
    ) -> Dict[str, Any]:
        """获取市场上已发布的指标列表

        About ``sort_by='score'`` (the new default):
            The composite score lives in qd_backtest_runs.result_json, which
            is opaque to SQL. We can't ORDER BY it cheaply. Instead, when
            the caller asks for score-sorted results, we:
              1. Pull the *full set* of approved + published indicators
                 (id-only, very cheap row).
              2. Batch-compute their scores via _fetch_indicator_kpis.
              3. Sort by score in Python.
              4. Slice [offset:offset+page_size] and re-query the full row
                 for just that slice.

            For other sort_by values (newest / hot / price / rating), the
            sort can be done in SQL, so we keep the original cheap path
            and only batch-compute KPIs for the visible page.

            The trade-off: score-sort is O(N) per request in indicators
            count, but N here is "how many indicators have ever been
            published" — currently realistic in the low hundreds. If the
            community grows past ~5k we'll want to denormalise the score
            onto qd_indicator_codes via a periodic job; until then this
            is fine and saves a schema migration.
        """
        offset = (page - 1) * page_size

        try:
            with get_db_connection() as db:
                cur = db.cursor()

                # 构建查询条件 - 只显示已发布且审核通过的指标
                where_clauses = ["i.publish_to_community = 1", "(i.review_status = 'approved' OR i.review_status IS NULL)"]
                params = []

                if keyword and keyword.strip():
                    where_clauses.append("(i.name ILIKE ? OR i.description ILIKE ?)")
                    search_term = f"%{keyword.strip()}%"
                    params.extend([search_term, search_term])

                if pricing_type == 'free':
                    where_clauses.append("(i.pricing_type = 'free' OR i.price <= 0)")
                elif pricing_type == 'paid':
                    where_clauses.append("(i.pricing_type != 'free' AND i.price > 0)")

                where_sql = " AND ".join(where_clauses)

                # SQL-friendly sorts:
                order_map = {
                    'newest': 'i.created_at DESC',
                    'hot': 'i.purchase_count DESC, i.view_count DESC',
                    'price_asc': 'i.price ASC, i.created_at DESC',
                    'price_desc': 'i.price DESC, i.created_at DESC',
                    'rating': 'i.avg_rating DESC, i.rating_count DESC'
                }

                # 获取总数（无论哪种排序，total 都是一样的）
                count_sql = f"SELECT COUNT(*) as count FROM qd_indicator_codes i WHERE {where_sql}"
                cur.execute(count_sql, tuple(params))
                total = cur.fetchone()['count']

                if sort_by == 'score':
                    # Score sort path: fetch ALL matching ids, score them,
                    # sort in Python, then refetch full rows for the page.
                    cur.execute(
                        f"SELECT i.id FROM qd_indicator_codes i WHERE {where_sql}",
                        tuple(params)
                    )
                    all_ids = [int(r['id']) for r in (cur.fetchall() or [])]
                    kpi_by_id = _fetch_indicator_kpis(cur, all_ids)
                    # Tie-break with created_at via id (newer id ≈ newer row)
                    # so deterministic ordering when many indicators score 0.
                    all_ids.sort(
                        key=lambda iid: (
                            -(kpi_by_id.get(iid, {}).get('score') or 0),
                            -iid
                        )
                    )
                    page_ids = all_ids[offset:offset + page_size]
                    if not page_ids:
                        cur.close()
                        return {
                            'items': [], 'total': total, 'page': page,
                            'page_size': page_size, 'total_pages': 0
                        }
                    id_placeholders = ','.join(['?'] * len(page_ids))
                    cur.execute(f"""
                        SELECT
                            i.id, i.name, i.description, i.pricing_type, i.price, COALESCE(i.vip_free, FALSE) as vip_free,
                            i.preview_image, i.purchase_count, i.avg_rating, i.rating_count,
                            i.view_count, i.created_at, i.updated_at,
                            u.id as author_id, u.username as author_username,
                            u.nickname as author_nickname, u.avatar as author_avatar
                        FROM qd_indicator_codes i
                        LEFT JOIN qd_users u ON i.user_id = u.id
                        WHERE i.id IN ({id_placeholders})
                    """, tuple(page_ids))
                    rows_unordered = cur.fetchall() or []
                    # Preserve our score-sorted order even though SQL won't
                    by_id = {r['id']: r for r in rows_unordered}
                    rows = [by_id[iid] for iid in page_ids if iid in by_id]
                    page_kpis = {iid: kpi_by_id.get(iid, _summarise_indicator_runs([])) for iid in page_ids}
                else:
                    order_sql = order_map.get(sort_by, 'i.created_at DESC')
                    query_sql = f"""
                        SELECT
                            i.id, i.name, i.description, i.pricing_type, i.price, COALESCE(i.vip_free, FALSE) as vip_free,
                            i.preview_image, i.purchase_count, i.avg_rating, i.rating_count,
                            i.view_count, i.created_at, i.updated_at,
                            u.id as author_id, u.username as author_username,
                            u.nickname as author_nickname, u.avatar as author_avatar
                        FROM qd_indicator_codes i
                        LEFT JOIN qd_users u ON i.user_id = u.id
                        WHERE {where_sql}
                        ORDER BY {order_sql}
                        LIMIT ? OFFSET ?
                    """
                    cur.execute(query_sql, tuple(params + [page_size, offset]))
                    rows = cur.fetchall() or []
                    page_kpis = _fetch_indicator_kpis(cur, [r['id'] for r in rows])

                # 如果有当前用户，查询已购买的指标
                purchased_ids = set()
                if user_id:
                    indicator_ids = [r['id'] for r in rows]
                    if indicator_ids:
                        placeholders = ','.join(['?'] * len(indicator_ids))
                        cur.execute(
                            f"SELECT indicator_id FROM qd_indicator_purchases WHERE buyer_id = ? AND indicator_id IN ({placeholders})",
                            tuple([user_id] + indicator_ids)
                        )
                        purchased_ids = {r['indicator_id'] for r in (cur.fetchall() or [])}

                cur.close()

                # 格式化返回数据
                items = []
                for row in rows:
                    kpi = page_kpis.get(row['id'], _summarise_indicator_runs([]))
                    items.append({
                        'id': row['id'],
                        'name': row['name'],
                        'description': row['description'][:200] if row['description'] else '',
                        'pricing_type': row['pricing_type'] or 'free',
                        'price': float(row['price'] or 0),
                        'vip_free': bool(row.get('vip_free') or False),
                        'preview_image': row['preview_image'] or '',
                        'purchase_count': row['purchase_count'] or 0,
                        'avg_rating': float(row['avg_rating'] or 0),
                        'rating_count': row['rating_count'] or 0,
                        'view_count': row['view_count'] or 0,
                        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                        'author': {
                            'id': row['author_id'],
                            'username': row['author_username'],
                            'nickname': row['author_nickname'] or row['author_username'],
                            'avatar': row['author_avatar'] or '/avatar2.jpg'
                        },
                        'is_purchased': row['id'] in purchased_ids,
                        'is_own': row['author_id'] == user_id,
                        # New: backtest-derived KPIs and applicability hints.
                        # All fields are guaranteed present even when an
                        # indicator has zero backtests — values just degrade
                        # to 0 / empty lists.
                        'score': kpi['score'],
                        'total_return': kpi['total_return'],
                        'annual_return': kpi['annual_return'],
                        'sharpe': kpi['sharpe'],
                        'max_drawdown': kpi['max_drawdown'],
                        'win_rate_backtest': kpi['win_rate'],
                        'profit_factor': kpi['profit_factor'],
                        'sample_size': kpi['sample_size'],
                        'applicable_symbols': kpi['symbols'],
                        'applicable_timeframes': kpi['timeframes'],
                    })

                return {
                    'items': items,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 0
                }

        except Exception as e:
            logger.error(f"get_market_indicators failed: {e}")
            return {'items': [], 'total': 0, 'page': 1, 'page_size': page_size, 'total_pages': 0}
    
    def get_indicator_detail(self, indicator_id: int, user_id: int = None) -> Optional[Dict[str, Any]]:
        """获取指标详情"""
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 获取指标信息
                cur.execute("""
                    SELECT 
                        i.id, i.name, i.description, i.pricing_type, i.price, COALESCE(i.vip_free, FALSE) as vip_free,
                        i.preview_image, i.purchase_count, i.avg_rating, i.rating_count,
                        i.view_count, i.publish_to_community, i.created_at, i.updated_at,
                        i.user_id,
                        u.id as author_id, u.username as author_username, 
                        u.nickname as author_nickname, u.avatar as author_avatar
                    FROM qd_indicator_codes i
                    LEFT JOIN qd_users u ON i.user_id = u.id
                    WHERE i.id = ?
                """, (indicator_id,))
                row = cur.fetchone()
                
                if not row:
                    cur.close()
                    return None
                
                # 检查是否已发布到社区（或者是自己的指标）
                if not row['publish_to_community'] and row['user_id'] != user_id:
                    cur.close()
                    return None
                
                # 检查是否已购买
                is_purchased = False
                has_update = False
                local_copy_id = None
                if user_id:
                    cur.execute(
                        "SELECT id FROM qd_indicator_purchases WHERE indicator_id = ? AND buyer_id = ?",
                        (indicator_id, user_id)
                    )
                    is_purchased = cur.fetchone() is not None
                    if is_purchased:
                        # Look up buyer's local copy so the frontend can tell
                        # whether a code-sync is needed.
                        local_copy = self._find_buyer_local_copy(
                            cur, buyer_id=user_id, indicator_id=indicator_id,
                            original_name=row['name']
                        )
                        if local_copy is not None:
                            local_copy_id = local_copy['id']
                            # "Has update" = original code differs from local copy code.
                            # The detail SELECT above doesn't include the full code blob,
                            # so fetch it here — only once, only when user has purchased it.
                            cur.execute(
                                "SELECT code FROM qd_indicator_codes WHERE id = ?",
                                (indicator_id,)
                            )
                            original_row = cur.fetchone()
                            original_code = original_row['code'] if original_row else None
                            local_code = local_copy.get('code')
                            has_update = (original_code or '') != (local_code or '')

                # 增加浏览次数
                cur.execute(
                    "UPDATE qd_indicator_codes SET view_count = COALESCE(view_count, 0) + 1 WHERE id = ?",
                    (indicator_id,)
                )
                db.commit()
                cur.close()
                
                return {
                    'id': row['id'],
                    'name': row['name'],
                    'description': row['description'] or '',
                    'pricing_type': row['pricing_type'] or 'free',
                    'price': float(row['price'] or 0),
                    'vip_free': bool(row.get('vip_free') or False),
                    'preview_image': row['preview_image'] or '',
                    'purchase_count': row['purchase_count'] or 0,
                    'avg_rating': float(row['avg_rating'] or 0),
                    'rating_count': row['rating_count'] or 0,
                    'view_count': (row['view_count'] or 0) + 1,
                    'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                    'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
                    'author': {
                        'id': row['author_id'],
                        'username': row['author_username'],
                        'nickname': row['author_nickname'] or row['author_username'],
                        'avatar': row['author_avatar'] or '/avatar2.jpg'
                    },
                    'is_purchased': is_purchased,
                    'is_own': row['user_id'] == user_id,
                    'has_update': has_update,
                    'local_copy_id': local_copy_id
                }
                
        except Exception as e:
            logger.error(f"get_indicator_detail failed: {e}")
            return None
    
    # ==========================================
    # 购买功能
    # ==========================================
    
    def purchase_indicator(self, buyer_id: int, indicator_id: int) -> Tuple[bool, str, Dict[str, Any]]:
        """
        购买指标
        
        Returns:
            (success, message, data)
        """
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 1. 获取指标信息
                cur.execute("""
                    SELECT id, user_id, name, code, description, pricing_type, price, COALESCE(vip_free, FALSE) as vip_free,
                           preview_image, is_encrypted
                    FROM qd_indicator_codes
                    WHERE id = ? AND publish_to_community = 1
                """, (indicator_id,))
                indicator = cur.fetchone()
                
                if not indicator:
                    cur.close()
                    return False, 'indicator_not_found', {}
                
                seller_id = indicator['user_id']
                price = float(indicator['price'] or 0)
                pricing_type = indicator['pricing_type'] or 'free'
                vip_free = bool(indicator.get('vip_free') or False)
                is_vip, _ = self.billing.get_user_vip_status(buyer_id)

                # VIP-free indicator: VIP users can get it without credits charge
                effective_price = 0.0 if (vip_free and is_vip) else price
                
                # 2. 检查是否购买自己的指标
                if seller_id == buyer_id:
                    cur.close()
                    return False, 'cannot_buy_own', {}
                
                # 3. 检查是否已购买
                cur.execute(
                    "SELECT id FROM qd_indicator_purchases WHERE indicator_id = ? AND buyer_id = ?",
                    (indicator_id, buyer_id)
                )
                if cur.fetchone():
                    cur.close()
                    return False, 'already_purchased', {}
                
                # 4. 如果是付费指标，检查并扣除积分
                if pricing_type != 'free' and effective_price > 0:
                    buyer_credits = self.billing.get_user_credits(buyer_id)
                    if buyer_credits < effective_price:
                        cur.close()
                        return False, 'insufficient_credits', {
                            'required': effective_price,
                            'current': float(buyer_credits)
                        }
                    
                    # 扣除买家积分
                    new_buyer_balance = buyer_credits - Decimal(str(effective_price))
                    cur.execute(
                        "UPDATE qd_users SET credits = ?, updated_at = NOW() WHERE id = ?",
                        (float(new_buyer_balance), buyer_id)
                    )
                    
                    # 记录买家积分日志
                    cur.execute("""
                        INSERT INTO qd_credits_log 
                        (user_id, action, amount, balance_after, feature, reference_id, remark, created_at)
                        VALUES (?, 'indicator_purchase', ?, ?, 'indicator_purchase', ?, ?, NOW())
                    """, (buyer_id, -effective_price, float(new_buyer_balance), str(indicator_id), 
                          f"购买指标: {indicator['name']}"))
                    
                    # 给卖家增加积分（可配置抽成比例，这里先100%给卖家）
                    seller_credits = self.billing.get_user_credits(seller_id)
                    new_seller_balance = seller_credits + Decimal(str(effective_price))
                    cur.execute(
                        "UPDATE qd_users SET credits = ?, updated_at = NOW() WHERE id = ?",
                        (float(new_seller_balance), seller_id)
                    )
                    
                    # 记录卖家积分日志
                    cur.execute("""
                        INSERT INTO qd_credits_log 
                        (user_id, action, amount, balance_after, feature, reference_id, remark, created_at)
                        VALUES (?, 'indicator_sale', ?, ?, 'indicator_sale', ?, ?, NOW())
                    """, (seller_id, effective_price, float(new_seller_balance), str(indicator_id),
                          f"出售指标: {indicator['name']}"))
                
                # 5. 创建购买记录
                cur.execute("""
                    INSERT INTO qd_indicator_purchases 
                    (indicator_id, buyer_id, seller_id, price, created_at)
                    VALUES (?, ?, ?, ?, NOW())
                """, (indicator_id, buyer_id, seller_id, effective_price))
                
                # 6. 复制指标到买家账户
                now_ts = int(time.time())
                # Get vip_free as boolean from indicator
                vip_free_value = bool(indicator.get('vip_free') or False)
                cur.execute("""
                    INSERT INTO qd_indicator_codes
                    (user_id, is_buy, end_time, name, code, description,
                     publish_to_community, pricing_type, price, is_encrypted, preview_image, vip_free,
                     source_indicator_id,
                     createtime, updatetime, created_at, updated_at)
                    VALUES (?, 1, 0, ?, ?, ?, 0, 'free', 0, ?, ?, ?, ?, ?, ?, NOW(), NOW())
                """, (
                    buyer_id,
                    indicator['name'],
                    indicator['code'],
                    indicator['description'],
                    indicator['is_encrypted'] or 0,
                    indicator['preview_image'],
                    vip_free_value,  # Use boolean value instead of integer 0
                    indicator_id,  # source_indicator_id — link back to the original
                    now_ts, now_ts
                ))
                
                # 7. 更新指标购买次数
                cur.execute("""
                    UPDATE qd_indicator_codes 
                    SET purchase_count = COALESCE(purchase_count, 0) + 1 
                    WHERE id = ?
                """, (indicator_id,))
                
                db.commit()
                cur.close()
                
                logger.info(f"User {buyer_id} purchased indicator {indicator_id} for {effective_price} credits (vip_free={vip_free}, is_vip={is_vip})")
                return True, 'success', {'indicator_name': indicator['name'], 'price': price, 'charged': effective_price, 'vip_free': vip_free}
                
        except Exception as e:
            logger.error(f"purchase_indicator failed: {e}")
            return False, f'error: {str(e)}', {}
    
    # ------------------------------------------------------------------
    # Local copy lookup / sync helpers
    # ------------------------------------------------------------------

    def _find_buyer_local_copy(self, cur, buyer_id: int, indicator_id: int, original_name: str = '') -> Optional[Dict[str, Any]]:
        """Find a buyer's local copy that originated from the given published indicator.

        Strategy:
          1. Prefer the explicit link via ``source_indicator_id`` (set on new purchases).
          2. Fall back to matching by ``(user_id, is_buy=1, name)`` for legacy copies
             created before the ``source_indicator_id`` column existed.

        Returns the row (id/name/code) or None if no candidate can be found.
        """
        try:
            cur.execute(
                """
                SELECT id, name, code, is_encrypted
                FROM qd_indicator_codes
                WHERE user_id = ? AND source_indicator_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (buyer_id, indicator_id)
            )
            row = cur.fetchone()
            if row:
                return {
                    'id': row['id'],
                    'name': row['name'],
                    'code': row.get('code'),
                    'is_encrypted': row.get('is_encrypted'),
                    'matched_by': 'source_id'
                }
        except Exception as e:
            # source_indicator_id column may be missing on very old DBs; ignore and fallback
            logger.debug(f"source_indicator_id lookup failed (likely legacy DB): {e}")

        if not original_name:
            return None

        try:
            cur.execute(
                """
                SELECT id, name, code, is_encrypted
                FROM qd_indicator_codes
                WHERE user_id = ? AND is_buy = 1 AND name = ?
                ORDER BY id DESC LIMIT 1
                """,
                (buyer_id, original_name)
            )
            row = cur.fetchone()
            if row:
                return {
                    'id': row['id'],
                    'name': row['name'],
                    'code': row.get('code'),
                    'is_encrypted': row.get('is_encrypted'),
                    'matched_by': 'name'
                }
        except Exception as e:
            logger.debug(f"legacy name-based lookup failed: {e}")

        return None

    def sync_purchased_indicator(self, buyer_id: int, indicator_id: int) -> Tuple[bool, str, Dict[str, Any]]:
        """Refresh a buyer's local copy with the publisher's latest code/description.

        The user must have already purchased ``indicator_id`` for this to succeed.
        The buyer's local copy (matched by ``source_indicator_id``, or by name for
        legacy copies) will be overwritten with the publisher's current content,
        and its ``source_indicator_id`` will be repaired if it was missing.

        If the original indicator has been unpublished/removed or the buyer's
        local copy no longer exists (e.g. user deleted it), a recoverable error
        is returned so the UI can explain what to do next.
        """
        try:
            with get_db_connection() as db:
                cur = db.cursor()

                # 1. Must have purchased this indicator
                cur.execute(
                    "SELECT id FROM qd_indicator_purchases WHERE indicator_id = ? AND buyer_id = ?",
                    (indicator_id, buyer_id)
                )
                if not cur.fetchone():
                    cur.close()
                    return False, 'not_purchased', {}

                # 2. Fetch the (still-published) original
                cur.execute(
                    """
                    SELECT id, user_id, name, code, description, preview_image, is_encrypted,
                           publish_to_community, updated_at
                    FROM qd_indicator_codes
                    WHERE id = ?
                    """,
                    (indicator_id,)
                )
                original = cur.fetchone()
                if not original:
                    cur.close()
                    return False, 'indicator_not_found', {}
                if not original.get('publish_to_community'):
                    cur.close()
                    return False, 'indicator_unpublished', {}

                # 3. Locate buyer's local copy
                local = self._find_buyer_local_copy(
                    cur, buyer_id=buyer_id, indicator_id=indicator_id,
                    original_name=original['name']
                )
                if not local:
                    cur.close()
                    return False, 'local_copy_not_found', {}

                # 4. Short-circuit when already identical
                if (local.get('code') or '') == (original.get('code') or ''):
                    # Still repair source_indicator_id on legacy rows so future
                    # syncs take the fast path and "has_update" detection is accurate.
                    if local.get('matched_by') == 'name':
                        cur.execute(
                            "UPDATE qd_indicator_codes SET source_indicator_id = ? WHERE id = ?",
                            (indicator_id, local['id'])
                        )
                        db.commit()
                    cur.close()
                    return True, 'already_latest', {
                        'local_copy_id': local['id'],
                        'updated': False
                    }

                # 5. Overwrite the local copy with the latest publisher content
                now_ts = int(time.time())
                cur.execute(
                    """
                    UPDATE qd_indicator_codes
                    SET code = ?,
                        description = ?,
                        preview_image = ?,
                        is_encrypted = ?,
                        source_indicator_id = ?,
                        updatetime = ?,
                        updated_at = NOW()
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        original['code'],
                        original['description'],
                        original['preview_image'],
                        original['is_encrypted'] or 0,
                        indicator_id,
                        now_ts,
                        local['id'],
                        buyer_id,
                    )
                )
                db.commit()
                cur.close()

                logger.info(
                    f"User {buyer_id} synced local indicator {local['id']} "
                    f"from published indicator {indicator_id} (matched_by={local.get('matched_by')})"
                )
                return True, 'success', {
                    'local_copy_id': local['id'],
                    'updated': True,
                    'indicator_name': original['name']
                }

        except Exception as e:
            logger.error(f"sync_purchased_indicator failed: {e}")
            return False, f'error: {str(e)}', {}

    def get_my_purchases(self, user_id: int, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """获取用户购买的指标列表"""
        offset = (page - 1) * page_size
        
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 获取总数
                cur.execute(
                    "SELECT COUNT(*) as count FROM qd_indicator_purchases WHERE buyer_id = ?",
                    (user_id,)
                )
                total = cur.fetchone()['count']
                
                # 获取列表
                cur.execute("""
                    SELECT 
                        p.id as purchase_id, p.price as purchase_price, p.created_at as purchase_time,
                        i.id, i.name, i.description, i.preview_image, i.avg_rating,
                        u.nickname as seller_nickname, u.avatar as seller_avatar
                    FROM qd_indicator_purchases p
                    LEFT JOIN qd_indicator_codes i ON p.indicator_id = i.id
                    LEFT JOIN qd_users u ON p.seller_id = u.id
                    WHERE p.buyer_id = ?
                    ORDER BY p.created_at DESC
                    LIMIT ? OFFSET ?
                """, (user_id, page_size, offset))
                rows = cur.fetchall() or []
                cur.close()
                
                items = []
                for row in rows:
                    items.append({
                        'purchase_id': row['purchase_id'],
                        'purchase_price': float(row['purchase_price'] or 0),
                        'purchase_time': row['purchase_time'].isoformat() if row['purchase_time'] else None,
                        'indicator': {
                            'id': row['id'],
                            'name': row['name'],
                            'description': row['description'][:100] if row['description'] else '',
                            'preview_image': row['preview_image'] or '',
                            'avg_rating': float(row['avg_rating'] or 0)
                        },
                        'seller': {
                            'nickname': row['seller_nickname'],
                            'avatar': row['seller_avatar'] or '/avatar2.jpg'
                        }
                    })
                
                return {
                    'items': items,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 0
                }
                
        except Exception as e:
            logger.error(f"get_my_purchases failed: {e}")
            return {'items': [], 'total': 0, 'page': 1, 'page_size': page_size, 'total_pages': 0}
    
    # ==========================================
    # 评论功能
    # ==========================================
    
    def get_comments(self, indicator_id: int, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """获取指标评论列表"""
        offset = (page - 1) * page_size
        
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 获取总数（只统计一级评论）
                cur.execute("""
                    SELECT COUNT(*) as count FROM qd_indicator_comments 
                    WHERE indicator_id = ? AND parent_id IS NULL AND is_deleted = 0
                """, (indicator_id,))
                total = cur.fetchone()['count']
                
                # 获取评论列表
                cur.execute("""
                    SELECT 
                        c.id, c.rating, c.content, c.created_at,
                        u.id as user_id, u.nickname, u.avatar
                    FROM qd_indicator_comments c
                    LEFT JOIN qd_users u ON c.user_id = u.id
                    WHERE c.indicator_id = ? AND c.parent_id IS NULL AND c.is_deleted = 0
                    ORDER BY c.created_at DESC
                    LIMIT ? OFFSET ?
                """, (indicator_id, page_size, offset))
                rows = cur.fetchall() or []
                cur.close()
                
                items = []
                for row in rows:
                    items.append({
                        'id': row['id'],
                        'rating': row['rating'],
                        'content': row['content'],
                        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                        'user': {
                            'id': row['user_id'],
                            'nickname': row['nickname'],
                            'avatar': row['avatar'] or '/avatar2.jpg'
                        }
                    })
                
                return {
                    'items': items,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 0
                }
                
        except Exception as e:
            logger.error(f"get_comments failed: {e}")
            return {'items': [], 'total': 0, 'page': 1, 'page_size': page_size, 'total_pages': 0}
    
    def add_comment(
        self, 
        user_id: int, 
        indicator_id: int, 
        rating: int, 
        content: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        添加评论（只有购买过的用户可以评论，且只能评论一次）
        """
        try:
            # 验证评分范围
            rating = max(1, min(5, int(rating)))
            content = (content or '').strip()[:500]  # 限制500字
            
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 检查指标是否存在
                cur.execute(
                    "SELECT id, user_id FROM qd_indicator_codes WHERE id = ? AND publish_to_community = 1",
                    (indicator_id,)
                )
                indicator = cur.fetchone()
                if not indicator:
                    cur.close()
                    return False, 'indicator_not_found', {}
                
                # 不能评论自己的指标
                if indicator['user_id'] == user_id:
                    cur.close()
                    return False, 'cannot_comment_own', {}
                
                # 检查是否已购买（免费指标也需要"获取"才能评论）
                cur.execute(
                    "SELECT id FROM qd_indicator_purchases WHERE indicator_id = ? AND buyer_id = ?",
                    (indicator_id, user_id)
                )
                if not cur.fetchone():
                    cur.close()
                    return False, 'not_purchased', {}
                
                # 检查是否已评论
                cur.execute(
                    "SELECT id FROM qd_indicator_comments WHERE indicator_id = ? AND user_id = ? AND parent_id IS NULL",
                    (indicator_id, user_id)
                )
                if cur.fetchone():
                    cur.close()
                    return False, 'already_commented', {}
                
                # 添加评论
                cur.execute("""
                    INSERT INTO qd_indicator_comments 
                    (indicator_id, user_id, rating, content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, NOW(), NOW())
                """, (indicator_id, user_id, rating, content))
                comment_id = cur.lastrowid
                
                # 更新指标的评分统计
                cur.execute("""
                    UPDATE qd_indicator_codes 
                    SET 
                        rating_count = COALESCE(rating_count, 0) + 1,
                        avg_rating = (
                            SELECT AVG(rating) FROM qd_indicator_comments 
                            WHERE indicator_id = ? AND parent_id IS NULL AND is_deleted = 0
                        )
                    WHERE id = ?
                """, (indicator_id, indicator_id))
                
                db.commit()
                cur.close()
                
                logger.info(f"User {user_id} commented on indicator {indicator_id} with rating {rating}")
                return True, 'success', {'comment_id': comment_id}
                
        except Exception as e:
            logger.error(f"add_comment failed: {e}")
            return False, f'error: {str(e)}', {}
    
    def update_comment(
        self,
        user_id: int,
        comment_id: int,
        indicator_id: int,
        rating: int,
        content: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        更新评论（只能修改自己的评论）
        """
        try:
            rating = max(1, min(5, int(rating)))
            content = (content or '').strip()[:500]
            
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 检查评论是否存在且属于当前用户
                cur.execute("""
                    SELECT id, rating as old_rating FROM qd_indicator_comments 
                    WHERE id = ? AND user_id = ? AND indicator_id = ? AND is_deleted = 0
                """, (comment_id, user_id, indicator_id))
                comment = cur.fetchone()
                
                if not comment:
                    cur.close()
                    return False, 'comment_not_found', {}
                
                old_rating = comment['old_rating']
                
                # 更新评论
                cur.execute("""
                    UPDATE qd_indicator_comments 
                    SET rating = ?, content = ?, updated_at = NOW()
                    WHERE id = ?
                """, (rating, content, comment_id))
                
                # 如果评分变了，更新指标的平均评分
                if old_rating != rating:
                    cur.execute("""
                        UPDATE qd_indicator_codes 
                        SET avg_rating = (
                            SELECT AVG(rating) FROM qd_indicator_comments 
                            WHERE indicator_id = ? AND parent_id IS NULL AND is_deleted = 0
                        )
                        WHERE id = ?
                    """, (indicator_id, indicator_id))
                
                db.commit()
                cur.close()
                
                logger.info(f"User {user_id} updated comment {comment_id}")
                return True, 'success', {'comment_id': comment_id}
                
        except Exception as e:
            logger.error(f"update_comment failed: {e}")
            return False, f'error: {str(e)}', {}
    
    def get_user_comment(self, user_id: int, indicator_id: int) -> Optional[Dict[str, Any]]:
        """获取用户对某个指标的评论"""
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute("""
                    SELECT id, rating, content, created_at, updated_at
                    FROM qd_indicator_comments
                    WHERE user_id = ? AND indicator_id = ? AND parent_id IS NULL AND is_deleted = 0
                """, (user_id, indicator_id))
                row = cur.fetchone()
                cur.close()
                
                if not row:
                    return None
                
                return {
                    'id': row['id'],
                    'rating': row['rating'],
                    'content': row['content'],
                    'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                    'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
                }
                
        except Exception as e:
            logger.error(f"get_user_comment failed: {e}")
            return None
    
    # ==========================================
    # 管理员审核功能
    # ==========================================
    
    def get_pending_indicators(
        self,
        page: int = 1,
        page_size: int = 20,
        review_status: str = 'pending'  # 'pending' / 'approved' / 'rejected' / 'all'
    ) -> Dict[str, Any]:
        """获取待审核的指标列表（管理员用）"""
        offset = (page - 1) * page_size
        
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 构建查询条件
                where_clauses = ["i.publish_to_community = 1"]
                params = []
                
                if review_status and review_status != 'all':
                    where_clauses.append("i.review_status = ?")
                    params.append(review_status)
                
                where_sql = " AND ".join(where_clauses)
                
                # 获取总数
                count_sql = f"""
                    SELECT COUNT(*) as count 
                    FROM qd_indicator_codes i 
                    WHERE {where_sql}
                """
                cur.execute(count_sql, tuple(params))
                total = cur.fetchone()['count']
                
                # 获取列表
                query_sql = f"""
                    SELECT 
                        i.id, i.name, i.description, i.pricing_type, i.price,
                        i.preview_image, i.code, i.review_status, i.review_note, 
                        i.reviewed_at, i.reviewed_by, i.created_at,
                        u.id as author_id, u.username as author_username, 
                        u.nickname as author_nickname, u.avatar as author_avatar,
                        r.username as reviewer_username
                    FROM qd_indicator_codes i
                    LEFT JOIN qd_users u ON i.user_id = u.id
                    LEFT JOIN qd_users r ON i.reviewed_by = r.id
                    WHERE {where_sql}
                    ORDER BY i.created_at DESC
                    LIMIT ? OFFSET ?
                """
                cur.execute(query_sql, tuple(params + [page_size, offset]))
                rows = cur.fetchall() or []
                cur.close()
                
                items = []
                for row in rows:
                    items.append({
                        'id': row['id'],
                        'name': row['name'],
                        'description': row['description'][:300] if row['description'] else '',
                        'pricing_type': row['pricing_type'] or 'free',
                        'price': float(row['price'] or 0),
                        'preview_image': row['preview_image'] or '',
                        'code': row['code'] or '',  # 管理员可以看代码
                        'review_status': row['review_status'] or 'pending',
                        'review_note': row['review_note'] or '',
                        'reviewed_at': row['reviewed_at'].isoformat() if row['reviewed_at'] else None,
                        'reviewer_username': row['reviewer_username'],
                        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                        'author': {
                            'id': row['author_id'],
                            'username': row['author_username'],
                            'nickname': row['author_nickname'] or row['author_username'],
                            'avatar': row['author_avatar'] or '/avatar2.jpg'
                        }
                    })
                
                return {
                    'items': items,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 0
                }
                
        except Exception as e:
            logger.error(f"get_pending_indicators failed: {e}")
            return {'items': [], 'total': 0, 'page': 1, 'page_size': page_size, 'total_pages': 0}
    
    def review_indicator(
        self,
        admin_id: int,
        indicator_id: int,
        action: str,  # 'approve' / 'reject'
        note: str = ''
    ) -> Tuple[bool, str]:
        """审核指标"""
        try:
            new_status = 'approved' if action == 'approve' else 'rejected'
            note = (note or '').strip()[:500]
            
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 检查指标是否存在且已发布到社区
                cur.execute("""
                    SELECT id, name, user_id FROM qd_indicator_codes 
                    WHERE id = ? AND publish_to_community = 1
                """, (indicator_id,))
                indicator = cur.fetchone()
                
                if not indicator:
                    cur.close()
                    return False, 'indicator_not_found'
                
                # 更新审核状态
                cur.execute("""
                    UPDATE qd_indicator_codes 
                    SET review_status = ?, review_note = ?, reviewed_at = NOW(), reviewed_by = ?
                    WHERE id = ?
                """, (new_status, note, admin_id, indicator_id))
                
                db.commit()
                cur.close()
                
                logger.info(f"Admin {admin_id} {action}d indicator {indicator_id}")
                return True, 'success'
                
        except Exception as e:
            logger.error(f"review_indicator failed: {e}")
            return False, f'error: {str(e)}'
    
    def unpublish_indicator(self, admin_id: int, indicator_id: int, note: str = '') -> Tuple[bool, str]:
        """下架指标（取消发布）"""
        try:
            note = (note or '').strip()[:500]
            
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 检查指标是否存在
                cur.execute("""
                    SELECT id, name FROM qd_indicator_codes WHERE id = ?
                """, (indicator_id,))
                indicator = cur.fetchone()
                
                if not indicator:
                    cur.close()
                    return False, 'indicator_not_found'
                
                # 下架（取消发布）
                cur.execute("""
                    UPDATE qd_indicator_codes 
                    SET publish_to_community = 0, review_status = 'rejected', 
                        review_note = ?, reviewed_at = NOW(), reviewed_by = ?
                    WHERE id = ?
                """, (f"下架: {note}" if note else "管理员下架", admin_id, indicator_id))
                
                db.commit()
                cur.close()
                
                logger.info(f"Admin {admin_id} unpublished indicator {indicator_id}")
                return True, 'success'
                
        except Exception as e:
            logger.error(f"unpublish_indicator failed: {e}")
            return False, f'error: {str(e)}'
    
    def admin_delete_indicator(self, admin_id: int, indicator_id: int) -> Tuple[bool, str]:
        """管理员删除指标"""
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                
                # 检查指标是否存在
                cur.execute("SELECT id, name FROM qd_indicator_codes WHERE id = ?", (indicator_id,))
                indicator = cur.fetchone()
                
                if not indicator:
                    cur.close()
                    return False, 'indicator_not_found'
                
                # 删除关联的评论
                cur.execute("DELETE FROM qd_indicator_comments WHERE indicator_id = ?", (indicator_id,))
                
                # 删除关联的购买记录
                cur.execute("DELETE FROM qd_indicator_purchases WHERE indicator_id = ?", (indicator_id,))
                
                # 删除指标
                cur.execute("DELETE FROM qd_indicator_codes WHERE id = ?", (indicator_id,))
                
                db.commit()
                cur.close()
                
                logger.info(f"Admin {admin_id} deleted indicator {indicator_id}")
                return True, 'success'
                
        except Exception as e:
            logger.error(f"admin_delete_indicator failed: {e}")
            return False, f'error: {str(e)}'
    
    def get_review_stats(self) -> Dict[str, int]:
        """获取审核统计"""
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute("""
                    SELECT 
                        COUNT(*) FILTER (WHERE review_status = 'pending' OR review_status IS NULL) as pending_count,
                        COUNT(*) FILTER (WHERE review_status = 'approved') as approved_count,
                        COUNT(*) FILTER (WHERE review_status = 'rejected') as rejected_count
                    FROM qd_indicator_codes
                    WHERE publish_to_community = 1
                """)
                row = cur.fetchone()
                cur.close()
                
                return {
                    'pending': row['pending_count'] or 0,
                    'approved': row['approved_count'] or 0,
                    'rejected': row['rejected_count'] or 0
                }
        except Exception as e:
            logger.error(f"get_review_stats failed: {e}")
            return {'pending': 0, 'approved': 0, 'rejected': 0}
    
    # ==========================================
    # 实盘表现（聚合回测 + 实盘交易数据）
    # ==========================================

    def get_indicator_performance(self, indicator_id: int) -> Dict[str, Any]:
        """
        获取指标的实盘表现统计（详情页用）。

        数据来源：
          1. qd_backtest_runs (result_json) – 全部成功回测
          2. qd_backtest_equity_points       – 最佳回测的净值曲线点
          3. qd_strategies_trading + qd_strategy_trades – 真实实盘记录

        Response keys
        -------------
        Backtest-derived (median across all successful runs, never NULL):
            score, total_return, annual_return, sharpe, max_drawdown,
            profit_factor, win_rate_backtest, sample_size,
            applicable_symbols, applicable_timeframes
        Live trading derived:
            live_strategy_count, live_trade_count, live_win_rate,
            live_total_profit
        Headline combined fields (preserved for backwards compatibility
        with the existing IndicatorDetail.vue template):
            strategy_count, trade_count, win_rate, total_profit
        Equity curve (best backtest only):
            best_run_id, best_run_meta { symbol, timeframe, total_return,
            sharpe, max_drawdown, started_at, ended_at },
            equity_curve [ { time, value } ]
        """
        default_result = {
            'strategy_count': 0,
            'trade_count': 0,
            'win_rate': 0.0,
            'total_profit': 0.0,
            'score': 0.0,
            'total_return': 0.0,
            'annual_return': 0.0,
            'sharpe': 0.0,
            'max_drawdown': 0.0,
            'profit_factor': 0.0,
            'win_rate_backtest': 0.0,
            'sample_size': 0,
            'applicable_symbols': [],
            'applicable_timeframes': [],
            'live_strategy_count': 0,
            'live_trade_count': 0,
            'live_win_rate': 0.0,
            'live_total_profit': 0.0,
            'best_run_id': None,
            'best_run_meta': None,
            'equity_curve': [],
        }

        try:
            with get_db_connection() as db:
                cur = db.cursor()

                # ---------- Part 1: 回测聚合（评分 + KPI + 适用范围） ----------
                # We re-use the same code path the list endpoint uses so
                # detail and list pages never disagree on score / KPI.
                cur.execute("""
                    SELECT id, indicator_id, symbol, timeframe, start_date, end_date, result_json
                    FROM qd_backtest_runs
                    WHERE indicator_id = %s AND status = 'success'
                          AND result_json IS NOT NULL AND result_json != ''
                """, (indicator_id,))
                bt_rows = [dict(r) for r in (cur.fetchall() or [])]
                kpi = _summarise_indicator_runs(bt_rows)

                # Surface the "best" run's metadata so the detail UI can
                # label the equity-curve panel with "this came from a
                # 4h BTC/USDT backtest, +12.4%, max DD -8.1%".
                # NB: schema columns are ``start_date`` / ``end_date``
                # (VARCHAR(20) yyyy-mm-dd), not ``started_at``/``ended_at``.
                best_run_meta = None
                if kpi['best_run_id']:
                    best_row = next((r for r in bt_rows if int(r.get('id') or 0) == kpi['best_run_id']), None)
                    if best_row:
                        rj = _parse_backtest_result(best_row.get('result_json')) or {}
                        best_run_meta = {
                            'symbol': best_row.get('symbol') or '',
                            'timeframe': best_row.get('timeframe') or '',
                            'total_return': float(rj.get('totalReturn') or 0),
                            'sharpe': float(rj.get('sharpeRatio') or 0),
                            'max_drawdown': float(rj.get('maxDrawdown') or 0),
                            'win_rate': float(rj.get('winRate') or 0),
                            'start_date': str(best_row.get('start_date') or '') or None,
                            'end_date': str(best_row.get('end_date') or '') or None,
                        }

                # Equity curve for the best run. Pulled from
                # qd_backtest_equity_points (one row per sample point) so
                # this works even if the run's result_json doesn't embed
                # the full curve.
                equity_curve: List[Dict[str, Any]] = []
                if kpi['best_run_id']:
                    try:
                        cur.execute("""
                            SELECT point_index, point_time, point_value
                            FROM qd_backtest_equity_points
                            WHERE run_id = %s
                            ORDER BY point_index ASC
                        """, (kpi['best_run_id'],))
                        for p in (cur.fetchall() or []):
                            equity_curve.append({
                                'time': p.get('point_time') or '',
                                'value': float(p.get('point_value') or 0),
                            })
                    except Exception:
                        logger.debug("equity_points query failed", exc_info=True)

                # ---------- Part 2: 实盘交易数据 ----------
                live_strategy_count = 0
                live_trade_count = 0
                live_win_rate = 0.0
                live_total_profit = 0.0

                try:
                    # 找出使用该指标的策略（indicator_config JSON 中 indicator_id 匹配）
                    # JSON 序列化有时带空格、有时不带，两种格式都试一遍。
                    cur.execute("""
                        SELECT id FROM qd_strategies_trading
                        WHERE indicator_config::text LIKE %s
                    """, (f'%"indicator_id": {indicator_id}%',))
                    strategy_rows = cur.fetchall()
                    if not strategy_rows:
                        cur.execute("""
                            SELECT id FROM qd_strategies_trading
                            WHERE indicator_config::text LIKE %s
                        """, (f'%"indicator_id":{indicator_id}%',))
                        strategy_rows = cur.fetchall()

                    if strategy_rows:
                        strategy_ids = [r['id'] for r in strategy_rows]
                        live_strategy_count = len(strategy_ids)

                        placeholders = ','.join(['%s'] * len(strategy_ids))
                        # ``profit IS NOT NULL`` excludes open events (they
                        # carry NULL profit). ``profit != 0`` was the legacy
                        # filter — it also worked because NULL comparisons
                        # return NULL (= falsy in SQL) but it accidentally
                        # dropped genuine break-even closes from the
                        # denominator. We use the more explicit NOT NULL
                        # check, in line with the dashboard fix.
                        cur.execute(f"""
                            SELECT
                                COUNT(*) as trade_count,
                                SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as win_count,
                                SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END) as loss_count,
                                SUM(profit) as total_profit
                            FROM qd_strategy_trades
                            WHERE strategy_id IN ({placeholders})
                              AND profit IS NOT NULL
                        """, tuple(strategy_ids))
                        trade_row = cur.fetchone()

                        if trade_row and (trade_row['trade_count'] or 0) > 0:
                            live_trade_count = int(trade_row['trade_count'] or 0)
                            win_count = int(trade_row['win_count'] or 0)
                            loss_count = int(trade_row['loss_count'] or 0)
                            decided = win_count + loss_count
                            # Win rate over *decided* trades — same convention
                            # as the dashboard fix, so a strategy with
                            # "2 wins / 0 losses / 1 break-even" reads as 100%.
                            live_win_rate = round(win_count / decided * 100, 2) if decided > 0 else 0.0
                            live_total_profit = round(float(trade_row['total_profit'] or 0), 2)
                except Exception:
                    logger.debug("Live trading query skipped or failed", exc_info=True)

                cur.close()

                # ---------- Combine ----------
                total_strategy_count = kpi['sample_size'] + live_strategy_count
                # Trade count from backtests is approximate (sum of per-run
                # totalTrades) — we don't claim it as a precise metric, just
                # a "size of evidence" hint on the detail page.
                bt_trades_total = 0
                for row in bt_rows:
                    rj = _parse_backtest_result(row.get('result_json')) or {}
                    bt_trades_total += int(rj.get('totalTrades') or 0)
                total_trade_count = bt_trades_total + live_trade_count

                # 综合胜率 / 总利润：实盘优先；没有实盘就退回回测中位。
                # (Previously this used the *mean* of backtest win-rates. We
                # switched to median because one weirdly successful run can
                # otherwise drag the rate from 45% to 70% on three samples.)
                if live_trade_count > 0:
                    combined_win_rate = live_win_rate
                    combined_profit = live_total_profit
                else:
                    combined_win_rate = kpi['win_rate']
                    combined_profit = kpi['total_return']

                if total_strategy_count == 0 and total_trade_count == 0 and not equity_curve:
                    return default_result

                return {
                    # Backwards-compatible headline fields
                    'strategy_count': total_strategy_count,
                    'trade_count': total_trade_count,
                    'win_rate': combined_win_rate,
                    'total_profit': round(combined_profit, 2),
                    # Backtest-derived stats (always populated, even with
                    # zero runs — values just degrade to 0)
                    'score': kpi['score'],
                    'total_return': kpi['total_return'],
                    'annual_return': kpi['annual_return'],
                    'sharpe': kpi['sharpe'],
                    'max_drawdown': kpi['max_drawdown'],
                    'profit_factor': kpi['profit_factor'],
                    'win_rate_backtest': kpi['win_rate'],
                    'sample_size': kpi['sample_size'],
                    'applicable_symbols': kpi['symbols'],
                    'applicable_timeframes': kpi['timeframes'],
                    # Live-only breakdown so the UI can show
                    # "live: X / backtest: Y" side by side if it wants.
                    'live_strategy_count': live_strategy_count,
                    'live_trade_count': live_trade_count,
                    'live_win_rate': live_win_rate,
                    'live_total_profit': live_total_profit,
                    # Equity curve panel data
                    'best_run_id': kpi['best_run_id'],
                    'best_run_meta': best_run_meta,
                    'equity_curve': equity_curve,
                }

        except Exception as e:
            logger.error(f"get_indicator_performance failed: {e}")
            return default_result


# 全局单例
_community_service = None


def get_community_service() -> CommunityService:
    """获取社区服务单例"""
    global _community_service
    if _community_service is None:
        _community_service = CommunityService()
    return _community_service
