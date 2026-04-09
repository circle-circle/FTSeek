# FTSeek Web 界面访问指南

**更新日期**: 2026-03-31  
**服务状态**: ✅ 运行中

---

## 📊 可用 Web 界面

FTSeek 提供以下 Web 界面用于查看交易详情和复盘数据：

### 1. 模拟盘监控看板 (Simulation Dashboard)

**用途**: 查看模拟交易详情、持仓、盈亏

**访问地址**: `http://<服务器 IP>:8899/simulation/`

**文件位置**: `/opt/ftseek/FTSeek/app/dashboard/simulation_dashboard.html`

**功能**:
- ✅ 实时持仓监控
- ✅ 交易记录查看
- ✅ 盈亏统计
- ✅ 图表可视化 (ECharts + Chart.js)

---

### 2. 风险仪表盘 (Risk Dashboard)

**用途**: 查看风控指标、仓位限制、告警信息

**访问地址**: `http://<服务器 IP>:8899/static/risk_dashboard_pro_v2.html`

**文件位置**: `/opt/ftseek/FTSeek/app/static/risk_dashboard_pro_v2.html`

**功能**:
- ✅ 风险指标监控
- ✅ 仓位限制展示
- ✅ 告警信息推送
- ✅ 专业量化指标

---

### 3. 主页 (Index)

**用途**: 系统状态概览

**访问地址**: `http://<服务器 IP>:8899/static/index_v2.html`

**文件位置**: `/opt/ftseek/FTSeek/app/static/index_v2.html`

---

## 🔌 API 接口

### 复盘数据 API

**端点**: `/api/review/`

**用途**: 获取复盘数据

**示例**:
```bash
# 获取复盘列表
curl http://localhost:8899/api/review/list

# 获取复盘详情
curl http://localhost:8899/api/review/detail?id=xxx
```

### 风险指标 API

**端点**: `/api/risk/`

**用途**: 获取量化指标

**示例**:
```bash
# 获取风险指标
curl http://localhost:8899/api/risk/metrics

# 获取持仓风险
curl http://localhost:8899/api/risk/positions
```

### 回测数据 API

**端点**: `/api/backtest/`

**用途**: 获取回测数据

**示例**:
```bash
# 获取回测结果
curl http://localhost:8899/api/backtest/results
```

---

## 🚀 访问步骤

### 步骤 1: 确认服务运行状态

```bash
# 检查 FTSeek 服务状态
systemctl status ftseek.service

# 预期输出:
# ● ftseek.service - FTSeek 港股策略交易服务
#    Active: active (running)
```

### 步骤 2: 确认端口监听

```bash
# 检查 8899 端口是否监听
netstat -tlnp | grep 8899

# 或使用 ss 命令
ss -tlnp | grep 8899
```

### 步骤 3: 配置防火墙 (如需外网访问)

```bash
# 开放 8899 端口
ufw allow 8899/tcp

# 或使用 iptables
iptables -A INPUT -p tcp --dport 8899 -j ACCEPT
```

### 步骤 4: 浏览器访问

在浏览器中输入:
```
http://<服务器 IP>:8899/simulation/
```

**服务器 IP 获取**:
- 本地访问：`http://localhost:8899/simulation/`
- 外网访问：`http://39.97.58.4:8899/simulation/` (根据实际 IP 替换)

---

## 🔐 安全建议

### 1. 使用 HTTPS

生产环境建议配置 Nginx 反向代理 + HTTPS:

```nginx
server {
    listen 443 ssl;
    server_name ftseek.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8899;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 2. 配置认证

FTSeek 支持 API Token 认证:

```bash
# 在 .env 中配置
API_TOKEN=your-secret-token-here
```

```bash
# 访问 API 时携带 Token
curl -H "Authorization: Bearer your-token" http://localhost:8899/api/review/list
```

### 3. 限制访问 IP

```bash
# 使用防火墙限制访问 IP
ufw allow from 192.168.1.0/24 to any port 8899
```

---

## 📁 文件位置汇总

| 界面 | 文件路径 | 访问路径 |
|------|---------|---------|
| 模拟盘看板 | `/opt/ftseek/FTSeek/app/dashboard/simulation_dashboard.html` | `/simulation/` |
| 风险仪表盘 | `/opt/ftseek/FTSeek/app/static/risk_dashboard_pro_v2.html` | `/static/risk_dashboard_pro_v2.html` |
| 主页 | `/opt/ftseek/FTSeek/app/static/index_v2.html` | `/static/index_v2.html` |
| 登录页 | `/opt/ftseek/FTSeek/app/static/login.html` | `/static/login.html` |

---

## 🔧 故障排查

### 问题 1: 无法访问 Web 界面

**检查步骤**:
```bash
# 1. 检查服务状态
systemctl status ftseek.service

# 2. 检查端口监听
netstat -tlnp | grep 8899

# 3. 检查防火墙
ufw status

# 4. 查看日志
tail -f /opt/ftseek/FTSeek/logs/ftseek.log
```

### 问题 2: 页面加载空白

**可能原因**:
- CDN 资源加载失败 (ECharts, Chart.js)
- 浏览器缓存问题

**解决方法**:
- 检查网络连接
- 清除浏览器缓存
- 使用无痕模式访问

### 问题 3: API 返回 401 错误

**原因**: 需要认证

**解决方法**:
```bash
# 在请求头中添加 Token
curl -H "Authorization: Bearer your-token" http://localhost:8899/api/review/list
```

---

## 📖 相关文档

| 文档 | 位置 |
|------|------|
| 统一决策引擎用户指南 | `/opt/ftseek/FTSeek/docs/trading_decision_user_guide.md` |
| 交易决策操作指南 | `/opt/ftseek/FTSeek/docs/trading_decision_ops_guide.md` |
| 配置规范 | `/opt/ftseek/FTSeek/CONFIG_SPEC.md` |

---

## 🎯 快速访问链接

**本地访问**:
- 模拟盘看板：http://localhost:8899/simulation/
- 风险仪表盘：http://localhost:8899/static/risk_dashboard_pro_v2.html
- 系统主页：http://localhost:8899/static/index_v2.html

**外网访问** (假设服务器 IP 为 39.97.58.4):
- 模拟盘看板：http://39.97.58.4:8899/simulation/
- 风险仪表盘：http://39.97.58.4:8899/static/risk_dashboard_pro_v2.html
- 系统主页：http://39.97.58.4:8899/static/index_v2.html

---

**服务端口**: 8899  
**服务状态**: ✅ 运行中  
**最后更新**: 2026-03-31 16:58
