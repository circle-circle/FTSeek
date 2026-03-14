"""
精简 API 端点 (Task 10)

提供必要的人工干预和数据查询接口:
1. GET /api/v2/positions - 查询所有持仓
2. GET /api/v2/performance - 查询绩效统计
3. POST /api/v2/mode/{code} - 切换交易模式
4. POST /api/v2/manual_exit/{code} - 手动平仓
"""
import os
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify, Response
from functools import wraps

# 延迟导入，避免循环依赖
from app.v4.core.decision_engine import DecisionEngine
from app.performance.tracker import PerformanceTracker

logger = logging.getLogger("ftseek.api.decision")

# API Key 配置 (从环境变量读取)
API_KEY = os.getenv('API_KEY') or os.getenv('API_TOKEN', 'dev-key-change-in-production')

# Flask 应用
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False


def _check_api_key():
    """API Key 认证检查"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return ('Missing API key', 401)
    
    if api_key != API_KEY:
        return ('Invalid API key', 401)
    
    return None


def require_api_key(f):
    """API Key 认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        error_result = _check_api_key()
        if error_result:
            error_msg, status_code = error_result
            return jsonify({
                'error': 'Authentication failed',
                'message': error_msg
            }), status_code
        return f(*args, **kwargs)
    return decorated_function


def get_decision_engine() -> Optional[DecisionEngine]:
    """获取 DecisionEngine 实例"""
    try:
        # 尝试从 DecisionEngine 获取单例实例
        if hasattr(DecisionEngine, 'get_instance'):
            return DecisionEngine.get_instance()
        
        # 如果没有单例，创建新实例
        from app import config
        db_path = getattr(config, 'DB_PATH', '/opt/ftseek/FTSeek/data/ftseek.db')
        
        engine = DecisionEngine(
            db_path=db_path,
            config={
                'max_signals_per_hour': 2,
                'cooldown_minutes': 15,
                'slippage': 0.005,
                'target_profit': 0.05,
                'stop_loss': 0.03,
                'timeout_hours': 24,
            },
            dingtalk_webhook=getattr(config, 'DINGTALK_WEBHOOK', None)
        )
        return engine
    except Exception as e:
        logger.error(f"Failed to get DecisionEngine: {e}")
        return None


def format_position(pos: Any) -> Dict[str, Any]:
    """格式化持仓数据"""
    if hasattr(pos, 'to_dict'):
        data = pos.to_dict()
    elif isinstance(pos, dict):
        data = pos
    else:
        # 从对象属性构建
        data = {
            'code': getattr(pos, 'code', 'UNKNOWN'),
            'quantity': getattr(pos, 'quantity', 0),
            'avg_cost': getattr(pos, 'avg_cost', 0.0),
            'current_price': getattr(pos, 'current_price', 0.0),
            'pnl': getattr(pos, 'pnl', 0.0),
            'pnl_pct': getattr(pos, 'pnl_pct', 0.0),
        }
    
    # 确保字段完整
    return {
        'code': data.get('code', 'UNKNOWN'),
        'quantity': data.get('quantity', 0),
        'avg_cost': round(data.get('avg_cost', 0.0), 2),
        'current_price': round(data.get('current_price', 0.0), 2),
        'pnl': round(data.get('pnl', 0.0), 2),
        'pnl_pct': round(data.get('pnl_pct', 0.0), 4),
    }


