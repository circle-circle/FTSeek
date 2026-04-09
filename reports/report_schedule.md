# 模拟交易报告生成计划

**生效日期**: 2026-04-08  
**验证期**: 8 周 (56 天)

---

## 📋 日报 (每日 17:00)

**文件**: `reports/simulation_daily_YYYYMMDD.md`

**内容**:
- 今日交易汇总
- 收益统计
- 系统状态
- 明日计划

**生成方式**:
```bash
cd /opt/ftseek/FTSeek
python3 scripts/generate_daily_report.py --date YYYY-MM-DD
```

---

## 📊 周报 (每周五 20:00)

**文件**: `reports/simulation_weekly_W{week}.md`

**内容**:
- 周度交易汇总
- 累计表现
- 验收进度
- 下周计划

**生成方式**:
```bash
cd /opt/ftseek/FTSeek
python3 scripts/generate_weekly_report.py --week {week}
```

---

## 📈 阶段报告 (每 2 周)

**文件**: `reports/simulation_phase_{phase}.md`

**阶段划分**:
- Phase 1 (Week 1-2): 稳定性验证
- Phase 2 (Week 3-4): 策略表现验证
- Phase 3 (Week 5-6): 盈利稳定性验证
- Phase 4 (Week 7-8): 实盘切换准备

**内容**:
- 阶段目标完成情况
- 关键指标统计
- 风险评估
- 下阶段计划

---

## 📑 总结报告 (8 周后)

**文件**: `reports/simulation_final_summary.md`

**内容**:
- 8 周完整表现
- 验收标准达成情况
- 实盘切换建议
- 经验总结

---

## 🔔 告警通知

**飞书群**: 已配置

**触发事件**:
- 交易执行 (每笔)
- 止损触发
- 日亏损>2%
- 系统异常
- 新闻源故障

---

## 📁 文件目录

```
/opt/ftseek/FTSeek/reports/
├── simulation_daily_20260408.md      # 今日日报
├── simulation_daily_20260409.md      # 明日报
├── ...
├── simulation_weekly_W1.md           # 第 1 周周报
├── ...
├── simulation_phase_1.md             # 阶段 1 报告
├── ...
└── simulation_final_summary.md       # 总结报告
```

---

*计划生成时间：2026-04-08 17:00*
