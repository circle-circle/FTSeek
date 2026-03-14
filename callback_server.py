#!/usr/bin/env python3.8
"""
FTSeek 钉钉回调服务器

启动 Flask Webhook 服务器接收钉钉回调
"""
import os
import sys
import logging
import time
from flask import Flask, request, jsonify

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.v4.risk.dingtalk_callback import DingTalkCallbackHandler
from app.v4.risk.overnight_hold import get_overnight_hold_evaluator as get_evaluator

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("ftseek.callback_server")

# 创建 Flask 应用
app = Flask(__name__)

# 初始化回调处理器 (从环境变量获取 Secret)
APP_SECRET = os.getenv('DINGTALK_APP_SECRET', '')
handler = DingTalkCallbackHandler(
    app_secret=APP_SECRET,
    callback_handler=None  # 稍后设置
)

logger.info(f"回调处理器已初始化，Secret 配置：{'是' if APP_SECRET else '否'}")


def handle_decision(decision: dict):
    """处理用户决策"""
    code = decision.get('code')
    user_decision = decision.get('decision')
    
    logger.info(f"收到决策：{code} - {user_decision}")
    
    # 更新隔夜持仓评估器
    try:
        evaluator = get_evaluator()
        if code in evaluator.pending_requests:
            req = evaluator.pending_requests[code]
            req.user_response = user_decision
            req.confirmed = True
            req.confirmed_time = time.time()
            logger.info(f"已更新隔夜持仓请求：{code}")
    except Exception as e:
        logger.error(f"更新评估器失败：{e}")


# 设置回调处理函数
handler.callback_handler = handle_decision


@app.route('/dingtalk/callback', methods=['POST'])
def dingtalk_callback():
    """钉钉回调接口"""
    try:
        # 获取签名
        signature = request.headers.get('X-DingTalk-Signature')
        timestamp = request.headers.get('X-DingTalk-Timestamp')
        
        # 获取请求体
        body = request.get_data(as_text=True)
        callback_data = request.get_json()
        
        logger.info(f"收到回调：{callback_data}")
        
        # 验证签名 (如果有)
        if signature and timestamp:
            if not handler.verify_signature(signature, timestamp, body):
                logger.warning("签名验证失败")
                return jsonify({'error': 'Invalid signature'}), 401
        
        # 处理回调
        decision = handler.process_callback(callback_data)
        
        if decision:
            logger.info(f"决策处理成功：{decision}")
            return jsonify({
                'success': True,
                'message': 'Decision received',
                'code': decision.get('code'),
                'decision': decision.get('decision')
            })
        else:
            return jsonify({'error': 'Unknown callback'}), 400
            
    except Exception as e:
        logger.error(f"回调处理失败：{e}")
        return jsonify({'error': str(e)}), 500


@app.route('/dingtalk/status', methods=['GET'])
def callback_status():
    """状态检查"""
    return jsonify({
        'status': 'running',
        'pending_decisions': handler.get_status(),
        'secret_configured': bool(APP_SECRET)
    })


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({'status': 'healthy'})


if __name__ == '__main__':
    port = int(os.getenv('DINGTALK_WEBHOOK_PORT', 5000))
    
    logger.info(f"启动钉钉回调服务器，端口：{port}")
    logger.info(f"回调 URL: http://0.0.0.0:{port}/dingtalk/callback")
    
    app.run(host='0.0.0.0', port=port, debug=False)
