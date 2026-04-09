# 🔐 FTSeek 安全配置指南

## 快速开始

### 1. 生成 API Token

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

复制输出的 Token，然后配置到以下位置：

### 2. 配置环境变量

**方式 A: systemd 服务 (推荐)**

编辑 `/opt/ftseek/FTSeek/scripts/ftseek.service`:

```ini
[Service]
Environment="API_TOKEN=你刚才生成的 token"
Environment="DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=你的 token"
Environment="DINGTALK_SECRET=你的加签 Secret"
```

重新加载并重启：

```bash
cp /opt/ftseek/FTSeek/scripts/ftseek.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart ftseek
```

**方式 B: 环境变量文件**

```bash
cd /opt/ftseek/FTSeek
cp .env.example .env
# 编辑 .env 文件，填入你的配置
source .env
```

### 3. 验证配置

```bash
# 检查 API Token 是否已设置
curl "http://localhost:8899/api/status?token=你的 token"

# 如果返回 403 或错误，说明配置未生效
```

---

## 安全配置清单

### ✅ 必须配置

| 配置项 | 环境变量 | 默认值 | 风险等级 |
|--------|----------|--------|----------|
| API Token | `API_TOKEN` | ❌ 无 (启动失败) | 🔴 高危 |
| 钉钉 Webhook | `DINGTALK_WEBHOOK_URL` | ❌ 无 | 🟡 中危 |
| 钉钉 Secret | `DINGTALK_SECRET` | ❌ 无 | 🟡 中危 |

### ⚠️ 建议配置

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| 交易环境 | `TRADE_ENV` | `1` (模拟) | 生产环境设为 `0` |
| 最大订单金额 | `MAX_ORDER_AMOUNT` | `50000.0` | 防止误操作 |
| 开发模式 | `FTSEEK_DEV_MODE` | `false` | 生产环境必须为 `false` |

---

## 网络安全

### 防火墙配置

如果服务器有公网 IP，建议限制 API 端口访问：

```bash
# 只允许本地访问
iptables -A INPUT -p tcp --dport 8899 -s 127.0.0.1 -j ACCEPT
iptables -A INPUT -p tcp --dport 8899 -j DROP

# 或只允许特定 IP
iptables -A INPUT -p tcp --dport 8899 -s 你的信任 IP -j ACCEPT
iptables -A INPUT -p tcp --dport 8899 -j DROP
```

### HTTPS 配置 (可选)

如果通过公网访问 API，建议配置反向代理 + HTTPS：

```nginx
# Nginx 配置示例
server {
    listen 443 ssl;
    server_name ftseek.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 审计日志

交易日志位置：`/opt/ftseek/FTSeek/logs/ftseek.log`

关键日志关键字：

- `🔔 信号已确认` - 用户确认了交易信号
- `准备下单` - 开始执行交易
- `下单成功` / `下单失败` - 交易结果
- `速率限制触发` - 检测到异常请求频率

---

## 常见问题

### Q: 启动时报 "API_TOKEN 环境变量未设置"

A: 这是正常的安全保护。请按照上面的步骤生成并配置 Token。

### Q: 可以在代码中硬编码 Token 吗？

A: ❌ 绝对不要。硬编码的 Token 会泄露到版本控制系统，导致安全风险。

### Q: Token 泄露了怎么办？

A: 立即：
1. 生成新的 Token
2. 更新配置并重启服务
3. 检查日志是否有异常访问记录

---

## 安全最佳实践

1. **定期轮换 Token** - 建议每 3 个月更换一次
2. **最小权限原则** - 只开放必要的网络端口
3. **监控日志** - 定期检查异常访问
4. **模拟环境测试** - 新策略先在模拟环境验证
5. **备份配置** - 保存配置文件的加密备份

---

## 联系支持

发现安全漏洞请报告给项目负责人。
