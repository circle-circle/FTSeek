#!/usr/bin/env python3
"""
FTSeek 钉钉消息处理器

监听钉钉群消息回复，处理用户确认操作
- 回复 "1" = 确认执行
- 回复 "2" = 延期
- 回复 "3" = 忽略

使用轮询方式检查新消息 (简化方案)
"""
import os
import sys
import time
import logging
from datetime import datetime

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('ftseek.message_handler')

# 待确认信号缓存 (实际应从数据库查询 pending 状态的信号)
pending_signals = {}

def load_pending_signals():
    """加载待确认的信号"""
    db = get_database()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT id, code, name, action, price, strategy, confidence 
               FROM signals 
               WHERE status = 'pending' 
               ORDER BY created_at DESC 
               LIMIT 10'''
        )
        for row in cursor.fetchall():
            pending_signals[row['id']] = dict(row)
    logger.info(f'加载了 {len(pending_signals)} 个待确认信号')

def handle_reply(user_id: str, message: str):
    """处理用户回复"""
    message = message.strip()
    
    # 解析回复
    if message == '1':
        action = 'confirm'
        action_text = '确认执行'
        emoji = '✅'
        new_status = 'confirmed'
    elif message == '2':
        action = 'defer'
        action_text = '延期'
        emoji = '⏰'
        new_status = 'deferred'
    elif message == '3':
        action = 'ignore'
        action_text = '忽略'
        emoji = '❌'
        new_status = 'ignored'
    elif message in ['帮助', 'help', 'h']:
        send_help_message()
        return
    else:
        logger.info(f'未知回复：{message} (用户：{user_id})')
        return
    
    # 获取最新的待确认信号
    if not pending_signals:
        load_pending_signals()
    
    if not pending_signals:
        send_text_message(f'{emoji} 当前没有待确认的信号')
        return
    
    # 处理第一个待确认信号 (简化方案)
    signal_id = list(pending_signals.keys())[0]
    signal = pending_signals[signal_id]
    
    # 更新数据库
    db = get_database()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        if action == 'confirm':
            cursor.execute(
                'UPDATE signals SET status = ?, confirmed_at = ? WHERE id = ?',
                (new_status, datetime.now().isoformat(), signal_id)
            )
        else:
            cursor.execute(
                'UPDATE signals SET status = ? WHERE id = ?',
                (new_status, signal_id)
            )
    
    logger.info(f'用户 {user_id} {action_text} 信号 {signal_id}')
    
    # 发送确认消息
    send_text_message(
        f'{emoji} 已{action_text}\n\n'
        f'信号 ID: {signal_id}\n'
        f'股票：{signal["code"]} {signal.get("name", "")}\n'
        f'操作：{signal["action"]}\n'
        f'价格：{signal["price"]}\n'
        f'状态：{new_status}'
    )
    
    # 从缓存移除
    del pending_signals[signal_id]

def send_text_message(text: str):
    """发送钉钉文本消息"""
    notifier = DingTalkNotifier()
    notifier.send_text_message(text)

def send_help_message():
    """发送帮助消息"""
    help_text = '''## 📖 使用帮助

### 确认信号

当收到买卖信号推送后，回复数字：
- **1** = ✅ 确认执行
- **2** = ⏰ 延期
- **3** = ❌ 忽略

### 查询命令

- `状态` - 查看系统状态
- `持仓` - 查看当前持仓
- `信号` - 查看待确认信号
- `帮助` - 显示此帮助

### 示例

收到信号后：
```
🔔 FTSeek 买入信号
📈 腾讯控股 (HK.00700)
...

你：1  (回复)

机器人：✅ 已确认执行
```

---
*FTSeek v3.0 - 模拟盘观察*'''
    
    send_text_message(help_text)

def main():
    """主循环 (轮询模式)"""
    logger.info('启动消息处理器')
    logger.info('监听钉钉回复：1=确认，2=延期，3=忽略')
    
    # 加载待确认信号
    load_pending_signals()
    
    # 轮询间隔 (秒)
    poll_interval = 30
    
    while True:
        try:
            # 实际项目中这里应该调用钉钉 API 获取新消息
            # 由于钉钉机器人 API 限制，简化方案是：
            # 1. 使用回调服务器 (需要 ngrok)
            # 2. 使用钉钉开放平台的事件订阅
            # 3. 手动在数据库更新状态
            
            # 这里仅做演示，实际使用需要配置钉钉事件订阅
            time.sleep(poll_interval)
            
            # 重新加载待确认信号
            load_pending_signals()
            
        except KeyboardInterrupt:
            logger.info('停止消息处理器')
            break
        except Exception as e:
            logger.error(f'错误：{e}')
            time.sleep(poll_interval)

if __name__ == '__main__':
    print('=== FTSeek 钉钉消息处理器 ===')
    print()
    print('⚠️  注意：由于钉钉机器人 API 限制，消息监听需要配置事件订阅')
    print()
    print('推荐方案:')
    print('1. 使用回调服务器 + ngrok (完整功能)')
    print('2. 手动在数据库更新信号状态 (简化方案)')
    print('3. 使用钉钉开放平台事件订阅 (生产环境)')
    print()
    print('当前演示模式，按 Ctrl+C 退出')
    print()
    main()
