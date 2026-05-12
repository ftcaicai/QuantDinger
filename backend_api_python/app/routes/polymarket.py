"""
Polymarket预测市场API路由
提供按需分析接口（只读，不涉及交易）
"""
from flask import Blueprint, jsonify, request, g

from app.utils.auth import login_required
from app.utils.logger import get_logger
from app.utils.db import get_db_connection
from app.data_sources.polymarket import PolymarketDataSource
import re
import json

logger = get_logger(__name__)

polymarket_bp = Blueprint('polymarket', __name__)

# 初始化服务
polymarket_source = PolymarketDataSource()


@polymarket_bp.route("/analyze", methods=["POST"])
@login_required
def analyze_polymarket():
    """
    分析Polymarket预测市场（用户输入链接或标题）
    
    POST /api/polymarket/analyze
    Body: {
        "input": "https://polymarket.com/event/xxx" 或 "市场标题",
        "language": "zh-CN" (optional)
    }
    
    流程：
    1. 从输入中解析market_id或slug
    2. 从API获取市场数据
    3. 检查计费并扣除积分
    4. 调用AI分析
    5. 返回分析结果
    """
    try:
        from app.services.billing_service import BillingService
        from app.services.polymarket_analyzer import PolymarketAnalyzer
        from decimal import Decimal
        
        user_id = getattr(g, 'user_id', None)
        if not user_id:
            return jsonify({
                "code": 0,
                "msg": "User not authenticated",
                "data": None
            }), 401
        
        data = request.get_json() or {}
        input_text = (data.get('input') or '').strip()
        language = data.get('language', 'zh-CN')
        
        if not input_text:
            return jsonify({
                "code": 0,
                "msg": "Input is required (Polymarket URL or market title)",
                "data": None
            }), 400
        
        # 1. 解析market_id或slug
        market_id = None
        slug = None
        
        # 尝试从URL中提取
        # `(?:/[a-z]{2}(?:-[A-Z]{2})?)?` 兼容Polymarket的语言前缀，例如 /zh/、/en/、/zh-CN/。
        # `([^/?#]+)` 截到下一个 /、?、# 之前。
        url_patterns = [
            r'polymarket\.com(?:/[a-z]{2}(?:-[A-Z]{2})?)?/event/([^/?#]+)',
            r'polymarket\.com(?:/[a-z]{2}(?:-[A-Z]{2})?)?/markets/(\d+)',
            r'polymarket\.com(?:/[a-z]{2}(?:-[A-Z]{2})?)?/market/(\d+)',
        ]
        
        for pattern in url_patterns:
            match = re.search(pattern, input_text)
            if match:
                extracted = match.group(1)
                # 如果是数字，是market_id；否则是slug
                if extracted.isdigit():
                    market_id = extracted
                else:
                    slug = extracted
                break
        
        # 2. 获取市场数据
        # 注意：如果用户输入了URL，slug或market_id已经被精确提取，必须按它精确取，
        # 不要回退到模糊搜索 —— 那会把无关的热门市场误当成用户要的市场返回。
        market = None
        if market_id:
            market = polymarket_source.get_market_details(market_id)
        elif slug:
            market = polymarket_source.get_market_details(slug)
            if market and not market_id:
                market_id = market.get('market_id')
        elif 'polymarket.com' in input_text.lower():
            # 看起来是个polymarket URL但正则没抓到 —— 不要回退到fuzzy title search
            # （那会把整段URL当作关键词去打分，必然返回无关结果）
            return jsonify({
                "code": 0,
                "msg": "Could not parse a market slug from this Polymarket URL. Please paste the URL directly from a market page (looks like https://polymarket.com/event/<slug>).",
                "data": None
            }), 400
        else:
            # 用户输入的不是URL，按标题做关键词搜索；只有"足够确信"才接受
            logger.info(f"Searching for market by title: {input_text[:100]}")
            search_results = polymarket_source.search_markets(input_text, limit=5)
            input_lower = input_text.lower()
            confident_match = next(
                (r for r in search_results if input_lower in (r.get('question') or '').lower()
                 or input_lower == (r.get('slug') or '').lower()),
                None,
            )
            if confident_match:
                market = confident_match
                market_id = market.get('market_id')
            elif search_results:
                # 找到了模糊候选但不够精准 —— 让用户改用URL，避免分析错市场
                return jsonify({
                    "code": 0,
                    "msg": "Multiple possible markets matched. Please paste the exact Polymarket URL.",
                    "data": {
                        "candidates": [
                            {
                                "market_id": r.get('market_id'),
                                "question": r.get('question'),
                                "polymarket_url": r.get('polymarket_url'),
                            }
                            for r in search_results[:5]
                        ]
                    },
                }), 409

        if not market:
            return jsonify({
                "code": 0,
                "msg": "Market not found. Please check the URL or title.",
                "data": None
            }), 404
        
        if not market_id:
            market_id = market.get('market_id')
        
        if not market_id:
            return jsonify({
                "code": 0,
                "msg": "Invalid market data",
                "data": None
            }), 400
        
        # 3. 检查计费
        billing = BillingService()
        cost = 0
        
        if billing.is_billing_enabled():
            cost = billing.get_feature_cost('polymarket_deep_analysis')
            
            if cost > 0:
                user_credits = billing.get_user_credits(user_id)
                if user_credits < Decimal(str(cost)):
                    return jsonify({
                        "code": 0,
                        "msg": "Insufficient credits",
                        "data": {
                            "required": cost,
                            "current": float(user_credits),
                            "shortage": float(Decimal(str(cost)) - user_credits)
                        }
                    }), 400
                
                # 扣除积分（使用check_and_consume方法，它会自动从配置中获取成本）
                success, error_msg = billing.check_and_consume(
                    user_id=user_id,
                    feature='polymarket_deep_analysis',
                    reference_id=f"polymarket_{market_id}"
                )
                
                if not success:
                    # 检查是否是积分不足的错误
                    if error_msg.startswith('insufficient_credits'):
                        parts = error_msg.split(':')
                        if len(parts) >= 3:
                            current_credits = parts[1]
                            required_credits = parts[2]
                            return jsonify({
                                "code": 0,
                                "msg": "Insufficient credits",
                                "data": {
                                    "required": float(required_credits),
                                    "current": float(current_credits),
                                    "shortage": float(Decimal(required_credits) - Decimal(current_credits))
                                }
                            }), 400
                    return jsonify({
                        "code": 0,
                        "msg": f"Failed to deduct credits: {error_msg}",
                        "data": None
                    }), 500
        
        # 4. 执行AI分析（传递语言和模型参数）
        analyzer = PolymarketAnalyzer()
        model = request.get_json().get('model')  # 可选：从请求中获取模型参数
        analysis_result = analyzer.analyze_market(
            market_id, 
            user_id=user_id, 
            use_cache=False,
            language=language,
            model=model
        )
        
        if analysis_result.get('error'):
            return jsonify({
                "code": 0,
                "msg": analysis_result.get('error', 'Analysis failed'),
                "data": None
            }), 500
        
        # 5. 获取剩余积分
        remaining_credits = 0
        if billing.is_billing_enabled():
            remaining_credits = float(billing.get_user_credits(user_id))
        
        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {
                "market": market,
                "analysis": analysis_result,
                "credits_charged": cost,
                "remaining_credits": remaining_credits
            }
        })
        
    except Exception as e:
        logger.error(f"Polymarket analyze API failed: {e}", exc_info=True)
        return jsonify({
            "code": 0,
            "msg": str(e),
            "data": None
        }), 500


