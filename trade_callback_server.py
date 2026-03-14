#!/usr/bin/env python3.8
"""
FTSeek 交易确认回调服务器 (带持仓管理)
端口：8901 (nginx 代理到 8900)
"""
import os
import sys
import time
import logging
from flask import Flask, request, jsonify, Response

sys.path.insert(0, os.path.dirname(__file__))

from app.v4.trader.opend_executor import get_opend_executor
from app.v4.trader.position_manager import get_position_manager
from app.v4.notifier.dingtalk_v4 import DingTalkNotifierV4

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger("ftseek.trade_callback_server")

# Flask 应用
app = Flask(__name__)

# 加载环境变量
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

# 初始化组件
DINGTALK_WEBHOOK = os.getenv('DINGTALK_WEBHOOK_URL', '')
DINGTALK_SECRET = os.getenv('DINGTALK_SECRET', '')

notifier = DingTalkNotifierV4(webhook_url=DINGTALK_WEBHOOK, secret=DINGTALK_SECRET) if DINGTALK_WEBHOOK else None
executor = get_opend_executor(simulate=True)
position_mgr = get_position_manager()

# 待确认请求存储
pending_requests = {}

# 超时自动确认配置
AUTO_CONFIRM_TIMEOUT = 600  # 10 分钟超时


def execute_trade(code: str, action: str, quantity: int, price: float, 
                  confirm_type: str = "manual") -> dict:
    """
    执行交易
    
    Args:
        code: 股票代码
        action: 买卖方向
        quantity: 数量
        price: 价格
        confirm_type: 确认类型 ("manual" or "auto")
    """
    logger.info(f"执行交易：{code} {action} {quantity}股 @ {price} (确认方式：{confirm_type})")
    
    if not executor.connect():
        return {"success": False, "message": "OpenD 连接失败"}
    
    result = executor.execute_order(code=code, action=action, quantity=quantity, price=price)
    
    # 更新持仓管理
    if result.get('success'):
        if action in ['BUY', 'SELL']:
            position_mgr.add_position(code, action, quantity, price, price)
        elif action == 'CLOSE':
            position_mgr.close_position(code, quantity, price)
    
    # 发送通知
    if notifier:
        if result.get('success'):
            confirm_text = "人工确认" if confirm_type == "manual" else "超时自动确认"
            msg = f"✅ **交易执行成功** ({confirm_text})\n\n📊 股票：{code}\n方向：{action}\n数量：{quantity}股\n成交价：{result.get('filled_price', price):.2f}"
            notifier.send(msg, msg_type='markdown')
        else:
            msg = f"❌ **交易执行失败**\n\n📊 股票：{code}\n\n⚠️ {result.get('message', '未知错误')}"
            notifier.send(msg, msg_type='markdown')
    
    return result


def create_trade_confirm_text(code: str, name: str, action: str,
                               price: float, qty: int,
                               stop_loss: float = None,
                               target: float = None) -> tuple:
    """生成交易确认文本消息（钉钉 Webhook 机器人支持）"""
    action_text = "买入" if action == "BUY" else "卖出"
    icon = "🟢" if action == "BUY" else "🔴"
    
    risk_info = ""
    if stop_loss:
        risk_info += f"\n止损：{stop_loss:.2f}"
    if target:
        risk_info += f"\n目标：{target:.2f}"
    
    card_id = f"trade_{code}_{int(time.time())}"
    
    text = f"""{icon}【交易确认】{code} {name}

方向：{action_text}
价格：${price:.2f}
数量：{qty}股
{risk_info}

━━━━━━━━━━━━━━━━━━━━━━
⏰ 有效期：5 分钟
━━━━━━━━━━━━━━━━━━━━━━

✅ 确认交易请回复：确认 {card_id}
❌ 取消交易请回复：忽略 {card_id}

或直接回复"确认"/"忽略"
"""
    return text, card_id


