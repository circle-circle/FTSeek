"""
FTSeek 钉钉回调服务器

处理用户在钉钉消息中的点击操作（确认/延期/忽略）

使用 Flask 提供 HTTP 接口，配合 ngrok 暴露公网地址
"""
import os
import sys
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载环境变量
if os.path.exists('.env'):
    with open('.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                key, value = line.strip().split('=', 1)
                os.environ.setdefault(key, value)

from app.storage.database import get_database
from app.notifier.dingtalk import DingTalkNotifier

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('ftseek.callback')

app = Flask(__name__)

# HTML 响应模板
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>FTSeek - {{ action_text }}</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #f5f5f5;
        }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .success { border-left: 4px solid #52c41a; }
        .warning { border-left: 4px solid #faad14; }
        .info { border-left: 4px solid #1890ff; }
        h1 { margin: 0 0 20px 0; font-size: 24px; }
        .signal-info { 
            background: #f9f9f9; 
            padding: 15px; 
            border-radius: 8px;
            margin: 20px 0;
        }
        .signal-info p { margin: 8px 0; }
        .label { color: #666; }
        .value { font-weight: 600; color: #333; }
        .timestamp { color: #999; font-size: 12px; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="card {{ status_class }}">
        <h1>{{ emoji }} {{ action_text }}</h1>
        <div class="signal-info">
            <p><span class="label">信号 ID:</span> <span class="value">{{ signal_id }}</span></p>
            <p><span class="label">股票:</span> <span class="value">{{ code }} {{ name }}</span></p>
            <p><span class="label">操作:</span> <span class="value">{{ action }}</span></p>
            <p><span class="label">状态:</span> <span class="value">{{ status }}</span></p>
        </div>
        <p class="timestamp">处理时间：{{ timestamp }}</p>
    </div>
</body>
</html>
'''

def get_signal(signal_id: str) -> dict:
    """从数据库获取信号信息"""
    db = get_database()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM signals WHERE id = ?',
            (signal_id,)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
    return None

def update_signal_status(signal_id: str, status: str, confirmed_at: str = None):
    """更新信号状态"""
    db = get_database()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        if confirmed_at:
            cursor.execute(
                'UPDATE signals SET status = ?, confirmed_at = ? WHERE id = ?',
                (status, confirmed_at, signal_id)
            )
        else:
            cursor.execute(
                'UPDATE signals SET status = ? WHERE id = ?',
                (status, signal_id)
            )

@app.route('/api/confirm/<signal_id>')
def confirm(signal_id: str):
    """处理确认执行"""
    logger.info(f'确认执行：{signal_id}')
    
    signal = get_signal(signal_id)
    if not signal:
        return render_template_string(
            HTML_TEMPLATE,
            action_text='信号不存在',
            emoji='❌',
            status_class='warning',
            signal_id=signal_id,
            code='-',
            name='',
            action='确认执行',
            status='信号不存在',
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
    
    # 更新状态
    confirmed_at = datetime.now().isoformat()
    update_signal_status(signal_id, 'confirmed', confirmed_at)
    
    # 发送钉钉通知
    try:
        notifier = DingTalkNotifier()
        notifier.send_text_message(
            f'✅ 信号已确认执行\\n\\n'
            f'股票：{signal["code"]} {signal.get("name", "")}\\n'
            f'操作：{signal["action"]}\\n'
            f'价格：{signal["price"]}'
        )
    except Exception as e:
        logger.error(f'发送通知失败：{e}')
    
    return render_template_string(
        HTML_TEMPLATE,
        action_text='已确认执行',
        emoji='✅',
        status_class='success',
        signal_id=signal_id,
        code=signal['code'],
        name=signal.get('name', ''),
        action=signal['action'],
        status='已确认，等待执行',
        timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )

@app.route('/api/defer/<signal_id>')
def defer(signal_id: str):
    """处理延期"""
    logger.info(f'延期：{signal_id}')
    
    signal = get_signal(signal_id)
    if not signal:
        return render_template_string(
            HTML_TEMPLATE,
            action_text='信号不存在',
            emoji='❌',
            status_class='warning',
            signal_id=signal_id,
            code='-',
            name='',
            action='延期',
            status='信号不存在',
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
    
    # 更新状态
    update_signal_status(signal_id, 'deferred')
    
    # 发送钉钉通知
    try:
        notifier = DingTalkNotifier()
        notifier.send_text_message(
            f'⏰ 信号已延期\\n\\n'
            f'股票：{signal["code"]} {signal.get("name", "")}\\n'
            f'操作：{signal["action"]}'
        )
    except Exception as e:
        logger.error(f'发送通知失败：{e}')
    
    return render_template_string(
        HTML_TEMPLATE,
        action_text='已延期',
        emoji='⏰',
        status_class='warning',
        signal_id=signal_id,
        code=signal['code'],
        name=signal.get('name', ''),
        action=signal['action'],
        status='已延期，稍后重新评估',
        timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )

@app.route('/api/ignore/<signal_id>')
def ignore(signal_id: str):
    """处理忽略"""
    logger.info(f'忽略：{signal_id}')
    
    signal = get_signal(signal_id)
    if not signal:
        return render_template_string(
            HTML_TEMPLATE,
            action_text='信号不存在',
            emoji='❌',
            status_class='warning',
            signal_id=signal_id,
            code='-',
            name='',
            action='忽略',
            status='信号不存在',
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
    
    # 更新状态
    update_signal_status(signal_id, 'ignored')
    
    # 发送钉钉通知
    try:
        notifier = DingTalkNotifier()
        notifier.send_text_message(
            f'❌ 信号已忽略\\n\\n'
            f'股票：{signal["code"]} {signal.get("name", "")}\\n'
            f'操作：{signal["action"]}'
        )
    except Exception as e:
        logger.error(f'发送通知失败：{e}')
    
    return render_template_string(
        HTML_TEMPLATE,
        action_text='已忽略',
        emoji='❌',
        status_class='info',
        signal_id=signal_id,
        code=signal['code'],
        name=signal.get('name', ''),
        action=signal['action'],
        status='已忽略，不再推送',
        timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )

@app.route('/health')
def health():
    """健康检查"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.getenv('CALLBACK_PORT', 5000))
    logger.info(f'启动回调服务器，端口：{port}')
    logger.info(f'本地访问：http://localhost:{port}')
    logger.info(f'健康检查：http://localhost:{port}/health')
    app.run(host='0.0.0.0', port=port, debug=False)