@polymarket_bp.route("/history", methods=["GET"])
@login_required
def get_polymarket_history():
    """
    Get user's Polymarket analysis history.
    
    GET /api/polymarket/history?page=1&page_size=20
    """
    try:
        user_id = g.user_id
        page = request.args.get('page', 1, type=int)
        page_size = min(request.args.get('page_size', 20, type=int), 100)
        offset = (page - 1) * page_size
        
        with get_db_connection() as db:
            cur = db.cursor()
            
            # 获取总数
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM qd_analysis_tasks
                WHERE user_id = %s AND market = 'Polymarket'
            """, (user_id,))
            total_row = cur.fetchone()
            total = total_row['total'] if total_row else 0
            
            # 获取历史记录
            cur.execute("""
                SELECT 
                    t.id,
                    t.symbol AS market_id,
                    t.model,
                    t.language,
                    t.status,
                    t.created_at,
                    t.completed_at,
                    t.result_json
                FROM qd_analysis_tasks t
                WHERE t.user_id = %s AND t.market = 'Polymarket'
                ORDER BY t.created_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, page_size, offset))
            rows = cur.fetchall() or []
            cur.close()
        
        # 解析结果
        items = []
        for row in rows:
            result_json = row.get('result_json', '{}')
            try:
                result_data = json.loads(result_json) if result_json else {}
            except:
                result_data = {}
            
            market_data = result_data.get('market', {})
            analysis_data = result_data.get('analysis', {})
            
            # SafeJSONProvider normalizes datetimes to UTC ISO automatically.
            created_at = row.get('created_at')
            completed_at = row.get('completed_at')

            items.append({
                'id': row.get('id'),
                'market_id': row.get('market_id'),
                'market_title': market_data.get('question') or market_data.get('title') or f"Market {row.get('market_id')}",
                'market_url': market_data.get('polymarket_url'),
                'ai_predicted_probability': analysis_data.get('ai_predicted_probability'),
                'market_probability': analysis_data.get('market_probability'),
                'recommendation': analysis_data.get('recommendation'),
                'opportunity_score': analysis_data.get('opportunity_score'),
                'confidence_score': analysis_data.get('confidence_score'),
                'status': row.get('status'),
                'created_at': created_at,
                'completed_at': completed_at
            })
        
        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size
            }
        })
        
    except Exception as e:
        logger.error(f"Get Polymarket history failed: {e}", exc_info=True)
        return jsonify({
            "code": 0,
            "msg": str(e),
            "data": None
        }), 500