def create_response_html(title: str, content: list, success: bool = True) -> str:
    """创建响应 HTML"""
    icon = "✅" if success else "❌"
    color = "#52c41a" if success else "#f5222d"
    
    items = "\n".join([f"<p>{item}</p>" for item in content])
    
    return f"""
    <html>
    <head>
        <meta charset='utf-8'>
        <meta name='viewport' content='width=device-width, initial-scale=1.0'>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; }}
            .container {{ max-width: 400px; margin: 0 auto; }}
            h1 {{ color: {color}; font-size: 24px; }}
            p {{ color: #666; font-size: 16px; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class='container'>
            <h1>{icon} {title}</h1>
            {items}
        </div>
    </body>
    </html>
    """


@app.route('/trade/callback', methods=['GET', 'POST'])
def trade_callback():
    """交易确认回调 (支持 GET 和 POST)"""
    try:
        # GET 请求 (钉钉卡片按钮)
        if request.method == 'GET':
            card_id = request.args.get('card_id', '')
            decision = request.args.get('decision', '').lower()
            code = request.args.get('code', 'HK.00700')
            action = request.args.get('action', 'BUY')
            quantity = request.args.get('quantity', 100, type=int)
            price = request.args.get('price', 520.0, type=float)
            
            logger.info(f"收到 GET 回调：{code} {decision} {action}")
            
            # 存储请求
            pending_requests[card_id] = {
                "code": code, "action": action, "quantity": quantity, "price": price,
                "decision": decision, "timestamp": time.time()
            }
            
            # 检查持仓状态
            pos = position_mgr.get_position(code)
            position_info = f"持仓：{pos.direction} {pos.quantity}股 @ {pos.entry_price:.2f}" if pos else "无持仓"
            
            # 处理不同决策
            if decision == 'confirm':
                # 根据 action 执行不同操作
                if action == 'BUY':
                    if pos and pos.direction == 'SELL':
                        # 反向平仓
                        result = execute_trade(code, 'CLOSE', min(quantity, pos.quantity), price)
                        title = "平仓已确认"
                        content = [f"<b>股票</b>：{code}", f"<b>操作</b>：平仓", f"<b>数量</b>：{quantity}股", f"<b>状态</b>：{'成功' if result.get('success') else '失败'}"]
                    else:
                        # 买入/加仓
                        result = execute_trade(code, 'BUY', quantity, price)
                        title = "买入已确认"
                        content = [f"<b>股票</b>：{code}", f"<b>操作</b>：{'加仓' if pos else '买入'}", f"<b>数量</b>：{quantity}股", f"<b>状态</b>：{'成功' if result.get('success') else '失败'}"]
                
                elif action == 'SELL':
                    if pos and pos.direction == 'BUY':
                        # 反向平仓
                        result = execute_trade(code, 'CLOSE', min(quantity, pos.quantity), price)
                        title = "平仓已确认"
                        content = [f"<b>股票</b>：{code}", f"<b>操作</b>：平仓", f"<b>数量</b>：{quantity}股", f"<b>状态</b>：{'成功' if result.get('success') else '失败'}"]
                    else:
                        # 卖空/加仓
                        result = execute_trade(code, 'SELL', quantity, price)
                        title = "卖空已确认"
                        content = [f"<b>股票</b>：{code}", f"<b>操作</b>：{'加仓' if pos else '卖空'}", f"<b>数量</b>：{quantity}股", f"<b>状态</b>：{'成功' if result.get('success') else '失败'}"]
                
                elif action == 'CLOSE':
                    # 平仓
                    result = execute_trade(code, 'CLOSE', quantity, price)
                    title = "平仓已确认"
                    content = [f"<b>股票</b>：{code}", f"<b>操作</b>：平仓", f"<b>数量</b>：{quantity}股", f"<b>状态</b>：{'成功' if result.get('success') else '失败'}"]
                
                else:
                    title = "未知操作"
                    content = [f"<b>操作</b>：{action}"]
                    result = {"success": False}
                
                content.insert(1, f"<b>持仓</b>：{position_info}")
                return Response(create_response_html(title, content, result.get('success')), 
                              mimetype='text/html; charset=utf-8')
            
            elif decision == 'ignore':
                # 忽略
                if notifier:
                    notifier.send(f"❌ 已取消操作：{code} {action}", msg_type='text')
                
                content = [
                    f"<b>股票</b>：{code}",
                    f"<b>操作</b>：已取消",
                    f"<b>持仓</b>：{position_info}"
                ]
                return Response(create_response_html("操作已取消", content, False), 
                              mimetype='text/html; charset=utf-8')
            
            else:
                logger.warning(f"未知决策：{decision}")
                content = [
                    f"<b>决策</b>：{decision}",
                    f"<b>状态</b>：不支持的决策类型"
                ]
                return Response(create_response_html("未知决策", content, False), 
                              mimetype='text/html; charset=utf-8'), 400
        
        # POST 请求 (钉钉回调)
        elif request.method == 'POST':
            callback_data = request.get_json()
            value = callback_data.get('value', {})
            
            if value.get('action') != 'trade_confirm':
                return jsonify({'error': 'Unknown action'}), 400
            
            card_id = value.get('card_id')
            decision = value.get('decision', '').lower()
            code = value.get('code')
            
            logger.info(f"收到 POST 回调：{code} {decision}")
            
            if decision in ['confirm', 'ignore']:
                return jsonify({'success': True, 'message': f'Decision {decision} received'})
            else:
                return jsonify({'error': 'Unknown decision'}), 400
    
    except Exception as e:
        logger.error(f"回调处理失败：{e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/trade/position', methods=['GET'])
def get_position():
    """获取持仓信息"""
    code = request.args.get('code', '')
    
    if code:
        pos = position_mgr.get_position(code)
        if pos:
            return jsonify(pos.to_dict())
        else:
            return jsonify({'error': 'No position'})
    else:
        return jsonify(position_mgr.to_dict())


@app.route('/trade/status', methods=['GET'])
def trade_status():
    """状态检查"""
    return jsonify({
        'status': 'running',
        'pending_requests': len(pending_requests),
        'positions': position_mgr.to_dict(),
        'executor_status': executor.get_status(),
    })


def check_pending_confirmations():
    """检查待确认请求，处理超时自动确认"""
    current_time = time.time()
    expired = []
    
    for card_id, req in pending_requests.items():
        elapsed = current_time - req.get('timestamp', current_time)
        if elapsed >= AUTO_CONFIRM_TIMEOUT and not req.get('processed', False):
            expired.append((card_id, req, elapsed))
    
    for card_id, req, elapsed in expired:
        logger.info(f"超时自动确认：{card_id} (等待{elapsed:.0f}秒)")
        
        code = req.get('code', '')
        action = req.get('action', 'BUY')
        quantity = req.get('quantity', 100)
        price = req.get('price', 0.0)
        
        # 执行交易（自动确认）
        result = execute_trade(code, action, quantity, price, confirm_type="auto")
        
        # 标记为已处理
        req['processed'] = True
        req['decision'] = 'auto_confirm'
        req['executed_at'] = current_time
        
        logger.info(f"超时自动确认执行结果：{result.get('success', False)}")


@app.route('/trade/pending', methods=['GET'])
def get_pending():
    """获取待确认列表"""
    current_time = time.time()
    pending = []
    for card_id, req in pending_requests.items():
        if not req.get('processed', False):
            elapsed = current_time - req.get('timestamp', current_time)
            remaining = AUTO_CONFIRM_TIMEOUT - elapsed
            pending.append({
                'card_id': card_id,
                'code': req.get('code'),
                'action': req.get('action'),
                'elapsed': elapsed,
                'remaining': remaining,
                'status': 'pending'
            })
    return jsonify({'pending': pending, 'count': len(pending)})


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        'status': 'healthy', 
        'service': 'ftseek-trade-callback', 
        'port': 8900,
        'pending_count': len([r for r in pending_requests.values() if not r.get('processed', False)]),
        'auto_confirm_timeout': AUTO_CONFIRM_TIMEOUT,
    })


if __name__ == '__main__':
    import argparse
    import threading
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8901, help='服务端口')
    args = parser.parse_args()
    
    # 启动后台线程定期检查超时
    def background_check():
        while True:
            time.sleep(60)  # 每分钟检查一次
            try:
                check_pending_confirmations()
            except Exception as e:
                logger.error(f"超时检查异常：{e}")
    
    check_thread = threading.Thread(target=background_check, daemon=True)
    check_thread.start()
    
    logger.info(f"超时自动确认已启用：{AUTO_CONFIRM_TIMEOUT}秒")
    
    app.run(host='0.0.0.0', port=args.port, debug=False)
