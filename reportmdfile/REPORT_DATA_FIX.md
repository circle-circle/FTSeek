# FTSeek 报告数据源修复

## 问题描述

2026-03-14 用户反馈：FTSeek 推送到钉钉的收盘统计和周报复盘数据不准确。

## 问题分析

### 项目架构
```
┌─────────────────┐     ┌─────────────────┐
│    FTSeek       │     │    futu_cp2     │
│  (信号/风控)    │     │  (交易/Web)     │
├─────────────────┤     ├─────────────────┤
│ 数据库：ftseek.db│     │ trade_journal.json│
│ - signals 表    │     │ - 实际交易数据   │
│ - trade_pairs 表│     │ - 持仓状态      │
│ (测试数据)      │     │ (真实数据)      │
└─────────────────┘     └─────────────────┘
         │                        │
         └──────────┬─────────────┘
                    │
              钉钉推送
         (收盘统计/周报)
```

### 根本原因
1. **数据源分离**：
   - FTSeek 的报告生成器从本地数据库 (`ftseek.db`) 读取数据
   - futu_cp2 的交易日志 (`trade_journal.json`) 存储实际交易
   - 两个项目之间没有数据同步

2. **数据库为空**：
   - FTSeek 的 `report_trades` 和 `report_positions` 表没有实际数据
   - 只有测试数据，导致报告统计为 0

3. **实际交易在 futu_cp2**：
   - 用户通过 futu_cp2 Web 界面执行交易
   - 交易记录保存在 `/opt/futu_cp2/data/trade_journal.json`
   - 持仓状态从 open 交易推导

## 修复方案

### 方案选择
修改 FTSeek 的报告生成器，**优先从 futu_cp2 的 `trade_journal.json` 读取数据**，本地数据库作为备选。

### 修改内容

#### 1. `app/v4/report/report_generator.py`

**新增常量**：
```python
FUTU_CP2_TRADE_JOURNAL = Path("/opt/futu_cp2/data/trade_journal.json")
```

**新增方法**：
- `_load_futu_cp2_trades()`: 从 futu_cp2 加载交易数据
- `_convert_trade_format()`: 转换数据格式

**修改方法**：
- `_query_trades_by_date()`: 优先从 futu_cp2 读取，数据库备选
- `_query_positions()`: 从 futu_cp2 的 open 交易推导持仓

### 数据流
```
报告生成请求
    ↓
检查 futu_cp2 trade_journal.json
    ↓ (有数据)
加载并转换格式 → 返回
    ↓ (无数据/失败)
查询本地数据库 → 返回
```

## 修复文件

| 文件 | 修改内容 |
|------|----------|
| `app/v4/report/report_generator.py` | 添加 futu_cp2 数据源支持 |
| `test_report_generator_fix.py` | 新增测试脚本 |
| `REPORT_DATA_FIX.md` | 本文档 |

## 测试验证

运行测试脚本：
```bash
cd /opt/ftseek/FTSeek
python3 test_report_generator_fix.py
```

### 测试结果
```
✅ 从 futu_cp2 加载 2 条交易记录
✅ 推导 1 条持仓记录 (TSLA: 50 股 @ 250.00)
✅ 日报生成成功
✅ 周报生成成功
```

## 数据示例

### futu_cp2 交易记录
```json
{
  "trades": [
    {
      "id": "ae38b066",
      "ticker": "TSLA",
      "trade_direction": "short",
      "entry_price": 250.0,
      "position_size": 50,
      "status": "open",
      "entry_date": "2026-02-25T23:09:28.474880"
    }
  ]
}
```

### 转换后格式
```python
{
    'code': 'TSLA',
    'action': 'SELL',
    'quantity': 50,
    'price': 250.0,
    'pnl': 0,
    'pnl_percent': 0,
    'timestamp': '2026-02-25T23:09:28.474880',
    'entry_date': '2026-02-25',
    'exit_date': None,
    'status': 'open'
}
```

## 验证步骤

1. **检查数据源**：
   ```bash
   cat /opt/futu_cp2/data/trade_journal.json | jq '.trades | length'
   ```

2. **查看持仓**：
   ```bash
   cd /opt/ftseek/FTSeek
   python3 test_report_generator_fix.py
   ```

3. **观察钉钉推送**：
   - 收盘统计 (16:30 HKT)
   - 周报 (周日 20:00 Asia/Shanghai)

## 后续优化建议

1. **数据同步**：考虑在 futu_cp2 交易执行后，自动同步到 FTSeek 数据库

2. **统一数据源**：长期方案是统一两个项目的数据存储

3. **缓存机制**：为 futu_cp2 数据加载添加缓存，避免频繁读取文件

4. **错误处理**：增强文件不存在、格式错误等异常处理

5. **监控告警**：当两个项目数据差异过大时告警

## 修复日期

2026-03-14

## 修复人员

OpenClaw Assistant

## 相关文档

- `/opt/futu_cp2/data/trade_journal.json` - 实际交易数据
- `/opt/ftseek/FTSeek/DAILY_STATS_FIX.md` - 收盘统计修复 (旧方案)
- `/opt/ftseek/FTSeek/test_report_generator_fix.py` - 测试脚本
