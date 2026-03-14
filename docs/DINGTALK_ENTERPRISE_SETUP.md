# 钉钉企业内部应用配置指南

## 📱 创建钉钉企业内部应用

### 步骤 1: 登录钉钉开发者后台

1. 访问：https://open-dev.dingtalk.com/
2. 使用钉钉扫码登录（需要企业管理员权限）
3. 进入"应用开发" → "企业内部开发"

### 步骤 2: 创建应用

1. 点击"创建应用"
2. 填写应用信息：
   - **应用名称**: FTSeek 交易助手
   - **应用图标**: 上传交易相关图标
   - **应用描述**: 自动化交易信号推送与确认
3. 点击"创建"

### 步骤 3: 获取应用凭证

创建成功后，在"凭证与基础信息"页面获取：
- **AppKey**: `dingxxxxxxxxxxxxx`
- **AppSecret**: `xxxxxxxxxxxxxxxxxxxxxxxx`

⚠️ **重要**: 保存 AppKey 和 AppSecret 到 `.env` 文件

### 步骤 4: 配置服务器回调 URL

在"开发管理" → "事件订阅"页面：

1. **回调 URL**: `http://39.97.58.4:8900/dingtalk/callback`
2. **消息接收模式**: 流式模式
3. **加密密钥**: 系统自动生成（保存为 `DINGTALK_AES_KEY`）
4. **Token**: 自定义设置（保存为 `DINGTALK_TOKEN`）

5. 点击"保存"后，钉钉会发送验证请求
6. 需要实现验证接口返回正确响应

### 步骤 5: 配置事件订阅

订阅以下事件：
- ✅ `im_chat` - 单聊消息
- ✅ `im_group` - 群聊消息（可选）
- ✅ `btn_callback` - 按钮回调事件

### 步骤 6: 配置服务器 IP 白名单

在"安全设置"页面添加服务器 IP：
- `39.97.58.4` (服务器公网 IP)
- `127.0.0.1` (本地测试)

---

## 🔧 修改 FTSeek 配置

### 1. 更新 .env 文件

```bash
vim /opt/ftseek/FTSeek/.env
```

添加以下配置：

```ini
# ============================================================
#  钉钉企业应用配置
# ============================================================
DINGTALK_APP_KEY=dingxxxxxxxxxxxxx
DINGTALK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
DINGTALK_AGENT_ID=1234567890

# 回调配置
DINGTALK_AES_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DINGTALK_TOKEN=your_custom_token
DINGTALK_WEBHOOK_PORT=8900
DINGTALK_CALLBACK_URL=/dingtalk/callback

# 推送模式：enterprise(企业应用) 或 webhook(机器人)
DINGTALK_PUSH_MODE=enterprise
```

### 2. 重启回调服务

```bash
# 停止旧服务
pkill -f trade_callback_server.py

# 启动新服务
cd /opt/ftseek/FTSeek
nohup python3.8 trade_callback_server.py --port 8901 > /tmp/trade_callback_8901.log 2>&1 &

# 验证服务
curl http://localhost:8901/health
```

---

## 🧪 测试回调功能

### 测试 1: 验证回调 URL

```bash
# 钉钉后台点击"验证"按钮
# 查看日志确认验证请求
tail -f /tmp/trade_callback_8901.log
```

### 测试 2: 发送测试消息

```bash
cd /opt/ftseek/FTSeek
python3 scripts/test_dingtalk_enterprise.py
```

### 测试 3: 点击按钮回调

1. 收到交互式卡片消息
2. 点击"确认"或"忽略"按钮
3. 查看回调日志确认收到事件

---

## 📊 日志查看

```bash
# 回调服务日志
tail -f /tmp/trade_callback_8901.log

# Nginx 日志
tail -f /var/log/nginx/ftseek_callback_access.log
tail -f /var/log/nginx/ftseek_callback_error.log

# 系统日志
journalctl -u ftseek-callback -f
```

---

## ⚠️ 常见问题

### 1. 回调 URL 验证失败

**原因**: 服务器未正确响应验证请求

**解决**: 确保回调接口实现验证逻辑：
```python
@app.route('/dingtalk/callback', methods=['GET', 'POST'])
def callback():
    if request.method == 'GET':
        # 验证回调 URL
        token = request.args.get('token')
        return jsonify({'token': token})
```

### 2. 按钮点击无响应

**原因**: 事件订阅未配置或 AppSecret 错误

**解决**: 
- 检查事件订阅是否包含 `btn_callback`
- 验证 AppKey 和 AppSecret 配置正确

### 3. 消息推送失败

**原因**: access_token 过期或权限不足

**解决**:
- access_token 有效期 2 小时，需要自动刷新
- 检查应用权限配置

---

## 🔐 安全建议

1. **保护 AppSecret**: 不要泄露到代码仓库
2. **验证签名**: 所有回调请求验证签名
3. **IP 白名单**: 限制可访问回调 URL 的 IP
4. **Token 刷新**: 实现 access_token 自动刷新机制

---

## 📞 获取帮助

- 钉钉开放文档：https://open.dingtalk.com/document/
- FTSeek 支持：查看 `/opt/ftseek/FTSeek/docs/`
