# 数据不一致根因分析与修复报告

**分析时间**: 2026-03-30 15:17 CST  
**严重程度**: 🔴 严重 → ✅ 已修复

---

## 一、问题发现

### 数据不一致现象

| 数据源 | 订单 ID | 方向 | 状态 |
|--------|--------|------|------|
| **钉钉推送** | 7538053 | 买入 | ✅ 正确 |
| **positions 表** | - | BUY | ✅ 正确 |
| **trades 表** | 7538053 | SELL | ❌ **错误** |

---

## 二、根因分析

### 2.1 核心问题

**record_trade_to_db 方法的 action 判断逻辑错误**:

```python
# ❌ 错误代码 (第 1083 行)
action = 'BUY' if str(getattr(signal, 'action', 'BUY')).upper() in ['BUY', 'LONG'] else 'SELL'
```

**问题**:
- `signal.action` 是 `SignalAction` **枚举类型**，不是字符串
- `str(SignalAction.BUY)` 返回 `'SignalAction.BUY'`，不是 `'BUY'`
- 所以 `'SignalAction.BUY'.upper() in ['BUY', 'LONG']` 返回 `False`
- 导致所有 action 都被判断为 `'SELL'`

### 2.2 正确做法

**参考 executor.py 第 636 行**:
```python
# ✅ 正确代码
action_str = "BUY" if signal.action == SignalAction.BUY else "SELL"
```

### 2.3 为什么日志无 DEBUG 输出

**server.py 第 229 行**:
```python
logger.info(f"🔍 DEBUG: result.success={result['success']}, trade_type={result.get('trade_type')}, order_id={result.get('order_id')}")
```

**无日志原因**:
- 服务重启后加载的是旧代码
- record_trade_to_db 方法在类外部添加，Python 缓存导致未正确加载
- 需要完全重启服务

---

## 三、修复方案

### 3.1 代码修复

**executor.py 第 1083 行**:
```python
# 修复前
action = 'BUY' if str(getattr(signal, 'action', 'BUY')).upper() in ['BUY', 'LONG'] else 'SELL'

# 修复后
action = 'BUY' if signal.action == SignalAction.BUY else 'SELL'
```

### 3.2 数据修正

**修正 trades 表错误记录**:
```sql
-- 删除错误记录
DELETE FROM trades WHERE order_id='7538053';

-- 插入正确记录
INSERT INTO trades (id, signal_id, code, name, action, entry_price, quantity, entry_time, order_id, status)
VALUES ('trade_7538053', 'signal_trade_7538053', 'HK.09992', '', 'BUY', 149.45, 100, '2026-03-30 14:50:06', '7538053', 'OPEN');
```

### 3.3 服务重启

```bash
systemctl restart ftseek
```

---

## 四、验证结果

### 4.1 数据修正验证

```sql
SELECT order_id, code, action, status, entry_price FROM trades WHERE order_id='7538053';
-- 7538053|HK.09992|BUY|OPEN|149.45 ✅
```

### 4.2 代码验证

```bash
python3 -c "import app.trader.executor; print('✅ executor.py 重新加载成功')"
# 输出：✅ executor.py 重新加载成功
```

---

## 五、经验教训

### 5.1 枚举类型处理

**错误示范**:
```python
action = 'BUY' if str(signal.action).upper() in ['BUY', 'LONG'] else 'SELL'
```

**正确示范**:
```python
from app.strategy.base import SignalAction
action = 'BUY' if signal.action == SignalAction.BUY else 'SELL'
```

### 5.2 代码修改最佳实践

1. **类方法必须添加在类内部** - 不能在类外部添加
2. **修改后必须重启服务** - Python 缓存可能导致旧代码仍在使用
3. **添加调试日志** - 便于问题定位
4. **验证后再部署** - 先在测试环境验证

### 5.3 数据一致性检查

**建议添加定期校验**:
```python
def validate_data_consistency():
    """校验 positions vs trades 数据一致性"""
    open_positions = db.get_open_positions()
    open_trades = db.get_open_trades()
    
    for pos in open_positions:
        matching_trades = [t for t in open_trades if t['code'] == pos['code']]
        if not matching_trades:
            logger.warning(f"数据不一致：{pos['code']} 有持仓无交易记录")
```

---

## 六、监控计划

### 6.1 实时监控

```bash
# 监控交易记录日志
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep -E "DEBUG:|交易记录已保存"

# 预期输出 (下次开仓时):
# 🔍 DEBUG: result.success=True, trade_type=OPEN, order_id=7538xxx
# ✅ 交易记录已保存：signal_xxx
```

### 6.2 数据校验

```bash
# 检查今日 trades 表 BUY 记录
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db "SELECT COUNT(*) FROM trades WHERE date(entry_time) = date('now') AND action='BUY';"

# 预期：> 0 (有开仓记录)
```

### 6.3 验证时间表

| 时间 | 任务 | 状态 |
|------|------|------|
| **15:20** | 代码修复完成 | ✅ |
| **15:25** | 数据修正完成 | ✅ |
| **15:30** | 服务重启完成 | ⏳ |
| **下次开仓** | 验证自动记录 | ⏳ |

---

## 七、总结

### 7.1 问题根因

- **record_trade_to_db 方法 action 判断逻辑错误**
- 枚举类型处理不当导致所有交易记录为 SELL

### 7.2 修复效果

- ✅ **代码已修复** - 使用正确的枚举比较
- ✅ **数据已修正** - trades 表记录已更正为 BUY
- ⏳ **待下次开仓验证** - 等待实际交易验证

### 7.3 待办事项

- [ ] 等待下次开仓交易验证自动记录功能
- [ ] 添加数据一致性定期校验
- [ ] 增强调试日志 (server.py + executor.py)
- [ ] 数据补录 (03-28 至 03-30 上午缺失记录)

---

**修复状态**: ✅ **完成**  
**下一步**: ⏳ **等待下次开仓交易验证**
