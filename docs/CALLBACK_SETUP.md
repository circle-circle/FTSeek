# FTSeek v3.0 回调接口配置指南

## 问题说明

钉钉消息中的按钮点击后无法访问，因为按钮链接需要**公网可访问的 URL**。

---

## 解决方案

### 方案 1: ngrok 隧道 (推荐，5 分钟搞定)

**适用场景**: 开发环境、测试环境、个人使用

**步骤**:

1. **安装 ngrok**
   ```bash
   # Linux
   curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc
   echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
   sudo apt update && sudo apt install ngrok
   
   # 或使用 npm
   npm install -g ngrok
   ```

2. **启动回调服务器**
   ```bash
   cd /opt/ftseek/FTSeek
   export CALLBACK_PORT=9000
   python3 app/callback_server.py &
   ```

3. **启动 ngrok 隧道**
   ```bash
   ngrok http 9000
   ```
   
   输出示例:
   ```
   Forwarding: https://abc123.ngrok.io -> http://localhost:9000
   ```

4. **更新钉钉按钮链接**
   
   编辑 `app/notifier/dingtalk.py`，将按钮 URL 改为：
   ```python
   'btns': [
       {'title': '✅ 确认执行', 'actionURL': 'https://abc123.ngrok.io/api/confirm/{signal_id}'},
       {'title': '⏰ 延期', 'actionURL': 'https://abc123.ngrok.io/api/defer/{signal_id}'},
       {'title': '❌ 忽略', 'actionURL': 'https://abc123.ngrok.io/api/ignore/{signal_id}'}
   ]
   ```

5. **重启服务**
   ```bash
   pkill -f callback_server
   cd /opt/ftseek/FTSeek
   python3 app/callback_server.py &
   ```

---

### 方案 2: 数据库手动确认 (简化方案)

**适用场景**: 临时使用、不频繁交易

**步骤**:

1. **查看待确认信号**
   ```bash
   cd /opt/ftseek/FTSeek
   sqlite3 data/ftseek.db "SELECT id, code, action, status FROM signals WHERE status='pending';"
   ```

2. **手动更新状态**
   ```bash
   # 确认执行
   sqlite3 data/ftseek.db "UPDATE signals SET status='confirmed', confirmed_at=datetime('now') WHERE id='signal-xxx';"
   
   # 延期
   sqlite3 data/ftseek.db "UPDATE signals SET status='deferred' WHERE id='signal-xxx';"
   
   # 忽略
   sqlite3 data/ftseek.db "UPDATE signals SET status='ignored' WHERE id='signal-xxx';"
   ```

3. **触发执行**
   ```bash
   python3 scripts/execute_confirmed_signals.py
   ```

---

### 方案 3: 钉钉事件订阅 (生产环境)

**适用场景**: 生产部署、企业使用

**步骤**:

1. **创建钉钉企业内部应用**
   - 访问 https://open-dev.dingtalk.com
   - 创建应用，获取 AppKey 和 AppSecret

2. **配置事件订阅**
   - 订阅 `im_message` 事件
   - 配置回调 URL (需要公网 IP 或域名)

3. **实现消息处理**
   ```python
   @app.route('/dingtalk/callback', methods=['POST'])
   def dingtalk_callback():
       data = request.json
       # 处理用户回复
       if data['text'] == '1':
           confirm_signal()
       elif data['text'] == '2':
           defer_signal()
       elif data['text'] == '3':
           ignore_signal()
   ```

4. **部署到服务器**
   - 使用 Nginx 反向代理
   - 配置 HTTPS 证书
   - 使用 systemd 管理进程

---

## 快速测试 (本地)

如果只想测试按钮功能，可以：

1. **启动回调服务器**
   ```bash
   cd /opt/ftseek/FTSeek
   export CALLBACK_PORT=9000
   python3 app/callback_server.py
   ```

2. **本地访问测试**
   ```
   http://localhost:9000/health
   http://localhost:9000/api/confirm/test-001
   http://localhost:9000/api/defer/test-001
   http://localhost:9000/api/ignore/test-001
   ```

3. **手机/电脑同网访问**
   ```
   http://<你的 IP>:9000/api/confirm/test-001
   ```

---

## 当前推荐

**模拟盘观察期间**，建议使用：

### 方案 A: ngrok (5 分钟)
- 优点：完整功能，按钮可点击
- 缺点：免费版有流量限制

### 方案 B: 数据库手动确认 (临时)
- 优点：无需额外配置
- 缺点：需要手动操作

---

## 联系支持

如需帮助，请查看：
- `QUICKSTART_V3.md` - 快速启动指南
- `docs/RISK_CONFIG.md` - 风控配置
- `docs/STRATEGY_CONFIG.md` - 策略配置
