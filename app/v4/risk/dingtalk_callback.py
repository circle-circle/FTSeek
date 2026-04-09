"""
DingTalkCallback - 钉钉回调处理器

功能:
- 接收钉钉卡片按钮点击回调
- 解析用户决策 (保留/平仓)
- 更新隔夜持仓请求状态
- 触发后续操作
"""
import json
import hmac
import hashlib
import base64
import time
import logging
from typing import Dict, Optional, Any, Callable
from datetime import datetime
from flask import Flask, request, jsonify

logger = logging.getLogger("ftseek.v4.dingtalk_callback")
logger.propagate = False  # 防止日志重复


class DingTalkCallbackHandler:
    """钉钉回调处理器"""
    
    def __init__(
        self,
        app_secret: str,
        callback_handler: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        """
        初始化回调处理器
        
        Args:
            app_secret: 钉钉应用密钥
            callback_handler: 回调处理函数
        """
        self.app_secret = app_secret
        self.callback_handler = callback_handler
        self.pending_decisions: Dict[str, Dict[str, Any]] = {}
        self.logger = logging.getLogger("ftseek.v4.dingtalk_callback")
logger.propagate = False  # 防止日志重复
    
    def verify_signature(
        self,
        signature: str,
        timestamp: str,
        body: str
    ) -> bool:
        """
        验证钉钉签名
        
        Args:
            signature: 签名
            timestamp: 时间戳
            body: 请求体
            
        Returns:
            是否验证通过
        """
        try:
            # 构建待签名字符串
            string_to_sign = f"{timestamp}\n{body}"
            
            # 计算签名
            hmac_code = hmac.new(
                self.app_secret.encode('utf-8'),
                string_to_sign.encode('utf-8'),
                digestmod=hashlib.sha256
            ).digest()
            
            expected_signature = base64.b64encode(hmac_code).decode('utf-8')
            
            # 验证
            is_valid = hmac.compare_digest(signature, expected_signature)
            
            self.logger.debug(f"签名验证：{'通过' if is_valid else '失败'}")
            return is_valid
            
        except Exception as e:
            self.logger.error(f"签名验证失败：{e}")
            return False
    
    def process_callback(
        self,
        callback_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        处理回调数据
        
        Args:
            callback_data: 回调数据
            
        Returns:
            解析后的决策数据
        """
        try:
            # 解析卡片回调
            if 'value' in callback_data:
                value = callback_data['value']
                
                if value.get('action') == 'overnight_hold':
                    decision = {
                        'code': value.get('code'),
                        'decision': value.get('decision'),  # "hold" or "close"
                        'card_id': value.get('card_id'),
                        'user_id': callback_data.get('user_id'),
                        'timestamp': callback_data.get('timestamp'),
                        'receive_time': time.time()
                    }
                    
                    # 存储决策
                    self.pending_decisions[decision['card_id']] = decision
                    
                    self.logger.info(
                        f"收到隔夜持仓决策：{decision['code']} - "
                        f"{decision['decision']}"
                    )
                    
                    # 调用回调处理函数
                    if self.callback_handler:
                        self.callback_handler(decision)
                    
                    return decision
            
            self.logger.warning(f"未知回调类型：{callback_data}")
            return None
            
        except Exception as e:
            self.logger.error(f"处理回调失败：{e}")
            return None
    
    def get_decision(self, card_id: str) -> Optional[Dict[str, Any]]:
        """
        获取决策
        
        Args:
            card_id: 卡片 ID
            
        Returns:
            决策数据
        """
        return self.pending_decisions.get(card_id)
    
    def cleanup(self, max_age: int = 3600):
        """
        清理过期决策
        
        Args:
            max_age: 最大保留时间 (秒)
        """
        current_time = time.time()
        
        expired = [
            card_id for card_id, decision in self.pending_decisions.items()
            if current_time - decision['receive_time'] > max_age
        ]
        
        for card_id in expired:
            del self.pending_decisions[card_id]
        
        if expired:
            self.logger.info(f"清理{len(expired)}个过期决策")
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            'pending_count': len(self.pending_decisions),
            'pending_cards': list(self.pending_decisions.keys()),
        }


# 全局实例
callback_handler: Optional[DingTalkCallbackHandler] = None


def get_callback_handler() -> DingTalkCallbackHandler:
    """获取回调处理器单例"""
    global callback_handler
    if callback_handler is None:
        # 需要配置 app_secret
        callback_handler = DingTalkCallbackHandler(app_secret="")
    return callback_handler


# ============================================================
#  Flask Webhook 服务器 (可选)
# ============================================================

def create_callback_app(handler: DingTalkCallbackHandler) -> Flask:
    """
    创建 Flask Webhook 应用
    
    Args:
        handler: 回调处理器
        
    Returns:
        Flask 应用
    """
    app = Flask(__name__)
    
    @app.route('/dingtalk/callback', methods=['POST'])
    def dingtalk_callback():
        """钉钉回调接口"""
        try:
            # 获取签名
            signature = request.headers.get('X-DingTalk-Signature')
            timestamp = request.headers.get('X-DingTalk-Timestamp')
            
            # 获取请求体
            body = request.get_data(as_text=True)
            callback_data = json.loads(body)
            
            # 验证签名
            if signature and timestamp:
                if not handler.verify_signature(signature, timestamp, body):
                    return jsonify({'error': 'Invalid signature'}), 401
            
            # 处理回调
            decision = handler.process_callback(callback_data)
            
            if decision:
                return jsonify({
                    'success': True,
                    'decision': decision
                })
            else:
                return jsonify({'error': 'Unknown callback'}), 400
                
        except Exception as e:
            logger.error(f"回调处理失败：{e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/dingtalk/status', methods=['GET'])
    def callback_status():
        """状态检查"""
        return jsonify(handler.get_status())
    
    return app


# ============================================================
#  集成到隔夜持仓系统
# ============================================================

def setup_overnight_hold_callback(
    evaluator: Any,
    app_secret: str
) -> DingTalkCallbackHandler:
    """
    设置隔夜持仓回调
    
    Args:
        evaluator: OvernightHoldEvaluator 实例
        app_secret: 钉钉应用密钥
        
    Returns:
        回调处理器
    """
    def handle_decision(decision: Dict[str, Any]):
        """处理决策"""
        code = decision.get('code')
        user_decision = decision.get('decision')
        
        if code in evaluator.pending_requests:
            request = evaluator.pending_requests[code]
            request.user_response = user_decision
            request.confirmed = True
            request.confirmed_time = time.time()
            
            logger.info(f"更新隔夜持仓请求：{code} - {user_decision}")
    
    handler = DingTalkCallbackHandler(
        app_secret=app_secret,
        callback_handler=handle_decision
    )
    
    logger.info("隔夜持仓回调处理器已设置")
    return handler
