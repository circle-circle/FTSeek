# FTSeek v2.1 回调地址配置指南

**问题**: 钉钉按钮无法访问回调 URL

**原因**: CALLBACK_EXTERNAL_URL 配置的外网地址无法访问

---

## 🔧 解决方案

### 方案 1: 本地测试 (快速验证)

适用于本地开发和测试：

```bash
# 1. 确认 FastAPI 服务运行
curl http://localhost:8899/health?token=test123

# 2. 手动访问回调 URL
# 在浏览器中打开:
http://localhost:8899/api/confirm?signal_id=test_1772184118&token=test123

# 3. 或使用 curl 测试
curl "http://localhost:8899/api/confirm?signal_id=test_1772184118&token=test123"
```

**优点**: 快速，无需配置
**缺点**: 钉钉按钮无法直接访问

---

### 方案 2: 使用服务器公网 IP (生产环境)

如果您的服务器有公网 IP：

```bash
# 1. 获取服务器公网 IP
curl ifconfig.me

# 2. 修改 .env 配置
vim /opt/ftseek/FTSeek/.env

# 修改为:
CALLBACK_EXTERNAL_URL=http://YOUR_SERVER_IP:8899

# 3. 重启 FastAPI 服务
pkill -f uvicorn
cd /opt/ftseek/FTSeek
nohup python3 -m uvicorn app.server:app --host 0.0.0.0 --port 8899 > logs/api.log 2>&1 &

# 4. 配置防火墙
# 确保 8899 端口开放
firewall-cmd --add-port=8899/tcp --permanent
firewall-cmd --reload
```

**优点**: 稳定，适合生产
**缺点**: 需要公网 IP 和防火墙配置

---

### 方案 3: 使用内网穿透工具 (推荐测试)

#### 使用 ngrok:

```bash
# 1. 安装 ngrok
wget https://bin.equinox.io/c/4VmDzA7iaHb/ngrok-stable-linux-amd64.zip
unzip ngrok-stable-linux-amd64.zip

# 2. 启动 ngrok (替换为你的 token)
./ngrok config add-authtoken YOUR_NGROK_TOKEN
./ngrok http 8899

# 3. 获取 ngrok 地址 (类似 https://xxx.ngrok.io)
# 修改 .env:
CALLBACK_EXTERNAL_URL=https://YOUR_NGROK_ID.ngrok.io

# 4. 重启 FastAPI 并重新发送信号
pkill -f uvicorn
source .env
nohup python3 -m uvicorn app.server:app --host 0.0.0.0 --port 8899 > logs/api.log 2>&1 &
```

**优点**: 无需公网 IP，快速
**缺点**: 免费版本有带宽限制

#### 使用 frp:

```bash
# 1. 配置 frpc.ini
[common]
server_addr = YOUR_FRP_SERVER_IP
server_port = 7000

[ftseek]
type = http
local_port = 8899
custom_domains = ftseek.yourdomain.com

# 2. 启动 frpc
./frpc -c frpc.ini

# 3. 修改 .env
CALLBACK_EXTERNAL_URL=http://ftseek.yourdomain.com
```

**优点**: 可控，适合长期使用
**缺点**: 需要自己的服务器

---

## 📝 当前配置检查

```bash
cd /opt/ftseek/FTSeek

# 查看当前配置
cat .env | grep CALLBACK

# 测试本地访问
curl http://localhost:8899/health?token=test123

# 查看服务状态
ps aux | grep uvicorn

# 查看日志
tail -f logs/api.log
```

---

## 🧪 测试回调

### 测试 1: 健康检查
```bash
curl http://localhost:8899/health?token=test123
# 预期：{"status":"ok"}
```

### 测试 2: 确认接口
```bash
curl "http://localhost:8899/api/confirm?signal_id=test_123&token=test123"
# 预期：HTML 确认页面或错误信息
```

### 测试 3: 信号列表
```bash
curl "http://localhost:8899/api/signals?token=test123"
# 预期：信号列表 JSON
```

---

## 📊 当前状态

| 服务 | 状态 | 地址 |
|------|------|------|
| FastAPI | ✅ 运行中 | http://localhost:8899 |
| OpenD | ✅ 运行中 | 127.0.0.1:11111 |
| 钉钉推送 | ✅ 正常 | Webhook 已配置 |
| 外网访问 | ❌ 未配置 | 需要配置 CALLBACK_EXTERNAL_URL |

---

## ⏭️ 下一步

### 立即测试 (本地模式):

```bash
# 1. 获取信号 ID
SIGNAL_ID="test_1772184118"

# 2. 直接调用回调接口
curl "http://localhost:8899/api/confirm?signal_id=$SIGNAL_ID&token=test123"

# 3. 查看执行结果
tail -f logs/api.log
```

### 配置外网访问后:

```bash
# 1. 修改 .env
CALLBACK_EXTERNAL_URL=http://YOUR_SERVER_IP:8899

# 2. 重启服务
pkill -f uvicorn
source .env
nohup python3 -m uvicorn app.server:app --host 0.0.0.0 --port 8899 > logs/api.log 2>&1 &

# 3. 发送新信号测试
python3 test_dingtalk.py
```

---

## 📞 快速帮助

**问题**: 按钮点击无响应
**解决**: 检查 CALLBACK_EXTERNAL_URL 是否可访问

**问题**: 回调 404 错误
**解决**: 检查 FastAPI 服务是否运行

**问题**: Token 验证失败
**解决**: 确认 API_TOKEN 配置正确

---

**选择适合的方案配置后即可正常联动!** 🚀
