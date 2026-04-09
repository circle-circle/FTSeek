# FTSeek 交易异常处理报告

**检查日期**：2026-03-31 15:29  
**状态**：✅ 服务已重启，修复代码已加载

---

## 发现的问题

### 1. 连接失败 (ECONNREFUSED)
```
_connect_sync: Connect fail: conn=0; msg=ECONNREFUSED
```
**原因**：OpenD 服务暂时不可用  
**状态**：✅ 已自动恢复

### 2. 平仓下单失败 - 证券类型错误
```
[HK] 平仓下单失败：当前模拟账号不支持此证券类型
```
**原因**：使用错误的账户进行交易  
**状态**：✅ 修复代码已部署，待验证

### 3. 多进程冲突
```
ERROR: [Errno 98] error while attempting to bind on address ('0.0.0.0', 8899): address already in use
```
**原因**：多个 FTSeek 进程同时运行  
**状态**：✅ 已清理，单一进程运行

---

## 已执行的修复

### 1. 证券类型错误处理
**文件**：`app/trader/executor.py`

**新增逻辑**：
```python
# 证券类型错误特殊处理：尝试切换账户
if any(kw in error_msg for kw in ["证券类型", "security type", "不支持此证券"]):
    logger.error(f"[证券类型错误] {signal.code} 账户 {acc_id} 不支持此证券类型")
    # 尝试切换到另一个账户
    alternative_acc_ids = [18820895, 8246818]
    for alt_acc_id in alternative_acc_ids:
        if alt_acc_id == acc_id:
            continue
        logger.info(f"[平仓] {signal.code} 尝试切换账户 {alt_acc_id}")
        ret, data = trd_ctx.place_order(...)
        if ret == 0:
            logger.info(f"[平仓] {signal.code} 账户 {alt_acc_id} 平仓成功")
            break
```

### 2. 服务重启
- ✅ 清除所有旧进程
- ✅ 清除 Python 缓存
- ✅ 重新启动 FTSeek 服务

---

## 当前服务状态

**进程信息**：
```
PID: 2479
命令: python3.8 app/main.py
状态: 运行中
```

**启动日志**：
```
2026-03-31 15:29:36 [ftseek.main] INFO: FTSeek 启动中...
[账户选择] 模块加载成功  ✅
2026-03-31 15:29:36 [ftseek.main] INFO: API 服务已启动: http://0.0.0.0:8899
```

---

## 验证方法

### 1. 监控日志

```bash
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep -E "(证券类型|账户选择|平仓)"
```

### 2. 预期行为

**触发平仓时**：
1. 首次下单失败（如果账户不匹配）
2. 输出 `[证券类型错误] HK.09992 账户 8246818 不支持此证券类型`
3. 输出 `[平仓] HK.09992 尝试切换账户 18820895`
4. 输出 `[平仓] HK.09992 账户 18820895 平仓成功`

### 3. 账户选择

**预期日志**：
```
[账户选择] HK.09992 -> 正股账户 18820895
[账户选择] HK.TCH260327P470000 -> 期权账户 8246818
```

---

## 账户配置

| 账户类型 | 账户 ID | 适用标的 |
|---------|--------|---------|
| **港股正股账户** | `18820895` | 港股正股、ETF、窝轮 |
| **港股期权账户** | `8246818` | 港股期权（Call/Put） |

---

## 下一步

1. **等待交易触发**：观察下一次平仓操作
2. **监控日志输出**：确认修复代码正确执行
3. **验证账户切换**：确认自动切换账户成功

---

**服务 PID**：2479  
**启动时间**：2026-03-31 15:29:36  
**状态**：✅ 运行中，等待交易验证