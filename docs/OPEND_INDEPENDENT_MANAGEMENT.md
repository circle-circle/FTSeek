# OpenD 独立管理方案

## 📋 方案概述

本方案实现OpenD完全独立于FTSeek运行，具备以下特性：

✅ **独立生命周期** - OpenD作为独立systemd服务，不受FTSeek启停影响  
✅ **自动监控恢复** - 每2分钟自动检查，异常时自动重启  
✅ **安全重启机制** - 带冷却期和次数限制，避免频繁重启  
✅ **进程+端口双验证** - 准确判断OpenD健康状态  
✅ **完整日志记录** - 所有操作留痕，便于问题排查  

---

## 🚀 安装步骤

### 1. 设置脚本执行权限

```bash
chmod +x /opt/ftseek/scripts/opend_auto_recovery.sh
chmod +x /opt/ftseek/scripts/opend_safe_start.sh
chmod +x /opt/ftseek/scripts/opend_safe_stop.sh
```

### 2. 方案选择（二选一）

#### 方案A：Crontab定时检查（推荐，轻量级）

```bash
# 编辑crontab
crontab -e

# 添加以下行（每2分钟检查一次）
*/2 * * * * /opt/ftseek/scripts/opend_auto_recovery.sh >> /opt/ftseek/logs/opend_recovery_cron.log 2>&1

# 保存后验证
crontab -l | grep opend
```

**优点：**
- 轻量级，无额外服务
- 配置简单
- 适合大多数场景

#### 方案B：Systemd服务管理（更专业）

```bash
# 1. 复制service文件到systemd目录
sudo cp /opt/ftseek/FTSeek/config/opend.service /etc/systemd/system/opend.service

# 2. 重载systemd配置
sudo systemctl daemon-reload

# 3. 停止当前OpenD（如果正在运行）
/opt/ftseek/scripts/opend_safe_stop.sh

# 4. 启用并启动OpenD服务
sudo systemctl enable opend
sudo systemctl start opend

# 5. 检查状态
sudo systemctl status opend

# 6. 查看日志
sudo journalctl -u opend -f
```

**优点：**
- 更专业的进程管理
- 自动重启策略更精细
- 与系统服务统一管理

---

## 📊 验证安装

### 验证Crontab方案

```bash
# 1. 确认crontab已配置
crontab -l | grep opend_auto_recovery

# 2. 手动执行一次测试
/opt/ftseek/scripts/opend_auto_recovery.sh

# 3. 查看日志
tail -f /opt/ftseek/logs/opend_recovery.log

# 4. 模拟OpenD故障测试
pkill -9 FutuOpenD
# 等待2分钟，检查是否自动恢复
tail -f /opt/ftseek/logs/opend_recovery.log
```

### 验证Systemd方案

```bash
# 1. 检查服务状态
sudo systemctl status opend

# 2. 查看实时日志
sudo journalctl -u opend -f

# 3. 测试自动重启
sudo systemctl stop opend
# 等待10秒，systemd会自动重启
sudo systemctl status opend
```

---

## 🔧 配置说明

### 自动恢复脚本配置

编辑 `/opt/ftseek/scripts/opend_auto_recovery.sh` 可调整以下参数：

```bash
# OpenD路径
OPEND_DIR="/opt/ftseek/Futu_OpenD_10.1.6108_Centos7/Futu_OpenD_10.1.6108_Centos7"

# 监控端口
OPEND_PORT=11111

# 重启冷却期（秒）- 5分钟内不重复重启
RESTART_COOLDOWN=300

# 最大重启次数 - 超过后停止自动重启
MAX_RESTART_ATTEMPTS=3
```

### 日志文件位置

| 日志类型 | 路径 | 说明 |
|---------|------|------|
| 自动恢复日志 | `/opt/ftseek/logs/opend_recovery.log` | 监控和重启记录 |
| OpenD标准输出 | `/opt/ftseek/Futu_OpenD_10.1.6108_Centos7/Futu_OpenD_10.1.6108_Centos7/logs/opend_stdout.log` | OpenD运行日志 |
| Systemd日志 | `journalctl -u opend` | 仅systemd方案 |
| Cron日志 | `/opt/ftseek/logs/opend_recovery_cron.log` | 仅crontab方案 |

---

## 🎯 运维命令速查

### 查看OpenD状态

```bash
# 快速检查
ps aux | grep FutuOpenD | grep -v grep
ss -tlnp | grep 11111

# 详细检查
/opt/ftseek/scripts/opend_auto_recovery.sh
tail -20 /opt/ftseek/logs/opend_recovery.log
```

### 手动重启OpenD

