# 🔐 FTSeek 安全最佳实践

**版本**: v1.0  
**更新日期**: 2026-02-26

---

## ✅ 已实施的安全措施

### 1. 前端安全

- ✅ **不显示默认账户** - 登录页面不显示用户名密码
- ✅ **密码加密传输** - SHA256 + Salt
- ✅ **Cookie 安全** - HttpOnly, SameSite
- ✅ **Session 管理** - 7 天有效期，自动过期
- ✅ **权限控制** - 管理员/普通用户分离

### 2. 后端安全

- ✅ **API Token 鉴权** - 所有 API 需要 Token
- ✅ **速率限制** - 防止暴力请求
- ✅ **输入验证** - 防止注入攻击
- ✅ **日志审计** - 记录所有操作

### 3. 网络安全

- ✅ **CORS 配置** - 限制跨域访问
- ✅ **响应头安全** - X-Frame-Options 等
- ✅ **端口保护** - 仅开放必要端口

---

## 🔒 账户安全管理

### 默认账户（首次启动时创建）

系统会创建默认管理员账户，但**不会在任何界面显示**。

**获取方式**:
1. 查看系统启动日志
2. 或联系系统部署人员
3. 或从配置文件中获取

### 修改密码

登录后应尽快修改密码：

```bash
# 管理员添加新用户
curl -X POST "http://localhost:8899/auth/users/add" \
  -H "Cookie: session_id=YOUR_SESSION" \
  -F "username=newuser" \
  -F "password=strong_password_here" \
  -F "role=user"
```

### 密码强度要求

- ✅ 至少 8 个字符
- ✅ 包含大小写字母
- ✅ 包含数字
- ✅ 包含特殊字符
- ❌ 不使用常见密码

---

## 🛡️ 推荐安全配置

### 1. HTTPS 配置

生产环境**必须**配置 HTTPS：

```nginx
server {
    listen 443 ssl http2;
    server_name aitz.cc;
    
    ssl_certificate /etc/ssl/certs/aitz.cc.crt;
    ssl_certificate_key /etc/ssl/private/aitz.cc.key;
    
    # 安全配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    
    # 安全头
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    
    location / {
        proxy_pass http://127.0.0.1:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

# HTTP 重定向到 HTTPS
server {
    listen 80;
    server_name aitz.cc;
    return 301 https://$server_name$request_uri;
}
```

### 2. 防火墙配置

```bash
# 只允许信任 IP 访问
iptables -A INPUT -p tcp --dport 8899 -s 信任 IP -j ACCEPT
iptables -A INPUT -p tcp --dport 8899 -j DROP

# 或使用 firewalld
firewall-cmd --permanent --add-port=8899/tcp
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="信任 IP" port port="8899" protocol="tcp" accept'
firewall-cmd --reload
```

### 3. Fail2Ban 配置

防止暴力破解：

```ini
# /etc/fail2ban/jail.local
[ftseek-auth]
enabled = true
port = 8899
filter = ftseek-auth
logpath = /opt/ftseek/FTSeek/logs/ftseek.log
maxretry = 5
bantime = 3600
findtime = 600
```

---

## 📋 安全清单

### 部署前检查

- [ ] 修改默认密码
- [ ] 配置 HTTPS
- [ ] 设置防火墙规则
- [ ] 配置 Fail2Ban
- [ ] 禁用不必要的端口
- [ ] 更新系统和依赖

### 日常运维

- [ ] 定期检查日志
- [ ] 监控异常登录
- [ ] 定期备份数据
- [ ] 更新安全补丁
- [ ] 审计用户权限

### 应急响应

- [ ] 制定应急预案
- [ ] 准备备份系统
- [ ] 配置警报通知
- [ ] 记录联系人列表

---

## ⚠️ 安全警告

### 不要做的事

1. ❌ **不要**在前端显示账户信息
2. ❌ **不要**使用弱密码
3. ❌ **不要**共享账户
4. ❌ **不要**在生产环境使用 HTTP
5. ❌ **不要**开放不必要的端口
6. ❌ **不要**忽略安全更新

### 应该做的事

1. ✅ **应该**定期修改密码
2. ✅ **应该**启用 HTTPS
3. ✅ **应该**配置防火墙
4. ✅ **应该**监控日志
5. ✅ **应该**定期备份
6. ✅ **应该**更新系统

---

## 🚨 安全事件响应

### 发现异常登录

1. 立即修改密码
2. 检查登录日志
3. 查看操作记录
4. 必要时重置所有账户

### 发现数据泄露

1. 立即停止服务
2. 隔离受影响系统
3. 调查泄露原因
4. 通知相关人员
5. 修复漏洞
6. 恢复服务

### 发现暴力破解

1. 检查 Fail2Ban 日志
2. 封禁攻击 IP
3. 加强密码策略
4. 考虑启用双因素认证

---

## 📚 相关文档

- [Web 前端使用指南](WEB_FRONTEND_GUIDE.md)
- [风险管理模块](RISK_MANAGEMENT_MODULE.md)
- [系统配置指南](../SECURITY.md)

---

**安全是持续的过程，不是一次性的配置。**

**保持警惕，定期审查，及时更新。**
