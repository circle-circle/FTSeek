# FTSeek 启动脚本更新说明

**更新日期**：2026-03-31 15:34  
**状态**：✅ 已完成

---

## 问题背景

每次重启 FTSeek 服务时，存在多个相同服务进程同时运行，导致：
1. 端口冲突 (`address already in use`)
2. 资源浪费
3. 日志混乱

---

## 解决方案

### 修改启动脚本

**文件**：`/opt/ftseek/FTSeek/start.sh`

**新增内容**：
```bash
# 清理旧进程 (2026-03-31: 确保单一实例运行)
echo ">>> 检查并清理旧进程..."
OLD_PIDS=$(pgrep -f "python.*app/main.py" 2>/dev/null)
if [ -n "$OLD_PIDS" ]; then
    echo ">>> 发现旧进程，正在清理: $OLD_PIDS"
    pkill -9 -f "python.*app/main.py" 2>/dev/null
    sleep 2
    echo "✅ 旧进程已清理"
else
    echo "✅ 无旧进程运行"
fi

# 清理 Python 缓存
find /opt/ftseek/FTSeek/app -name "*.pyc" -delete 2>/dev/null
find /opt/ftseek/FTSeek/app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
```

---

## 执行效果

### 启动日志

```bash
========================================
  FTSeek v2.1 启动 (Python 3.8)
========================================

>>> 检查并清理旧进程...
>>> 发现旧进程，正在清理: 2731
✅ 旧进程已清理
✅ FTSeek 主程序已启动 (PID: 2771)
```

### 进程验证

```bash
$ ps aux | grep "python.*main.py" | grep -v grep

PID: 2771 | 进程: python3.8 app/main.py
```

**结果**：✅ 只有单一进程运行

---

## 启动流程

```
1. 检查旧进程
   ↓
2. 发现旧进程 → pkill -9 清理
   ↓
3. 清理 Python 缓存
   ↓
4. 启动新进程
   ↓
5. 输出 PID 和日志路径
```

---

## 手动清理命令

如需手动清理所有 FTSeek 进程：

```bash
pkill -9 -f "python.*main.py"
```

查看当前运行的进程：

```bash
ps aux | grep "python.*main.py" | grep -v grep
```

---

## 注意事项

1. **OpenD 进程不受影响**：只清理 FTSeek 主程序进程
2. **缓存自动清理**：每次启动时清除 `.pyc` 和 `__pycache__`
3. **强制清理**：使用 `pkill -9` 确保进程被终止

---

**更新完成时间**：2026-03-31 15:34  
**当前服务 PID**：2771  
**状态**：✅ 单一进程运行正常