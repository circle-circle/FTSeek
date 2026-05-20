"""
复盘系统 API 接口

提供复盘报告、错误模式、改进建议、对比数据的查询接口
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from flask import Blueprint, jsonify, request, current_app

logger = logging.getLogger(__name__)

# 创建 Blueprint
review_api = Blueprint('review_api', __name__, url_prefix='/api/review')


# ==================== 交易记录 API ====================

@review_api.route('/trades', methods=['POST'])
def create_trade():
    """
    创建交易记录 (含复盘字段)
    
    Request Body:
    {
        "stock_code": "AAPL",
        "side": "buy",
        "quantity": 100,
        "price": 150.50,
        "trade_time": "2026-03-28T10:30:00",
        "decision_reason": "technical_breakout",
        "decision_confidence": 8,
        "emotion_state": "calm",
        "emotion_intensity": 3,
        ...
    }
    
    Response:
    {
        "trade_id": "trade_abc123",
        "status": "success"
    }
    """
    try:
        data = request.get_json()
        
        # 必填字段验证
        required_fields = ['stock_code', 'side', 'quantity', 'price', 'trade_time']
        for field in required_fields:
            if field not in data:
                return jsonify({
                    'error': f'缺少必填字段：{field}'
                }), 400
        
        # 导入交易记录器
        from app.storage.trade_recorder import TradeRecorder
        recorder = TradeRecorder()
        
        # 记录交易
        trade_id = recorder.record(
            stock_code=data['stock_code'],
            side=data['side'],
            quantity=int(data['quantity']),
            price=float(data['price']),
            trade_time=data.get('trade_time'),
            order_id=data.get('order_id'),
            strategy_name=data.get('strategy_name'),
            account_type=data.get('account_type', 'paper'),
            decision_reason=data.get('decision_reason'),
            decision_confidence=int(data.get('decision_confidence', 5)),
            decision_notes=data.get('decision_notes'),
            emotion_state=data.get('emotion_state', 'calm'),
            emotion_intensity=int(data.get('emotion_intensity', 5)),
            psychology_notes=data.get('psychology_notes'),
        )
        
        logger.info(f"交易记录创建成功：{trade_id}")
        
        return jsonify({
            'trade_id': trade_id,
            'status': 'success'
        }), 201
        
    except Exception as e:
        logger.error(f"创建交易记录失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


@review_api.route('/trades/<trade_id>/review', methods=['PUT'])
def update_trade_review(trade_id):
    """
    更新交易复盘信息
    
    Request Body:
    {
        "entry_timing_score": 7,
        "exit_timing_score": 5,
        "stop_loss_executed": true,
        "key_learning": "突破确认后再入场更稳妥",
        ...
    }
    """
    try:
        data = request.get_json()
        
        from app.storage.trade_recorder import TradeRecorder
        recorder = TradeRecorder()
        
        # 更新复盘信息
        recorder.update_review(trade_id=trade_id, **data)
        
        logger.info(f"交易复盘更新成功：{trade_id}")
        
        return jsonify({
            'trade_id': trade_id,
            'status': 'success'
        })
        
    except Exception as e:
        logger.error(f"更新交易复盘失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 复盘报告 API ====================

@review_api.route('/report', methods=['GET'])
def get_review_report():
    """
    获取复盘报告
    
    Query Parameters:
    - type: daily | weekly | monthly (default: daily)
    - date: 日期 (YYYY-MM-DD 格式)
    
    Response:
    {
        "report_type": "daily",
        "date": "2026-03-28",
        "stats": {
            "total_trades": 5,
            "win_rate": 60.0,
            "total_return": 2.5,
            ...
        },
        "dimensions": {
            "decision": 8,
            "timing": 6,
            ...
        },
        "equity_curve": {
            "dates": [...],
            "values": [...]
        },
        "best_worst_trades": {...},
        "key_learnings": [...],
        "improvement_goals": [...]
    }
    """
    try:
        report_type = request.args.get('type', 'daily')
        date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if report_type == 'daily':
            from app.review.daily_review import DailyReviewGenerator
            generator = DailyReviewGenerator()
            report = generator.generate(date=date_str)
        elif report_type == 'weekly':
            from app.review.weekly_review import WeeklyReviewGenerator
            generator = WeeklyReviewGenerator()
            # 计算周起始日期 (周一)
            date = datetime.strptime(date_str, '%Y-%m-%d')
            week_start = date - timedelta(days=date.weekday())
            report = generator.generate(week_start=week_start.strftime('%Y-%m-%d'))
        elif report_type == 'monthly':
            # P1 2026-04-19: 月度复盘未实现
            return jsonify({
                'error': '月度复盘暂未实现'
            }), 501
        else:
            return jsonify({
                'error': f'不支持的报告类型：{report_type}'
            }), 400
        
        return jsonify(report)
        
    except Exception as e:
        logger.error(f"获取复盘报告失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 错误模式 API ====================

@review_api.route('/error-patterns', methods=['GET'])
def get_error_patterns():
    """
    获取错误模式分析
    
    Query Parameters:
    - period: 7d | 30d | 90d | all (default: 30d)
    - min_occurrences: 最少出现次数 (default: 1)
    
    Response:
    {
        "patterns": [
            {
                "type": "stop_loss_not_executed",
                "name": "止损不执行",
                "count": 8,
                "trend": "up",
                "impact": "$1,240",
                "suggestion": "..."
            },
            ...
        ],
        "trend": {
            "weekly": [...]
        },
        "actions": [...]
    }
    """
    try:
        period = request.args.get('period', '30d')
        min_occurrences = int(request.args.get('min_occurrences', 1))
        
        from app.review.error_pattern_detector import ErrorPatternDetector
        detector = ErrorPatternDetector()
        
        # 检测错误模式
        patterns = detector.detect(
            period=period,
            min_occurrences=min_occurrences
        )
        
        # 获取趋势数据
        trend = detector.get_trend(period=period)
        
        # 获取改进行动
        from app.storage.trade_recorder import TradeRecorder
        recorder = TradeRecorder()
        actions = recorder.get_improvement_actions()
        
        return jsonify({
            'patterns': patterns,
            'trend': trend,
            'actions': actions
        })
        
    except Exception as e:
        logger.error(f"获取错误模式失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 改进建议 API ====================

@review_api.route('/suggestions', methods=['GET'])
def get_suggestions():
    """
    获取智能改进建议
    
    Query Parameters:
    - limit: 返回数量 (default: 10)
    - priority: P0 | P1 | P2 | P3 (default: all)
    
    Response:
    {
        "suggestions": [
            {
                "id": "suggestion_123",
                "priority": "P0",
                "suggestion": "考虑暂停交易，调整心态",
                "reason": "连续亏损 >= 3 笔",
                "rule_id": "rule_001"
            },
            ...
        ]
    }
    """
    try:
        limit = int(request.args.get('limit', 10))
        priority = request.args.get('priority')
        
        from app.review.suggestion_engine import SuggestionEngine
        engine = SuggestionEngine()
        
        suggestions = engine.get_suggestions(
            limit=limit,
            priority=priority
        )
        
        return jsonify({
            'suggestions': suggestions
        })
        
    except Exception as e:
        logger.error(f"获取改进建议失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 模拟 vs 实盘对比 API ====================

@review_api.route('/sim-real-compare', methods=['GET'])
def get_sim_real_compare():
    """
    获取模拟 vs 实盘对比数据
    
    Query Parameters:
    - period: 7d | 30d | 90d | all (default: 30d)
    
    Response:
    {
        "metrics": {
            "paper": {
                "total_return": 12.5,
                "annual_return": 45.2,
                "max_drawdown": -3.2,
                "sharpe_ratio": 2.1,
                "win_rate": 62.0,
                "profit_factor": 2.8,
                "avg_hold_time": 4.2,
                "trade_frequency": 3.5
            },
            "real": {
                "total_return": 8.3,
                "annual_return": 30.1,
                "max_drawdown": -5.8,
                "sharpe_ratio": 1.5,
                "win_rate": 55.0,
                "profit_factor": 2.1,
                "avg_hold_time": 2.8,
                "trade_frequency": 5.2
            }
        },
        "equity_curves": {
            "paper": {
                "dates": [...],
                "values": [...]
            },
            "real": {
                "dates": [...],
                "values": [...]
            }
        },
        "holdings": {
            "paper": [...],
            "real": [...]
        },
        "analysis": {
            "differences": [...],
            "suggestions": [...]
        }
    }
    """
    try:
        period = request.args.get('period', '30d')
        
        # TODO: 实现模拟 vs 实盘对比逻辑
        # 这里返回示例数据
        
        return jsonify({
            'metrics': {
                'paper': {
                    'total_return': 12.5,
                    'annual_return': 45.2,
                    'max_drawdown': -3.2,
                    'sharpe_ratio': 2.1,
                    'win_rate': 62.0,
                    'profit_factor': 2.8,
                    'avg_hold_time': 4.2,
                    'trade_frequency': 3.5
                },
                'real': {
                    'total_return': 8.3,
                    'annual_return': 30.1,
                    'max_drawdown': -5.8,
                    'sharpe_ratio': 1.5,
                    'win_rate': 55.0,
                    'profit_factor': 2.1,
                    'avg_hold_time': 2.8,
                    'trade_frequency': 5.2
                }
            },
            'equity_curves': {
                'paper': {
                    'dates': ['03/01', '03/05', '03/10', '03/15', '03/20', '03/25', '03/28'],
                    'values': [100000, 103500, 102800, 107200, 110500, 109200, 112500]
                },
                'real': {
                    'dates': ['03/01', '03/05', '03/10', '03/15', '03/20', '03/25', '03/28'],
                    'values': [100000, 101500, 100800, 104200, 106500, 105200, 108300]
                }
            },
            'holdings': {
                'paper': [
                    {'code': 'AAPL', 'quantity': 100, 'pnl': 1250, 'pnl_pct': 8.3},
                    {'code': 'MSFT', 'quantity': 50, 'pnl': 680, 'pnl_pct': 5.2},
                    {'code': 'GOOGL', 'quantity': 30, 'pnl': 420, 'pnl_pct': 3.8}
                ],
                'real': [
                    {'code': 'AAPL', 'quantity': 50, 'pnl': 625, 'pnl_pct': 8.3},
                    {'code': 'NVDA', 'quantity': 20, 'pnl': -380, 'pnl_pct': -4.2}
                ]
            },
            'analysis': {
                'differences': [
                    '心理因素影响：实盘交易时情绪波动更大',
                    '止损执行率：模拟 95%，实盘 78%',
                    '持仓时间：实盘更短，可能过早止盈',
                    '交易频率：实盘更频繁，冲动交易比例高'
                ],
                'suggestions': [
                    '降低实盘仓位至模拟的 50%',
                    '设置价格提醒提高止损执行率',
                    '建立交易清单减少冲动交易',
                    '延长持仓时间避免过早止盈'
                ]
            }
        })
        
    except Exception as e:
        logger.error(f"获取对比数据失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 策略表现 API ====================

@review_api.route('/strategy/<strategy_name>', methods=['GET'])
def get_strategy_performance(strategy_name: str):
    """
    获取策略表现分析
    
    Path Parameters:
    - strategy_name: 策略名称
    
    Query Parameters:
    - start_date: 开始日期 (YYYY-MM-DD)
    - end_date: 结束日期 (YYYY-MM-DD)
    
    Response:
    {
        "strategy_name": "ma_cross_v2",
        "period": {
            "start": "2026-01-01",
            "end": "2026-03-28"
        },
        "stats": {
            "total_trades": 45,
            "win_rate": 58.0,
            "avg_return": 2.3,
            "max_drawdown": -4.5,
            "sharpe_ratio": 1.8,
            ...
        }
    }
    """
    try:
        start_date = request.args.get('start_date', (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d'))
        end_date = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))
        
        from app.review.strategy_analysis import StrategyAnalyzer
        analyzer = StrategyAnalyzer()
        
        stats = analyzer.analyze_strategy(
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date
        )
        
        return jsonify({
            'strategy_name': strategy_name,
            'period': {
                'start': start_date,
                'end': end_date
            },
            'stats': stats
        })
        
    except Exception as e:
        logger.error(f"获取策略表现失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 心理分析 API ====================

@review_api.route('/psychology', methods=['GET'])
def get_psychology_analysis():
    """
    获取心理状态分析
    
    Query Parameters:
    - period: 7d | 30d | 90d | all (default: 30d)
    
    Response:
    {
        "emotion_distribution": {
            "calm": 45,
            "nervous": 20,
            "excited": 15,
            ...
        },
        "emotion_performance": {
            "calm": {"win_rate": 65, "avg_return": 2.5},
            "nervous": {"win_rate": 45, "avg_return": -1.2},
            ...
        },
        "correlation": {
            "emotion_intensity_vs_return": -0.35,
            "confidence_vs_return": 0.42
        }
    }
    """
    try:
        period = request.args.get('period', '30d')
        
        from app.review.psychology_analyzer import PsychologyAnalyzer
        analyzer = PsychologyAnalyzer()
        
        analysis = analyzer.analyze(period=period)
        
        return jsonify(analysis)
        
    except Exception as e:
        logger.error(f"获取心理分析失败：{e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500


# ==================== 健康检查 API ====================

@review_api.route('/health', methods=['GET'])
def health_check():
    """
    健康检查
    
    Response:
    {
        "status": "healthy",
        "timestamp": "2026-03-28T22:35:00"
    }
    """
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })
