# FTSeek 服务管理配置报告

**更新日期**：2026-03-31 15:40  
**状态**：✅ 配置完成

---

## 配置概述

FTSeek 服务已配置为使用 **systemd** 管理，确保：
1. **进程唯一性**：通过进程锁文件 `/run/ftseek.lock` 确保
2. **自动重启**：服务异常时自动重启
3. **开机自启**：服务设置为开机自启动

---

## 服务状态

### 当前运行状态

```bash
$ systemctl status ftseek.service

● ftseek.service - FTSeek 港股策略交易服务
   Loaded: loaded (/etc/systemd/system/ftseek.service; enabled; ...)
   Active: active (running) since Tue 2026-03-31 15:39:39 CST
 Main PID: 3138 (python3.8)
```

### 进程信息

```bash
$ ps aux | grep "python3.8 -m app.main" | grep -v grep

root 3138 ... /usr/bin/python3.8 -m app.main
```

**结果**：✅ 只有单一进程运行

### 进程锁

```bash
$ ls -la /run/ftseek.lock

-rw-r--r-- 1 root root 0 Mar 31 15:39 /run/ftseek.lock
```

**结果**：✅ 进程锁文件存在

---

## 管理命令

### 启动服务

```bash
# 方式 1: 使用 systemctl (推荐)
systemctl start ftseek.service

# 方式 2: 使用启动脚本
./start.sh
```

### 停止服务

```bash
# 方式 1: 使用 systemctl (推荐)
systemctl stop ftseek.service

# 方式 2: 使用停止脚本
./stop.sh
```

### 重启服务

```bash
# 方式 1: 使用 systemctl (推荐)
systemctl restart ftseek.service

# 方式 2: 使用重启脚本
./restart.sh
```

### 查看状态

```bash
systemctl status ftseek.service
```

### 查看日志

```bash
# 实时日志
tail -f /opt/ftseek/FTSeek/logs/ftseek.log

# systemd 日志
journalctl -u ftseek.service -f
```

---

## 服务文件配置

### 位置

`/etc/systemd/system/ftseek.service`

### 关键配置

```ini
[Unit]
Description=FTSeek 港股策略交易服务
# 进程锁：确保只有一个实例运行
ConditionPathExists=!/run/ftseek.lock

[Service]
Type=simple
# 启动前创建进程锁
ExecStartPre=/bin/touch /run/ftseek.lock
ExecStart=/usr/bin/python3.8 -m app.main
# 停止后删除进程锁
ExecStopPost=/bin/rm -f /run/ftseek.lock

# 自动重启策略
Restart=on-failure
RestartSec=10
StartLimitBurst=3
StartLimitInterval=300

[Install]
WantedBy=multi-user.target
```

---

## 脚本文件

| 脚本 | 路径 | 功能 |
|------|------|------|
| start.sh | /opt/ftseek/FTSeek/start.sh | 启动服务 |
| stop.sh | /opt/ftseek/FTSeek/stop.sh | 停止服务 |
| restart.sh | /opt/ftseek/FTSeek/restart.sh | 重启服务 |

---

## 进程锁机制

### 工作原理

1. **启动前**：检查 `/run/ftseek.lock` 是否存在
2. **存在**：拒绝启动（防止多实例）
3. **不存在**：创建锁文件，启动服务
4. **停止后**：删除锁文件

### 手动清理锁文件

```bash
rm -f /run/ftseek.lock
```

**警告**：仅在服务停止后手动清理

---

## 自动重启策略

| 配置项 | 值 | 说明 |
|--------|-----|------|
| Restart | on-failure | 仅在异常退出时重启 |
| RestartSec | 10 | 重启前等待 10 秒 |
| StartLimitBurst | 3 | 5 分钟内最多重启 3 次 |
| StartLimitInterval | 300 | 重启计数窗口期 300 秒 |

---

## 注意事项

### 禁止手动启动

❌ **不要**使用 `nohup python app/main.py &` 手动启动

✅ **应该**使用 `systemctl start ftseek.service` 或 `./start.sh`

### 多实例冲突

如果尝试手动启动第二个实例：
- systemd 会检测到锁文件
- 启动会被拒绝
- 日志会显示 "ConditionPathExists" 失败

### FTSeek-US 服务

FTSeek-US (美股) 是独立的服务：
- 服务名：`ftseek-us.service`
- 使用不同的端口和配置
- 可以与 FTSeek (港股) 同时运行

---

## 验证清单

- [x] systemd 服务文件已配置
- [x] 进程锁机制已启用
- [x] 自动重启策略已设置
- [x] 开机自启已启用
- [x] 只有单一进程运行
- [x] 锁文件已创建
- [x] 启动/停止/重启脚本已创建

---

**配置完成时间**：2026-03-31 15:40  
**服务 PID**：3138  
**状态**：✅ 运行正常