# 信号查询工具使用指南

**版本**: 1.0  
**更新日期**: 2026-03-01  
**脚本路径**: `scripts/query_signals.py`

---

## 概述

`query_signals.py` 是 FTSeek 系统的命令行查询工具，用于从 SQLite 数据库中查询交易信号。支持多种过滤条件、输出格式和报表模式。

---

## 快速开始

### 基础查询

```bash
# 查询最近 50 条信号（默认）
python3 scripts/query_signals.py

# 查询最近 10 条信号
python3 scripts/query_signals.py --limit 10
```

### 按股票查询

```bash
# 查询腾讯控股的信号
python3 scripts/query_signals.py --stock HK0700 --limit 5

# 查询比亚迪的信号
python3 scripts/query_signals.py --stock HK1211
```

### 按状态查询

```bash
# 查询已执行的信号
python3 scripts/query_signals.py --status executed

# 查询待处理的信号
python3 scripts/query_signals.py --status pending
```

---

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--stock CODE` | 股票代码 (如 HK0700, US-AAPL) | 无 |
| `--from YYYY-MM-DD` | 开始日期 | 无 |
| `--to YYYY-MM-DD` | 结束日期 | 无 |
| `--status` | 信号状态 (pending/executed/ignored) | 无 |
| `--strategy NAME` | 策略名称 | 无 |
| `--limit N` | 限制返回条数 | 50 |
| `--format` | 输出格式 (table/json/csv) | table |
| `--output FILE` | 输出文件路径 | stdout |
| `--daily-report` | 日报模式：查询昨天至今 | 无 |
| `--weekly-report` | 周报模式：查询过去 7 天 | 无 |

---

## 输出格式

### 表格格式 (默认)

适合终端直接查看，自动对齐：

```bash
python3 scripts/query_signals.py --limit 5
```

输出示例：
```
ID      股票代码     操作    价格      置信度   状态        创建时间               
------  -------  ----  ------  ----  --------  -------------------
sig005  US-AAPL  BUY   175.20  0.80  pending   2026-02-28 16:00:00
sig008  HK1211   BUY   245.80  0.77  pending   2026-02-28 11:45:00
```

### JSON 格式

适合程序处理或 API 集成：

```bash
# 输出到终端
python3 scripts/query_signals.py --stock HK0700 --format json --limit 3

# 输出到文件
python3 scripts/query_signals.py --stock HK0700 --format json --output /tmp/signals.json
```

JSON 结构：
```json
[
  {
    "id": "sig001",
    "code": "HK0700",
    "name": "腾讯控股",
    "action": "BUY",
    "price": 380.5,
    "quantity": 100,
    "strategy": "momentum",
    "technical_score": 0.75,
    "sentiment_score": 0.68,
    "confidence": 0.72,
    "reason": "突破阻力位，成交量放大",
    "created_at": "2026-02-28 10:30:00",
    "status": "pending"
  }
]
```

### CSV 格式

适合 Excel 或数据分析工具：

```bash
python3 scripts/query_signals.py --status executed --format csv --output /tmp/executed.csv
```

CSV 包含表头，UTF-8 编码，支持中文。

---

## 报表模式

### 日报

生成昨日至今天的信号报告，包含统计信息：

```bash
python3 scripts/query_signals.py --daily-report --output /tmp/daily.json
```

输出结构：
```json
[
  {
    "type": "daily",
    "from_date": "2026-02-28",
    "to_date": "2026-03-01",
    "generated_at": "2026-03-01T10:09:23",
    "signals": [...],
    "stats": {
      "total": 5,
      "pending": 3,
      "executed": 1,
      "ignored": 1,
      "avg_confidence": 0.72
    }
  }
]
```

### 周报

生成过去 7 天的信号报告：

```bash
python3 scripts/query_signals.py --weekly-report --output /tmp/weekly.json
```

---

## 组合查询示例

### 查询某股票在特定日期范围内的信号

```bash
python3 scripts/query_signals.py --stock HK0700 --from 2026-02-01 --to 2026-02-28 --limit 10
```

### 查询特定策略的已执行信号

```bash
python3 scripts/query_signals.py --strategy momentum --status executed --format csv --output momentum_executed.csv
```

### 查询高置信度信号

```bash
# 查询后手动过滤，或使用 SQL 查询
python3 scripts/query_signals.py --limit 100 --format json | \
  python3 -c "import json,sys; signals=json.load(sys.stdin); print(json.dumps([s for s in signals if s['confidence'] > 0.75], indent=2))"
```

---

## 错误处理

### 数据库不存在

```
错误：数据库文件不存在 /opt/ftseek/FTSeek/data/ftseek.db
请确保已运行初始化脚本或数据生成脚本。
```

### 日期格式错误

```
错误：日期格式无效，请使用 YYYY-MM-DD 格式
```

### 参数冲突

```
错误：日报/周报模式必须指定 --output 参数
```

---

## 技术细节

### 数据库结构

查询工具从 `data/ftseek.db` 的 `signals` 表读取数据，主要字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | TEXT | 信号 ID (如 sig001) |
| code | TEXT | 股票代码 |
| name | TEXT | 股票名称 |
| action | TEXT | 操作 (BUY/SELL) |
| price | REAL | 价格 |
| quantity | INTEGER | 数量 |
| strategy | TEXT | 策略名 |
| confidence | REAL | 置信度 (0-1) |
| status | TEXT | 状态 |
| created_at | TEXT | 创建时间 |

### 依赖

- Python 3.6+
- `tabulate` 库 (可选，用于美化表格输出)
- 标准库：`argparse`, `json`, `csv`, `sqlite3`, `datetime`

---

## 常见问题

**Q: 如何查看所有可用的股票代码？**
```bash
python3 scripts/query_signals.py --format json --limit 1000 | \
  python3 -c "import json,sys; signals=json.load(sys.stdin); print('\n'.join(sorted(set(s['code'] for s in signals))))"
```

**Q: 如何统计各状态的信号数量？**
```bash
python3 scripts/query_signals.py --format json --limit 1000 | \
  python3 -c "import json,sys; from collections import Counter; signals=json.load(sys.stdin); print(Counter(s['status'] for s in signals))"
```

**Q: 如何导出所有信号到 Excel？**
```bash
python3 scripts/query_signals.py --format csv --output all_signals.csv
# 然后用 Excel 打开 all_signals.csv
```

---

## 相关文档

- [QUICKSTART_V2.md](../QUICKSTART_V2.md) - 快速入门
- [FEATURES_V2_1.md](FEATURES_V2_1.md) - 功能列表
- [MODULAR_CONFIG.md](MODULAR_CONFIG.md) - 模块化配置

---

**最后更新**: 2026-03-01