@app.route('/api/v2/positions', methods=['GET'])
@require_api_key
def get_positions():
    """
    查询所有持仓
    
    Response:
    {
        "positions": [
            {"code": "0700.HK", "quantity": 100, "avg_cost": 382.40, 
             "current_price": 401.52, "pnl": 1912.00, "pnl_pct": 0.05}
        ],
        "total_count": 1
    }
    """
    try:
        engine = get_decision_engine()
        if engine is None:
            return jsonify({
                'error': 'Engine not initialized',
                'message': 'Decision engine is not available'
            }), 503
        
        positions = engine.get_all_positions()
        
        formatted_positions = [format_position(pos) for pos in positions]
        
        return jsonify({
            'positions': formatted_positions,
            'total_count': len(formatted_positions)
        }), 200
    
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/api/v2/performance', methods=['GET'])
@require_api_key
def get_performance():
    """
    查询绩效统计
    
    Query Params:
    - code: 股票代码 (必需)
    - days: 统计天数 (可选，默认 30)
    
    Response:
    {
        "code": "0700.HK",
        "total_trades": 12,
        "winning_trades": 8,
        "win_rate": 0.67,
        "total_pnl": 8500.00,
        "total_return": 0.085,
        "max_drawdown": -0.042
    }
    """
    try:
        code = request.args.get('code')
        if not code:
            return jsonify({
                'error': 'Missing parameter',
                'message': 'code parameter is required'
            }), 400
        
        days = request.args.get('days', '30')
        try:
            days = int(days)
        except ValueError:
            return jsonify({
                'error': 'Invalid parameter',
                'message': 'days must be an integer'
            }), 400
        
        # 使用 PerformanceTracker 获取绩效统计
        from app import config
        db_path = getattr(config, 'DB_PATH', '/opt/ftseek/FTSeek/data/ftseek.db')
        tracker = PerformanceTracker(db_path=db_path)
        
        # 获取绩效统计
        stats = tracker.get_performance_stats(code, days=days)
        
        # 格式化响应
        response_data = {
            'code': code,
            'total_trades': stats.get('total_trades', 0),
            'winning_trades': stats.get('winning_trades', 0),
            'win_rate': round(stats.get('win_rate', 0.0), 2),
            'total_pnl': round(stats.get('total_pnl', 0.0), 2),
            'total_return': round(stats.get('total_return', 0.0), 4),
            'max_drawdown': round(stats.get('max_drawdown', 0.0), 4),
        }
        
        return jsonify(response_data), 200
    
    except Exception as e:
        logger.error(f"Error getting performance: {e}")
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/api/v2/mode/<code>', methods=['POST'])
@require_api_key
def switch_mode(code: str):
    """
    切换交易模式 (人工确认)
    
    Body:
    {
        "mode": "PAPER"  # PAPER/HYBRID/LIVE/DISABLED
    }
    
    Response:
    {
        "code": "0700.HK",
        "old_mode": "HYBRID",
        "new_mode": "PAPER",
        "confirmed_by": "user",
        "timestamp": "2026-03-12T16:45:00"
    }
    """
    try:
        # 验证请求体
        if not request.is_json:
            return jsonify({
                'error': 'Invalid content type',
                'message': 'Content-Type must be application/json'
            }), 400
        
        data = request.get_json()
        if not data or 'mode' not in data:
            return jsonify({
                'error': 'Missing parameter',
                'message': 'mode is required in request body'
            }), 400
        
        new_mode = data['mode'].upper()
        valid_modes = ['PAPER', 'HYBRID', 'LIVE', 'DISABLED']
        
        if new_mode not in valid_modes:
            return jsonify({
                'error': 'Invalid mode',
                'message': f'Mode must be one of: {", ".join(valid_modes)}'
            }), 400
        
        engine = get_decision_engine()
        if engine is None:
            return jsonify({
                'error': 'Engine not initialized',
                'message': 'Decision engine is not available'
            }), 503
        
        # 获取当前模式
        old_mode_obj = engine.get_mode(code)
        if old_mode_obj is None:
            old_mode = 'PAPER'  # 默认模式
        elif hasattr(old_mode_obj, 'value'):
            old_mode = old_mode_obj.value
        else:
            old_mode = str(old_mode_obj)
        
        # 切换模式 (需要转换为 TradingMode 枚举)
        from app.v4.core.decision_engine import TradingMode
        mode_enum = TradingMode.from_string(new_mode)
        if mode_enum is None:
            return jsonify({
                'error': 'Invalid mode',
                'message': f'Cannot convert {new_mode} to TradingMode'
            }), 400
        
        engine.set_mode(code, mode_enum)
        
        timestamp = datetime.now().isoformat(timespec='seconds')
        
        return jsonify({
            'code': code,
            'old_mode': old_mode,
            'new_mode': new_mode,
            'confirmed_by': 'user',
            'timestamp': timestamp
        }), 200
    
    except Exception as e:
        logger.error(f"Error switching mode: {e}")
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/api/v2/manual_exit/<code>', methods=['POST'])
@require_api_key
def manual_exit(code: str):
    """
    手动平仓 (人工干预)
    
    Body (可选):
    {
        "quantity": 100  # 可选，默认全部
    }
    
    Response:
    {
        "code": "0700.HK",
        "action": "SELL",
        "quantity": 100,
        "price": 401.52,
        "pnl": 1912.00,
        "status": "executed"
    }
    """
    try:
        # 解析请求体 (可选)
        quantity = None
        if request.is_json and request.get_json():
            data = request.get_json()
            quantity = data.get('quantity')
            
            # 验证数量
            if quantity is not None:
                try:
                    quantity = int(quantity)
                    if quantity <= 0:
                        return jsonify({
                            'error': 'Invalid quantity',
                            'message': 'Quantity must be a positive integer'
                        }), 400
                except (ValueError, TypeError):
                    return jsonify({
                        'error': 'Invalid quantity',
                        'message': 'Quantity must be an integer'
                    }), 400
        
        engine = get_decision_engine()
        if engine is None:
            return jsonify({
                'error': 'Engine not initialized',
                'message': 'Decision engine is not available'
            }), 503
        
        # 执行手动平仓
        result = engine.manual_exit(code, quantity=quantity)
        
        # 格式化响应
        if isinstance(result, dict):
            response_data = result
        elif hasattr(result, 'to_dict'):
            response_data = result.to_dict()
        else:
            # 从对象构建
            response_data = {
                'code': code,
                'action': 'SELL',
                'quantity': getattr(result, 'quantity', quantity or 0),
                'price': getattr(result, 'price', 0.0),
                'pnl': getattr(result, 'pnl', 0.0),
                'status': getattr(result, 'status', 'executed'),
            }
        
        # 确保必要字段
        response_data.setdefault('code', code)
        response_data.setdefault('action', 'SELL')
        response_data.setdefault('status', 'executed')
        
        # 格式化数值
        if 'price' in response_data:
            response_data['price'] = round(response_data['price'], 2)
        if 'pnl' in response_data:
            response_data['pnl'] = round(response_data['pnl'], 2)
        
        return jsonify(response_data), 200
    
    except Exception as e:
        logger.error(f"Error in manual exit: {e}")
        return jsonify({
            'error': 'Internal server error',
            'message': str(e)
        }), 500


@app.route('/api/v2/health', methods=['GET'])
def health_check():
    """健康检查端点 (无需认证)"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(timespec='seconds')
    }), 200


@app.errorhandler(404)
def not_found(error):
    """404 错误处理"""
    return jsonify({
        'error': 'Not found',
        'message': 'The requested endpoint does not exist'
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """500 错误处理"""
    return jsonify({
        'error': 'Internal server error',
        'message': 'An unexpected error occurred'
    }), 500


def create_app(testing: bool = False) -> Flask:
    """创建 Flask 应用 (用于测试)"""
    app.config['TESTING'] = testing
    return app


if __name__ == '__main__':
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 启动服务器
    port = int(os.getenv('API_PORT', '8900'))
    logger.info(f"Starting Decision API server on port {port}")
    logger.info(f"API Key authentication enabled")
    
    app.run(host='0.0.0.0', port=port, debug=False)
