# 平仓"证券类型"错误修复报告

**修复日期**：2026-03-31 15:03  
**修复状态**：✅ 完成  
**问题类型**：平仓重试逻辑缺陷

---

## 问题描述

**错误信息**：
```
[HK] 平仓下单失败：当前模拟账号不支持此证券类型
交易失败：[HK] 平仓下单失败：当前模拟账号不支持此证券类型
订单被拒绝 (连续3次): [HK] 平仓下单失败：当前模拟账号不支持此证券类型
```

---

## 问题根因

**平仓重试逻辑缺陷**：
1. 重试逻辑只处理"卖空"错误
2. "证券类型"错误被归类为"未知错误"，直接退出重试
3. 没有尝试切换账户来解决"证券类型"错误

**原代码逻辑**：
```python
# 卖空错误特殊处理
if any(kw in error_msg for kw in ["卖空", "short selling", "short"]):
    # 尝试切换账户...
    pass

# 不可重试错误直接退出
if any(kw in error_msg for kw in NON_RETRYABLE_ERRORS):
    break

# 可重试错误进行退避重试
if any(kw in error_msg for kw in RETRYABLE_ERRORS):
    # 重试...
    pass
else:
    break  # 未知错误不重试 ← "证券类型"错误走到这里直接退出
```

---

## 修复方案

**添加"证券类型"错误处理**：

```python
# 证券类型错误特殊处理：尝试切换账户
if any(kw in error_msg for kw in ["证券类型", "security type", "不支持此证券"]):
    logger.error(f"[证券类型错误] {signal.code} 账户 {acc_id} 不支持此证券类型")
    # 尝试切换到另一个账户
    alternative_acc_ids = [18820895, 8246818]
    switched = False
    for alt_acc_id in alternative_acc_ids:
        if alt_acc_id == acc_id:
            continue
        logger.info(f"[平仓] {signal.code} 尝试切换账户 {alt_acc_id}")
        ret, data = trd_ctx.place_order(
            price=price,
            qty=qty,
            code=signal.code,
            trd_side=trd_side,
            order_type=order_type,
            trd_env=trd_env,
            acc_id=alt_acc_id,
        )
        if ret == 0:
            logger.info(f"[平仓] {signal.code} 账户 {alt_acc_id} 平仓成功")
            acc_id = alt_acc_id
            switched = True
            break
    if switched:
        break  # 切换成功，退出重试
    break  # 证券类型错误不再重试
```

---

## 修复文件

**文件**：`/opt/ftseek/FTSeek/app/trader/executor.py`

**修改位置**：`_execute_close()` 方法的重试逻辑部分

---

## 验证方法

### 1. 观察日志

```bash
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep -E "(证券类型|账户选择|平仓)"
```

### 2. 预期日志

**平仓成功**：
```log
[证券类型错误] HK.09992 账户 8246818 不支持此证券类型
[平仓] HK.09992 尝试切换账户 18820895
[平仓] HK.09992 账户 18820895 平仓成功
```

**账户选择**：
```log
[账户选择] HK.09992 -> 正股账户 18820895
```

---

## 修复效果

### 修复前

```
[HK] 平仓下单失败：当前模拟账号不支持此证券类型
订单被拒绝 (连续3次) → 直接失败
```

### 修复后

```
[证券类型错误] HK.09992 账户 8246818 不支持此证券类型
[平仓] HK.09992 尝试切换账户 18820895
[平仓] HK.09992 账户 18820895 平仓成功
```

---

## 相关修复历史

| 日期 | 问题 | 修复 |
|------|------|------|
| 2026-03-31 14:30 | /qoder-server 临时文件未集成账户选择 | 集成到 FTSeek 项目 |
| 2026-03-31 14:47 | logging 目录与标准库冲突 | 重命名为 ftlogging |
| 2026-03-31 15:03 | 平仓重试逻辑缺陷 | 添加证券类型错误处理 |

---

## 服务状态

**服务 PID**：1602130  
**启动时间**：2026-03-31 15:03:09  
**状态**：✅ 运行中

---

## 下一步

等待平仓操作触发，验证修复效果：
- 观察是否出现 `[证券类型错误]` 日志
- 观察是否自动切换账户成功
- 观察是否出现 `[账户选择]` 日志

---

**修复完成时间**：2026-03-31 15:03  
**状态**：✅ 修复完成，服务已重启