```bash
# 方法1：使用原脚本
/opt/ftseek/start_opend.sh

# 方法2：使用新脚本（更安全）
/opt/ftseek/scripts/opend_safe_stop.sh
sleep 3
/opt/ftseek/scripts/opend_safe_start.sh

# 方法3：systemd方案
sudo systemctl restart opend
```

### 查看监控日志

```bash
# 实时查看自动恢复日志
tail -f /opt/ftseek/logs/opend_recovery.log

# 查看最近的检查记录
grep -E "(✅|❌|🔧)" /opt/ftseek/logs/opend_recovery.log | tail -20

# 查看重启历史
grep "重启" /opt/ftseek/logs/opend_recovery.log
```

### 临时禁用自动恢复

```bash
# 注释掉crontab中的行
crontab -e
# 在opend_auto_recovery.sh行首添加 #

# 或者停止systemd服务
sudo systemctl stop opend
sudo systemctl disable opend
```

---

## ⚠️ 注意事项

### 1. FTSeek配置确认

确保FTSeek的systemd服务包含以下配置（已配置）：

```ini
Environment="OPEND_MANAGED=external"
KillMode=process  # 确保不杀子进程
```

验证：
```bash
systemctl cat ftseek | grep OPEND_MANAGED
```

### 2. 重启冷却期

- 默认5分钟内不重复重启，避免频繁重启影响交易
- 达到3次重启上限后，需手动干预
- 重置计数器：`echo "0" > /tmp/opend_restart_count`

### 3. OpenD登录状态

- OpenD重启后可能需要重新输入验证码
- 建议在工作日交易时段前检查OpenD状态
- 可在早上添加一次手动检查

### 4. 内存监控

脚本会记录OpenD内存使用，超过2GB会告警。如发现内存持续增长：
```bash
# 查看内存趋势
grep "内存" /opt/ftseek/logs/opend_recovery.log | tail -20

# 手动重启释放内存
/opt/ftseek/scripts/opend_safe_stop.sh
sleep 3
/opt/ftseek/scripts/opend_safe_start.sh
```

---

## 📈 监控建议

### 添加钉钉告警（可选）

在 `opend_auto_recovery.sh` 中的重启成功后添加：

```bash
# 发送钉钉通知
curl -s "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "msgtype": "text",
    "text": {
      "content": "⚠️ OpenD已自动重启，请检查交易状态\n时间: '"$(date)"'"
    }
  }'
```

### 添加健康检查API（可选）

FTSeek可以添加API端点供外部监控：

```python
# 在 app/server.py 中添加
@app.get("/api/health/opend")
def check_opend_health():
    import subprocess
    result = subprocess.run(["pgrep", "-f", "FutuOpenD"], capture_output=True)
    return {"opend_running": result.returncode == 0}
```

---

## 🔍 故障排查

### 问题1：OpenD频繁重启

```bash
# 查看重启原因
grep -E "(❌|重启)" /opt/ftseek/logs/opend_recovery.log | tail -30

# 检查系统资源
free -h
df -h
dmesg | grep -i oom

# 检查OpenD日志
tail -100 /opt/ftseek/Futu_OpenD_10.1.6108_Centos7/Futu_OpenD_10.1.6108_Centos7/logs/opend_stdout.log
```

### 问题2：自动恢复未生效

```bash
# 检查crontab是否运行
systemctl status crond

# 检查cron日志
grep opend_auto_recovery /opt/ftseek/logs/opend_recovery_cron.log

# 手动测试脚本
bash -x /opt/ftseek/scripts/opend_auto_recovery.sh
```

### 问题3：FTSeek无法连接OpenD

```bash
# 确认OpenD运行
ps aux | grep FutuOpenD
ss -tlnp | grep 11111

# 检查FTSeek日志
tail -50 /opt/ftseek/FTSeek/logs/ftseek.log | grep -E "(OpenD|ECONNREFUSED)"

# 重启FTSeek（不影响OpenD）
systemctl restart ftseek
```

---

## 📝 变更日志

- **2026-04-20**: 创建OpenD独立管理方案
  - 自动监控恢复脚本
  - 安全启动/停止脚本
  - Systemd服务配置
  - 完整文档

---

## 🆘 技术支持

如遇问题，请提供以下信息：

```bash
# 收集诊断信息
echo "=== OpenD进程 ==="
ps aux | grep FutuOpenD | grep -v grep

echo "=== 端口状态 ==="
ss -tlnp | grep 11111

echo "=== 最近日志 ==="
tail -30 /opt/ftseek/logs/opend_recovery.log

echo "=== Crontab配置 ==="
crontab -l | grep opend

echo "=== 系统资源 ==="
free -h
```
