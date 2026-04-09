---
name: ftseek-daily-review
description: FTSeek每日交易复盘分析技能。自动收集交易数据、识别核心问题、提出优化建议。功能包括：(1) 盈亏汇总分析 (2) 问题根因诊断 (3) 优化建议生成 (4) 待办事项跟踪。触发条件：用户请求"复盘"、"今日复盘"、"交易分析"、"盈亏分析"、"问题诊断"、"优化建议"，或指定日期进行历史复盘。
---

# FTSeek 每日交易复盘

自动执行交易复盘，识别问题，提出优化建议。

## 快速开始

执行今日复盘：

```bash
cd /opt/ftseek/FTSeek
python3 skills/ftseek-daily-review/scripts/run_review.py
```

指定日期复盘：

```bash
python3 skills/ftseek-daily-review/scripts/run_review.py --date 2026-03-24
```

JSON输出：

```bash
python3 skills/ftseek-daily-review/scripts/run_review.py --output json
```

## 复盘流程

1. **收集数据** - 从数据库和日志收集交易记录
2. **分析盈亏** - 计算总盈亏、胜率、盈亏比
3. **识别问题** - 检测大额亏损、止损未触发、重复开仓等
4. **生成建议** - 根据问题提出优化建议

## 问题识别规则

| 类型 | 条件 | 严重程度 |
|------|------|----------|
| 大额亏损 | 亏损 > 1000 HKD 或跌幅 > 5% | critical |
| 止损未触发 | 出场价 < 止损价 (BUY) | high |
| 重复开仓 | 同一秒内同标的多次开仓 | high |
| 胜率过低 | 胜率 < 50% | medium |

## 数据源

- 数据库: `data/ftseek.db`
- 日志: `logs/ftseek.log`
- 信号: `data/signal_records/signals_YYYY-MM-DD.json`

## 详细分析指南

参见 [analysis_guide.md](references/analysis_guide.md) 了解：
- 数据库查询语句
- 问题分类标准
- 优化建议模板
- 关键指标说明